import os
import sys
from pathlib import Path

import environ
from fake_useragent import UserAgent

# Resolve settings from the checkout root, not the caller's working directory.
PROJECT_ROOT = Path(__file__).resolve().parent

env = environ.Env()
ENV_FILE = Path(os.environ.get("STRMNTR_ENV_FILE", PROJECT_ROOT / ".env"))
if ENV_FILE.exists():
    environ.Env.read_env(str(ENV_FILE))


def _project_path(value):
    path = Path(value)
    return path if path.is_absolute() else PROJECT_ROOT / path


DOWNLOADS_DIR = str(_project_path(env.str("STRMNTR_DOWNLOAD_DIR", "downloads")))
CONFIG_FILE = str(_project_path(env.str("STRMNTR_CONFIG", "config.json")))
MIN_FREE_DISK_PERCENT = env.float("STRMNTR_MIN_FREE_SPACE", 5.0)  # in %
DEBUG = env.bool("STRMNTR_DEBUG", False)

# The camsoda bot ignores this setting in favor of a chrome useragent generated with the fake-useragent library
HTTP_USER_AGENT = env.str("STRMNTR_USER_AGENT", UserAgent().chrome)

# Specify the full path to the ffmpeg binary. By default, ffmpeg found on PATH is used.
FFMPEG_PATH = env.str("STRMNTR_FFMPEG_PATH", 'ffmpeg')

# You can enter a number to select a specific height.
# Use a huge number here and closest match to get the highest resolution variant
# Eg: 240, 360, 480, 720, 1080, 1440, 99999
WANTED_RESOLUTION = env.int("STRMNTR_RESOLUTION", 1080)

# Specify match type when specified height
# Possible values: exact, exact_or_least_higher, exact_or_highest_lower, closest
# Beware of the exact policy. Nothing gets downloaded if the wanted resolution is not available
WANTED_RESOLUTION_PREFERENCE = env.str("STRMNTR_RESOLUTION_PREF", 'closest')

# Specify output container here
# Suggested values are 'mkv' or 'mp4'
CONTAINER = env.str("STRMNTR_CONTAINER", 'mp4')

# Rewrite LL-HLS/CMAF master playlists to a temporary local playlist so ffmpeg
# can align the separate audio and video renditions. This applies to Chaturbate
# and MyFreeCams. Disable it to pass the original master URL to ffmpeg instead
# (no resolution selection).
CMAF_MASTER_PLAYLIST = env.bool("STRMNTR_CMAF_MASTER_PLAYLIST", True)

# Add auto-generated VR format suffix to files
VR_FORMAT_SUFFIX = env.bool("STRMNTR_VR_FORMAT_SUFFIX", True)

# Limit how fast ffmpeg reads a live stream. It is disabled by default
# because pacing the input makes ffmpeg run ahead of low latency HLS streams
# (such as Chaturbate's) until one of the renditions stops delivering data,
# which stalls the recording after a few minutes. Use a value such as 1.3
# only if ffmpeg reads a stream too far ahead without it.
FFMPEG_READRATE = env.float("STRMNTR_FFMPEG_READRATE", 0)

# Stop and restart ffmpeg when it produces no output for this many seconds.
# Live HLS demuxers can block forever (for example when one rendition stops
# receiving segments), which would otherwise leave a recording stuck at a few
# minutes without any error. Set to 0 to disable the watchdog.
FFMPEG_STALL_TIMEOUT = env.int("STRMNTR_FFMPEG_STALL_TIMEOUT", 45)

# Deadlines for non-streaming ffmpeg work. Streaming recordings are governed
# by FFMPEG_STALL_TIMEOUT instead; probes, remuxes and recovery merges must
# always finish, otherwise a damaged file can block the next recording.
FFMPEG_PROBE_TIMEOUT = env.int("STRMNTR_FFMPEG_PROBE_TIMEOUT", 60)
FFMPEG_REMUX_TIMEOUT = env.int("STRMNTR_FFMPEG_REMUX_TIMEOUT", 900)

# Native HLS transport policy. Playlist refreshes and segment transfers are
# short requests, so they get finite connect/read timeouts and a small retry
# budget. Long-lived streams (ffmpeg, WebRTC) do not use these values.
HTTP_CONNECT_TIMEOUT = env.float("STRMNTR_HTTP_CONNECT_TIMEOUT", 10.0)
HLS_PLAYLIST_TIMEOUT = env.float("STRMNTR_HLS_PLAYLIST_TIMEOUT", 20.0)
HLS_SEGMENT_TIMEOUT = env.float("STRMNTR_HLS_SEGMENT_TIMEOUT", 60.0)
HTTP_RETRY_ATTEMPTS = env.int("STRMNTR_HTTP_RETRY_ATTEMPTS", 3)
HTTP_RETRY_BASE_DELAY = env.float("STRMNTR_HTTP_RETRY_BASE_DELAY", 0.5)
HTTP_RETRY_MAX_DELAY = env.float("STRMNTR_HTTP_RETRY_MAX_DELAY", 8.0)

# How long shutdown waits for the native HLS worker after requesting a stop.
# A blocked request is already bounded by the timeouts above; this deadline
# only protects the caller from an unexpected hang.
HLS_STOP_DEADLINE = env.float("STRMNTR_HLS_STOP_DEADLINE", 90.0)

# Specify the segment time in seconds
# If None, the video will be downloaded as a single file
# Example:
# 5 minutes
# SEGMENT_TIME = 300
# 1 hour
# SEGMENT_TIME = 3600
# Also see the ffmpeg documentation for the segment_time option
# You can specify time in hh:mm:ss format
# Example:
# 1 hour
# SEGMENT_TIME = '1:00:00'
SEGMENT_TIME = env.str("STRMNTR_SEGMENT_TIME", None)

# Agora recording (ManyVids)
#
# The recorder joins Agora's browser WebRTC gateway from a Python subprocess
# (aiortc + PyAV).  No Agora native SDK, Java or Maven is required.
#   webrtc - Python WebRTC gateway worker (recommended)
AGORA_BACKEND = env.str("STRMNTR_AGORA_BACKEND", "webrtc")

# Python used to run the recorder worker.  Defaults to the interpreter running
# StreamMonitor so the worker sees the same installed packages.
AGORA_PYTHON = env.str("STRMNTR_AGORA_PYTHON", sys.executable)

# Agora exposes simulcast high and low streams; pick one here.
AGORA_STREAM_TYPE = env.str("STRMNTR_AGORA_STREAM", "high")

# Seconds to wait for the recorder to join a channel and receive its first
# media frame before the downloader gives up.
AGORA_JOIN_TIMEOUT = env.int("STRMNTR_AGORA_JOIN_TIMEOUT", 45)

# Seconds to wait for a graceful recorder shutdown before killing it.
AGORA_STOP_TIMEOUT = env.int("STRMNTR_AGORA_STOP_TIMEOUT", 15)

# Hard limit in seconds for one recording segment; 0 disables the limit.
AGORA_MAX_DURATION = env.int("STRMNTR_AGORA_MAX_DURATION", 0)

# Renew a channel token this many seconds before it expires. If the refreshed
# credentials change the channel or uid, the current file is finalized and a
# new segment is started.
AGORA_TOKEN_REFRESH_MARGIN = env.int("STRMNTR_AGORA_TOKEN_MARGIN", 300)

# HTTP Manager configuration

# Bind address for the web server
# 0.0.0.0 for remote access from all host
WEBSERVER_HOST = env.str("STRMNTR_HOST", "127.0.0.1")
WEBSERVER_PORT = env.int("STRMNTR_PORT", 5000)

# Web UI skin
# Available options:
# - kseen715 - 2nd skin, currently broken
# - truck-kun (default) - 3rd skin, row oriented
# - shaftoverflow - 4th skin, card layout, links in menus
WEBSERVER_SKIN = env.str("STRMNTR_SKIN", "truck-kun")

# set frequency in seconds of how often the streamer list will update
WEB_LIST_FREQUENCY = env.int("STRMNTR_LIST_FREQ", 30)

# set frequency in seconds of how often the streamer's status will update on the recording page
WEB_STATUS_FREQUENCY = env.int("STRMNTR_STATUS_FREQ", 5)

# set theater_mode
WEB_THEATER_MODE = env.bool("STRMNTR_THEATER_MODE", False)

# confirm deletes, default to mobile-only.
# set to empty string to disable
# set to "MOBILE" to explicitly confirm deletes only on mobile
# set to any other non-falsy value to always check
WEB_CONFIRM_DELETES = env.str("STRMNTR_CONFIRM_DEL", "MOBILE")

# Password for the web server
# If empty no auth required, else username admin and choosen password
WEBSERVER_PASSWORD = env.str("STRMNTR_PASSWORD", "admin")
