import json
import os
import subprocess
from contextlib import closing
from threading import Thread

from websocket import WebSocketConnectionClosedException, WebSocketException, create_connection

from parameters import CONTAINER, DEBUG, FFMPEG_PATH, FFMPEG_REMUX_TIMEOUT, HLS_STOP_DEADLINE, SEGMENT_TIME
from streamonitor.downloaders.process import format_failure, run_bounded


def getVideoWSSVR(self, url, filename):
    self.stopDownloadFlag = False
    error = False
    url = url.replace('fmp4s://', 'wss://')

    suffix = ''
    if hasattr(self, 'filename_extra_suffix'):
        suffix = self.filename_extra_suffix

    basefilename = filename[: -len('.' + CONTAINER)]
    filename = basefilename + suffix + '.' + CONTAINER
    tmpfilename = basefilename + '.tmp.mp4'

    def debug_(message):
        self.debug(message, filename + '.log')

    def execute():
        nonlocal error
        with open(tmpfilename, 'wb') as outfile:
            while not self.stopDownloadFlag:
                try:
                    with closing(create_connection(url, timeout=10)) as conn:
                        conn.send('{"url":"stream/hello","version":"0.0.1"}')
                        while not self.stopDownloadFlag:
                            t = conn.recv()
                            try:
                                tj = json.loads(t)
                                if 'url' in tj:
                                    if tj['url'] == 'stream/qual':
                                        conn.send('{"quality":"test","url":"stream/play","version":"0.0.1"}')
                                        debug_('Connection opened')
                                        break
                                if 'message' in tj:
                                    if tj['message'] == 'ping':
                                        debug_('Server is not ready or there was a change')
                                        error = True
                                        return
                            except Exception:
                                debug_('Failed to open the connection')
                                error = True
                                return

                        while not self.stopDownloadFlag:
                            outfile.write(conn.recv())
                except WebSocketConnectionClosedException:
                    debug_('WebSocket connection closed - try to continue')
                    continue
                except WebSocketException as wex:
                    debug_('Error when downloading')
                    debug_(wex)
                    error = True
                    return

    def terminate():
        self.stopDownloadFlag = True

    process = Thread(target=execute, daemon=True)
    process.start()
    self.stopDownload = terminate
    still_alive = False
    try:
        process.join(HLS_STOP_DEADLINE)
        still_alive = process.is_alive()
        if still_alive:
            self.logger.error(
                f'WebSocket downloader did not stop within {HLS_STOP_DEADLINE}s; '
                f'keeping {os.path.basename(tmpfilename)} for recovery'
            )
    finally:
        self.stopDownload = None

    if error or still_alive:
        return False

    # Post-processing
    cmd = [FFMPEG_PATH, '-ignore_editlist', '1', '-i', tmpfilename, '-c:a', 'copy', '-c:v', 'copy']
    if SEGMENT_TIME is not None:
        cmd.extend(['-f', 'segment', '-reset_timestamps', '1', '-segment_time', str(SEGMENT_TIME)])
        filename = basefilename + '_%03d' + suffix + '.' + CONTAINER
    cmd.append(filename)

    try:
        result = run_bounded(
            cmd,
            timeout=FFMPEG_REMUX_TIMEOUT,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
    except OSError as e:
        self.logger.error(f'Could not run ffmpeg to finalize the recording: {e}')
        return False

    if DEBUG:
        _write_debug_log(filename + '.postprocess_stderr.log', result.stderr)
        _write_debug_log(filename + '.postprocess_stdout.log', result.stdout)

    if result.timed_out:
        self.logger.error(
            f'ffmpeg did not finalize the recording within {FFMPEG_REMUX_TIMEOUT}s; '
            f'keeping {os.path.basename(tmpfilename)} for recovery'
        )
        return False
    if result.returncode != 0 and result.returncode != 255:
        self.logger.error(f'ffmpeg could not finalize the recording ({format_failure(result)})')
        return False

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
