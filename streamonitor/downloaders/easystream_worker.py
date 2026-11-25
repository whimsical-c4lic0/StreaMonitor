"""WebSocket + ffmpeg worker for AmateurTV EasyStream.

The worker runs as a separate process.  It subscribes to one EasyStream
target, receives fragmented MP4 over the socket, obtains a ClearKey license
when the init segment is encrypted, and feeds the decrypted-by-ffmpeg bytes
into a single temporary MP4.  The parent process owns part naming, token
refresh callbacks and final remuxing.

Commands arrive on stdin as JSON lines (or the word ``stop``); events leave on
stdout as JSON lines.  Media never crosses the JSON channel.
"""

from __future__ import annotations

import asyncio
import json
import os
import queue
import random
import signal
import sys
import threading
import time
from collections import deque
from urllib.parse import urlparse

import parameters
from streamonitor.downloaders.easystream_protocol import (
    EasyStreamProtocolError,
    LicenseError,
    classify_media,
    clearkey_challenge,
    decode_message,
    parse_clearkey_license,
    parse_fragment,
    parse_init,
    resolution_dimension,
    select_layer,
    validate_server_message,
)

try:  # websockets >= 13
    from websockets.asyncio.client import connect
    from websockets.exceptions import ConnectionClosed
except ImportError:  # pragma: no cover - older websockets releases
    from websockets.client import connect  # type: ignore[no-redef]
    from websockets.exceptions import ConnectionClosed  # type: ignore[no-redef]


class _SessionEnd(Exception):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class EasyStreamSession:
    def __init__(self, config: dict):
        self.config = config
        self.target: dict = dict(config["target"])
        self.token: str = config["token"]
        self.edges: list[dict] = list(config.get("edges") or [])
        self.pick_k: int = int(config.get("pickK") or len(self.edges) or 1)
        self.expires_at = config.get("expiresAt")
        self.output: str = config["output"]
        self.referer: str = config.get("referer") or "https://amateur.tv/"
        self.user_agent: str = config.get("userAgent") or parameters.HTTP_USER_AGENT
        self.wanted_resolution = int(config.get("wantedResolution") or parameters.WANTED_RESOLUTION)
        self.preference = str(config.get("resolutionPreference") or parameters.WANTED_RESOLUTION_PREFERENCE)
        self.max_duration = int(config.get("maxDurationSec") or 0)

        self.ws = None
        self.stop_requested = False
        self.reason = ""
        self.started_at = time.monotonic()
        self.last_message_at = self.started_at
        self.last_ping = 0.0
        self.ping_id = 0
        self.refresh_requested = False

        self.subscribed = False
        self.stream_id: str | None = None
        self.layers: list[int] = []
        self.resolutions: list = []
        self.current_layer: int | None = None
        self.selected_layer: int | None = None
        # Set while the worker waits for the edge to acknowledge a layer
        # ``select``.  Media arriving in the meantime still belongs to the
        # previous layer, so it is buffered instead of recorded.
        self.layer_pending = False
        self.layer_pending_since = 0.0
        self.layer_confirmed_at: float | None = None
        self.drm_mode: str | None = None
        self.media_mode: str | None = None
        self.access_allowed = False

        self.init_info = None
        self.init_fingerprint = None
        self.init_bytes: bytes | None = None
        self.pre_init: list[bytes] = []
        self.pre_init_bytes = 0
        self.buffer: list[bytes] = []
        self.buffer_bytes = 0
        self.buffer_started: float | None = None
        self.buffered_media_data = False
        self.first_media_wall: float | None = None
        self.media_bytes = 0
        self.media_fragments = 0
        self.media_emitted = False
        self.last_fragment_start: dict[int, float] = {}

        self.pending_kid: str | None = None
        self.pending_license_deadline = 0.0
        self.next_license_send = 0.0
        self.license_attempts = 0
        self.license_denied = False
        self.key_hex: str | None = None

        self.ffmpeg = None
        self.ffmpeg_exit: int | None = None
        self.ffmpeg_failed = False
        self.write_queue: asyncio.Queue = asyncio.Queue()
        self.queued_bytes = 0
        self.writer_task: asyncio.Task | None = None
        self.stderr_task: asyncio.Task | None = None
        self.ffmpeg_wait_task: asyncio.Task | None = None
        self.stderr_lines: deque[str] = deque(maxlen=40)
        self.stdin_closed = False

        self._commands: queue.Queue = queue.Queue()
        self._shutting_down = False
        self._stopped_emitted = False
        self._secrets: set[str] = {self.token}

    # -- logging and redaction -------------------------------------------

    def _redact(self, value):
        if isinstance(value, str):
            for secret in self._secrets:
                if secret and secret in value:
                    value = value.replace(secret, "<redacted>")
            return value
        if isinstance(value, dict):
            return {key: self._redact(item) for key, item in value.items()}
        if isinstance(value, list):
            return [self._redact(item) for item in value]
        return value

    def _emit(self, event: str, **fields) -> None:
        payload = {"event": event}
        payload.update(self._redact(fields))
        try:
            print(json.dumps(payload, ensure_ascii=False), flush=True)
        except (OSError, ValueError):
            pass

    def _warn(self, message: str) -> None:
        self._emit("warning", message=self._redact(message))

    def _debug(self, message: str) -> None:
        if parameters.DEBUG:
            self._emit("debug", message=self._redact(message))

    # -- lifecycle -------------------------------------------------------

    def request_stop(self, reason: str = "requested") -> None:
        if not self.stop_requested:
            self.reason = reason
            self.stop_requested = True

    def _end(self, reason: str) -> None:
        self.request_stop(reason)
        raise _SessionEnd(reason)

    def _start_command_reader(self) -> None:
        def read_stdin():
            try:
                for line in sys.stdin:
                    self._commands.put(line.strip())
            except (OSError, ValueError):
                pass
            self._commands.put("__eof__")

        threading.Thread(target=read_stdin, name="easystream-commands", daemon=True).start()

    async def run(self) -> None:
        self._start_command_reader()
        os.makedirs(os.path.dirname(os.path.abspath(self.output)), exist_ok=True)
        reason = ""
        try:
            await self._connect()
            await self._send_json({"type": "subscribe", **self.target, "token": self.token})
            await self._main_loop()
        except _SessionEnd as end:
            reason = end.reason
        except asyncio.CancelledError:
            reason = self.reason or "cancelled"
        except Exception as error:  # noqa: BLE001 - report and shut down cleanly
            reason = "internal"
            self._emit("error", code="internal", message=f"{type(error).__name__}: {error}")
        finally:
            await self._shutdown(reason or self.reason or "stopped")

    async def _connect(self) -> None:
        if not self.edges:
            self._end("connect_failed")
        for edge in self._ordered_edges():
            uri = edge.get("url")
            if not isinstance(uri, str):
                continue
            headers = {
                "Origin": self._origin(),
                "Referer": self.referer,
                "User-Agent": self.user_agent,
            }
            try:
                self.ws = await self._open(uri, headers)
            except Exception as error:  # noqa: BLE001 - try the next edge
                self._warn(f"EasyStream edge connection failed: {type(error).__name__}")
                continue
            self._debug("EasyStream edge connected")
            self.last_message_at = time.monotonic()
            return
        self._end("connect_failed")

    def _origin(self) -> str:
        try:
            parsed = urlparse(self.referer)
            if parsed.scheme and parsed.netloc:
                return f"{parsed.scheme}://{parsed.netloc}"
        except ValueError:
            pass
        return "https://amateur.tv"

    async def _open(self, uri: str, headers: dict):
        kwargs = {
            "open_timeout": parameters.EASYSTREAM_CONNECT_TIMEOUT,
            "close_timeout": 5,
            "ping_interval": None,
            "max_size": parameters.EASYSTREAM_MAX_MESSAGE_BYTES,
        }
        try:
            return await connect(uri, additional_headers=headers, **kwargs)
        except TypeError:
            return await connect(uri, extra_headers=headers, **kwargs)

    def _ordered_edges(self) -> list[dict]:
        remaining = [edge for edge in self.edges if isinstance(edge, dict)]
        order: list[dict] = []
        while remaining:
            count = max(1, min(self.pick_k, len(remaining)))
            candidates = remaining[:count]
            weights = [max(1e-9, float(edge.get("weight") or 0.0)) for edge in candidates]
            target = random.random() * sum(weights)
            selected = candidates[-1]
            for edge, weight in zip(candidates, weights):
                target -= weight
                if target <= 0:
                    selected = edge
                    break
            order.append(selected)
            remaining.remove(selected)
        return order

    async def _main_loop(self) -> None:
        while True:
            await self._drain_commands()
            if self.stop_requested:
                raise _SessionEnd(self.reason or "requested")
            try:
                raw = await asyncio.wait_for(self.ws.recv(), timeout=0.5)
            except asyncio.TimeoutError:
                raw = None
            except ConnectionClosed as error:
                self._end(self._close_reason(error))
            now = time.monotonic()
            if raw is not None:
                self.last_message_at = now
                await self._handle_message(raw)
            await self._check_deadlines(now)

    @staticmethod
    def _close_reason(error: ConnectionClosed) -> str:
        code = getattr(error, "code", None)
        if code == 4401:
            return "unauthorized"
        if code in (4404, 4410):
            return "ended"
        if code == 4503:
            return "edge_drain"
        return "transport"

    async def _drain_commands(self) -> None:
        while True:
            try:
                line = self._commands.get_nowait()
            except queue.Empty:
                return
            if line in ("__eof__", "stop", ""):
                if line == "__eof__" and not self.stop_requested:
                    self.request_stop("stop")
                elif line == "stop":
                    self.request_stop("stop")
                continue
            try:
                command = json.loads(line)
            except ValueError:
                continue
            if not isinstance(command, dict):
                continue
            if command.get("command") == "stop":
                self.request_stop("stop")
            elif command.get("command") == "renew":
                await self._renew(command.get("credentials") or {})

    async def _renew(self, credentials: dict) -> None:
        token = credentials.get("token")
        if not isinstance(token, str) or not token:
            return
        self.token = token
        self._secrets.add(token)
        expiry = credentials.get("expiresAt")
        if isinstance(expiry, (int, float)) and not isinstance(expiry, bool):
            self.expires_at = float(expiry)
        edges = credentials.get("edges")
        if isinstance(edges, list) and edges:
            self.edges = [edge for edge in edges if isinstance(edge, dict)]
        pick = credentials.get("pickK")
        if isinstance(pick, int) and not isinstance(pick, bool) and pick > 0:
            self.pick_k = pick
        self.refresh_requested = False
        await self._send_json({"type": "token", **self.target, "token": token})

    async def _send_json(self, message: dict) -> None:
        if self.ws is None:
            raise _SessionEnd("transport")
        try:
            await self.ws.send(json.dumps(message, separators=(",", ":")))
        except ConnectionClosed:
            self._end("transport")
        except (OSError, ValueError):
            self._end("transport")

    # -- incoming messages ----------------------------------------------

    async def _handle_message(self, raw) -> None:
        if isinstance(raw, (bytes, bytearray)):
            await self._handle_binary(bytes(raw))
            return
        try:
            message = decode_message(raw)
        except EasyStreamProtocolError as error:
            self._warn(str(error))
            return
        try:
            validate_server_message(message)
        except EasyStreamProtocolError as error:
            self._warn(f"EasyStream protocol error: {error}")
            if message.get("type") in ("subscribed", "access", "license", "error"):
                self._end("invalid_protocol")
            return
        await self._dispatch(message)

    async def _dispatch(self, message: dict) -> None:
        kind = message["type"]
        if kind == "subscribed":
            await self._on_subscribed(message)
        elif kind == "layer":
            self.current_layer = message.get("layer")
            self.selected_layer = self.current_layer
            self._emit("layer", layer=self.current_layer)
            if self.layer_pending and self.layer_confirmed_at is None:
                # The new layer's init segment follows this message; wait
                # briefly for it instead of recording the old init.
                self.layer_confirmed_at = time.monotonic()
        elif kind == "access":
            self.media_mode = message.get("media", self.media_mode)
            self._emit("access", access=message.get("access"), media=self.media_mode, reason=message.get("reason"))
            if message.get("access") != "public" or self.media_mode in ("none", "audio"):
                self._end("access_denied")
            if not self.access_allowed:
                self.access_allowed = True
                # Media that arrived before the authorization event stays
                # buffered until the edge confirms public access.
                if self.pending_kid is None:
                    await self._flush_buffered_media()
            if self.pending_kid is not None and self.license_denied:
                # The SDK waits for a fresh access event before retrying a
                # denied license; this is that event.
                self.license_denied = False
                self.next_license_send = time.monotonic()
        elif kind == "license":
            await self._on_license(message)
        elif kind == "error":
            await self._on_error(message)
        elif kind == "ping":
            await self._send_json({"type": "pong", "id": message.get("id")})
        elif kind == "reconnect":
            self._end("edge_drain" if message.get("reason") == "drain" else "transport")
        elif kind == "end":
            self._end("ended")
        elif kind == "revoked":
            self._end("revoked")
        elif kind == "restricted":
            self._end("restricted")
        elif kind == "layers":
            self.layers = [int(layer) for layer in message["layers"]]
            resolutions = message.get("resolutions")
            # A layers event without resolutions invalidates the old mapping.
            self.resolutions = resolutions if isinstance(resolutions, list) else []
            choice = select_layer(
                self.layers,
                self.resolutions,
                self.wanted_resolution,
                self.preference,
                current=self.current_layer,
            )
            if choice is not None and choice.layer != self.current_layer:
                self._begin_layer_switch(choice.layer)
                await self._send_json({"type": "select", "stream": self.stream_id, "layer": choice.layer})
        elif kind == "warning":
            self._warn("EasyStream edge warning")
        # pong, gap, bandwidth, input_buffer, heartbeat, alive, certificate and
        # unknown optional events are intentionally ignored.

    def _begin_layer_switch(self, layer: int) -> None:
        """Defer recording until the edge has applied a layer ``select``.

        The edge keeps sending the current layer's init and fragments until it
        processes the request.  Starting ffmpeg on that init would make the
        switch look like an ``init_changed`` restart, turning one recording
        into a series of sub-second parts.
        """
        self.selected_layer = layer
        self.layer_pending = True
        self.layer_pending_since = time.monotonic()
        self.layer_confirmed_at = None

    def _reset_init_state(self) -> None:
        """Forget an init segment and everything buffered for it."""
        self.init_info = None
        self.init_fingerprint = None
        self.init_bytes = None
        self.pre_init = []
        self.pre_init_bytes = 0
        self.buffer = []
        self.buffer_bytes = 0
        self.buffer_started = None
        self.buffered_media_data = False
        self.pending_kid = None
        self.key_hex = None
        self.last_fragment_start = {}

    def _store_pending_init(self, info, data: bytes, fingerprint: tuple) -> None:
        self.init_info = info
        self.init_fingerprint = fingerprint
        self.init_bytes = data
        self._buffer(data)
        if info.has_media:
            self.buffered_media_data = True
        if info.kids:
            if len(info.kids) > 1:
                self._end("multiple_keys")
            self._begin_license(info.kids[0])

    def _init_is_current_layer(self, info) -> bool:
        """Whether ``info`` still describes the layer being left behind.

        Unknown resolutions count as "no", so an edge that does not publish
        them keeps the old accept-the-first-init behaviour.
        """
        if not self.layers or not self.resolutions or self.current_layer not in self.layers:
            return False
        index = self.layers.index(self.current_layer)
        if index >= len(self.resolutions):
            return False
        expected = resolution_dimension(self.resolutions[index])
        video = info.video
        if expected is None or video is None or video.width is None or video.height is None:
            return False
        return min(video.width, video.height) == expected

    async def _on_subscribed(self, message: dict) -> None:
        self.subscribed = True
        self.stream_id = message["stream"]
        self.layers = [int(layer) for layer in message["layers"]]
        resolutions = message.get("resolutions")
        self.resolutions = resolutions if isinstance(resolutions, list) else []
        self.current_layer = message.get("layer")
        self.drm_mode = message.get("drm")
        choice = select_layer(
            self.layers,
            self.resolutions,
            self.wanted_resolution,
            self.preference,
            current=self.current_layer,
        )
        if choice is not None:
            self.selected_layer = choice.layer
            if self.current_layer != choice.layer:
                self._begin_layer_switch(choice.layer)
                await self._send_json({"type": "select", "stream": self.stream_id, "layer": choice.layer})
            self._emit(
                "subscribed",
                stream=self.stream_id,
                layer=choice.layer,
                layers=self.layers,
                resolution=choice.label(),
                drm=self.drm_mode,
            )
        else:
            self.selected_layer = self.current_layer
            self._emit(
                "subscribed",
                stream=self.stream_id,
                layer=self.current_layer,
                layers=self.layers,
                resolution=None,
                drm=self.drm_mode,
            )
        await self._send_json({"type": "want", "stream": self.stream_id, "media": "all"})

    async def _on_error(self, message: dict) -> None:
        code = str(message.get("code") or "")
        text = str(message.get("message") or "")
        if code in ("invalid_token", "wrong_viewer"):
            self._emit("refresh_needed", reason=code)
            self.refresh_requested = True
            return
        if self.pending_kid is not None and code in (
            "license_denied",
            "license_in_progress",
            "unknown_key",
            "cdm_revoked",
            "license_unavailable",
            "license_rate_limited",
            "drm_not_enabled",
            "drm_unsupported",
        ):
            if code == "license_denied":
                self.license_denied = True
                self.next_license_send = float("inf")
                self._warn("EasyStream license was denied; waiting for access")
            elif code == "license_in_progress":
                self.next_license_send = time.monotonic() + 0.5
            elif code in ("unknown_key", "cdm_revoked"):
                self._end("license_revoked" if code == "cdm_revoked" else "license_unknown_key")
            elif code in ("drm_not_enabled", "drm_unsupported"):
                self._end("drm_unsupported")
            else:
                self.license_attempts = max(self.license_attempts, 1)
                self.next_license_send = time.monotonic() + min(5.0, 0.25 * 2**self.license_attempts)
            return
        if code in ("unknown_layer", "layer_unavailable"):
            self._warn(f"EasyStream layer selection failed: {code}")
            if self.layer_pending:
                # Record the layer the edge will keep sending instead of
                # waiting out the select timeout.
                self.layer_pending = False
                self.layer_confirmed_at = None
                await self._flush_buffered_media()
            return
        if code in ("unsupported_codec", "drm_unsupported"):
            self._end("unsupported")
        if code in ("stream_unavailable", "not_subscribed", "superseded"):
            self._end("superseded")
        if code == "bad_message":
            self._end("invalid_protocol")
        self._warn(f"EasyStream edge error: {code} {text}".strip())

    async def _on_license(self, message: dict) -> None:
        kid = str(message.get("kid") or "")
        if not self.pending_kid:
            self._warn("EasyStream license arrived without a pending request")
            return
        if kid.lower() != self.pending_kid.lower():
            self._end("license_invalid")
        try:
            key = parse_clearkey_license(message["payload"], self.pending_kid)
        except LicenseError:
            self._end("license_invalid")
            return
        self.key_hex = key.hex()
        self._secrets.add(self.key_hex)
        self.pending_kid = None
        self._emit("license", kid=kid)
        await self._flush_buffered_media()

    # -- binary media ----------------------------------------------------

    async def _handle_binary(self, data: bytes) -> None:
        if len(data) > parameters.EASYSTREAM_MAX_MESSAGE_BYTES:
            self._end("oversized_media")
        try:
            kind = classify_media(data)
        except EasyStreamProtocolError as error:
            if self.init_info is None:
                self._end("invalid_init")
            self._warn(f"EasyStream binary message was malformed: {error}")
            return
        if kind == "init":
            await self._on_init(data)
        elif kind == "media":
            await self._on_media(data)
        elif self.init_info is None:
            self._buffer_pre_init(data)
        else:
            self._warn("EasyStream sent an unrecognized binary message")

    def _buffer_pre_init(self, data: bytes) -> None:
        self.pre_init.append(data)
        self.pre_init_bytes += len(data)
        if self.pre_init_bytes > parameters.EASYSTREAM_BUFFER_BYTES:
            self._end("media_buffer_overflow")

    async def _on_init(self, data: bytes) -> None:
        try:
            info = parse_init(data)
        except EasyStreamProtocolError:
            self._end("invalid_init")
            return
        if not self._init_supported(info):
            self._end("drm_unsupported")
        fingerprint = info.fingerprint
        if self.layer_pending:
            confirmed = self.layer_confirmed_at is not None
            if not confirmed and self.init_fingerprint is None:
                # First init after the select.  If it is already at another
                # resolution than the layer being left, the edge applied the
                # select before sending media and there is nothing to wait for.
                if self._init_is_current_layer(info):
                    self._store_pending_init(info, data, fingerprint)
                    return
                self.layer_pending = False
            elif not confirmed:
                if fingerprint == self.init_fingerprint:
                    return
                # Keep only the newest interim init; fragments that belong to
                # an older one are dropped with it.
                self._reset_init_state()
                if self._init_is_current_layer(info):
                    self._store_pending_init(info, data, fingerprint)
                    return
                self.layer_pending = False
            else:
                if self.init_fingerprint is not None and fingerprint == self.init_fingerprint:
                    # The acknowledged layer kept the same init, so the
                    # buffered data is already at the wanted resolution.
                    self.layer_pending = False
                    self.layer_confirmed_at = None
                    await self._flush_buffered_media()
                    return
                if self.init_fingerprint is not None:
                    # The selected layer's init arrived: drop the interim
                    # layer's init/fragments instead of finalizing a part.
                    self._reset_init_state()
                self.layer_pending = False
            self.layer_confirmed_at = None
        if self.init_fingerprint is None:
            self.init_info = info
            self.init_fingerprint = fingerprint
            self.init_bytes = data
            if info.kids:
                if len(info.kids) > 1:
                    self._end("multiple_keys")
                self._begin_license(info.kids[0])
                self._buffer(data)
                if info.has_media and not self.access_allowed:
                    self.buffered_media_data = True
                elif info.has_media:
                    self._note_media(info)
            elif self.access_allowed:
                await self._start_ffmpeg(None)
                self._feed(data)
                if info.has_media:
                    self._note_media(info)
            else:
                # Do not feed even a clear init (which may be a complete MP4)
                # before the edge authorizes this viewer.
                self._buffer(data)
                if info.has_media:
                    self.buffered_media_data = True
            pending, self.pre_init = self.pre_init, []
            self.pre_init_bytes = 0
            for item in pending:
                await self._on_media(item)
            return
        if fingerprint == self.init_fingerprint:
            if self.init_fingerprint is not None and info.has_media and self.media_bytes == 0:
                # A complete encrypted MP4 (used by synthetic tests and some
                # edge implementations) arrives as one message.
                if info.kids:
                    self._begin_license(info.kids[0])
                    self._buffer(data)
                elif self.access_allowed:
                    await self._start_ffmpeg(None)
                    self._feed(data)
                else:
                    self._buffer(data)
                if self.access_allowed:
                    self._note_media(info)
                else:
                    self.buffered_media_data = True
            return
        if self.media_bytes == 0 and self.ffmpeg is None and not self.buffered_media_data:
            # The stream changed before the first fragment: replace the init
            # and any buffered license state rather than starting a bad part.
            self.init_info = info
            self.init_fingerprint = fingerprint
            self.init_bytes = data
            self.pending_kid = None
            self.buffer = []
            self.buffer_bytes = 0
            self.buffer_started = None
            self.buffered_media_data = False
            if info.kids:
                if len(info.kids) > 1:
                    self._end("multiple_keys")
                self._begin_license(info.kids[0])
                self._buffer(data)
            elif self.access_allowed:
                await self._start_ffmpeg(None)
                self._feed(data)
            else:
                self._buffer(data)
                if info.has_media:
                    self.buffered_media_data = True
            return
        self._end("init_changed")

    @staticmethod
    def _init_supported(info) -> bool:
        if any(track.encrypted and not track.kid for track in info.tracks):
            return False
        return not any(track.encrypted and track.scheme not in ("cenc", "cbcs") for track in info.tracks)

    async def _on_media(self, data: bytes) -> None:
        if self.init_info is None:
            self._buffer_pre_init(data)
            return
        if self.layer_pending:
            # Fragments before the layer acknowledgement are not part of the
            # recording; buffer them until the switch resolves.
            self._buffer(data)
            self.buffered_media_data = True
            return
        try:
            fragments = parse_fragment(data, {track.track_id: track.timescale for track in self.init_info.tracks})
        except EasyStreamProtocolError:
            self._end("invalid_media")
            return
        known = {track.track_id for track in self.init_info.tracks}
        if any(fragment.track_id not in known for fragment in fragments):
            self._end("unsupported")
            return
        for fragment in fragments:
            if fragment.start_s is None:
                continue
            previous = self.last_fragment_start.get(fragment.track_id)
            if previous is not None and fragment.start_s < previous - 0.5:
                # A timestamp reset without a new init cannot be appended to
                # the same file; finalize this part and reconnect.
                self._end("timestamp_reset")
            self.last_fragment_start[fragment.track_id] = fragment.start_s
        if not self.access_allowed:
            self._buffer(data)
            self.buffered_media_data = True
            return
        self._note_media(self.init_info, len(data))
        if self.ffmpeg is None:
            self._buffer(data)
            return
        self._feed(data)

    def _note_media(self, info, size: int | None = None) -> None:
        if size is not None:
            self.media_bytes += size
        elif info.has_media:
            self.media_bytes += max(0, len(self.init_bytes or b""))
        self.media_fragments += 1
        if self.first_media_wall is None:
            self.first_media_wall = time.time()
        if not self.media_emitted:
            self.media_emitted = True
            video = info.video
            audio = info.audio
            self._emit(
                "media",
                width=info.width,
                height=info.height,
                video=video.codec if video else None,
                audio=audio.codec if audio else None,
                encrypted=bool(info.kids),
                layer=self.selected_layer if self.selected_layer is not None else self.current_layer,
            )

    def _buffer(self, data: bytes) -> None:
        if self.buffer_started is None:
            self.buffer_started = time.monotonic()
        self.buffer.append(data)
        self.buffer_bytes += len(data)
        if self.buffer_bytes > parameters.EASYSTREAM_BUFFER_BYTES:
            self._end("media_buffer_overflow")

    async def _flush_buffered_media(self) -> None:
        if self.layer_pending or not self.access_allowed or not self.buffer:
            return
        await self._start_ffmpeg(self.key_hex)
        buffered, self.buffer = self.buffer, []
        self.buffer_bytes = 0
        self.buffer_started = None
        if buffered and self.buffered_media_data:
            self._note_media(self.init_info, sum(len(item) for item in buffered))
        self.buffered_media_data = False
        for item in buffered:
            self._feed(item)

    def _begin_license(self, kid: str) -> None:
        self.pending_kid = kid
        self.license_attempts = 0
        now = time.monotonic()
        self.pending_license_deadline = now + parameters.EASYSTREAM_LICENSE_TIMEOUT
        self.next_license_send = now

    async def _send_license_request(self) -> None:
        if not self.pending_kid or not self.stream_id:
            return
        if self.license_denied and self.next_license_send > time.monotonic():
            return
        self.license_attempts += 1
        try:
            payload = clearkey_challenge(self.pending_kid)
        except LicenseError:
            self._end("license_invalid")
            return
        self.next_license_send = time.monotonic() + min(5.0, 0.25 * 2**self.license_attempts)
        await self._send_json(
            {
                "type": "license",
                "stream": self.stream_id,
                "system": "clearkey",
                "kid": self.pending_kid,
                "payload": payload,
            }
        )

    # -- ffmpeg ----------------------------------------------------------

    async def _start_ffmpeg(self, key_hex: str | None) -> None:
        if self.ffmpeg is not None:
            return
        args = [
            parameters.FFMPEG_PATH,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "mp4",
        ]
        if key_hex:
            args.extend(["-decryption_key", key_hex])
        args.extend(["-i", "pipe:0", "-c", "copy", self.output])
        try:
            self.ffmpeg = await asyncio.create_subprocess_exec(
                *args,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
        except (OSError, ValueError) as error:
            self._emit("error", code="ffmpeg_failed", message=f"could not start ffmpeg: {error}")
            self._end("ffmpeg_failed")
            return
        self.writer_task = asyncio.create_task(self._writer())
        self.stderr_task = asyncio.create_task(self._drain_stderr())
        self.ffmpeg_wait_task = asyncio.create_task(self._watch_ffmpeg())

    async def _writer(self) -> None:
        while True:
            data = await self.write_queue.get()
            if data is None:
                return
            try:
                self.ffmpeg.stdin.write(data)
                await self.ffmpeg.stdin.drain()
            except (BrokenPipeError, ConnectionResetError, OSError, ValueError):
                self.ffmpeg_failed = True
                self.request_stop("ffmpeg_failed")
                return
            finally:
                self.queued_bytes = max(0, self.queued_bytes - len(data))

    async def _watch_ffmpeg(self) -> None:
        if self.ffmpeg is None:
            return
        self.ffmpeg_exit = await self.ffmpeg.wait()
        if not self._shutting_down and not self.ffmpeg_failed and not self.stop_requested:
            self.ffmpeg_failed = True
            self.request_stop("ffmpeg_failed")

    async def _drain_stderr(self) -> None:
        if self.ffmpeg is None or self.ffmpeg.stderr is None:
            return
        while True:
            try:
                line = await self.ffmpeg.stderr.readline()
            except (OSError, ValueError):
                return
            if not line:
                return
            text = line.decode("utf-8", "replace").strip()
            if text:
                self.stderr_lines.append(self._redact(text))

    def _feed(self, data: bytes) -> None:
        if self.ffmpeg is None:
            self._buffer(data)
            return
        if self.queued_bytes + len(data) > parameters.EASYSTREAM_OUTPUT_BUFFER_BYTES:
            self._end("output_overload")
        self.queued_bytes += len(data)
        self.write_queue.put_nowait(data)

    async def _stop_ffmpeg(self) -> None:
        if self.ffmpeg is None:
            return
        self._shutting_down = True
        if self.writer_task is not None:
            self.write_queue.put_nowait(None)
            try:
                await asyncio.wait_for(self.writer_task, timeout=5)
            except asyncio.TimeoutError:
                self.writer_task.cancel()
        if not self.stdin_closed and self.ffmpeg.stdin is not None:
            self.stdin_closed = True
            try:
                self.ffmpeg.stdin.close()
                wait_closed = getattr(self.ffmpeg.stdin, "wait_closed", None)
                if wait_closed is not None:
                    await asyncio.wait_for(wait_closed(), timeout=5)
            except (OSError, ValueError, asyncio.TimeoutError):
                pass
        try:
            self.ffmpeg_exit = await asyncio.wait_for(self.ffmpeg.wait(), timeout=parameters.EASYSTREAM_STOP_TIMEOUT)
        except asyncio.TimeoutError:
            self.ffmpeg.kill()
            try:
                self.ffmpeg_exit = await asyncio.wait_for(self.ffmpeg.wait(), timeout=5)
            except asyncio.TimeoutError:
                pass
        if self.stderr_task is not None:
            try:
                await asyncio.wait_for(self.stderr_task, timeout=5)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                self.stderr_task.cancel()

    # -- deadlines -------------------------------------------------------

    async def _check_deadlines(self, now: float) -> None:
        if self.stop_requested:
            raise _SessionEnd(self.reason or "requested")
        if not self.subscribed and now - self.started_at > parameters.EASYSTREAM_STARTUP_TIMEOUT:
            self._end("subscribe_timeout")
        if self.subscribed and self.media_bytes == 0 and now - self.started_at > parameters.EASYSTREAM_STARTUP_TIMEOUT:
            self._end("no_media")
        if self.subscribed and now - self.last_message_at > parameters.EASYSTREAM_SILENCE_TIMEOUT:
            self._end("silence")

        wall = time.time()
        if self.expires_at:
            if (
                wall >= float(self.expires_at) - parameters.EASYSTREAM_TOKEN_REFRESH_MARGIN
                and not self.refresh_requested
            ):
                self._emit("refresh_needed", reason="expiring")
                self.refresh_requested = True
            if wall >= float(self.expires_at):
                self._end("token_expired")

        if self.pending_kid is not None:
            if now >= self.pending_license_deadline:
                self._end("license_denied" if self.license_denied else "license_timeout")
            elif now >= self.next_license_send:
                await self._send_license_request()

        if self.layer_pending:
            confirmed = self.layer_confirmed_at
            if confirmed is not None:
                if now - confirmed > parameters.EASYSTREAM_LAYER_CONFIRM_TIMEOUT:
                    # The acknowledged layer reused the current init.
                    self.layer_pending = False
                    self.layer_confirmed_at = None
                    await self._flush_buffered_media()
            elif now - self.layer_pending_since > parameters.EASYSTREAM_LAYER_SELECT_TIMEOUT:
                # The edge never acknowledged the select; record what it sends
                # rather than stalling until the silence timeout.
                self.layer_pending = False
                await self._flush_buffered_media()

        if self.buffer_started is not None and now - self.buffer_started > parameters.EASYSTREAM_BUFFER_SECONDS:
            self._end("license_buffer_overflow")

        if self.max_duration and self.first_media_wall is not None:
            if time.time() - self.first_media_wall >= self.max_duration:
                self._end("max_duration")

        if now - self.last_ping >= 1.0:
            self.last_ping = now
            self.ping_id += 1
            await self._send_json({"type": "ping", "id": self.ping_id})

    async def _shutdown(self, reason: str) -> None:
        if self._stopped_emitted:
            return
        self._stopped_emitted = True
        self._shutting_down = True
        ws, self.ws = self.ws, None
        if ws is not None:
            try:
                await asyncio.wait_for(ws.close(), timeout=5)
            except (asyncio.TimeoutError, OSError, ConnectionClosed):
                pass
        await self._stop_ffmpeg()
        if self.ffmpeg_failed and self.stderr_lines:
            self._warn("ffmpeg: " + self.stderr_lines[-1])
        self._emit(
            "stopped",
            reason=reason,
            file=self.output,
            mediaBytes=self.media_bytes,
            video=self.init_info.video.codec if self.init_info and self.init_info.video else None,
            audio=self.init_info.audio.codec if self.init_info and self.init_info.audio else None,
            width=self.init_info.width if self.init_info else None,
            height=self.init_info.height if self.init_info else None,
            ffmpegExit=self.ffmpeg_exit,
        )


def main() -> int:
    # Bootstrap on the same private pipe used for subsequent commands. No
    # credential file can survive a killed monitor, even before subscription.
    config = json.loads(sys.stdin.readline())
    session = EasyStreamSession(config)

    async def run():
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                loop.add_signal_handler(sig, session.request_stop, "requested")
            except (NotImplementedError, RuntimeError):
                pass
        await session.run()

    asyncio.run(run())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
