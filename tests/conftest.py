"""Shared test configuration.

Media integration tests use FFmpeg to generate and inspect real files. They
skip when the binaries are missing, which is convenient locally but hides
breakage in CI. Setting ``STRMNTR_REQUIRE_MEDIA_TOOLS=1`` turns that skip into a
hard preflight failure instead.
"""

from __future__ import annotations

import os
import shutil
import subprocess

import pytest


def _find_tool(env_name: str, default: str) -> str | None:
    candidate = os.environ.get(env_name) or default
    if os.path.sep in candidate:
        return candidate if os.path.isfile(candidate) and os.access(candidate, os.X_OK) else None
    return shutil.which(candidate)


def pytest_configure(config: pytest.Config) -> None:
    if os.environ.get("STRMNTR_REQUIRE_MEDIA_TOOLS") != "1":
        return

    missing = []
    for env_name, default in (("FFMPEG", "ffmpeg"), ("FFPROBE", "ffprobe")):
        tool = _find_tool(env_name, default)
        if tool is None:
            missing.append(default)
            continue
        try:
            probe = subprocess.run([tool, "-version"], capture_output=True, timeout=30)
        except OSError:
            missing.append(default)
            continue
        if probe.returncode != 0:
            missing.append(default)

    if missing:
        raise pytest.UsageError(
            "STRMNTR_REQUIRE_MEDIA_TOOLS=1 but required media tools are missing or not runnable: " + ", ".join(missing)
        )
