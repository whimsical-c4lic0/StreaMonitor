"""Tests for bounded transport and subprocess contracts.

These tests use fake sessions and tiny shell scripts, so they do not contact a
network or rely on a real ffmpeg build.
"""

from __future__ import annotations

import logging
import stat
import threading
import time
from dataclasses import dataclass

import pytest

import streamonitor.bot as bot_module
from parameters import CONTAINER
from streamonitor.bot import Bot
from streamonitor.downloaders.process import format_failure, run_bounded, stderr_tail
from streamonitor.downloaders.transport import (
    RetryPolicy,
    TransferCancelled,
    TransportError,
    fetch,
    redact_headers,
    redact_url,
    retry_after_seconds,
)

# -- transport --------------------------------------------------------------


@dataclass
class FakeResponse:
    status_code: int = 200
    content: bytes = b""
    headers: dict | None = None
    text: str = ""


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response


def fast_policy(**overrides):
    values = dict(attempts=3, timeout=(1, 2), base_delay=0.0, max_delay=0.0, jitter=0.0)
    values.update(overrides)
    return RetryPolicy(**values)


def test_fetch_returns_a_complete_body_and_passes_the_timeout():
    session = FakeSession([FakeResponse(content=b"media")])

    body = fetch(session, "https://example.test/segment.ts", policy=fast_policy())

    assert body == b"media"
    assert session.calls[0][1]["timeout"] == (1, 2)


def test_fetch_retries_transient_statuses():
    session = FakeSession([FakeResponse(status_code=503), FakeResponse(status_code=200, content=b"ok")])

    assert fetch(session, "https://example.test/x", policy=fast_policy()) == b"ok"
    assert len(session.calls) == 2


def test_fetch_does_not_retry_permanent_statuses():
    session = FakeSession([FakeResponse(status_code=403)])

    with pytest.raises(TransportError) as excinfo:
        fetch(session, "https://example.test/x", policy=fast_policy())

    assert excinfo.value.status == 403
    assert excinfo.value.retryable is False
    assert len(session.calls) == 1


def test_fetch_retries_connection_errors():
    session = FakeSession([ConnectionError("reset"), FakeResponse(content=b"ok")])

    assert fetch(session, "https://example.test/x", policy=fast_policy()) == b"ok"


def test_fetch_retries_a_truncated_body():
    truncated = FakeResponse(
        content=b"abc",
        headers={"Content-Length": "10"},
    )
    good = FakeResponse(content=b"abcdefghij", headers={"Content-Length": "10"})
    session = FakeSession([truncated, good])

    assert fetch(session, "https://example.test/x", policy=fast_policy()) == b"abcdefghij"
    assert len(session.calls) == 2


def test_fetch_honours_cancellation():
    cancel = threading.Event()
    cancel.set()

    with pytest.raises(TransferCancelled):
        fetch(FakeSession([]), "https://example.test/x", cancel=cancel)


def test_retry_after_parses_seconds_and_http_dates():
    assert retry_after_seconds(FakeResponse(headers={"Retry-After": "12"})) == 12
    # A date in the past clamps to zero.
    assert retry_after_seconds(FakeResponse(headers={"Retry-After": "Wed, 21 Oct 2015 07:28:00 GMT"})) == 0
    assert retry_after_seconds(FakeResponse(headers={"Retry-After": "bogus"})) is None


def test_redact_url_masks_credentials_and_signed_parameters():
    url = "https://user:pass@edge.example/live/index.m3u8?token=abc&psch=v1&keep=1#frag"

    redacted = redact_url(url)

    assert "pass" not in redacted
    assert "token=abc" not in redacted
    assert "psch=v1" not in redacted
    assert "keep=1" in redacted
    assert redacted.startswith("https://***@edge.example/")
    assert "#" not in redacted


def test_redact_headers_masks_authorization_and_cookies():
    redacted = redact_headers({"User-Agent": "ua", "Authorization": "Bearer secret", "Cookie": "sid=1"})

    assert redacted["User-Agent"] == "ua"
    assert redacted["Authorization"] == "***"
    assert redacted["Cookie"] == "***"


# -- bounded subprocesses ---------------------------------------------------


def make_script(tmp_path, body):
    path = tmp_path / "script"
    path.write_text("#!/bin/sh\n" + body + "\n")
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return str(path)


def test_run_bounded_reaps_a_timed_out_process_and_bounds_stderr(tmp_path):
    script = make_script(tmp_path, "echo 'diagnostic line' >&2\nsleep 30")

    started = time.monotonic()
    result = run_bounded([script], timeout=0.2)
    elapsed = time.monotonic() - started

    assert result.timed_out is True
    assert elapsed < 10
    assert "diagnostic line" in stderr_tail(result.stderr)
    assert "timed out" in format_failure(result)


def test_stderr_tail_is_bounded():
    tail = stderr_tail(b"x" * 5000, limit=20)

    assert len(tail) <= 23
    assert tail.startswith("...")


def make_bot():
    bot = object.__new__(Bot)
    bot.logger = logging.getLogger("test-bounded-bot")
    bot.recording_base = None
    bot.recording_parts = []
    bot.log = lambda message: bot.logger.info(message)
    return bot


def test_probe_timeout_returns_false_quickly(tmp_path, monkeypatch):
    script = make_script(tmp_path, "sleep 30")
    monkeypatch.setattr(bot_module, "FFMPEG_PATH", script)
    monkeypatch.setattr(bot_module, "FFMPEG_PROBE_TIMEOUT", 0.2)
    bot = make_bot()

    started = time.monotonic()
    assert bot._isReadablePart(str(tmp_path / "part.mp4")) is False
    assert time.monotonic() - started < 10


def test_merge_timeout_keeps_all_parts(tmp_path, monkeypatch):
    script = make_script(tmp_path, "sleep 30")
    monkeypatch.setattr(bot_module, "FFMPEG_PATH", script)
    monkeypatch.setattr(bot_module, "FFMPEG_REMUX_TIMEOUT", 0.2)
    bot = make_bot()
    monkeypatch.setattr(bot, "_isReadablePart", lambda path: True)

    base = tmp_path / "show.mp4"
    part = tmp_path / "show.part1.mp4"
    base.write_bytes(b"a" * 100)
    part.write_bytes(b"b" * 100)

    started = time.monotonic()
    bot._consolidateParts(str(base), [str(base), str(part)])

    assert time.monotonic() - started < 10
    assert base.exists() and part.exists()
    assert not (tmp_path / "show.concat.txt").exists()
    assert not (tmp_path / "show.merged.mp4").exists()


def test_recovery_timeout_keeps_the_temp_file(tmp_path, monkeypatch):
    script = make_script(tmp_path, "sleep 30")
    monkeypatch.setattr(bot_module, "FFMPEG_PATH", script)
    monkeypatch.setattr(bot_module, "FFMPEG_REMUX_TIMEOUT", 0.2)
    monkeypatch.setattr(bot_module, "CONTAINER", CONTAINER)
    bot = make_bot()
    monkeypatch.setattr(bot, "_hasSegmentFiles", lambda path: False)

    temp = tmp_path / "show.tmp.ts"
    temp.write_bytes(b"raw")

    started = time.monotonic()
    assert bot._recoverTempRecording(str(temp)) is False
    assert time.monotonic() - started < 10
    assert temp.exists()
