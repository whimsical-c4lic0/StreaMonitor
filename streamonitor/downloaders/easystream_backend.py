"""Subprocess backend for the EasyStream WebSocket recorder worker.

The worker owns the WebSocket and ffmpeg; this module only knows about the
process, its private stdin pipe and the JSON-lines command/event
protocol.  Keeping the socket stack out of process isolates a crashed decoder
or a hung TLS connection from the monitor thread.
"""

from __future__ import annotations

import json
import os
import queue
import shutil
import subprocess
import threading
from collections import deque

import parameters


class EasyStreamBackendUnavailable(RuntimeError):
    """The EasyStream worker cannot be started on this host."""


def _edges_json(credentials) -> list[dict]:
    return [{"url": edge.url, "weight": edge.weight} for edge in credentials.edges]


class EasyStreamBackend:
    def __init__(self, logger=None):
        self.logger = logger
        self._events: queue.Queue = queue.Queue()
        self._stderr = deque(maxlen=40)
        self._process: subprocess.Popen | None = None
        self._stdout_thread: threading.Thread | None = None
        self._stderr_thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._secrets: set[str] = set()
        self.reported_file: str | None = None
        self.media: dict | None = None
        self.exit_code: int | None = None

    # -- discovery ---------------------------------------------------------

    @classmethod
    def python_path(cls) -> str:
        return shutil.which(parameters.EASYSTREAM_PYTHON) or parameters.EASYSTREAM_PYTHON

    @classmethod
    def ffmpeg_available(cls) -> bool:
        binary = parameters.FFMPEG_PATH
        return shutil.which(binary) is not None or os.path.isfile(binary)

    @classmethod
    def available(cls) -> bool:
        if not cls.ffmpeg_available() or not os.path.isfile(cls.worker_path()):
            return False
        try:
            # Import the actual worker, including its dependencies, using the
            # interpreter that will run it (not the monitor's interpreter).
            result = subprocess.run(
                [cls.python_path(), "-c", "import streamonitor.downloaders.easystream_worker"],
                cwd=cls.project_root(),
                env=cls.worker_env(),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=10,
            )
        except (OSError, subprocess.TimeoutExpired):
            return False
        return result.returncode == 0

    @classmethod
    def worker_env(cls) -> dict[str, str]:
        env = os.environ.copy()
        env["PYTHONPATH"] = cls.project_root() + os.pathsep + env.get("PYTHONPATH", "")
        return env

    @staticmethod
    def worker_path() -> str:
        return os.path.abspath(os.path.join(os.path.dirname(__file__), "easystream_worker.py"))

    @staticmethod
    def project_root() -> str:
        return os.path.abspath(os.path.dirname(os.path.dirname(os.path.dirname(__file__))))

    # -- lifecycle ---------------------------------------------------------

    def start(self, stream, output: str, max_duration: int = 0) -> None:
        if not self.available():
            raise EasyStreamBackendUnavailable(
                "EasyStream recorder is not available: install websockets and ffmpeg "
                f"(python={self.python_path()})"
            )
        credentials = stream.credentials
        self._secrets = {credentials.token}
        self.reported_file = None
        self.media = None
        self.exit_code = None

        config = {
            "target": stream.target.as_message(),
            "token": credentials.token,
            "edges": _edges_json(credentials),
            "pickK": credentials.pick_k,
            "expiresAt": credentials.expires_at,
            "output": output,
            "referer": stream.referer or "https://amateur.tv/",
            "userAgent": stream.user_agent or parameters.HTTP_USER_AGENT,
            "wantedResolution": parameters.WANTED_RESOLUTION,
            "resolutionPreference": parameters.WANTED_RESOLUTION_PREFERENCE,
            "maxDurationSec": max(0, int(max_duration or 0)),
        }

        cmd = [
            self.python_path(),
            "-m",
            "streamonitor.downloaders.easystream_worker",
        ]
        try:
            self._process = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
                cwd=self.project_root(),
                env=self.worker_env(),
            )
        except OSError as error:
            raise EasyStreamBackendUnavailable(f"failed to start EasyStream worker: {error}") from error

        self._stdout_thread = threading.Thread(target=self._read_stdout, name="easystream-worker-events", daemon=True)
        self._stdout_thread.start()
        self._stderr_thread = threading.Thread(target=self._read_stderr, name="easystream-worker-stderr", daemon=True)
        self._stderr_thread.start()
        try:
            self._process.stdin.write(json.dumps(config) + "\n")
            self._process.stdin.flush()
        except (OSError, ValueError) as error:
            self.close(1)
            raise EasyStreamBackendUnavailable("failed to send EasyStream worker configuration") from error

    def _read_stdout(self) -> None:
        process = self._process
        if process is None or process.stdout is None:
            return
        for raw in process.stdout:
            line = raw.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except ValueError:
                self._debug(f"recorder: {self._redact(line)}")
                continue
            if not isinstance(event, dict) or "event" not in event:
                self._debug(f"recorder: {self._redact(line)}")
                continue
            if event.get("file"):
                self.reported_file = event["file"]
            if event["event"] == "media":
                self.media = event
            self._events.put(event)
        code = process.wait()
        self.exit_code = code
        self._events.put({"event": "process_exit", "code": code})

    def _read_stderr(self) -> None:
        process = self._process
        if process is None or process.stderr is None:
            return
        for raw in process.stderr:
            line = self._redact(raw.strip())
            if line:
                self._stderr.append(line)
                self._debug(f"recorder: {line}")

    def _redact(self, text: str) -> str:
        for secret in self._secrets:
            if secret and secret in text:
                text = text.replace(secret, "<redacted>")
        return text

    def _debug(self, message: str) -> None:
        if self.logger is not None:
            self.logger.debug(message)

    # -- events and commands ----------------------------------------------

    def next_event(self, timeout: float = 0.5) -> dict | None:
        try:
            return self._events.get(timeout=timeout)
        except queue.Empty:
            return None

    @property
    def exited(self) -> bool:
        return self._process is not None and self._process.poll() is not None

    def request_stop(self) -> None:
        with self._lock:
            process = self._process
            if process is None or process.poll() is not None or process.stdin is None:
                return
            try:
                process.stdin.write("stop\n")
                process.stdin.flush()
            except (BrokenPipeError, OSError, ValueError):
                pass

    def renew(self, credentials) -> bool:
        with self._lock:
            process = self._process
            if process is None or process.poll() is not None or process.stdin is None:
                return False
            try:
                self._secrets.add(credentials.token)
                command = {
                    "command": "renew",
                    "credentials": {
                        "token": credentials.token,
                        "edges": _edges_json(credentials),
                        "pickK": credentials.pick_k,
                        "expiresAt": credentials.expires_at,
                    },
                }
                process.stdin.write(json.dumps(command) + "\n")
                process.stdin.flush()
                return True
            except (BrokenPipeError, OSError, ValueError):
                return False

    def wait(self, timeout: float) -> int | None:
        process = self._process
        if process is None:
            return self.exit_code
        try:
            return process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            return None

    def terminate(self) -> None:
        process = self._process
        if process is not None and process.poll() is None:
            process.terminate()

    def kill(self) -> None:
        process = self._process
        if process is not None and process.poll() is None:
            process.kill()

    def close(self, timeout: float = 15) -> int | None:
        """Stop gracefully, escalating to SIGTERM and SIGKILL."""
        self.request_stop()
        code = self.wait(timeout)
        if code is None:
            self.terminate()
            code = self.wait(5)
        if code is None:
            self.kill()
            code = self.wait(5)
        if self._process is not None and self._process.stdin is not None:
            try:
                self._process.stdin.close()
            except OSError:
                pass
        for thread in (self._stdout_thread, self._stderr_thread):
            if thread is not None and thread.is_alive():
                thread.join(timeout=2)
        return code

    def stderr_tail(self) -> str:
        return "\n".join(self._stderr)
