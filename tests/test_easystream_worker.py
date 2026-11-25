"""Deterministic tests for the EasyStream WebSocket worker."""

from __future__ import annotations

import asyncio
import base64
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace

import pytest

import parameters
from streamonitor.downloaders.easystream import EasyStreamStream
from streamonitor.downloaders.easystream_backend import EasyStreamBackend
from streamonitor.downloaders.easystream_protocol import EasyStreamCredentials, EasyStreamEdge, EasyStreamTarget
from streamonitor.downloaders.easystream_worker import EasyStreamSession
from tests.easystream_fixtures import (
    KID,
    STREAM_ID,
    EdgeServer,
    cenc_fragments,
    clear_behaviour,
    encrypted_behaviour,
    fragment,
    init_segment,
    read_license_challenge,
    send_json,
    write_fake_ffmpeg,
)

KEY = "000102030405060708090a0b0c0d0e0f"


def make_config(output, edge_url: str, *, expires_at=None, wanted=720) -> dict:
    return {
        "target": {"stream": STREAM_ID},
        "token": "SECRET-VIEWER-TOKEN",
        "edges": [{"url": edge_url, "weight": 1.0}],
        "pickK": 1,
        "expiresAt": expires_at,
        "output": str(output),
        "referer": "https://amateur.tv/",
        "userAgent": "test-agent",
        "wantedResolution": wanted,
        "resolutionPreference": "closest",
        "maxDurationSec": 0,
    }


def run_session(config: dict, monkeypatch, *, no_stdin=True):
    if no_stdin:
        monkeypatch.setattr(EasyStreamSession, "_start_command_reader", lambda self: None)
    session = EasyStreamSession(config)
    events: list[dict] = []
    monkeypatch.setattr(session, "_emit", lambda event, **fields: events.append({"event": event, **fields}))
    asyncio.run(session.run())
    return session, events


def by_event(events: list[dict], name: str) -> list[dict]:
    return [event for event in events if event.get("event") == name]


def test_clear_stream_records_media_and_stops(monkeypatch, tmp_path):
    monkeypatch.setattr(parameters, "FFMPEG_PATH", str(write_fake_ffmpeg(tmp_path / "ffmpeg")))
    handler = clear_behaviour()
    server = EdgeServer(handler).start()
    try:
        session, events = run_session(make_config(tmp_path / "out.mp4", server.url()), monkeypatch)
    finally:
        server.close()

    assert (tmp_path / "out.mp4").stat().st_size > 0
    assert by_event(events, "media")
    stopped = by_event(events, "stopped")[-1]
    assert stopped["reason"] == "ended"
    assert stopped["mediaBytes"] > 0
    assert stopped["width"] == 1280
    kinds = [message["type"] for message in handler.received]
    assert kinds[0] == "subscribe"
    assert "select" in kinds
    assert "want" in kinds
    assert session.ffmpeg_exit == 0


def test_worker_selects_the_wanted_layer_before_media(monkeypatch, tmp_path):
    monkeypatch.setattr(parameters, "FFMPEG_PATH", str(write_fake_ffmpeg(tmp_path / "ffmpeg")))
    handler = clear_behaviour()
    server = EdgeServer(handler).start()
    try:
        _, events = run_session(make_config(tmp_path / "out.mp4", server.url(), wanted=1080), monkeypatch)
    finally:
        server.close()

    select = next(message for message in handler.received if message["type"] == "select")
    assert select == {"type": "select", "stream": STREAM_ID, "layer": 2}
    subscribed = by_event(events, "subscribed")[-1]
    assert subscribed["layer"] == 2
    assert "1920x1080" in subscribed["resolution"]


def test_layers_event_reselects_a_removed_or_remapped_layer(monkeypatch, tmp_path):
    monkeypatch.setattr(parameters, "FFMPEG_PATH", str(write_fake_ffmpeg(tmp_path / "ffmpeg")))
    received: list[dict] = []

    async def handler(ws):
        async for raw in ws:
            message = json.loads(raw)
            received.append(message)
            kind = message.get("type")
            if kind == "subscribe":
                await send_json(
                    ws,
                    {
                        "type": "subscribed",
                        "stream": STREAM_ID,
                        "session": "session-1",
                        "n": 0,
                        "layer": 0,
                        "layers": [0, 1, 2],
                        "resolutions": [360, 480, 720],
                        "codecs": "avc1.64002a,mp4a.40.2",
                        "drm": "none",
                    },
                )
                await send_json(ws, {"type": "access", "stream": STREAM_ID, "access": "public", "media": "all"})
            elif kind == "want":
                # The encoder dropped the top layer between subscribe and media.
                await send_json(
                    ws,
                    {
                        "type": "layers",
                        "stream": STREAM_ID,
                        "layers": [0, 1],
                        "resolutions": [360, 720],
                    },
                )
                await send_json(ws, {"type": "layer", "stream": STREAM_ID, "layer": 1, "codecs": "avc1.64002a"})
                await ws.send(init_segment())
                await ws.send(fragment())
                await send_json(ws, {"type": "end", "stream": STREAM_ID})

    server = EdgeServer(handler).start()
    try:
        _, events = run_session(make_config(tmp_path / "out.mp4", server.url(), wanted=1080), monkeypatch)
    finally:
        server.close()

    selects = [message["layer"] for message in received if message["type"] == "select"]
    assert selects == [2, 1]
    assert by_event(events, "stopped")[-1]["reason"] == "ended"


def test_timestamp_reset_ends_the_part_for_reconnect(monkeypatch, tmp_path):
    monkeypatch.setattr(parameters, "FFMPEG_PATH", str(write_fake_ffmpeg(tmp_path / "ffmpeg")))

    async def handler(ws):
        async for raw in ws:
            message = json.loads(raw)
            kind = message.get("type")
            if kind == "subscribe":
                await send_json(
                    ws,
                    {
                        "type": "subscribed",
                        "stream": STREAM_ID,
                        "session": "session-1",
                        "n": 0,
                        "layer": 0,
                        "layers": [0],
                        "resolutions": [720],
                        "codecs": "avc1.64002a,mp4a.40.2",
                        "drm": "none",
                    },
                )
                await send_json(ws, {"type": "access", "stream": STREAM_ID, "access": "public", "media": "all"})
            elif kind == "want":
                await ws.send(init_segment())
                await ws.send(fragment(decode_time=900000))
                await ws.send(fragment(decode_time=0))

    server = EdgeServer(handler).start()
    try:
        _, events = run_session(make_config(tmp_path / "out.mp4", server.url()), monkeypatch)
    finally:
        server.close()

    assert by_event(events, "stopped")[-1]["reason"] == "timestamp_reset"


def test_media_is_not_accepted_before_public_access(monkeypatch, tmp_path):
    monkeypatch.setattr(parameters, "FFMPEG_PATH", str(write_fake_ffmpeg(tmp_path / "ffmpeg")))

    async def handler(ws):
        async for raw in ws:
            message = json.loads(raw)
            kind = message.get("type")
            if kind == "subscribe":
                await send_json(
                    ws,
                    {
                        "type": "subscribed",
                        "stream": STREAM_ID,
                        "session": "session-1",
                        "n": 0,
                        "layer": 0,
                        "layers": [0],
                        "resolutions": [720],
                        "codecs": "avc1.64002a,mp4a.40.2",
                        "drm": "none",
                    },
                )
            elif kind == "want":
                await ws.send(init_segment())
                await ws.send(fragment())
                await asyncio.sleep(0.2)
                await send_json(ws, {"type": "access", "stream": STREAM_ID, "access": "public", "media": "all"})
                await send_json(ws, {"type": "end", "stream": STREAM_ID})

    server = EdgeServer(handler).start()
    try:
        _, events = run_session(make_config(tmp_path / "out.mp4", server.url()), monkeypatch)
    finally:
        server.close()

    names = [event["event"] for event in events]
    assert "media" in names
    assert names.index("media") > names.index("access")
    assert by_event(events, "stopped")[-1]["reason"] == "ended"
    assert (tmp_path / "out.mp4").stat().st_size > 0


def test_encrypted_stream_requests_clearkey_and_feeds_ffmpeg(monkeypatch, tmp_path):
    monkeypatch.setattr(parameters, "FFMPEG_PATH", str(write_fake_ffmpeg(tmp_path / "ffmpeg")))
    handler = encrypted_behaviour()
    server = EdgeServer(handler).start()
    try:
        _, events = run_session(make_config(tmp_path / "out.mp4", server.url()), monkeypatch)
    finally:
        server.close()

    license_requests = [message for message in handler.received if message["type"] == "license"]
    assert license_requests
    assert read_license_challenge(license_requests[0]) == KID
    assert by_event(events, "license")
    assert by_event(events, "media")[-1]["encrypted"] is True
    assert (tmp_path / "out.mp4").stat().st_size > 0
    assert by_event(events, "stopped")[-1]["reason"] == "ended"
    serialized = json.dumps(events)
    assert "SECRET-VIEWER-TOKEN" not in serialized
    assert KEY not in serialized


def test_denied_license_fails_without_writing_media(monkeypatch, tmp_path):
    monkeypatch.setattr(parameters, "FFMPEG_PATH", str(write_fake_ffmpeg(tmp_path / "ffmpeg")))
    monkeypatch.setattr(parameters, "EASYSTREAM_LICENSE_TIMEOUT", 0.5)
    handler = encrypted_behaviour(deny=True)
    server = EdgeServer(handler).start()
    try:
        _, events = run_session(make_config(tmp_path / "out.mp4", server.url()), monkeypatch)
    finally:
        server.close()

    assert by_event(events, "stopped")[-1]["reason"] == "license_denied"
    assert not by_event(events, "license")
    assert not (tmp_path / "out.mp4").exists()


def test_access_withdrawal_stops_promptly(monkeypatch, tmp_path):
    monkeypatch.setattr(parameters, "FFMPEG_PATH", str(write_fake_ffmpeg(tmp_path / "ffmpeg")))

    async def handler(ws):
        async for raw in ws:
            message = json.loads(raw)
            if message.get("type") == "subscribe":
                await send_json(
                    ws,
                    {
                        "type": "subscribed",
                        "stream": STREAM_ID,
                        "session": "s",
                        "n": 0,
                        "layer": 0,
                        "layers": [0],
                        "resolutions": ["640x360"],
                        "codecs": "avc1,mp4a.40.2",
                        "drm": "none",
                    },
                )
            elif message.get("type") == "want":
                await ws.send(init_segment())
                await ws.send(fragment())
                await send_json(
                    ws,
                    {"type": "access", "stream": STREAM_ID, "access": "private", "media": "none", "reason": "private"},
                )

    server = EdgeServer(handler).start()
    try:
        _, events = run_session(make_config(tmp_path / "out.mp4", server.url()), monkeypatch)
    finally:
        server.close()

    assert by_event(events, "stopped")[-1]["reason"] == "access_denied"


def test_no_media_times_out(monkeypatch, tmp_path):
    monkeypatch.setattr(parameters, "FFMPEG_PATH", str(write_fake_ffmpeg(tmp_path / "ffmpeg")))
    monkeypatch.setattr(parameters, "EASYSTREAM_STARTUP_TIMEOUT", 0.3)

    async def handler(ws):
        async for raw in ws:
            message = json.loads(raw)
            if message.get("type") == "subscribe":
                await send_json(
                    ws,
                    {
                        "type": "subscribed",
                        "stream": STREAM_ID,
                        "session": "s",
                        "n": 0,
                        "layer": 0,
                        "layers": [0],
                        "resolutions": ["640x360"],
                        "codecs": "avc1",
                        "drm": "none",
                    },
                )
            await asyncio.sleep(0.05)

    server = EdgeServer(handler).start()
    try:
        _, events = run_session(make_config(tmp_path / "out.mp4", server.url()), monkeypatch)
    finally:
        server.close()

    assert by_event(events, "stopped")[-1]["reason"] == "no_media"


def test_worker_replies_to_server_pings(monkeypatch, tmp_path):
    monkeypatch.setattr(parameters, "FFMPEG_PATH", str(write_fake_ffmpeg(tmp_path / "ffmpeg")))
    pongs: list[dict] = []

    async def handler(ws):
        async for raw in ws:
            message = json.loads(raw)
            if message.get("type") == "subscribe":
                await send_json(
                    ws,
                    {
                        "type": "subscribed",
                        "stream": STREAM_ID,
                        "session": "s",
                        "n": 0,
                        "layer": 0,
                        "layers": [0],
                        "resolutions": ["640x360"],
                        "codecs": "avc1",
                        "drm": "none",
                    },
                )
                await send_json(ws, {"type": "ping", "id": 99})
            elif message.get("type") == "pong":
                pongs.append(message)
                await ws.send(init_segment())
                await ws.send(fragment())
                await send_json(ws, {"type": "end", "stream": STREAM_ID})

    server = EdgeServer(handler).start()
    try:
        run_session(make_config(tmp_path / "out.mp4", server.url()), monkeypatch)
    finally:
        server.close()

    assert pongs and pongs[0]["id"] == 99


def test_renew_updates_the_token_in_place(monkeypatch):
    monkeypatch.setattr(EasyStreamSession, "_start_command_reader", lambda self: None)
    sent: list[dict] = []

    class FakeSocket:
        async def send(self, raw):
            sent.append(json.loads(raw))

    session = EasyStreamSession(make_config("/tmp/unused.mp4", "ws://127.0.0.1:1/v1/viewer-connections"))
    session.ws = FakeSocket()
    asyncio.run(session._renew({"token": "NEW-TOKEN", "expiresAt": 12345, "pickK": 1}))

    assert session.token == "NEW-TOKEN"
    assert session.expires_at == 12345
    assert sent == [{"type": "token", "stream": STREAM_ID, "token": "NEW-TOKEN"}]


def test_backend_bootstraps_without_a_credential_file(monkeypatch, tmp_path):
    fake_ffmpeg = str(write_fake_ffmpeg(tmp_path / "ffmpeg"))
    monkeypatch.setattr(parameters, "FFMPEG_PATH", fake_ffmpeg)
    monkeypatch.setenv("STRMNTR_FFMPEG_PATH", fake_ffmpeg)

    def forbid_config_file(*args, **kwargs):
        pytest.fail("worker credentials must not be written to a temporary file")

    monkeypatch.setattr(tempfile, "mkstemp", forbid_config_file)
    seen: list[bool] = []

    async def handler(ws):
        async for raw in ws:
            seen.append(True)
            await asyncio.sleep(0.05)

    server = EdgeServer(handler).start()
    stream = EasyStreamStream(
        target=EasyStreamTarget(stream=STREAM_ID),
        credentials=EasyStreamCredentials(
            token="SECRET-VIEWER-TOKEN",
            edges=(EasyStreamEdge(server.url(), 1.0),),
            pick_k=1,
        ),
    )
    backend = EasyStreamBackend()
    try:
        backend.start(stream, str(tmp_path / "out.mp4"))
        deadline = time.time() + 5
        while not seen and time.time() < deadline:
            time.sleep(0.05)
        assert seen
        assert "SECRET-VIEWER-TOKEN" not in " ".join(backend._process.args)
        assert "--config" not in backend._process.args
        # Closing the pipe models the EOF the worker sees if its monitor dies.
        backend._process.stdin.close()
        assert backend.wait(5) == 0
    finally:
        backend.close(5)
        server.close()

    assert backend.exited


@pytest.mark.parametrize("fragmented", [False, True], ids=["complete", "fragmented"])
def test_real_ffmpeg_decrypts_a_synthetic_cenc_file(monkeypatch, tmp_path, fragmented):
    ffmpeg = shutil.which(parameters.FFMPEG_PATH)
    if ffmpeg is None:
        pytest.skip("configured ffmpeg is not available")
    encrypted = tmp_path / "encrypted.mp4"
    generated = subprocess.run(
        [
            ffmpeg,
            "-y",
            "-f",
            "lavfi",
            "-i",
            "testsrc=size=320x240:rate=15",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:sample_rate=44100",
            "-t",
            "2",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-shortest",
            "-movflags",
            "+faststart",
            "-encryption_scheme",
            "cenc-aes-ctr",
            "-encryption_key",
            KEY,
            "-encryption_kid",
            KID,
            *(["-an", "-bf", "0", "-g", "15"] if fragmented else []),
            str(encrypted),
        ],
        capture_output=True,
        timeout=60,
    )
    if generated.returncode != 0:
        pytest.skip("this ffmpeg build cannot write encrypted MP4")
    data = encrypted.read_bytes()
    messages = cenc_fragments(data) if fragmented else [data]
    received: list[dict] = []

    async def handler(ws):
        async for raw in ws:
            message = json.loads(raw)
            received.append(message)
            kind = message.get("type")
            if kind == "subscribe":
                await send_json(
                    ws,
                    {
                        "type": "subscribed",
                        "stream": STREAM_ID,
                        "session": "s",
                        "n": 0,
                        "layer": 0,
                        "layers": [0],
                        "resolutions": ["320x240"],
                        "codecs": "avc1,mp4a.40.2",
                        "drm": "on",
                    },
                )
                await send_json(ws, {"type": "access", "stream": STREAM_ID, "access": "public", "media": "all"})
            elif kind == "want":
                await ws.send(messages[0])
            elif kind == "license":
                encoded_key = base64.urlsafe_b64encode(bytes.fromhex(KEY)).rstrip(b"=").decode("ascii")
                encoded_kid = base64.urlsafe_b64encode(bytes.fromhex(message["kid"])).rstrip(b"=").decode("ascii")
                body = json.dumps({"keys": [{"kty": "oct", "kid": encoded_kid, "k": encoded_key}]})
                await send_json(
                    ws,
                    {
                        "type": "license",
                        "stream": STREAM_ID,
                        "kid": message["kid"],
                        "payload": base64.b64encode(body.encode()).decode(),
                    },
                )
                for media in messages[1:]:
                    await ws.send(media)
                    await asyncio.sleep(0.02)
                await send_json(ws, {"type": "end", "stream": STREAM_ID})

    server = EdgeServer(handler).start()
    monkeypatch.setattr(parameters, "FFMPEG_PATH", ffmpeg)
    try:
        session, events = run_session(make_config(tmp_path / "decrypted.mp4", server.url()), monkeypatch)
    finally:
        server.close()

    assert session.ffmpeg_exit == 0
    if fragmented:
        assert session.init_info.has_media is False
        assert session.media_fragments >= 2
    output = tmp_path / "decrypted.mp4"
    assert output.stat().st_size > 0
    assert by_event(events, "stopped")[-1]["reason"] == "ended"
    probe = subprocess.run(
        [ffmpeg, "-v", "error", "-xerror", "-i", str(output), "-map", "0:v:0", "-f", "framehash", "-"],
        capture_output=True,
        timeout=60,
    )
    assert probe.returncode == 0, probe.stderr.decode("utf-8", "replace")
    assert not probe.stderr, probe.stderr.decode("utf-8", "replace")
    frames = [line for line in probe.stdout.splitlines() if line and not line.startswith(b"#")]
    assert len(frames) == 30


@pytest.mark.parametrize("returncode", [0, 1])
def test_backend_probes_configured_python(monkeypatch, returncode):
    calls = []
    monkeypatch.setattr(parameters, "EASYSTREAM_PYTHON", "/custom/python")
    monkeypatch.setattr(EasyStreamBackend, "ffmpeg_available", lambda: True)

    def probe(command, **kwargs):
        calls.append((command, kwargs))
        return SimpleNamespace(returncode=returncode)

    monkeypatch.setattr(subprocess, "run", probe)
    assert EasyStreamBackend.available() is (returncode == 0)
    command, kwargs = calls[0]
    assert command[0] == "/custom/python"
    assert "easystream_worker" in command[2]
    assert kwargs["timeout"] == 10
    assert kwargs["cwd"] == EasyStreamBackend.project_root()
    assert kwargs["env"]["PYTHONPATH"].startswith(EasyStreamBackend.project_root())


def test_backend_rejects_python_without_worker_dependencies(monkeypatch, tmp_path):
    import venv

    venv.EnvBuilder(with_pip=False).create(tmp_path / "isolated")
    python = tmp_path / "isolated" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    monkeypatch.setattr(parameters, "EASYSTREAM_PYTHON", str(python))
    monkeypatch.setattr(EasyStreamBackend, "ffmpeg_available", lambda: True)
    assert not EasyStreamBackend.available()
    monkeypatch.setattr(parameters, "EASYSTREAM_PYTHON", sys.executable)
    assert EasyStreamBackend.available()
