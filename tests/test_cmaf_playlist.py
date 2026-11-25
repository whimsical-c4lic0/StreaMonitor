"""Tests for the shared CMAF master playlist rewrite.

MFC and Chaturbate serve LL-HLS/CMAF masters that list audio separately from
the video variants.  The rewrite selects one variant and writes a local master
that keeps that variant's audio rendition, so ffmpeg records both tracks.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

import streamonitor.bot as bot_module
from streamonitor.bot import Bot
from streamonitor.sites import myfreecams
from streamonitor.sites.chaturbate import Chaturbate
from streamonitor.sites.myfreecams import MyFreeCams

MASTER_URL = "https://edgevideo.example/llhls/NxServer/1050/ngrp:mfc_a_147386996.f4v_cmaf/playlist_cmaf.m3u8"
BASE_URL = MASTER_URL.rsplit("/", 1)[0] + "/"

MFC_MASTER_URL = "https://edgevideo.myfreecams.com/llhls/NxServer/1050/ngrp:mfc_a_147386996.f4v_cmaf/playlist_cmaf.m3u8"
MFC_BASE_URL = MFC_MASTER_URL.rsplit("/", 1)[0] + "/"

CB_MASTER_URL = "https://chaturbate.example/live-edge/llhls.m3u8"

MASTER_PLAYLIST = """#EXTM3U
#EXT-X-INDEPENDENT-SEGMENTS
#EXT-X-VERSION:9
#EXT-X-MEDIA:TYPE=AUDIO,NAME="Audio-1",GROUP-ID="1",LANGUAGE="en-us",AUTOSELECT=YES,DEFAULT=YES,CHANNELS="2",URI="chunklist_ao.m3u8"
#EXT-X-STREAM-INF:BANDWIDTH=614000,AVERAGE-BANDWIDTH=550000,NAME="180p 500kbps",CODECS="avc1.4d4015,mp4a.40.2",RESOLUTION=320x180,AUDIO="1",CLOSED-CAPTIONS=NONE
chunklist_vo_180.m3u8
#EXT-X-STREAM-INF:BANDWIDTH=3364000,AVERAGE-BANDWIDTH=3300000,NAME="720p 5.0mbps",CODECS="avc1.4d4028,mp4a.40.2",RESOLUTION=1280x720,AUDIO="1",CLOSED-CAPTIONS=NONE
chunklist_vo_720.m3u8
"""


class FakeResponse:
    def __init__(self, status_code=200, text=""):
        self.status_code = status_code
        self.text = text


class FakeSession:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.response


def makeBot():
    bot = object.__new__(Bot)
    bot.logger = logging.getLogger("test-cmaf")
    bot.headers = {"User-Agent": "test-agent"}
    bot.cookies = {}
    return bot


@pytest.fixture(autouse=True)
def wanted720(monkeypatch):
    monkeypatch.setattr(bot_module, "WANTED_RESOLUTION", 720)
    monkeypatch.setattr(bot_module, "WANTED_RESOLUTION_PREFERENCE", "closest")


def test_build_master_playlist_pairs_variant_with_audio():
    master = type("Master", (), {"version": 9})()
    variant = {
        "url": BASE_URL + "chunklist_vo_720.m3u8",
        "resolution": (1280, 720),
        "frame_rate": 30.0,
        "bandwidth": 3364000,
        "codecs": "avc1.4d4028,mp4a.40.2",
        "audio_group": "1",
    }
    media = {
        "group_id": "1",
        "name": "Audio-1",
        "channels": "2",
        "uri": BASE_URL + "chunklist_ao.m3u8",
    }

    content = Bot._buildMasterPlaylist(master, variant, media)

    assert content.startswith("#EXTM3U\n")
    assert "#EXT-X-VERSION:9" in content
    assert 'TYPE=AUDIO,GROUP-ID="1",NAME="Audio-1"' in content
    assert 'URI="' + BASE_URL + 'chunklist_ao.m3u8"' in content
    assert 'RESOLUTION=1280x720' in content
    assert 'FRAME-RATE=30.000' in content
    assert 'AUDIO="1"' in content
    assert content.rstrip().endswith(BASE_URL + "chunklist_vo_720.m3u8")


def test_get_cmaf_playlist_writes_selected_variant_and_audio(tmp_path):
    bot = makeBot()
    bot.session = FakeSession(FakeResponse(200, MASTER_PLAYLIST))

    result = bot.getCmafPlaylist(MASTER_URL)

    assert isinstance(result, Path)
    try:
        content = result.read_text()
        assert 'URI="' + BASE_URL + 'chunklist_ao.m3u8"' in content
        assert BASE_URL + "chunklist_vo_720.m3u8" in content
        assert "chunklist_vo_180.m3u8" not in content
    finally:
        result.unlink()


def test_get_cmaf_playlist_disabled_returns_master_url():
    bot = makeBot()
    bot.session = FakeSession(FakeResponse(200, MASTER_PLAYLIST))

    assert bot.getCmafPlaylist(MASTER_URL, enabled=False) == MASTER_URL
    assert bot.session.calls == []


def test_get_cmaf_playlist_fetch_failure_falls_back_to_master_url():
    bot = makeBot()
    bot.session = FakeSession(FakeResponse(404))

    assert bot.getCmafPlaylist(MASTER_URL) == MASTER_URL


def test_myfreecams_uses_edgevideo_cmaf_master(monkeypatch):
    bot = MyFreeCams("tester")
    bot.attrs = {
        "data-cam-preview-model-id-value": "47386996",
        "data-cam-preview-server-id-value": "1050",
        "data-cam-preview-is-wzobs-value": "true",
    }
    session = FakeSession(FakeResponse(200, MASTER_PLAYLIST))
    monkeypatch.setattr(bot, "session", session)
    monkeypatch.setattr(myfreecams, "CMAF_MASTER_PLAYLIST", True)

    result = bot.getVideoUrl(refresh=True)

    assert isinstance(result, Path)
    assert session.calls[0][0] == MFC_MASTER_URL
    try:
        content = result.read_text()
        assert 'URI="' + MFC_BASE_URL + 'chunklist_ao.m3u8"' in content
        assert MFC_BASE_URL + "chunklist_vo_720.m3u8" in content
    finally:
        result.unlink()


def test_myfreecams_returns_none_when_master_is_unavailable(monkeypatch):
    bot = MyFreeCams("tester")
    bot.attrs = {
        "data-cam-preview-model-id-value": "47386996",
        "data-cam-preview-server-id-value": "1050",
        "data-cam-preview-is-wzobs-value": "true",
    }
    monkeypatch.setattr(bot, "session", FakeSession(FakeResponse(404)))
    monkeypatch.setattr(myfreecams, "CMAF_MASTER_PLAYLIST", True)

    assert bot.getVideoUrl(refresh=True) is None


def test_chaturbate_llhls_uses_shared_cmaf_helper(monkeypatch):
    bot = Chaturbate("tester")
    bot.lastInfo = {"url": CB_MASTER_URL}
    monkeypatch.setattr(bot, "getStatus", lambda: None)
    calls = []

    def fake_get_cmaf(url, **kwargs):
        calls.append((url, kwargs))
        return "cmaf-result"

    monkeypatch.setattr(bot, "getCmafPlaylist", fake_get_cmaf)

    assert bot.getVideoUrl() == "cmaf-result"
    assert calls == [(CB_MASTER_URL, {"enabled": True})]
