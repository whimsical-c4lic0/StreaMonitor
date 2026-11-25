"""AmateurTV EasyStream recorder.

EasyStream is AmateurTV's non-HLS transport: a site-issued viewer token is
redeemed on a weighted set of WebSocket edges, the edge sends fragmented MP4
over the socket, and ClearKey is used for encrypted tracks.  This module owns
the monitor-facing lifecycle (parts, token renewal, reconnect, finalization)
and a subprocess worker (see :mod:`easystream_worker`) owns the socket and
ffmpeg.

The viewer token, license keys and raw protocol payloads are never logged.
``EasyStreamStream`` deliberately prints only the target and edge count.
"""

from __future__ import annotations

import glob
import os
import random
import subprocess
import threading
import time
from dataclasses import dataclass
from typing import Callable, Optional

import parameters
from parameters import CONTAINER, DEBUG, FFMPEG_PATH
from streamonitor.downloaders.easystream_backend import EasyStreamBackend, EasyStreamBackendUnavailable
from streamonitor.downloaders.easystream_protocol import EasyStreamCredentials, EasyStreamTarget
from streamonitor.downloaders.process import format_failure, run_bounded

_MIN_PLAYABLE_SIZE = 1024


def _has_media_box(path: str) -> bool:
    """Return whether an MP4 file contains a non-empty top-level ``mdat``.

    Remuxing a header-only init segment can produce a file larger than any
    sensible size threshold, so success checks the container structure rather
    than trusting the byte count alone.
    """
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as handle:
            offset = 0
            for _ in range(100000):
                if offset + 8 > size:
                    return False
                handle.seek(offset)
                header = handle.read(8)
                if len(header) != 8:
                    return False
                box_size = int.from_bytes(header[:4], "big")
                box_type = header[4:8]
                header_size = 8
                if box_size == 1:
                    extended = handle.read(8)
                    if len(extended) != 8:
                        return False
                    box_size = int.from_bytes(extended, "big")
                    header_size = 16
                elif box_size == 0:
                    box_size = size - offset
                if box_size < header_size or offset + box_size > size:
                    return False
                if box_type == b"mdat" and box_size > header_size:
                    return True
                offset += box_size
    except OSError:
        return False
    return False


@dataclass
class EasyStreamStream:
    target: EasyStreamTarget
    credentials: EasyStreamCredentials
    refresh: Optional[Callable[[], EasyStreamCredentials | None]] = None
    reconnect: Optional[Callable[[], "EasyStreamStream | None"]] = None
    referer: str | None = None
    user_agent: str | None = None
    label: str = "EasyStream"

    def __str__(self) -> str:
        return f"{self.label} {self.target}"

    def __repr__(self) -> str:
        return self.__str__()


def _part_filename(filename: str, part: int) -> str:
    if part <= 1:
        return filename
    base, extension = os.path.splitext(filename)
    return f"{base}_part{part}{extension}"


def _temp_filename(filename: str) -> str:
    base = os.path.splitext(filename)[0]
    temp = base + ".easystream.mp4"
    if os.path.exists(temp):
        temp = base + f".easystream-{int(time.time())}.mp4"
    return temp


def _segment_prefix(filename: str, suffix: str = "") -> tuple[str, str]:
    folder = os.path.dirname(os.path.abspath(filename))
    stem = os.path.splitext(os.path.basename(filename))[0]
    # Finalization runs faster than real time, so wall-clock strftime names
    # would overwrite segments created in the same second. Keep the recording
    # and reconnect-part identity, and give every segment its own counter.
    return folder, f"{stem}_%05d{suffix}.{CONTAINER}"


def _write_debug_log(path: str, data) -> None:
    if not data:
        return
    try:
        with open(path, "wb") as log:
            log.write(data)
    except OSError:
        pass


def finalize_recording(source: str, filename: str, logger=None, suffix: str = "") -> bool:
    """Remux one EasyStream part into the configured container.

    ``source`` is kept when finalization fails so a recoverable recording is
    not thrown away.  It is removed only after a playable output exists.
    """
    if not source or not os.path.isfile(source) or os.path.getsize(source) <= 0:
        if logger is not None:
            logger.warning("EasyStream recording produced no media")
        return False

    os.makedirs(os.path.dirname(os.path.abspath(filename)), exist_ok=True)
    folder, segment_pattern = _segment_prefix(filename, suffix)
    cmd = [FFMPEG_PATH, "-y", "-i", source, "-c", "copy"]

    before: set[str] = set()
    if parameters.SEGMENT_TIME is not None:
        before = set(glob.glob(os.path.join(folder, f"*{suffix}.{CONTAINER}")))
        cmd.extend(
            [
                "-f",
                "segment",
                "-reset_timestamps",
                "1",
                "-segment_time",
                str(parameters.SEGMENT_TIME),
                os.path.join(folder, segment_pattern),
            ]
        )
        output_path = None
    else:
        output_path = os.path.splitext(filename)[0] + "." + CONTAINER
        cmd.append(output_path)

    try:
        result = run_bounded(
            cmd,
            timeout=parameters.FFMPEG_REMUX_TIMEOUT,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
    except OSError as error:
        if logger is not None:
            logger.error(f"Failed to run ffmpeg: {error}")
        return False

    if DEBUG:
        _write_debug_log(filename + ".postprocess_stderr.log", result.stderr)
        _write_debug_log(filename + ".postprocess_stdout.log", result.stdout)

    if result.timed_out:
        if logger is not None:
            logger.error(f"ffmpeg timed out after {parameters.FFMPEG_REMUX_TIMEOUT}s finalizing the EasyStream recording")
        return False
    if result.returncode != 0:
        if logger is not None:
            logger.error(f"ffmpeg could not finalize the EasyStream recording ({format_failure(result)})")
        return False

    if output_path is not None:
        produced = [output_path] if os.path.isfile(output_path) else []
    else:
        after = set(glob.glob(os.path.join(folder, f"*{suffix}.{CONTAINER}")))
        produced = [path for path in after - before if os.path.isfile(path)]
    produced = [path for path in produced if os.path.getsize(path) >= _MIN_PLAYABLE_SIZE]
    if not produced:
        if logger is not None:
            logger.error("ffmpeg produced no playable EasyStream recording")
        return False

    try:
        os.unlink(source)
    except OSError:
        pass
    if logger is not None:
        logger.info(f"EasyStream recording saved: {', '.join(sorted(produced))}")
    return True


# Worker reasons that mean the show is over or the format is unsupported.  A
# new part cannot fix these, so the monitor should re-poll instead of looping.
_TERMINAL_REASONS = {
    "access_denied",
    "ended",
    "revoked",
    "restricted",
    "unsupported",
    "multiple_keys",
    "invalid_init",
    "invalid_media",
    "license_denied",
    "license_invalid",
    "license_timeout",
    "license_buffer_overflow",
    "media_buffer_overflow",
    "drm_unsupported",
    "ffmpeg_failed",
    "subscribe_timeout",
    "no_media",
    "max_duration",
}
_RESTART_REASONS = {
    "transport",
    "silence",
    "init_changed",
    "key_changed",
    "timestamp_reset",
    "output_overload",
    "token_expired",
    "edge_drain",
    "superseded",
    "connect_failed",
    "process_exit",
}


class EasyStreamDownloader:
    def __init__(self, bot, stream: EasyStreamStream, backend=None):
        self.bot = bot
        self.stream = stream
        self.backend = backend
        self._backend_used = False
        self._active_backend = None
        self._stopped = threading.Event()
        self._lock = threading.Lock()
        self._success: list[bool] = []

    def _make_backend(self):
        if self.backend is not None and not self._backend_used:
            self._backend_used = True
            return self.backend
        return EasyStreamBackend(self.bot.logger)

    def stop(self) -> None:
        """Request a stop.  Safe to call repeatedly and from another thread."""
        self._stopped.set()
        with self._lock:
            backend = self._active_backend
        if backend is not None:
            backend.request_stop()

    def _set_active(self, backend) -> None:
        with self._lock:
            self._active_backend = backend

    def _log(self, level: str, message: str) -> None:
        logger = getattr(self.bot, "logger", None)
        if logger is None:
            return
        getattr(logger, level)(message)

    def record(self, filename: str) -> bool:
        if not self._validate() or self._stopped.is_set():
            return False
        self.bot.stopDownload = self.stop
        part = 0
        stream = self.stream
        restarts = 0
        try:
            while not self._stopped.is_set():
                part_filename = _part_filename(filename, part + 1)
                success, restart = self._record_part(stream, part_filename)
                self._success.append(success)
                if restart is None:
                    break
                if restart not in _RESTART_REASONS or restart in _TERMINAL_REASONS:
                    self._log("error", f"EasyStream recording ended: {restart}")
                    break
                restarts += 1
                if restarts > parameters.EASYSTREAM_MAX_RECONNECTS:
                    self._log(
                        "error",
                        f"EasyStream gave up after {parameters.EASYSTREAM_MAX_RECONNECTS} reconnects",
                    )
                    break
                stream = self._reconnect_stream(stream)
                if stream is None:
                    break
                self.stream = stream
                part += 1
                delay = min(parameters.EASYSTREAM_RECONNECT_MAX, parameters.EASYSTREAM_RECONNECT_BASE * 2 ** (restarts - 1))
                delay *= 0.5 + random.random()
                self._log("info", f"EasyStream reconnecting in {delay:.1f}s ({restart})")
                if self._stopped.wait(delay):
                    break
        finally:
            self.bot.stopDownload = None
        # A finalized part stays valid even if a later reconnect failed.
        return any(self._success)

    def _validate(self) -> bool:
        credentials = self.stream.credentials
        if not credentials.token:
            self._log("error", "EasyStream stream is missing its viewer token")
            return False
        if not credentials.edges:
            self._log("error", "EasyStream stream has no usable edge")
            return False
        if parameters.EASYSTREAM_BACKEND.lower() != "websocket":
            self._log(
                "error",
                f"Unsupported EasyStream backend {parameters.EASYSTREAM_BACKEND!r}; only 'websocket' is implemented",
            )
            return False
        return True

    def _record_part(self, stream: EasyStreamStream, filename: str):
        """Record one part.  Returns ``(success, restart_reason_or_None)``."""
        temp = _temp_filename(filename)
        os.makedirs(os.path.dirname(os.path.abspath(temp)), exist_ok=True)
        backend = self._make_backend()
        try:
            backend.start(stream, temp, max_duration=parameters.EASYSTREAM_MAX_DURATION)
        except EasyStreamBackendUnavailable as error:
            self._log("error", str(error))
            return False, None
        except Exception as error:  # noqa: BLE001 - a broken sidecar must not kill the bot
            self._log("error", f"Failed to start the EasyStream recorder: {error}")
            return False, None

        self._set_active(backend)
        started = time.monotonic()
        got_media = False
        failed = False
        restart: str | None = None
        refresh_pending = False
        next_refresh_attempt = 0.0
        credentials = stream.credentials
        startup_deadline = started + parameters.EASYSTREAM_STARTUP_TIMEOUT + 5

        try:
            while not self._stopped.is_set():
                event = backend.next_event(0.5)
                now = time.monotonic()
                if event is not None:
                    name = event.get("event")
                    if name == "subscribed":
                        layers = event.get("layers") or []
                        layer = event.get("layer")
                        resolution = event.get("resolution")
                        drm = event.get("drm")
                        self._log(
                            "info",
                            f"EasyStream subscribed: layer={layer} layers={layers} "
                            f"resolution={resolution or '?'} drm={drm or 'none'}",
                        )
                    elif name == "layer":
                        self._log("info", f"EasyStream layer changed to {event.get('layer')}")
                    elif name == "media":
                        got_media = True
                        self._log(
                            "info",
                            "EasyStream recording media: "
                            f"{event.get('width') or '?'}x{event.get('height') or '?'} "
                            f"video={event.get('video') or '?'} audio={event.get('audio') or '?'} "
                            f"layer={event.get('layer') or '?'}",
                        )
                    elif name == "warning":
                        self._log("warning", "EasyStream recorder: " + str(event.get("message")))
                    elif name == "access":
                        access = event.get("access")
                        media = event.get("media")
                        self._log("info", f"EasyStream access={access} media={media}")
                        if access != "public" or media in ("none", "audio"):
                            failed = True
                            restart = "access_denied"
                            break
                    elif name == "refresh_needed":
                        refresh_pending = True
                    elif name == "restart":
                        restart = str(event.get("reason") or "transport")
                        break
                    elif name == "error":
                        failed = True
                        restart = str(event.get("code") or "error")
                        self._log(
                            "error",
                            f"EasyStream recorder error: {event.get('message')} (code {event.get('code')})",
                        )
                    elif name == "stopped":
                        restart = str(event.get("reason") or "ended")
                        break
                    elif name == "process_exit":
                        code = event.get("code")
                        if code not in (0, None) and not backend.reported_file:
                            failed = True
                            restart = "process_exit"
                            self._log("error", f"EasyStream recorder exited with code {code}")
                        elif code not in (0, None):
                            self._log(
                                "debug",
                                f"EasyStream recorder ended with code {code} after finalizing media",
                            )
                        restart = restart or "transport"
                        break

                if self._stopped.is_set():
                    break
                # The worker may exit before its queued lifecycle events are
                # consumed. Drain those first so a stopped/restart reason is
                # not lost behind an earlier subscribed or media event.
                if event is None and backend.exited:
                    restart = restart or "process_exit"
                    break

                expiry = credentials.expires_at
                wall_now = time.time()
                margin = parameters.EASYSTREAM_TOKEN_REFRESH_MARGIN
                expiring = expiry is not None and wall_now >= expiry - margin
                if (refresh_pending or expiring) and wall_now >= next_refresh_attempt:
                    refreshed = self._refresh()
                    next_refresh_attempt = wall_now + 30
                    if refreshed is not None and backend.renew(refreshed):
                        credentials = refreshed
                        stream.credentials = refreshed
                        self.stream.credentials = refreshed
                        refresh_pending = False
                        self._log("info", "EasyStream viewer token renewed")
                    elif expiry is not None and wall_now >= expiry:
                        restart = "token_expired"
                        break
                    else:
                        # Keep retrying while the old token is still valid; a
                        # forced refresh (invalid_token) must not be dropped
                        # after one failed callback.
                        self._log("warning", "EasyStream token refresh failed; retrying with the current token")

                if not got_media and now >= startup_deadline:
                    failed = True
                    restart = "no_media"
                    self._log(
                        "error",
                        f"EasyStream recorder received no media within {parameters.EASYSTREAM_STARTUP_TIMEOUT}s",
                    )
                    break
        except Exception as error:  # noqa: BLE001 - keep the bot alive
            failed = True
            restart = "internal"
            self._log("error", f"EasyStream recorder failed: {error}")
        finally:
            self._set_active(None)
            try:
                code = backend.close(parameters.EASYSTREAM_STOP_TIMEOUT)
                if code not in (0, None):
                    level = "debug" if backend.reported_file else "warning"
                    self._log(level, f"EasyStream recorder stopped with code {code}")
            except Exception as error:  # noqa: BLE001
                self._log("warning", f"Failed to stop the EasyStream recorder cleanly: {error}")

        source = self._find_source(backend, temp)
        if source is None:
            if not failed and not self._stopped.is_set():
                self._log("warning", "EasyStream recording produced no file")
            return False, restart
        if not _has_media_box(source):
            self._log("warning", "EasyStream recording has no media data; keeping the temp file")
            return False, restart
        success = finalize_recording(
            source,
            filename,
            self.bot.logger,
            suffix=getattr(self.bot, "filename_extra_suffix", ""),
        )
        if not success and failed:
            self._log("error", "EasyStream recording failed and could not be finalized")
        if self._stopped.is_set():
            return success, None
        return success, restart

    def _refresh(self) -> EasyStreamCredentials | None:
        refresh = self.stream.refresh
        if refresh is None:
            return None
        try:
            credentials = refresh()
        except Exception as error:  # noqa: BLE001 - the old token may still work
            self._log("warning", f"EasyStream token refresh failed: {error}")
            return None
        if credentials is None or not credentials.token:
            return None
        return credentials

    def _reconnect_stream(self, current: EasyStreamStream) -> EasyStreamStream | None:
        reconnect = self.stream.reconnect
        if reconnect is not None:
            try:
                stream = reconnect()
            except Exception as error:  # noqa: BLE001 - the monitor will re-poll
                self._log("warning", f"EasyStream reconnect failed: {error}")
                stream = None
            # An installed callback owns the status/access decision. A
            # refusal (or failed check) must not fall back to the stale target.
            return stream if isinstance(stream, EasyStreamStream) else None
        refreshed = self._refresh()
        if refreshed is not None:
            return EasyStreamStream(
                target=current.target,
                credentials=refreshed,
                refresh=current.refresh,
                reconnect=current.reconnect,
                referer=current.referer,
                user_agent=current.user_agent,
                label=current.label,
            )
        return None

    @staticmethod
    def _find_source(backend, temp: str) -> str | None:
        reported = getattr(backend, "reported_file", None)
        if reported and os.path.isfile(reported) and os.path.getsize(reported) > 0:
            return reported
        if os.path.isfile(temp) and os.path.getsize(temp) > 0:
            return temp
        base = os.path.splitext(temp)[0]
        candidates = [
            path
            for path in glob.glob(base + "*.mp4")
            if os.path.isfile(path) and os.path.getsize(path) > 0
        ]
        if not candidates:
            return None
        return max(candidates, key=os.path.getmtime)


def getVideoEasyStream(bot, stream: EasyStreamStream, filename: str) -> bool:
    return EasyStreamDownloader(bot, stream).record(filename)
