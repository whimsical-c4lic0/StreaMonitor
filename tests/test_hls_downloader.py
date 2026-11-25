"""Deterministic tests for the native HLS downloader.

Every test uses a local HTTP server.  No test contacts a live site, and the
post-processing ffmpeg is a small shell script that copies the temp file.
"""

from __future__ import annotations

import logging
import stat
import threading
import time
import urllib.parse
from collections import OrderedDict
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

import streamonitor.downloaders.hls as hls_module


class FakeBot:
    def __init__(self):
        self.username = "tester"
        self.siteslug = "cb"
        self.logger = logging.getLogger("test-hls")
        self.headers = {"User-Agent": "test-agent"}
        self.cookies = {}
        self.stopDownload = None
        self.stopDownloadFlag = False
        self.filename_extra_suffix = ""
        self.debug_messages: list[str] = []

    def debug(self, message, filename=None):
        self.debug_messages.append(message)


@dataclass
class Response:
    status: int = 200
    body: bytes = b""
    headers: dict[str, str] = field(default_factory=dict)
    delay: float = 0.0
    truncate: bool = False


class ServerState:
    def __init__(self):
        self.routes: dict[str, object] = {}
        self.counts: dict[str, int] = {}
        self.requests: list[str] = []


def make_handler():
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 - stdlib name
            state: ServerState = self.server.state
            state.requests.append(self.path)
            path = urllib.parse.urlsplit(self.path).path
            count = state.counts.get(path, 0) + 1
            state.counts[path] = count

            route = state.routes.get(path)
            if route is None:
                spec = Response(status=404)
            elif callable(route):
                spec = route(count)
            else:
                spec = route
            if not isinstance(spec, Response):
                spec = Response(**spec)

            if spec.delay:
                time.sleep(spec.delay)
            declared = len(spec.body) + (100 if spec.truncate else 0)
            try:
                self.send_response(spec.status)
                headers = {"Content-Length": str(declared)} | dict(spec.headers)
                for name, value in headers.items():
                    self.send_header(name, value)
                self.end_headers()
                if spec.body:
                    self.wfile.write(spec.body)
                if spec.truncate:
                    self.wfile.flush()
                    self.close_connection = True
                    self.connection.close()
            except (BrokenPipeError, ConnectionResetError, OSError):
                self.close_connection = True

        def log_message(self, *args):  # silence the test output
            pass

    return Handler


@pytest.fixture
def server():
    state = ServerState()
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler())
    httpd.daemon_threads = True
    httpd.state = state
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    try:
        yield state, base
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(5)


def playlist(*uris: str) -> bytes:
    lines = ["#EXTM3U", "#EXT-X-TARGETDURATION:1"]
    for uri in uris:
        lines.extend(["#EXTINF:1,", uri])
    return ("\n".join(lines) + "\n").encode()


def make_copy_ffmpeg(tmp_path):
    path = tmp_path / "fake-ffmpeg"
    path.write_text(
        '#!/bin/sh\n'
        'prev=""; in=""; out=""\n'
        'for a in "$@"; do\n'
        '  [ "$prev" = "-i" ] && in="$a"\n'
        '  out="$a"; prev="$a"\n'
        'done\n'
        'cp "$in" "$out"\n'
    )
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return str(path)


def setup_downloader(tmp_path, monkeypatch):
    fake = make_copy_ffmpeg(tmp_path)
    monkeypatch.setattr(hls_module, "FFMPEG_PATH", fake)
    monkeypatch.setattr(hls_module, "SEGMENT_TIME", None)
    monkeypatch.setattr(hls_module, "CONTAINER", "mp4")
    monkeypatch.setattr(hls_module, "DEBUG", False)
    monkeypatch.setattr(hls_module, "FFMPEG_REMUX_TIMEOUT", 10)
    monkeypatch.setattr(hls_module, "HTTP_RETRY_ATTEMPTS", 3)
    monkeypatch.setattr(hls_module, "HTTP_RETRY_BASE_DELAY", 0.0)
    monkeypatch.setattr(hls_module, "HTTP_RETRY_MAX_DELAY", 0.0)
    monkeypatch.setattr(hls_module, "HLS_STOP_DEADLINE", 10)
    return tmp_path / "show.mp4"


def test_resolves_relative_absolute_and_query_segment_urls(server, tmp_path, monkeypatch):
    state, base = server
    output = setup_downloader(tmp_path, monkeypatch)
    playlist_url = base + "/live/index.m3u8"
    port = urllib.parse.urlsplit(base).port

    state.routes["/live/index.m3u8"] = lambda count: Response(
        body=playlist(
            "seg0.ts",
            "../shared/seg1.ts",
            f"http://127.0.0.1:{port}/abs/seg2.ts?x=1",
            "sub/seg3.ts?v=2",
        )
        if count == 1
        else playlist()
    )
    state.routes["/live/seg0.ts"] = Response(body=b"zero")
    state.routes["/shared/seg1.ts"] = Response(body=b"one")
    state.routes["/abs/seg2.ts"] = Response(body=b"two")
    state.routes["/live/sub/seg3.ts"] = Response(body=b"three")

    assert hls_module.getVideoNativeHLS(FakeBot(), playlist_url, str(output)) is True

    assert output.read_bytes() == b"zeroonetwothree"
    assert state.requests[:5] == [
        "/live/index.m3u8",
        "/live/seg0.ts",
        "/shared/seg1.ts",
        "/abs/seg2.ts?x=1",
        "/live/sub/seg3.ts?v=2",
    ]


def test_transient_segment_error_retries_without_duplicates(server, tmp_path, monkeypatch):
    state, base = server
    output = setup_downloader(tmp_path, monkeypatch)
    playlist_url = base + "/live/index.m3u8"

    state.routes["/live/index.m3u8"] = lambda count: Response(
        body=playlist("seg0.ts", "seg1.ts") if count == 1 else playlist()
    )
    state.routes["/live/seg0.ts"] = Response(body=b"A" * 16)
    state.routes["/live/seg1.ts"] = lambda count: (Response(status=503) if count == 1 else Response(body=b"B" * 16))

    assert hls_module.getVideoNativeHLS(FakeBot(), playlist_url, str(output)) is True

    assert output.read_bytes() == b"A" * 16 + b"B" * 16
    assert state.counts["/live/seg1.ts"] == 2


def test_truncated_body_is_retried_without_duplicates(server, tmp_path, monkeypatch):
    state, base = server
    output = setup_downloader(tmp_path, monkeypatch)
    playlist_url = base + "/live/index.m3u8"

    state.routes["/live/index.m3u8"] = lambda count: Response(body=playlist("seg0.ts") if count == 1 else playlist())
    state.routes["/live/seg0.ts"] = lambda count: (
        Response(body=b"C" * 20, truncate=True) if count == 1 else Response(body=b"C" * 20)
    )

    assert hls_module.getVideoNativeHLS(FakeBot(), playlist_url, str(output)) is True

    assert output.read_bytes() == b"C" * 20
    assert state.counts["/live/seg0.ts"] == 2


def test_missing_segment_fails_observably_and_keeps_recoverable_temp(server, tmp_path, monkeypatch):
    state, base = server
    output = setup_downloader(tmp_path, monkeypatch)
    playlist_url = base + "/live/index.m3u8"

    state.routes["/live/index.m3u8"] = Response(body=playlist("seg0.ts", "missing.ts"))
    state.routes["/live/seg0.ts"] = Response(body=b"kept")

    assert hls_module.getVideoNativeHLS(FakeBot(), playlist_url, str(output)) is False

    temp = output.with_suffix(".tmp.ts")
    assert temp.read_bytes() == b"kept"
    assert not output.exists()


def test_worker_exception_reaches_the_caller(server, tmp_path, monkeypatch, caplog):
    state, base = server
    output = setup_downloader(tmp_path, monkeypatch)
    playlist_url = base + "/live/index.m3u8"

    state.routes["/live/index.m3u8"] = Response(body=b"#EXTM3U\n#EXT-X-TARGETDURATION:not-a-number\n")

    with caplog.at_level(logging.ERROR, logger="test-hls"):
        assert hls_module.getVideoNativeHLS(FakeBot(), playlist_url, str(output)) is False

    assert "malformed playlist" in caplog.text
    # Nothing was downloaded, so the empty temp file is removed.
    assert not output.with_suffix(".tmp.ts").exists()


def test_exhausted_retries_log_a_bounded_failure(server, tmp_path, monkeypatch, caplog):
    state, base = server
    output = setup_downloader(tmp_path, monkeypatch)
    monkeypatch.setattr(hls_module, "HTTP_RETRY_ATTEMPTS", 2)
    playlist_url = base + "/live/index.m3u8"

    state.routes["/live/index.m3u8"] = Response(body=playlist("seg0.ts"))
    state.routes["/live/seg0.ts"] = Response(status=503)

    with caplog.at_level(logging.ERROR, logger="test-hls"):
        assert hls_module.getVideoNativeHLS(FakeBot(), playlist_url, str(output)) is False

    assert state.counts["/live/seg0.ts"] == 2
    assert "HLS download failed" in caplog.text
    assert not output.exists()


def test_stop_cancels_a_retry_backoff(server, tmp_path, monkeypatch):
    state, base = server
    output = setup_downloader(tmp_path, monkeypatch)
    monkeypatch.setattr(hls_module, "HTTP_RETRY_ATTEMPTS", 5)
    monkeypatch.setattr(hls_module, "HTTP_RETRY_MAX_DELAY", 8.0)
    playlist_url = base + "/live/index.m3u8"

    state.routes["/live/index.m3u8"] = Response(body=playlist("seg0.ts"))
    state.routes["/live/seg0.ts"] = Response(status=429, headers={"Retry-After": "30"})

    bot = FakeBot()
    result = []
    worker = threading.Thread(
        target=lambda: result.append(hls_module.getVideoNativeHLS(bot, playlist_url, str(output))),
        daemon=True,
    )
    worker.start()

    deadline = time.monotonic() + 5
    while state.counts.get("/live/seg0.ts", 0) == 0 and time.monotonic() < deadline:
        time.sleep(0.01)
    while bot.stopDownload is None and time.monotonic() < deadline:
        time.sleep(0.01)

    started = time.monotonic()
    bot.stopDownload()
    worker.join(5)

    assert not worker.is_alive()
    assert time.monotonic() - started < 5
    assert result == [False]


def test_remux_timeout_keeps_the_temp_file(tmp_path, monkeypatch):
    temp = tmp_path / "show.tmp.ts"
    temp.write_bytes(b"raw")
    output = tmp_path / "show.mp4"

    sleeper = tmp_path / "sleep-ffmpeg"
    sleeper.write_text("#!/bin/sh\nsleep 30\n")
    sleeper.chmod(sleeper.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setattr(hls_module, "FFMPEG_PATH", str(sleeper))
    monkeypatch.setattr(hls_module, "FFMPEG_REMUX_TIMEOUT", 0.2)
    monkeypatch.setattr(hls_module, "SEGMENT_TIME", None)
    monkeypatch.setattr(hls_module, "CONTAINER", "mp4")
    monkeypatch.setattr(hls_module, "DEBUG", False)

    started = time.monotonic()
    assert hls_module._remux(FakeBot(), str(temp), str(output)) is False
    assert time.monotonic() - started < 10
    assert temp.exists()


def test_prune_history_bounds_the_window():
    history = OrderedDict((f"u{i}", None) for i in range(10))

    hls_module._prune_history(history, {f"u{i}" for i in range(7, 10)}, cap=2)

    assert list(history) == ["u8", "u9"]
