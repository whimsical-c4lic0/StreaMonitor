"""Synthetic EasyStream protocol and MP4 fixtures used by the unit tests.

Nothing here comes from a live performer or a real viewer token.  The boxes are
small but structurally valid enough for the bounded parser, and the edge server
speaks the documented message contract over a local ``ws://`` socket.
"""

from __future__ import annotations

import asyncio
import json
import struct
import threading
from pathlib import Path
from typing import Callable

CLEARKEY_SYSTEM_ID = bytes.fromhex("1077efecc0b24d02ace33c1e52e2fb4b")
WIDEVINE_SYSTEM_ID = bytes.fromhex("edef8ba979d64acea3c827dcd51d21ed")
STREAM_ID = "0123456789abcdef"
KID = "00112233445566778899aabbccddeeff"


def box(box_type: str, payload: bytes = b"") -> bytes:
    return struct.pack(">I", len(payload) + 8) + box_type.encode("ascii") + payload


def extended_box(box_type: str, payload: bytes = b"") -> bytes:
    return struct.pack(">I", 1) + box_type.encode("ascii") + struct.pack(">Q", len(payload) + 16) + payload


def fullbox(version: int, flags: int, payload: bytes = b"") -> bytes:
    return bytes([version]) + flags.to_bytes(3, "big") + payload


def tkhd(track_id: int, *, version: int = 0) -> bytes:
    if version == 1:
        body = struct.pack(">QQI", 0, 0, track_id)
    else:
        body = struct.pack(">III", 0, 0, track_id)
    return box("tkhd", fullbox(version, 0, body + b"\0" * 8))


def mdhd(timescale: int, *, version: int = 0) -> bytes:
    if version == 1:
        body = struct.pack(">QQI", 0, 0, timescale)
    else:
        body = struct.pack(">III", 0, 0, timescale)
    return box("mdhd", fullbox(version, 0, body + b"\0" * 4))


def hdlr(handler: str) -> bytes:
    return box("hdlr", fullbox(0, 0, b"\0" * 4 + handler.encode("ascii") + b"\0"))


def tenc(kid: str, *, version: int = 1, pattern: int = 1) -> bytes:
    body = bytes([0, pattern, 1, 8]) + bytes.fromhex(kid)
    return box("tenc", fullbox(version, 0, body))


def sinf(kid: str | None = None, *, original: str = "avc1", version: int = 1) -> bytes:
    children = box("frma", original.encode("ascii"))
    if kid is not None:
        children += box("schi", tenc(kid, version=version))
    return box("sinf", children)


def video_entry(kid: str | None = None) -> bytes:
    # 6 reserved, data_reference_index, pre_defined/reserved, width, height.
    header = b"\0" * 6 + struct.pack(">H", 1) + b"\0" * 16 + struct.pack(">HH", 1280, 720) + b"\0" * 50
    entry_type = "encv" if kid is not None else "avc1"
    children = sinf(kid) if kid is not None else box("avcC", b"\x01\x64\x00\x2a")
    return box(entry_type, header + children)


def audio_entry(kid: str | None = None) -> bytes:
    # 6 reserved, data_reference_index, 20 bytes of audio fields.
    header = b"\0" * 6 + struct.pack(">H", 1) + b"\0" * 20
    entry_type = "enca" if kid is not None else "mp4a"
    children = sinf(kid, original="mp4a") if kid is not None else box("esds", b"\0" * 4)
    return box(entry_type, header + children)


def trak(track_id: int, handler: str, timescale: int, entry: bytes) -> bytes:
    stsd = box("stsd", fullbox(0, 0, struct.pack(">I", 1) + entry))
    stbl = box("stbl", stsd)
    minf = box("minf", stbl)
    mdia = box("mdia", mdhd(timescale) + hdlr(handler) + minf)
    return box("trak", tkhd(track_id) + mdia)


def pssh(system_id: bytes = CLEARKEY_SYSTEM_ID, *, kid: str | None = None) -> bytes:
    body = system_id
    if kid is not None:
        # Version 1 carries KID records before the data.
        return box("pssh", fullbox(1, 0, body + struct.pack(">I", 1) + bytes.fromhex(kid) + struct.pack(">I", 0)))
    return box("pssh", fullbox(0, 0, body + struct.pack(">I", 0)))


def init_segment(
    *,
    kid: str | None = None,
    audio_kid: str | None = None,
    pssh_kid: bool = False,
    complete: bool = False,
) -> bytes:
    ftyp = box("ftyp", b"isom" + struct.pack(">I", 512) + b"isomiso2avc1mp41")
    moov = box(
        "moov",
        trak(1, "vide", 90000, video_entry(kid))
        + trak(2, "soun", 48000, audio_entry(audio_kid))
        + pssh(kid=kid if pssh_kid and kid else None),
    )
    data = ftyp + moov
    if complete:
        data += box("mdat", b"\0" * 64)
    return data


def fragment(track_id: int = 1, *, timescale: int = 90000, decode_time: int = 0, count: int = 2) -> bytes:
    tfhd = box("tfhd", fullbox(0, 0x020000, struct.pack(">I", track_id)))
    tfdt = box("tfdt", fullbox(1, 0, struct.pack(">Q", decode_time)))
    durations = b"".join(struct.pack(">I", 3000) for _ in range(count))
    trun = box("trun", fullbox(0, 0x100, struct.pack(">I", count) + durations))
    moof = box("moof", box("traf", tfhd + tfdt + trun))
    return moof + box("mdat", b"\0" * (32 + 16 * count))


def write_fake_ffmpeg(path: Path, *, exit_code: int = 0, delay: float = 0.0) -> Path:
    script = f"""#!/usr/bin/env python3
import sys, time
time.sleep({delay!r})
if {exit_code!r}:
    sys.exit({exit_code!r})
args = sys.argv[1:]
try:
    source = args[args.index("-i") + 1]
except (ValueError, IndexError):
    source = None
target = args[-1]
if source == "pipe:0":
    data = sys.stdin.buffer.read()
else:
    data = open(source, "rb").read() if source else b""
with open(target, "wb") as handle:
    handle.write(data)
"""
    path.write_text(script)
    path.chmod(0o755)
    return path


class EdgeServer:
    """A local WebSocket edge backed by an async handler."""

    def __init__(self, handler: Callable):
        self._handler = handler
        self._stop: threading.Event | None = None
        self._ready = threading.Event()
        self._thread = threading.Thread(target=self._run, name="easystream-test-edge", daemon=True)
        self.port: int | None = None
        self.error: BaseException | None = None

    def start(self) -> "EdgeServer":
        self._stop = threading.Event()
        self._thread.start()
        if not self._ready.wait(5):
            raise RuntimeError("fake EasyStream edge did not start")
        if self.error is not None:
            raise self.error
        return self

    def _run(self) -> None:
        try:
            from websockets.asyncio.server import serve
        except ImportError:  # pragma: no cover
            from websockets.server import serve  # type: ignore[no-redef]

        stop = self._stop

        async def main() -> None:
            async with serve(self._handler, "127.0.0.1", 0) as server:
                self.port = server.sockets[0].getsockname()[1]
                self._ready.set()
                while not stop.is_set():
                    await asyncio.sleep(0.02)

        try:
            asyncio.run(main())
        except BaseException as error:  # noqa: BLE001 - surfaced to the test
            self.error = error
            self._ready.set()

    def url(self) -> str:
        return f"ws://127.0.0.1:{self.port}/v1/viewer-connections"

    def close(self) -> None:
        if self._stop is not None:
            self._stop.set()
        self._thread.join(5)


async def send_json(ws, message: dict) -> None:
    await ws.send(json.dumps(message, separators=(",", ":")))


def read_license_challenge(message: dict) -> str:
    import base64

    body = json.loads(base64.b64decode(message["payload"]))
    encoded_kid = body["kids"][0]
    return base64.urlsafe_b64decode(encoded_kid + "=" * (-len(encoded_kid) % 4)).hex()


def clear_behaviour(media_delay: float = 0.0):
    """Return a handler that subscribes, selects, sends clear media and ends."""

    received: list[dict] = []

    async def handler(ws) -> None:
        try:
            async for raw in ws:
                message = json.loads(raw)
                received.append(message)
                if message.get("type") == "subscribe":
                    await send_json(
                        ws,
                        {
                            "type": "subscribed",
                            "stream": STREAM_ID,
                            "session": "session-1",
                            "n": 0,
                            "layer": 0,
                            "layers": [0, 1, 2],
                            "resolutions": ["640x360", "1280x720", "1920x1080"],
                            "codecs": "avc1.64002a,mp4a.40.2",
                            "drm": "none",
                            "sub": "alias",
                        },
                    )
                    await send_json(ws, {"type": "access", "stream": STREAM_ID, "access": "public", "media": "all"})
                elif message.get("type") == "want":
                    if media_delay:
                        await asyncio.sleep(media_delay)
                    await ws.send(init_segment())
                    await ws.send(fragment())
                    await ws.send(json.dumps({"type": "end", "stream": STREAM_ID}))
        except Exception:  # noqa: BLE001 - the test asserts through events
            pass

    handler.received = received  # type: ignore[attr-defined]
    return handler


def encrypted_behaviour(*, deny: bool = False, send_after_license: bool = True):
    received: list[dict] = []

    async def handler(ws) -> None:
        try:
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
                            "layer": 1,
                            "layers": [0, 1, 2],
                            "resolutions": ["640x360", "1280x720", "1920x1080"],
                            "codecs": "avc1.64002a,mp4a.40.2",
                            "drm": "on",
                        },
                    )
                    await send_json(ws, {"type": "access", "stream": STREAM_ID, "access": "public", "media": "all"})
                elif kind == "want":
                    await ws.send(init_segment(kid=KID))
                    await ws.send(fragment())
                elif kind == "license":
                    if deny:
                        await send_json(
                            ws,
                            {
                                "type": "error",
                                "code": "license_denied",
                                "message": "denied",
                                "stream": STREAM_ID,
                            },
                        )
                        continue
                    import base64
                    key = base64.urlsafe_b64encode(bytes.fromhex("000102030405060708090a0b0c0d0e0f")).rstrip(b"=")
                    encoded_kid = base64.urlsafe_b64encode(bytes.fromhex(message["kid"])).rstrip(b"=")
                    body = json.dumps(
                        {
                            "keys": [
                                {
                                    "kty": "oct",
                                    "kid": encoded_kid.decode("ascii"),
                                    "k": key.decode("ascii"),
                                }
                            ]
                        }
                    )

                    await send_json(
                        ws,
                        {
                            "type": "license",
                            "stream": STREAM_ID,
                            "kid": message["kid"],
                            "payload": base64.b64encode(body.encode()).decode(),
                        },
                    )
                    if send_after_license:
                        await ws.send(fragment(decode_time=6000))
                        await ws.send(json.dumps({"type": "end", "stream": STREAM_ID}))
        except Exception:  # noqa: BLE001
            pass

    handler.received = received  # type: ignore[attr-defined]
    return handler


def public_handler(handler_fn):
    return handler_fn


def cenc_fragments(data: bytes) -> list[bytes]:
    """Repackage ffmpeg's single-track CENC file into init + media messages.

    ffmpeg's fragmented encryption muxer produces unreadable files on some
    builds. Preserve its real encrypted samples and per-sample IV/subsample
    metadata instead, moving that metadata from stbl to each fragment's traf.
    """
    from streamonitor.downloaders.easystream_protocol import boxes

    def children(parent):
        return {item.type: item for item in boxes(data, parent.payload, parent.end)}

    def raw(item):
        return data[item.start:item.end]

    def u32(offset):
        return struct.unpack_from(">I", data, offset)[0]

    top = {item.type: item for item in boxes(data)}
    moov = children(top["moov"])
    trak = children(moov["trak"])
    mdia = children(trak["mdia"])
    minf = children(mdia["minf"])
    stbl = children(minf["stbl"])
    stsz = stbl["stsz"]
    count = u32(stsz.payload + 8)
    fixed_size = u32(stsz.payload + 4)
    sizes = [fixed_size or u32(stsz.payload + 12 + 4 * i) for i in range(count)]
    durations = []
    stts = stbl["stts"]
    for i in range(u32(stts.payload + 4)):
        offset = stts.payload + 8 + i * 8
        durations.extend([u32(offset + 4)] * u32(offset))
    assert len(durations) == count
    # The synthetic video has one contiguous chunk and no B-frame offsets.
    assert u32(stbl["stco"].payload + 4) == 1
    media_offset = u32(stbl["stco"].payload + 8)
    assert sum(sizes) == top["mdat"].end - media_offset
    sync = stbl.get("stss")
    sync_samples = set(range(1, count + 1)) if sync is None else {
        u32(sync.payload + 8 + i * 4) for i in range(u32(sync.payload + 4))
    }
    saiz = stbl["saiz"]
    default_aux_size = data[saiz.payload + 4]
    aux_sizes = [default_aux_size or data[saiz.payload + 9 + i] for i in range(count)]
    senc = stbl["senc"]
    senc_header = data[senc.payload:senc.payload + 4]
    aux_offset = senc.payload + 8
    auxiliary = []
    samples = []
    for size, aux_size in zip(sizes, aux_sizes):
        samples.append(data[media_offset:media_offset + size])
        auxiliary.append(data[aux_offset:aux_offset + aux_size])
        media_offset += size
        aux_offset += aux_size
    assert aux_offset == senc.end

    empty_tables = b"".join(box(name, fullbox(0, 0, struct.pack(">I", 0))) for name in ("stts", "stsc", "stco"))
    empty_tables += box("stsz", fullbox(0, 0, struct.pack(">II", 0, 0)))
    init_stbl = box("stbl", raw(stbl["stsd"]) + empty_tables)

    def replace(items, name, replacement):
        return b"".join(replacement if key == name else raw(item) for key, item in items.items())

    init_minf = box("minf", replace(minf, "stbl", init_stbl))
    init_mdia = box("mdia", replace(mdia, "minf", init_minf))
    init_trak = box("trak", replace(trak, "mdia", init_mdia))
    track_id = u32(trak["tkhd"].payload + 12)
    trex = box("trex", fullbox(0, 0, struct.pack(">IIIII", track_id, 1, 0, 0, 0)))
    init_moov = box("moov", replace(moov, "trak", init_trak) + box("mvex", trex))
    messages = [raw(top["ftyp"]) + init_moov]
    decode_time = 0
    for sequence, start in enumerate(range(0, count, 15), 1):
        end = min(start + 15, count)
        tfhd = box("tfhd", fullbox(0, 0x020000, struct.pack(">I", track_id)))
        tfdt = box("tfdt", fullbox(1, 0, struct.pack(">Q", decode_time)))
        entries = b"".join(struct.pack(">III", durations[i], sizes[i], 0 if i + 1 in sync_samples else 0x10000)
                           for i in range(start, end))
        fragment_senc = box("senc", senc_header + struct.pack(">I", end - start) + b"".join(auxiliary[start:end]))
        fragment_saiz = box("saiz", fullbox(0, 0, b"\0" + struct.pack(">I", end - start) + bytes(aux_sizes[start:end])))
        mfhd = box("mfhd", fullbox(0, 0, struct.pack(">I", sequence)))

        def make_moof(data_offset, aux_offset):
            trun = box("trun", fullbox(0, 0x701, struct.pack(">Ii", end - start, data_offset) + entries))
            saio = box("saio", fullbox(0, 0, struct.pack(">II", 1, aux_offset)))
            return box("moof", mfhd + box("traf", tfhd + tfdt + trun + fragment_senc + fragment_saiz + saio))

        moof = make_moof(0, 0)
        # senc IVs start after its box header, full-box flags and sample count.
        aux_start = moof.index(b"senc") - 4 + 16
        moof = make_moof(len(moof) + 8, aux_start)
        messages.append(moof + box("mdat", b"".join(samples[start:end])))
        decode_time += sum(durations[start:end])
    assert len(messages) >= 3
    return messages
