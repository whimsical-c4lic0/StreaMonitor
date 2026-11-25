"""Unit tests for the EasyStream protocol, downloader and ATV integration."""

from __future__ import annotations

import base64
import json
import logging
import shutil
import subprocess
import threading
import time

import pytest

import parameters
from streamonitor.downloaders import easystream
from streamonitor.downloaders.easystream import (
    EasyStreamDownloader,
    EasyStreamStream,
    finalize_recording,
)
from streamonitor.downloaders.easystream_protocol import (
    EasyStreamCredentials,
    EasyStreamEdge,
    EasyStreamProtocolError,
    EasyStreamTarget,
    LicenseError,
    clearkey_challenge,
    decode_message,
    parse_clearkey_license,
    parse_fragment,
    parse_init,
    parse_target,
    parse_viewer_token,
    select_layer,
    validate_server_message,
)
from streamonitor.enums import Status
from streamonitor.sites.amateurtv import AmateurTV, getVideoAmateurTV
from tests.easystream_fixtures import KID, STREAM_ID, fragment, init_segment, write_fake_ffmpeg

TOKEN = "viewer.token.signature"


def credentials(token: str = TOKEN, expires_at: float | None = None) -> EasyStreamCredentials:
    return EasyStreamCredentials(
        token=token,
        edges=(EasyStreamEdge("wss://edge.example/v1/viewer-connections", 1.0),),
        pick_k=1,
        expires_at=expires_at,
    )


def jwt_token(exp: int) -> str:
    payload = base64.urlsafe_b64encode(json.dumps({"exp": exp}).encode()).rstrip(b"=").decode()
    return f"header.{payload}.signature"


# -- target and viewer token -------------------------------------------------


def test_target_prefers_valid_stream_and_falls_back_to_alias():
    assert parse_target({"stream": STREAM_ID, "sub": "alias"}).stream == STREAM_ID
    assert parse_target({"stream": "bad", "sub": "alias"}).sub == "alias"
    assert parse_target({"stream": "bad", "sub": "bad alias"}) is None
    assert parse_target(None) is None


def test_target_requires_exactly_one_value():
    with pytest.raises(EasyStreamProtocolError):
        EasyStreamTarget()
    with pytest.raises(EasyStreamProtocolError):
        EasyStreamTarget(stream=STREAM_ID, sub="alias")


def test_credentials_repr_never_contains_the_token():
    value = credentials("SUPER-SECRET-TOKEN")
    assert "SUPER-SECRET-TOKEN" not in str(value)
    assert "SUPER-SECRET-TOKEN" not in repr(value)
    assert "1" in str(value)


def test_viewer_token_accepts_flat_and_body_payloads():
    edge = {"url": "wss://edge.example/v1/viewer-connections", "weight": 2}
    flat = parse_viewer_token({"token": TOKEN, "edges": [edge], "pickK": 1, "expiresIn": 120}, now=1000)
    wrapped = parse_viewer_token(
        {"result": "OK", "body": {"token": TOKEN, "edges": [edge], "expiresIn": 120}},
        now=1000,
    )
    assert flat.expires_at == 1120
    assert wrapped.pick_k == 1
    assert wrapped.edges[0].url == edge["url"]


def test_viewer_token_uses_jwt_expiry_before_expires_in():
    expiry = int(time.time()) + 777
    parsed = parse_viewer_token(
        {"token": jwt_token(expiry), "edges": [{"url": "wss://edge.example/v1/viewer-connections", "weight": 1}]},
        now=time.time(),
    )
    assert parsed.expires_at == expiry


@pytest.mark.parametrize(
    "edges",
    [
        [],
        [{"url": "wss://edge.example/other", "weight": 1}],
        [{"url": "ws://edge.example/v1/viewer-connections", "weight": 1}],
        [{"url": "wss://edge.example/v1/viewer-connections", "weight": -1}],
        [{"url": "wss://user:pass@edge.example/v1/viewer-connections", "weight": 1}],
        [{"url": "wss://127.0.0.1/v1/viewer-connections", "weight": 1}],
    ],
)
def test_viewer_token_rejects_edges_outside_the_contract(edges):
    with pytest.raises(EasyStreamProtocolError):
        parse_viewer_token({"token": TOKEN, "edges": edges}, now=1000)


def test_viewer_token_private_hosts_only_when_explicitly_allowed():
    edge = {"url": "wss://127.0.0.1/v1/viewer-connections", "weight": 1}
    parsed = parse_viewer_token({"token": TOKEN, "edges": [edge]}, now=1000, allow_private_hosts=True)
    assert parsed.edges[0].url == edge["url"]


def test_viewer_token_rejects_non_ok_result():
    with pytest.raises(EasyStreamProtocolError):
        parse_viewer_token({"result": "KO", "body": {"token": TOKEN, "edges": []}}, now=1000)


# -- messages and ClearKey ---------------------------------------------------


def test_server_message_validation_accepts_the_observed_shapes():
    subscribed = validate_server_message(
        {
            "type": "subscribed",
            "stream": STREAM_ID,
            "session": "s",
            "n": 0,
            "layer": 1,
            "layers": [0, 1],
            "resolutions": ["640x360", "1280x720"],
            "codecs": "avc1.64002a,mp4a.40.2",
            "drm": "on",
        }
    )
    assert subscribed["layer"] == 1
    assert validate_server_message(
        {"type": "access", "stream": STREAM_ID, "access": "public", "media": "none", "reason": "private"}
    )
    assert validate_server_message({"type": "future-event", "whatever": True})


@pytest.mark.parametrize(
    "message",
    [
        {"type": "subscribed", "stream": "bad", "session": "s", "n": 0, "layer": 0, "layers": [1], "codecs": "x"},
        {"type": "subscribed", "stream": STREAM_ID, "session": "s", "n": 0, "layer": 0, "layers": [15], "codecs": "x"},
        {"type": "license", "stream": STREAM_ID, "kid": "short", "payload": "AAAA"},
        {"type": "license", "stream": STREAM_ID, "kid": KID, "payload": "not base64"},
        {"type": "access", "stream": STREAM_ID, "access": "strange"},
    ],
)
def test_server_message_validation_rejects_unsafe_fields(message):
    with pytest.raises(EasyStreamProtocolError):
        validate_server_message(message)


def test_decode_message_bounds_and_requires_an_object():
    assert decode_message(b'{"type":"ping","id":1}')["id"] == 1
    with pytest.raises(EasyStreamProtocolError):
        decode_message(b"[]")
    with pytest.raises(EasyStreamProtocolError):
        decode_message(b"x" * (1024 * 1024 + 1))


def test_clearkey_challenge_contains_the_actual_kid():
    challenge = json.loads(base64.b64decode(clearkey_challenge(KID)))
    encoded = challenge["kids"][0]
    assert base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)).hex() == KID
    assert challenge["type"] == "temporary"


def test_clearkey_license_accepts_matching_jwk():
    key = bytes.fromhex("000102030405060708090a0b0c0d0e0f")
    body = {
        "keys": [
            {
                "kty": "oct",
                "kid": base64.urlsafe_b64encode(bytes.fromhex(KID)).rstrip(b"=").decode(),
                "k": base64.urlsafe_b64encode(key).rstrip(b"=").decode(),
            }
        ]
    }
    payload = base64.b64encode(json.dumps(body).encode()).decode()
    assert parse_clearkey_license(payload, KID) == key


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"keys": [{"kty": "RSA", "kid": KID, "k": "AAAA"}]},
        {"keys": [{"kty": "oct", "kid": "AAAAAAAAAAAAAAAAAAAAAA", "k": "AAAA"}]},
        {"keys": [{"kty": "oct", "kid": KID, "k": "AAAA"}]},
    ],
)
def test_clearkey_license_rejects_wrong_keys(body):
    payload = base64.b64encode(json.dumps(body).encode()).decode()
    with pytest.raises(LicenseError):
        parse_clearkey_license(payload, KID)


# -- MP4 parsing -------------------------------------------------------------


def test_parse_clear_init_reads_video_and_audio_tracks():
    info = parse_init(init_segment())
    assert info.width == 1280
    assert info.height == 720
    assert info.video.codec == "avc1"
    assert info.audio.codec == "mp4a"
    assert not info.kids
    assert info.tracks[0].timescale == 90000


def test_parse_encrypted_init_reads_cbcs_kid_and_pssh():
    info = parse_init(init_segment(kid=KID, pssh_kid=True))
    assert info.kids == (KID,)
    assert info.video.encrypted is True
    assert info.video.scheme == "cbcs"
    assert info.audio.encrypted is False
    assert "clearkey" in info.pssh_systems


def test_parse_init_rejects_truncated_boxes():
    with pytest.raises(EasyStreamProtocolError):
        parse_init(init_segment()[:-20])


def test_parse_fragment_reads_track_and_timestamps():
    fragments = parse_fragment(fragment(), {1: 90000})
    assert len(fragments) == 1
    assert fragments[0].track_id == 1
    assert fragments[0].start_s == 0
    assert fragments[0].end_s == pytest.approx(0.066666, rel=1e-4)
    assert fragments[0].keyframe is True
    assert fragments[0].has_mdat is True


@pytest.mark.parametrize(
    "wanted,preference,expected",
    [
        (1080, "closest", 1),
        (720, "exact", 1),
        (500, "exact_or_least_higher", 1),
        (500, "exact_or_highest_lower", 0),
        (1080, "exact", None),
    ],
)
def test_select_layer_respects_preference(wanted, preference, expected):
    choice = select_layer([0, 1], ["640x360", "1280x720"], wanted, preference)
    assert (choice.layer if choice else None) == expected


def test_select_layer_uses_width_for_portrait_streams():
    choice = select_layer([0, 1], ["360x640", "720x1280"], 720, "closest")
    assert choice.layer == 1
    assert choice.width == 720
    assert choice.height == 1280


def test_select_layer_accepts_integer_quality_labels():
    choice = select_layer([0, 1, 2, 3], [360, 480, 720, 1080], 720, "exact")
    assert choice.layer == 2
    assert choice.label() == "720p (layer 2)"


def test_select_layer_integer_labels_use_the_short_side():
    choice = select_layer([0, 1], [360, 720], 1080, "closest")
    assert choice.layer == 1
    assert choice.diff == -360


# -- finalization ------------------------------------------------------------


def test_finalize_recording_writes_container_and_removes_source(tmp_path, monkeypatch):
    ffmpeg = write_fake_ffmpeg(tmp_path / "ffmpeg")
    source = tmp_path / "source.mp4"
    source.write_bytes(b"\0" * 4096)
    filename = tmp_path / "user-20260101-120000.mp4"
    monkeypatch.setattr(easystream, "FFMPEG_PATH", str(ffmpeg))
    monkeypatch.setattr(parameters, "SEGMENT_TIME", None)

    assert finalize_recording(str(source), str(filename)) is True
    assert not source.exists()
    assert filename.stat().st_size >= 1024


def test_finalize_recording_keeps_source_when_ffmpeg_fails(tmp_path, monkeypatch):
    ffmpeg = write_fake_ffmpeg(tmp_path / "ffmpeg", exit_code=1)
    source = tmp_path / "source.mp4"
    source.write_bytes(b"\0" * 4096)
    monkeypatch.setattr(easystream, "FFMPEG_PATH", str(ffmpeg))
    monkeypatch.setattr(parameters, "SEGMENT_TIME", None)

    assert finalize_recording(str(source), str(tmp_path / "user.mp4")) is False
    assert source.exists()


# -- downloader lifecycle ----------------------------------------------------


class FakeBot:
    def __init__(self):
        self.logger = logging.getLogger("test-easystream")
        self.stopDownload = None
        self.filename_extra_suffix = ""


class FakeBackend:
    def __init__(self, events=None, *, write_media=True, start_error=None):
        self.events = list(events or [])
        self.write_media = write_media
        self.start_error = start_error
        self.reported_file = None
        self.media = None
        self.exit_code = None
        self.started_with = None
        self.renewed = []
        self.closed = False
        self.stop_requested = threading.Event()
        self._finished = False

    def start(self, stream, output, max_duration=0):
        if self.start_error:
            raise self.start_error
        self.started_with = (stream, output, max_duration)
        self.reported_file = output
        if self.write_media:
            with open(output, "wb") as media_file:
                media_file.write(init_segment(complete=True))

    def next_event(self, timeout=0.5):
        if self.events:
            return self.events.pop(0)
        if not self._finished:
            self._finished = True
            return {"event": "stopped", "reason": "ended", "file": self.reported_file}
        self.stop_requested.wait(timeout)
        return None

    @property
    def exited(self):
        return self._finished

    def request_stop(self):
        self.stop_requested.set()

    def renew(self, value):
        self.renewed.append(value)
        return True

    def close(self, timeout=15):
        self.closed = True
        return 0


def make_stream(refresh=None, reconnect=None, expires_at=None) -> EasyStreamStream:
    return EasyStreamStream(
        target=EasyStreamTarget(stream=STREAM_ID),
        credentials=credentials(expires_at=expires_at),
        refresh=refresh,
        reconnect=reconnect,
        label="Test/EasyStream",
    )


def patch_finalize(monkeypatch):
    calls = []

    def fake(source, filename, logger=None, suffix=""):
        calls.append((source, filename))
        return True

    monkeypatch.setattr(easystream, "finalize_recording", fake)
    return calls


def test_downloader_records_a_part_and_clears_the_monitor_hook(tmp_path, monkeypatch):
    calls = patch_finalize(monkeypatch)
    backend = FakeBackend([{"event": "subscribed", "layers": [0, 1]}, {"event": "media", "width": 1280}])
    downloader = EasyStreamDownloader(FakeBot(), make_stream(), backend=backend)

    assert downloader.record(str(tmp_path / "user.mp4")) is True
    assert calls
    assert backend.closed is True
    assert downloader.bot.stopDownload is None


def test_downloader_denies_private_access_without_retrying(tmp_path, monkeypatch):
    patch_finalize(monkeypatch)
    backend = FakeBackend(
        [{"event": "access", "access": "private", "media": "none"}],
        write_media=False,
    )
    downloader = EasyStreamDownloader(FakeBot(), make_stream(), backend=backend)

    assert downloader.record(str(tmp_path / "user.mp4")) is False
    assert backend.closed is True
    assert downloader.bot.stopDownload is None


def test_downloader_renews_the_token_in_place(tmp_path, monkeypatch):
    patch_finalize(monkeypatch)
    newer = credentials("NEW-TOKEN")
    backend = FakeBackend([{"event": "refresh_needed"}, {"event": "stopped", "reason": "ended"}])
    downloader = EasyStreamDownloader(FakeBot(), make_stream(refresh=lambda: newer), backend=backend)

    assert downloader.record(str(tmp_path / "user.mp4")) is True
    assert backend.renewed == [newer]


def test_downloader_reports_unavailable_backend(tmp_path, monkeypatch):
    downloader = EasyStreamDownloader(
        FakeBot(),
        make_stream(),
        backend=FakeBackend(start_error=easystream.EasyStreamBackendUnavailable("missing")),
    )
    assert downloader.record(str(tmp_path / "user.mp4")) is False
    assert downloader.bot.stopDownload is None


# -- ATV routing -------------------------------------------------------------


def test_atv_routes_easystream_descriptors_to_the_websocket_downloader(monkeypatch):
    stream = make_stream()
    calls = []
    monkeypatch.setattr("streamonitor.sites.amateurtv.getVideoEasyStream", lambda bot, source, name: calls.append(("easy", source, name)) or True)
    monkeypatch.setattr("streamonitor.sites.amateurtv.getVideoFfmpeg", lambda bot, source, name: calls.append(("ffmpeg", source, name)) or True)
    bot = object()
    assert getVideoAmateurTV(bot, stream, "out.mp4") is True
    assert getVideoAmateurTV(bot, "https://example/master.m3u8", "out.mp4") is True
    assert calls[0][0] == "easy"
    assert calls[1][0] == "ffmpeg"


def test_atv_rejects_unknown_sources():
    assert getVideoAmateurTV(object(), object(), "out.mp4") is False


def test_atv_legacy_variants_skip_malformed_qualities():
    bot = AmateurTV("model")
    bot.lastInfo = {
        "piece": "fstream",
        "qualities": ["1280x720", None, "bad", "0x720", "640x360"],
        "videoTechnologies": {"fmp4": "https://cdn.example/live"},
    }
    variants = bot.getPlaylistVariants(None)
    assert [item["resolution"] for item in variants] == [(1280, 720), (640, 360)]
    assert variants[0]["url"].endswith("&variant=720")


def test_atv_easystream_has_no_legacy_variants():
    bot = AmateurTV("model")
    bot.lastInfo = {"piece": "easystream", "qualities": [""]}
    assert bot.getPlaylistVariants(None) == []


def test_atv_get_video_url_dispatches_on_piece(monkeypatch):
    bot = AmateurTV("model")
    bot.lastInfo = {"piece": "easystream", "easystream": {"stream": STREAM_ID}}
    marker = object()
    monkeypatch.setattr(bot, "_buildEasyStreamStream", lambda require_public=False: marker)
    assert bot.getVideoUrl() is marker

    bot.lastInfo = {"piece": "weird"}
    assert bot.getVideoUrl() is None


class FakeResponse:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        if self._payload is None:
            raise ValueError("not json")
        return self._payload


def test_atv_fetch_viewer_token_uses_session_and_headers(monkeypatch):
    bot = AmateurTV("model")
    bot.cookies = {"session": "cookie"}
    calls = []
    payload = {
        "result": "OK",
        "body": {
            "token": TOKEN,
            "edges": [{"url": "wss://edge.example/v1/viewer-connections", "weight": 1}],
            "expiresIn": 900,
        },
    }

    def get(url, **kwargs):
        calls.append((url, kwargs))
        return FakeResponse(200, payload)

    monkeypatch.setattr(bot.session, "get", get)
    parsed = bot._fetchViewerToken()
    assert parsed is not None
    assert parsed.token == TOKEN
    assert calls[0][1]["cookies"] == {"session": "cookie"}
    assert calls[0][1]["headers"]["Referer"] == "https://amateur.tv/"


def test_atv_fetch_viewer_token_rejects_http_error_and_malformed_body(monkeypatch):
    bot = AmateurTV("model")
    monkeypatch.setattr(bot.session, "get", lambda *args, **kwargs: FakeResponse(503))
    assert bot._fetchViewerToken() is None
    monkeypatch.setattr(
        bot.session,
        "get",
        lambda *args, **kwargs: FakeResponse(200, {"token": TOKEN, "edges": []}),
    )
    assert bot._fetchViewerToken() is None


def test_atv_status_remains_public_for_easystream(monkeypatch):
    bot = AmateurTV("model")
    monkeypatch.setattr(
        bot.session,
        "get",
        lambda *args, **kwargs: FakeResponse(
            200,
            {
                "status": "online",
                "privateChatStatus": None,
                "piece": "easystream",
                "easystream": {"stream": STREAM_ID},
                "qualities": [""],
            },
        ),
    )
    assert bot.getStatus() == Status.PUBLIC


# -- regressions: segmentation and reconnect decisions ------------------------


def test_finalize_segments_keep_all_media_and_part_identity(tmp_path, monkeypatch):
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        pytest.skip("ffmpeg is required for the segment finalization regression")
    source = tmp_path / "source.mp4"
    subprocess.run(
        [
            ffmpeg, "-v", "error", "-f", "lavfi", "-i",
            "testsrc2=size=160x120:rate=10", "-t", "6",
            "-c:v", "mpeg4", "-g", "10", str(source),
        ],
        check=True,
        capture_output=True,
        timeout=30,
    )
    monkeypatch.setattr(easystream, "FFMPEG_PATH", ffmpeg)
    monkeypatch.setattr(easystream, "CONTAINER", "mp4")
    monkeypatch.setattr(parameters, "SEGMENT_TIME", "1")
    filename = tmp_path / "user-20260101-120000_part2.mp4"

    assert finalize_recording(str(source), str(filename), suffix="_extra")

    segments = sorted(tmp_path.glob("user-20260101-120000_part2_*_extra.mp4"))
    assert [path.name for path in segments] == [
        f"user-20260101-120000_part2_{index:05d}_extra.mp4" for index in range(6)
    ]
    assert all(easystream._has_media_box(str(path)) for path in segments)
    assert not source.exists()


@pytest.mark.parametrize("raises", [False, True])
def test_reconnect_callback_refusal_does_not_refresh_stale_target(raises):
    refresh_calls = []

    def reconnect():
        if raises:
            raise RuntimeError("status check failed")
        return None  # For example, the show is now private or offline.

    stream = make_stream(
        refresh=lambda: refresh_calls.append(True) or credentials(),
        reconnect=reconnect,
    )
    downloader = EasyStreamDownloader(FakeBot(), stream)

    assert downloader._reconnect_stream(stream) is None
    assert not refresh_calls


def test_reconnect_without_callback_can_refresh_credentials():
    renewed = credentials("renewed-token")
    stream = make_stream(refresh=lambda: renewed)
    downloader = EasyStreamDownloader(FakeBot(), stream)

    replacement = downloader._reconnect_stream(stream)

    assert replacement is not None
    assert replacement.target == stream.target
    assert replacement.credentials is renewed


@pytest.mark.parametrize("reason", ["transport", "revoked", "token_expired"])
def test_exited_worker_drains_queued_stop_reason(tmp_path, reason):
    class ExitedBackend(FakeBackend):
        @property
        def exited(self):
            return True

    backend = ExitedBackend(
        [
            {"event": "subscribed", "layers": [0]},
            {"event": "media", "width": 1280},
            {"event": "stopped", "reason": reason},
        ],
        write_media=False,
    )
    stream = make_stream()
    downloader = EasyStreamDownloader(FakeBot(), stream, backend=backend)

    success, restart = downloader._record_part(stream, str(tmp_path / "user.mp4"))

    assert not success
    assert restart == reason
    assert not backend.events
    assert backend.closed


def test_exited_worker_without_events_requests_reconnect(tmp_path):
    class ExitedBackend(FakeBackend):
        @property
        def exited(self):
            return True

        def next_event(self, timeout=0.5):
            return None

    backend = ExitedBackend(write_media=False)
    stream = make_stream()
    downloader = EasyStreamDownloader(FakeBot(), stream, backend=backend)

    assert downloader._record_part(stream, str(tmp_path / "user.mp4")) == (False, "process_exit")
    assert backend.closed
