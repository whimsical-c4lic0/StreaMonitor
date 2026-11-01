"""Tests for resuming shows across downloader restarts.

A live show can be interrupted several times by network or demuxer errors.
These tests cover the part file naming, the merge that turns those parts back
into a single video, and the ffmpeg watchdog that stops a stalled recording.
"""

from __future__ import annotations

import logging
import os
import shutil
import stat
import subprocess
import time
from itertools import count

import pytest

from streamonitor import bot as bot_module
from streamonitor.bot import Bot
from streamonitor.downloaders import ffmpeg as ffmpeg_module
from streamonitor.downloaders.ffmpeg import getVideoFfmpeg

FFMPEG = os.environ.get("FFMPEG") or shutil.which("ffmpeg")
FFPROBE = os.environ.get("FFPROBE") or shutil.which("ffprobe")


class FakeBot:
    def __init__(self):
        self.username = "tester"
        self.siteslug = "cb"
        self.logger = logging.getLogger("test-recording-parts")
        self.headers = {"User-Agent": "test-agent"}
        self.cookies = {}
        self.stopDownload = None
        self.filename_extra_suffix = ""


def makeBot(tmp_path, monkeypatch):
    bot = object.__new__(Bot)
    bot.username = "tester"
    bot.siteslug = "cb"
    bot.logger = logging.getLogger("test-recording-parts")
    bot.recording_base = None
    bot.recording_parts = []
    numbers = count(1)

    def genOutFilename(create_dir=True):
        # Distinct names, unlike the second resolution timestamps of a real run.
        return str(tmp_path / f"tester-20260101-00{next(numbers):04d}.mp4")

    bot.genOutFilename = genOutFilename
    return bot


def writeVideo(path, seconds=1):
    subprocess.run(
        [FFMPEG, "-hide_banner", "-loglevel", "error", "-y",
         "-f", "lavfi", "-i", f"testsrc=size=160x120:rate=15:duration={seconds}",
         "-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}",
         "-c:v", "mpeg4", "-q:v", "5", "-c:a", "aac", "-shortest", path],
        check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)


def probeDuration(path):
    result = subprocess.run(
        [FFPROBE, "-v", "error", "-show_entries", "format=duration",
         "-of", "csv=p=0", path],
        check=True, capture_output=True, text=True)
    return float(result.stdout.strip())


def testNextRecordingFilenameReusesShowBase(tmp_path, monkeypatch):
    bot = makeBot(tmp_path, monkeypatch)
    monkeypatch.setattr(bot_module, "SEGMENT_TIME", None)

    base = bot.nextRecordingFilename()
    assert base.endswith(".mp4")
    assert bot.hasRecordingInProgress()

    second = bot.nextRecordingFilename()
    third = bot.nextRecordingFilename()
    stem, ext = os.path.splitext(base)
    assert second == f"{stem}.part1{ext}"
    assert third == f"{stem}.part2{ext}"
    assert bot.recording_parts == [base, second, third]


def testNextRecordingFilenameStartsNewShowAfterFinalize(tmp_path, monkeypatch):
    bot = makeBot(tmp_path, monkeypatch)
    monkeypatch.setattr(bot_module, "SEGMENT_TIME", None)

    first = bot.nextRecordingFilename()
    bot.finalizeRecording()
    assert not bot.hasRecordingInProgress()

    second = bot.nextRecordingFilename()
    assert second != first
    assert second.endswith(".mp4")


def testSegmentModeNeverReusesBase(tmp_path, monkeypatch):
    bot = makeBot(tmp_path, monkeypatch)
    monkeypatch.setattr(bot_module, "SEGMENT_TIME", 300)

    first = bot.nextRecordingFilename()
    second = bot.nextRecordingFilename()
    assert first != second
    assert ".part" not in second


def testFinalizeMergesPartsIntoOneFile(tmp_path, monkeypatch):
    if not FFMPEG or not FFPROBE:
        pytest.skip("ffmpeg and ffprobe are required")

    bot = makeBot(tmp_path, monkeypatch)
    monkeypatch.setattr(bot_module, "SEGMENT_TIME", None)
    monkeypatch.setattr(bot_module, "FFMPEG_PATH", FFMPEG)

    base = bot.nextRecordingFilename()
    second = bot.nextRecordingFilename()
    writeVideo(base, seconds=1)
    writeVideo(second, seconds=1)

    bot.finalizeRecording()

    assert os.path.exists(base)
    assert not os.path.exists(second)
    assert not os.path.exists(os.path.splitext(base)[0] + ".concat.txt")
    assert probeDuration(base) == pytest.approx(2.0, abs=0.5)
    assert bot.recording_parts == []
    assert not bot.hasRecordingInProgress()


def testFinalizeMergesFragmentedParts(tmp_path, monkeypatch):
    if not FFMPEG or not FFPROBE:
        pytest.skip("ffmpeg and ffprobe are required")

    bot = makeBot(tmp_path, monkeypatch)
    monkeypatch.setattr(bot_module, "SEGMENT_TIME", None)
    monkeypatch.setattr(bot_module, "FFMPEG_PATH", FFMPEG)

    base = bot.nextRecordingFilename()
    second = bot.nextRecordingFilename()
    for path in (base, second):
        subprocess.run(
            [FFMPEG, "-hide_banner", "-loglevel", "error", "-y",
             "-f", "lavfi", "-i", "testsrc=size=160x120:rate=15:duration=1",
             "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
             "-c:v", "mpeg4", "-q:v", "5", "-c:a", "aac", "-shortest",
             "-movflags", "+frag_keyframe+empty_moov", "-f", "mp4", path],
            check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)

    bot.finalizeRecording()

    assert os.path.exists(base)
    assert not os.path.exists(second)
    assert probeDuration(base) == pytest.approx(2.0, abs=0.5)


def testFinalizeKeepsDamagedPart(tmp_path, monkeypatch):
    if not FFMPEG:
        pytest.skip("ffmpeg is required")

    bot = makeBot(tmp_path, monkeypatch)
    monkeypatch.setattr(bot_module, "SEGMENT_TIME", None)
    monkeypatch.setattr(bot_module, "FFMPEG_PATH", FFMPEG)

    base = bot.nextRecordingFilename()
    second = bot.nextRecordingFilename()
    writeVideo(base, seconds=1)
    with open(second, "wb") as broken:
        broken.write(b"not a video" * 100)

    bot.finalizeRecording()

    assert os.path.getsize(base) > 0
    assert os.path.getsize(second) > 0


def testFinalizeDropsEmptyParts(tmp_path, monkeypatch):
    if not FFMPEG:
        pytest.skip("ffmpeg is required")

    bot = makeBot(tmp_path, monkeypatch)
    monkeypatch.setattr(bot_module, "SEGMENT_TIME", None)
    monkeypatch.setattr(bot_module, "FFMPEG_PATH", FFMPEG)

    base = bot.nextRecordingFilename()
    second = bot.nextRecordingFilename()
    third = bot.nextRecordingFilename()
    writeVideo(base, seconds=1)
    open(second, "wb").close()  # ffmpeg never wrote anything
    writeVideo(third, seconds=1)

    bot.finalizeRecording()

    assert os.path.exists(base)
    assert not os.path.exists(second)
    assert not os.path.exists(third)
    assert probeDuration(base) == pytest.approx(2.0, abs=0.5)


def testFinalizeRenamesSingleRemainingPart(tmp_path, monkeypatch):
    if not FFMPEG:
        pytest.skip("ffmpeg is required")

    bot = makeBot(tmp_path, monkeypatch)
    monkeypatch.setattr(bot_module, "SEGMENT_TIME", None)
    monkeypatch.setattr(bot_module, "FFMPEG_PATH", FFMPEG)

    base = bot.nextRecordingFilename()
    second = bot.nextRecordingFilename()
    open(base, "wb").close()  # the first attempt failed before writing
    writeVideo(second, seconds=1)

    bot.finalizeRecording()

    assert os.path.exists(base)
    assert os.path.getsize(base) > 0
    assert not os.path.exists(second)


def makeFakeFfmpeg(tmp_path, body):
    path = tmp_path / "fake-ffmpeg"
    path.write_text("#!/bin/sh\n" + body + "\n")
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return str(path)


def testWatchdogStopsStalledRecording(tmp_path, monkeypatch):
    output = tmp_path / "tester-20260101-000000.mp4"
    fake = makeFakeFfmpeg(tmp_path, f"echo x > '{output}'; sleep 30")
    monkeypatch.setattr(ffmpeg_module, "FFMPEG_PATH", fake)
    monkeypatch.setattr(ffmpeg_module, "FFMPEG_READRATE", 0)
    monkeypatch.setattr(ffmpeg_module, "FFMPEG_STALL_TIMEOUT", 2)

    fbot = FakeBot()
    started = time.monotonic()
    result = getVideoFfmpeg(fbot, "https://example.com/live.m3u8", str(output))
    elapsed = time.monotonic() - started

    assert result is False
    assert elapsed < 25


def testWatchdogLeavesGrowingRecordingAlone(tmp_path, monkeypatch):
    output = tmp_path / "tester-20260101-000000.mp4"
    fake = makeFakeFfmpeg(
        tmp_path,
        f"for i in 1 2 3 4 5 6 7 8; do echo x >> '{output}'; sleep 0.3; done")
    monkeypatch.setattr(ffmpeg_module, "FFMPEG_PATH", fake)
    monkeypatch.setattr(ffmpeg_module, "FFMPEG_READRATE", 0)
    monkeypatch.setattr(ffmpeg_module, "FFMPEG_STALL_TIMEOUT", 2)

    fbot = FakeBot()
    result = getVideoFfmpeg(fbot, "https://example.com/live.m3u8", str(output))

    assert result is True
    assert output.stat().st_size > 0


def testReadrateIsOmittedWhenDisabled(tmp_path, monkeypatch):
    args_file = tmp_path / "args"
    fake = makeFakeFfmpeg(tmp_path, f"printf '%s\\n' \"$@\" > '{args_file}'")
    monkeypatch.setattr(ffmpeg_module, "FFMPEG_PATH", fake)
    monkeypatch.setattr(ffmpeg_module, "FFMPEG_STALL_TIMEOUT", 0)
    monkeypatch.setattr(ffmpeg_module, "FFMPEG_READRATE", 0)

    fbot = FakeBot()
    getVideoFfmpeg(fbot, "https://example.com/live.m3u8",
                   str(tmp_path / "tester-20260101-000000.mp4"))

    assert "-readrate" not in args_file.read_text().splitlines()


def testMp4OutputIsFragmentedSoPartsSurviveKills(tmp_path, monkeypatch):
    args_file = tmp_path / "args"
    fake = makeFakeFfmpeg(tmp_path, f"printf '%s\\n' \"$@\" > '{args_file}'")
    monkeypatch.setattr(ffmpeg_module, "FFMPEG_PATH", fake)
    monkeypatch.setattr(ffmpeg_module, "FFMPEG_STALL_TIMEOUT", 0)
    monkeypatch.setattr(ffmpeg_module, "FFMPEG_READRATE", 0)
    monkeypatch.setattr(ffmpeg_module, "CONTAINER", "mp4")

    fbot = FakeBot()
    getVideoFfmpeg(fbot, "https://example.com/live.m3u8",
                   str(tmp_path / "tester-20260101-000000.mp4"))

    args = args_file.read_text().splitlines()
    assert args[args.index("-movflags") + 1] == "+frag_keyframe+empty_moov"


def testOtherContainersAreNotFragmented(tmp_path, monkeypatch):
    args_file = tmp_path / "args"
    fake = makeFakeFfmpeg(tmp_path, f"printf '%s\\n' \"$@\" > '{args_file}'")
    monkeypatch.setattr(ffmpeg_module, "FFMPEG_PATH", fake)
    monkeypatch.setattr(ffmpeg_module, "FFMPEG_STALL_TIMEOUT", 0)
    monkeypatch.setattr(ffmpeg_module, "FFMPEG_READRATE", 0)
    monkeypatch.setattr(ffmpeg_module, "CONTAINER", "mkv")

    fbot = FakeBot()
    getVideoFfmpeg(fbot, "https://example.com/live.m3u8",
                   str(tmp_path / "tester-20260101-000000.mkv"))

    assert "-movflags" not in args_file.read_text().splitlines()


def testReadrateIsPassedWhenConfigured(tmp_path, monkeypatch):
    args_file = tmp_path / "args"
    fake = makeFakeFfmpeg(tmp_path, f"printf '%s\\n' \"$@\" > '{args_file}'")
    monkeypatch.setattr(ffmpeg_module, "FFMPEG_PATH", fake)
    monkeypatch.setattr(ffmpeg_module, "FFMPEG_STALL_TIMEOUT", 0)
    monkeypatch.setattr(ffmpeg_module, "FFMPEG_READRATE", 1.3)

    fbot = FakeBot()
    getVideoFfmpeg(fbot, "https://example.com/live.m3u8",
                   str(tmp_path / "tester-20260101-000000.mp4"))

    args = args_file.read_text().splitlines()
    assert args[args.index("-readrate") + 1] == "1.3"
