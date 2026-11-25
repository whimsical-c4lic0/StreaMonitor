"""Bounded subprocess execution for probing, remuxing and recovery.

``subprocess.run`` with a timeout kills and reaps the child, so a hung ffmpeg
cannot block the monitor forever.  Captured stderr is kept in a temporary file
and truncated to a bounded tail; this preserves useful diagnostics without
letting a noisy process consume memory.

On Windows, helpers are started without a console window and with Windows
Error Reporting dialogs disabled, so a crashing ffmpeg cannot block the
operator with a modal dialog.
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
from dataclasses import dataclass

__all__ = [
    "CommandResult",
    "child_process_options",
    "format_failure",
    "run_bounded",
    "stderr_tail",
]

# Windows Error Reporting shows a modal dialog and writes a crash dump when a
# child process dies of an unhandled exception.  A helper that crashes is
# already reported as a failed command, so the dialog would only block the
# operator.  The error mode is inherited by children created afterwards.
_SEM_FAILCRITICALERRORS = 0x0001
_SEM_NOGPFAULTERRORBOX = 0x0002
_SEM_NOOPENFILEERRORBOX = 0x8000
_QUIET_ERROR_MODE = _SEM_FAILCRITICALERRORS | _SEM_NOGPFAULTERRORBOX | _SEM_NOOPENFILEERRORBOX

_error_mode_set = False


def suppress_child_error_dialogs() -> None:
    """Disable Windows Error Reporting dialogs for child processes.

    The error mode is process-wide and inherited by newly created children, so
    this only has to run once before the first helper starts.  Failures are
    ignored: a missing dialog is cosmetic, while recordings must keep working.
    """
    global _error_mode_set
    if sys.platform != "win32" or _error_mode_set:
        return
    _error_mode_set = True
    try:
        import ctypes

        ctypes.windll.kernel32.SetErrorMode(_QUIET_ERROR_MODE)
    except Exception:
        pass


def _hidden_startupinfo():
    """STARTUPINFO that keeps a console helper from flashing a window."""
    startupinfo = subprocess.STARTUPINFO()
    startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startupinfo.wShowWindow = subprocess.SW_HIDE
    return startupinfo


def child_process_options() -> dict:
    """Platform options that keep a crashing helper process quiet.

    On POSIX this is empty.  On Windows it hides the helper's console window
    and stops a crash from raising a Windows Error Reporting dialog.
    """
    if sys.platform != "win32":
        return {}
    suppress_child_error_dialogs()
    return {
        "startupinfo": _hidden_startupinfo(),
        "creationflags": subprocess.CREATE_NO_WINDOW,
    }


# Enough context for ffmpeg's final error line without unbounded output.
STDERR_CAPTURE_LIMIT = 64 * 1024
STDERR_TAIL_LIMIT = 2000


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: bytes | None = None
    stderr: bytes | None = None
    timed_out: bool = False

    @property
    def ok(self) -> bool:
        return not self.timed_out and self.returncode == 0


def run_bounded(
    cmd: list[str],
    *,
    timeout: float,
    stdout: int | None = subprocess.DEVNULL,
    stderr: int | None = subprocess.PIPE,
    stdin: int | None = subprocess.DEVNULL,
    cwd: str | None = None,
) -> CommandResult:
    """Run ``cmd`` with a deadline and return a structured result.

    When ``stderr`` is ``subprocess.PIPE`` the output is written to a temporary
    file and only a bounded tail is returned, so a process that never shuts up
    cannot grow memory without limit.
    """
    capture_stderr = stderr == subprocess.PIPE
    stderr_target = tempfile.TemporaryFile() if capture_stderr else stderr
    try:
        completed = subprocess.run(
            cmd,
            timeout=timeout,
            stdout=stdout,
            stderr=stderr_target,
            stdin=stdin,
            cwd=cwd,
            **child_process_options(),
        )
    except subprocess.TimeoutExpired:
        # subprocess.run has already killed and waited for the child.
        return CommandResult(
            returncode=-9,
            stdout=None,
            stderr=_read_captured(stderr_target) if capture_stderr else None,
            timed_out=True,
        )
    else:
        return CommandResult(
            returncode=completed.returncode,
            stdout=getattr(completed, "stdout", None),
            stderr=_read_captured(stderr_target) if capture_stderr else None,
        )
    finally:
        if capture_stderr:
            try:
                stderr_target.close()  # type: ignore[union-attr]
            except OSError:
                pass


def _read_captured(stream) -> bytes:
    if stream is None or not hasattr(stream, "seek"):
        return b""
    try:
        stream.seek(0)
        data = stream.read()
    except (OSError, ValueError):
        return b""
    if isinstance(data, str):
        data = data.encode("utf-8", "replace")
    if len(data) > STDERR_CAPTURE_LIMIT:
        data = data[-STDERR_CAPTURE_LIMIT:]
    return data


def stderr_tail(data: bytes | str | None, limit: int = STDERR_TAIL_LIMIT) -> str:
    """Return a single-line, bounded tail of a process's stderr."""
    if not data:
        return ""
    if isinstance(data, bytes):
        text = data.decode("utf-8", "replace")
    else:
        text = data
    text = text.strip()
    if len(text) > limit:
        text = "..." + text[-limit:]
    return text


def format_failure(result: CommandResult, *, limit: int = STDERR_TAIL_LIMIT) -> str:
    """Describe why a bounded command failed, without dumping all of stderr."""
    if result.timed_out:
        return "timed out"
    reason = f"exit code {result.returncode}"
    tail = stderr_tail(result.stderr, limit)
    return f"{reason}: {tail}" if tail else reason
