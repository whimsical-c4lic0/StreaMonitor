import errno
import os
import subprocess
import sys

import requests.cookies
from threading import Thread
from parameters import DEBUG, SEGMENT_TIME, CONTAINER, FFMPEG_PATH, FFMPEG_READRATE


def _buildInputArgs(url):
    if isinstance(url, os.PathLike):
        local_playlist = os.fspath(url)
        return (
            ['-protocol_whitelist', 'file,http,https,tcp,tls,crypto,data', '-i', local_playlist],
            local_playlist,
        )
    return ['-i', url], None


def getVideoFfmpeg(self, url, filename):
    is_local_playlist = isinstance(url, os.PathLike)
    cmd = [FFMPEG_PATH]

    # HTTP protocol options are rejected by ffmpeg when the input is a local
    # playlist. Chaturbate's chunklists only need the session token embedded in
    # their URLs, so they work without the cookie and user-agent options.
    if not is_local_playlist:
        cmd.extend([
            '-user_agent', self.headers['User-Agent']
        ])

        if type(self.cookies) is requests.cookies.RequestsCookieJar:
            cookies_text = ''
            for cookie in self.cookies:
                cookies_text += cookie.name + "=" + cookie.value + "; path=" + cookie.path + '; domain=' + cookie.domain + '\n'
            if len(cookies_text) > 10:
                cookies_text = cookies_text[:-1]
            cmd.extend([
                '-cookies', cookies_text
            ])

    if FFMPEG_READRATE:
        cmd.extend(['-readrate', f'{FFMPEG_READRATE!s}'])

    # These are HLS demuxer options. FFmpeg refuses to start when they are
    # passed for a non-HLS input (e.g. AmateurTV's progressive fMP4 stream).
    if is_local_playlist or '.m3u8' in str(url).lower():
        cmd.extend([
            '-max_reload', '20',
            '-seg_max_retry', '20',
            '-m3u8_hold_counters', '20',
        ])

    # CMAF master playlists are written to a local temporary file so ffmpeg can
    # align the separate audio and video renditions by program date time.
    input_args, local_playlist = _buildInputArgs(url)
    cmd.extend(input_args)
    cmd.extend(['-c:a', 'copy', '-c:v', 'copy'])

    suffix = ''
    if hasattr(self, 'filename_extra_suffix'):
        suffix = self.filename_extra_suffix

    if SEGMENT_TIME is not None:
        username = filename.rsplit('-', maxsplit=2)[0]
        cmd.extend([
            '-f', 'segment',
            '-reset_timestamps', '1',
            '-segment_time', str(SEGMENT_TIME),
            '-strftime', '1',
            f'{username}-%Y%m%d-%H%M%S{suffix}.{CONTAINER}'
        ])
    else:
        cmd.extend([
            os.path.splitext(filename)[0] + suffix + '.' + CONTAINER
        ])

    class _Stopper:
        def __init__(self):
            self.stop = False

        def pls_stop(self):
            self.stop = True

    stopping = _Stopper()
    error = False

    def execute():
        nonlocal error
        try:
            stderr = open(filename + '.stderr.log', 'w+') if DEBUG else subprocess.DEVNULL
            startupinfo = None
            if sys.platform == "win32":
                startupinfo = subprocess.STARTUPINFO()
                startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            process = subprocess.Popen(
                args=cmd, stdin=subprocess.PIPE, stderr=stderr, stdout=subprocess.DEVNULL, startupinfo=startupinfo)
        except OSError as e:
            if e.errno == errno.ENOENT:
                self.logger.error('FFMpeg executable not found!')
                error = True
                return
            else:
                self.logger.error("Got OSError, errno: " + str(e.errno))
                error = True
                return

        while process.poll() is None:
            if stopping.stop:
                process.communicate(b'q')
                break
            try:
                process.wait(1)
            except subprocess.TimeoutExpired:
                pass

        if process.returncode and process.returncode != 0 and process.returncode != 255:
            self.logger.error('The process exited with an error. Return code: ' + str(process.returncode))
            error = True
            return

    try:
        thread = Thread(target=execute)
        thread.start()
        self.stopDownload = lambda: stopping.pls_stop()
        thread.join()
    finally:
        self.stopDownload = None
        if local_playlist:
            try:
                os.unlink(local_playlist)
            except OSError:
                pass
    return not error
