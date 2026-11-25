import os
import time

import parameters
from streamonitor.bot import Bot
from streamonitor.downloaders.easystream import EasyStreamStream, getVideoEasyStream
from streamonitor.downloaders.easystream_protocol import parse_target, parse_viewer_token
from streamonitor.downloaders.ffmpeg import getVideoFfmpeg
from streamonitor.enums import Status

_VIEWER_TOKEN_URL = "https://www.amateur.tv/v3/readmodel/easystream/viewer-token"
_EASYSTREAM_PIECES = {"astream", "easystream"}


def getVideoAmateurTV(bot, source, filename):
    """Dispatch an EasyStream descriptor or a legacy fMP4 URL.

    ``Bot.run()`` calls ``self.getVideo(self, source, filename)``, so this is an
    unbound function taking the bot first.  Keeping the decision here (rather
    than swapping ``bot.getVideo`` while the bot runs) means a backend change
    can never leave a stale downloader installed.
    """
    if isinstance(source, EasyStreamStream):
        return getVideoEasyStream(bot, source, filename)
    if isinstance(source, (str, os.PathLike)):
        return getVideoFfmpeg(bot, source, filename)
    logger = getattr(bot, "logger", None)
    if logger is not None:
        logger.error(f"AmateurTV returned an unsupported stream source: {type(source).__name__}")
    return False


class AmateurTV(Bot):
    site = 'AmateurTV'
    siteslug = 'ATV'

    def __init__(self, username):
        super().__init__(username)
        # Bot.run() calls self.getVideo(self, url, file), so this must remain an
        # unbound function taking the bot as its first argument.
        self.getVideo = getVideoAmateurTV

    @staticmethod
    def _piece(info):
        piece = info.get("piece")
        return piece.strip().lower() if isinstance(piece, str) else ""

    def getPlaylistVariants(self, url):
        if self._piece(self.lastInfo) in _EASYSTREAM_PIECES:
            # EasyStream is a WebSocket descriptor, not an HLS playlist.
            return []

        qualities = self.lastInfo.get("qualities")
        video = self.lastInfo.get("videoTechnologies")
        if not isinstance(video, dict) or not isinstance(video.get("fmp4"), str):
            self.logger.error("AmateurTV readmodel has no legacy fMP4 URL")
            return []
        if not isinstance(qualities, list):
            self.logger.error("AmateurTV readmodel has no legacy qualities")
            return []

        sources = []
        for resolution in qualities:
            if not isinstance(resolution, str):
                self.logger.warning(f"Ignoring malformed AmateurTV quality {resolution!r}")
                continue
            parts = resolution.split('x')
            if len(parts) != 2 or not all(part.isdigit() for part in parts):
                self.logger.warning(f"Ignoring malformed AmateurTV quality {resolution!r}")
                continue
            width, height = int(parts[0]), int(parts[1])
            if width <= 0 or height <= 0:
                self.logger.warning(f"Ignoring malformed AmateurTV quality {resolution!r}")
                continue
            sources.append(
                {
                    'url': f"{video['fmp4']}&variant={height}",
                    'resolution': (width, height),
                    'frame_rate': None,
                    'bandwidth': None,
                }
            )
        return sources

    def getVideoUrl(self):
        piece = self._piece(self.lastInfo)
        if piece in _EASYSTREAM_PIECES:
            return self._buildEasyStreamStream()
        if piece in ("", "fstream"):
            return self.getWantedResolutionPlaylist(None)
        self.logger.error(f"Unsupported AmateurTV stream backend {piece!r}")
        return None

    def _token_headers(self):
        return self.headers | {
            'Accept': 'application/json',
            'Content-Type': 'application/json',
            'Referer': 'https://amateur.tv/',
        }

    def _fetchViewerToken(self):
        try:
            response = self.session.get(
                _VIEWER_TOKEN_URL,
                headers=self._token_headers(),
                cookies=self.cookies,
                timeout=parameters.HTTP_CONNECT_TIMEOUT,
            )
        except Exception as error:  # noqa: BLE001 - the monitor re-polls
            self.logger.warning(f"EasyStream viewer-token request failed: {error}")
            return None
        if response.status_code != 200:
            self.logger.warning(f"EasyStream viewer-token request returned HTTP {response.status_code}")
            return None
        try:
            payload = response.json()
        except Exception as error:  # noqa: BLE001
            self.logger.warning(f"EasyStream viewer-token response is not JSON: {error}")
            return None
        try:
            return parse_viewer_token(payload, now=time.time())
        except Exception as error:  # noqa: BLE001 - never log the token
            self.logger.warning(f"EasyStream viewer-token response was rejected: {error}")
            return None

    def _refreshToken(self):
        return self._fetchViewerToken()

    def _buildEasyStreamStream(self, require_public=False):
        if require_public and self.sc != Status.PUBLIC:
            return None
        target = parse_target(self.lastInfo.get("easystream"))
        if target is None:
            self.logger.error("AmateurTV EasyStream readmodel has no valid stream or alias")
            return None
        credentials = self._fetchViewerToken()
        if credentials is None:
            return None
        return EasyStreamStream(
            target=target,
            credentials=credentials,
            refresh=self._refreshToken,
            reconnect=self._reconnectEasyStream,
            referer='https://amateur.tv/',
            user_agent=self.headers.get('User-Agent'),
            label=f"EasyStream/{self.username}",
        )

    def _reconnectEasyStream(self):
        try:
            status = self.getStatus()
        except Exception as error:  # noqa: BLE001 - the monitor re-polls on failure
            self.logger.warning(f"EasyStream reconnect status check failed: {error}")
            return None
        if status != Status.PUBLIC:
            self.logger.info(f"EasyStream show is no longer public ({status.name})")
            return None
        return self._buildEasyStreamStream(require_public=False)

    def getStatus(self):
        headers = self.headers | {'Content-Type': 'application/json', 'Referer': 'https://amateur.tv/'}
        r = self.session.get(f'https://www.amateur.tv/v3/readmodel/show/{self.username}/en', headers=headers)

        if r.status_code != 200:
            return Status.UNKNOWN

        self.lastInfo = r.json()

        if self.lastInfo.get('message') == 'NOT_FOUND':
            return Status.NOTEXIST
        if self.lastInfo.get('result') == 'KO':
            return Status.UNKNOWN
        if self.lastInfo.get('status') == 'online':
            if self.lastInfo.get('privateChatStatus') is None:
                return Status.PUBLIC
            else:
                return Status.PRIVATE
        if self.lastInfo.get('status') == 'offline':
            return Status.OFFLINE
        return Status.UNKNOWN
