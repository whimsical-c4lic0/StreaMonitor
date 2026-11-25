"""Native HLS downloader.

This downloader is used where ffmpeg's HTTP/TLS fingerprint is rejected or
where a playlist has to be decoded before ffmpeg can see it.  It keeps the
recoverable ``<show>.tmp.ts`` file until a bounded remux succeeds, so a killed
process or a network failure never discards recorded media.

Reliability contract:

* A segment is marked downloaded only after its complete body has been
  received and written.  Retries therefore cannot duplicate output.
* All requests have finite connect/read timeouts and a capped retry budget.
* Playlist URIs are resolved with :func:`urllib.parse.urljoin`, matching HLS
  semantics for relative, absolute, parent-path and query-string references.
* Exceptions in the worker thread are reported to the caller; a failed worker
  can never be mistaken for a completed recording.
"""

from __future__ import annotations

import os
import subprocess
import threading
from collections import OrderedDict
from urllib.parse import urljoin

import m3u8

from parameters import (
    CONTAINER,
    DEBUG,
    FFMPEG_PATH,
    FFMPEG_REMUX_TIMEOUT,
    HLS_PLAYLIST_TIMEOUT,
    HLS_SEGMENT_TIMEOUT,
    HLS_STOP_DEADLINE,
    HTTP_CONNECT_TIMEOUT,
    HTTP_RETRY_ATTEMPTS,
    HTTP_RETRY_BASE_DELAY,
    HTTP_RETRY_MAX_DELAY,
    SEGMENT_TIME,
)
from streamonitor.downloaders.process import format_failure, run_bounded
from streamonitor.downloaders.transport import (
    RetryPolicy,
    TransferCancelled,
    TransportError,
    fetch,
    redact_url,
)

_http_lib = None
if not _http_lib:
    try:
        import pycurl_requests as requests

        _http_lib = 'pycurl'
    except ImportError:
        pass
if not _http_lib:
    try:
        import requests

        _http_lib = 'requests'
    except ImportError:
        pass
if not _http_lib:
    raise ImportError("Please install requests or pycurl package to proceed")

# A live HLS window is small.  The hard cap only protects against a malformed
# playlist that keeps growing, which would otherwise leak memory forever.
_MAX_SEGMENT_HISTORY = 4096
_MIN_PLAYLIST_WAIT = 1.0
_MAX_PLAYLIST_WAIT = 10.0


def _retry_policy(read_timeout: float) -> RetryPolicy:
    return RetryPolicy(
        attempts=max(1, HTTP_RETRY_ATTEMPTS),
        timeout=(HTTP_CONNECT_TIMEOUT, read_timeout),
        base_delay=max(0.0, HTTP_RETRY_BASE_DELAY),
        max_delay=max(0.0, HTTP_RETRY_MAX_DELAY),
    )


def _prune_history(history: OrderedDict[str, None], current: set[str], cap: int = _MAX_SEGMENT_HISTORY) -> None:
    """Drop entries the current playlist window no longer contains.

    A valid live playlist only moves forward, so forgotten segments cannot
    reappear.  The hard cap additionally protects against a malformed playlist
    whose window keeps growing.
    """
    for known in list(history):
        if known not in current:
            del history[known]
    while len(history) > cap:
        history.popitem(last=False)


def _playlist_wait(playlist) -> float:
    """Wait for a live playlist to advance without hammering the server."""
    target_duration = getattr(playlist, 'target_duration', None)
    try:
        wait = float(target_duration)
    except (TypeError, ValueError):
        wait = _MIN_PLAYLIST_WAIT
    return min(_MAX_PLAYLIST_WAIT, max(_MIN_PLAYLIST_WAIT, wait))


def _segment_urls(playlist, base_url):
    """Return playlist URIs resolved against the playlist URL.

    EXT-X-MAP initialisation segments are included because fragmented MP4
    playlists need them before their media segments.
    """
    urls = []
    seen = set()
    for segment in list(playlist.segment_map) + list(playlist.segments):
        uri = getattr(segment, 'uri', None)
        if not uri:
            continue
        resolved = urljoin(base_url, uri)
        if resolved not in seen:
            seen.add(resolved)
            urls.append(resolved)
    return urls


def _remux(self, tmpfilename, filename):
    """Remux downloaded MPEG-TS into the configured container.

    Returns ``True`` only when the recording is usable.  On failure the temp
    file is deliberately kept for ``recoverOrphanedRecordings``.
    """
    output_path = filename
    cmd = [FFMPEG_PATH, '-hide_banner', '-loglevel', 'error', '-i', tmpfilename, '-c:a', 'copy', '-c:v', 'copy']
    if SEGMENT_TIME is not None:
        suffix = getattr(self, 'filename_extra_suffix', '')
        output_path = filename[: -len('.' + CONTAINER)] + '_%03d' + suffix + '.' + CONTAINER
        cmd.extend(
            [
                '-f',
                'segment',
                '-reset_timestamps',
                '1',
                '-segment_time',
                str(SEGMENT_TIME),
            ]
        )
    cmd.append(output_path)

    try:
        result = run_bounded(
            cmd,
            timeout=FFMPEG_REMUX_TIMEOUT,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
    except OSError as exc:
        self.logger.error(f'Could not run ffmpeg to finalize the HLS recording: {exc}')
        return False

    if DEBUG:
        _write_debug_log(filename + '.postprocess_stderr.log', result.stderr)
        _write_debug_log(filename + '.postprocess_stdout.log', result.stdout)

    if result.timed_out:
        self.logger.error(
            f'ffmpeg did not finalize the HLS recording within {FFMPEG_REMUX_TIMEOUT}s; '
            f'keeping {os.path.basename(tmpfilename)} for recovery'
        )
        return False

    if result.returncode != 0:
        # 255 is what ffmpeg reports when it is killed, usually by a stop
        # request.  The downloaded segments are still valid, so keep them for
        # recoverOrphanedRecordings() instead of discarding useful output.
        if result.returncode == 255:
            if SEGMENT_TIME is not None:
                return True
            return os.path.exists(output_path) and os.path.getsize(output_path) > 0
        self.logger.error(f'ffmpeg could not finalize the HLS recording ({format_failure(result)})')
        return False

    # The remux succeeded, so the raw segments are no longer needed.
    try:
        os.remove(tmpfilename)
    except OSError:
        pass
    return True


def _write_debug_log(path, data):
    if not data:
        return
    try:
        with open(path, 'wb') as log:
            log.write(data)
    except OSError:
        pass


def getVideoNativeHLS(self, url, filename, m3u_processor=None):
    self.stopDownloadFlag = False
    cancel = threading.Event()
    tmpfilename = filename[: -len('.' + CONTAINER)] + '.tmp.ts'
    session = requests.Session()
    error = False

    playlist_policy = _retry_policy(HLS_PLAYLIST_TIMEOUT)
    segment_policy = _retry_policy(HLS_SEGMENT_TIMEOUT)

    def fetch_playlist():
        body = fetch(
            session,
            url,
            headers=self.headers,
            cookies=self.cookies,
            policy=playlist_policy,
            cancel=cancel,
            logger=self.logger,
        )
        try:
            content = body.decode('utf-8')
        except UnicodeDecodeError as exc:
            raise TransportError(f'playlist is not valid UTF-8: {exc}', url=url) from exc
        if m3u_processor:
            content = m3u_processor(content)
            if not content:
                raise TransportError('playlist decoder returned no content', url=url)
        try:
            return m3u8.loads(content)
        except Exception as exc:  # noqa: BLE001 - malformed playlist
            raise TransportError(f'malformed playlist: {exc}', url=url) from exc

    def execute():
        nonlocal error
        history: OrderedDict[str, None] = OrderedDict()
        try:
            with open(tmpfilename, 'wb') as outfile:
                while not cancel.is_set():
                    playlist = fetch_playlist()
                    if cancel.is_set():
                        return
                    urls = _segment_urls(playlist, url)
                    if not urls:
                        # An empty live playlist means the show is over.
                        return

                    # Keep the history bounded by the current playlist window.
                    # Segments outside the window cannot reappear in a valid
                    # live playlist, so forgetting them is safe.
                    _prune_history(history, set(urls))

                    pending = [chunk_url for chunk_url in urls if chunk_url not in history]
                    for chunk_url in pending:
                        if cancel.is_set():
                            return
                        self.debug('Downloading ' + redact_url(chunk_url))
                        # Buffer the complete body first: a retry must never
                        # append a partial segment to the output twice.
                        body = fetch(
                            session,
                            chunk_url,
                            headers=self.headers,
                            cookies=self.cookies,
                            policy=segment_policy,
                            cancel=cancel,
                            logger=self.logger,
                        )
                        if cancel.is_set():
                            return
                        try:
                            outfile.write(body)
                            outfile.flush()
                        except OSError as exc:
                            raise TransportError(f'could not write segment: {exc}', url=chunk_url) from exc
                        history[chunk_url] = None

                    if not pending:
                        if cancel.wait(_playlist_wait(playlist)):
                            return
        except TransferCancelled:
            # A requested stop is not an error.  Whatever was written stays in
            # the temp file and is finalized below.
            pass
        except Exception as exc:  # noqa: BLE001 - reported to the caller
            error = True
            if isinstance(exc, TransportError):
                self.logger.error(f'HLS download failed: {exc}')
            else:
                self.logger.exception(f'HLS download failed: {exc}')

    def terminate():
        cancel.set()
        self.stopDownloadFlag = True

    process = threading.Thread(target=execute, name=f'HLS-{getattr(self, "username", "recording")}', daemon=True)
    process.start()
    self.stopDownload = terminate
    still_alive = False
    try:
        process.join(HLS_STOP_DEADLINE)
        still_alive = process.is_alive()
        if still_alive:
            error = True
            self.logger.error(
                f'HLS downloader did not stop within {HLS_STOP_DEADLINE}s; '
                f'keeping {os.path.basename(tmpfilename)} for recovery'
            )
    finally:
        self.stopDownload = None
        close = getattr(session, 'close', None)
        if callable(close):
            try:
                close()
            except Exception:  # noqa: BLE001 - cleanup must not mask the result
                pass

    # A failed worker that wrote nothing leaves only an empty placeholder.
    # Anything with media stays for recoverOrphanedRecordings().
    if not still_alive and os.path.exists(tmpfilename) and os.path.getsize(tmpfilename) == 0:
        try:
            os.remove(tmpfilename)
        except OSError:
            pass

    if error or still_alive:
        return False

    if not os.path.exists(tmpfilename):
        return False

    if os.path.getsize(tmpfilename) == 0:
        try:
            os.remove(tmpfilename)
        except OSError:
            pass
        return False

    return _remux(self, tmpfilename, filename)
