"""Validation and MP4 helpers for AmateurTV's EasyStream transport.

EasyStream is a WebSocket transport that carries fragmented MP4.  The site
returns a viewer token and a set of weighted edges; a subscriber sends JSON
control messages and receives either JSON events or complete MP4 boxes (an
init segment first, then media fragments).

This module deliberately contains no network code.  It validates what the site
and the edge send so a malformed response cannot become a token leak, an
arbitrary network destination, or an unbounded allocation.  Keeping it free of
``websockets`` lets the protocol be tested without a live edge.
"""

from __future__ import annotations

import base64
import binascii
import json
import math
import re
import struct
from dataclasses import dataclass
from typing import Any, Sequence
from urllib.parse import urlparse

__all__ = [
    "Box",
    "EasyStreamProtocolError",
    "FragmentInfo",
    "InitInfo",
    "LayerChoice",
    "LicenseError",
    "TrackInfo",
    "boxes",
    "classify_media",
    "clearkey_challenge",
    "decode_message",
    "jwt_expiry",
    "parse_clearkey_license",
    "parse_fragment",
    "parse_init",
    "parse_viewer_token",
    "select_layer",
    "validate_server_message",
    "valid_alias",
    "valid_stream_id",
]

STREAM_ID_RE = re.compile(r"^[a-z0-9]{16}$")
ALIAS_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
KID_RE = re.compile(r"^[0-9a-f]{32}$")
ACCESS_RE = re.compile(r"^[a-z0-9_-]{0,64}$")
BASE64_RE = re.compile(
    r"^(?:[A-Za-z0-9+/]{4})*(?:[A-Za-z0-9+/][AQgw]==|[A-Za-z0-9+/]{2}[AEIMQUYcgkosw048]=)?$"
)

ACCESS_VALUES = {"public", "private", "private_video", "private_audio"}
MEDIA_VALUES = {"all", "audio", "none"}
DRM_SYSTEMS = {"clearkey", "widevine", "fairplay"}
CLEARKEY_SYSTEM_ID = bytes.fromhex("1077efecc0b24d02ace33c1e52e2fb4b")
WIDEVINE_SYSTEM_ID = bytes.fromhex("edef8ba979d64acea3c827dcd51d21ed")

_MAX_MESSAGE_BYTES = 1024 * 1024
_MAX_BOXES = 100000
_MAX_SAMPLES = 1048576
_MAX_LICENSE_BYTES = 32768


class EasyStreamProtocolError(ValueError):
    """The site or edge returned data outside the reviewed EasyStream contract."""


class LicenseError(EasyStreamProtocolError):
    """A ClearKey license was denied, malformed, or for a different key."""


# ---------------------------------------------------------------------------
# Token grants and targets


@dataclass(frozen=True)
class EasyStreamTarget:
    """A validated subscription target: exactly one of stream or alias."""

    stream: str | None = None
    sub: str | None = None

    def __post_init__(self) -> None:
        if (self.stream is None) == (self.sub is None):
            raise EasyStreamProtocolError("EasyStream target needs exactly one of stream or sub")
        if self.stream is not None and not valid_stream_id(self.stream):
            raise EasyStreamProtocolError("invalid EasyStream stream id")
        if self.sub is not None and not valid_alias(self.sub):
            raise EasyStreamProtocolError("invalid EasyStream alias")

    def as_message(self) -> dict[str, str]:
        return {"stream": self.stream} if self.stream is not None else {"sub": self.sub}

    def __str__(self) -> str:
        return f"EasyStreamTarget(stream={self.stream or ''} sub={self.sub or ''})"


@dataclass(frozen=True)
class EasyStreamEdge:
    url: str
    weight: float = 1.0


@dataclass(frozen=True)
class EasyStreamCredentials:
    token: str
    edges: tuple[EasyStreamEdge, ...]
    pick_k: int
    expires_at: float | None = None

    def __str__(self) -> str:
        return f"EasyStreamCredentials(edges={len(self.edges)} pickK={self.pick_k})"

    def __repr__(self) -> str:
        return self.__str__()


def valid_stream_id(value: Any) -> bool:
    return isinstance(value, str) and STREAM_ID_RE.fullmatch(value) is not None


def valid_alias(value: Any) -> bool:
    return isinstance(value, str) and ALIAS_RE.fullmatch(value) is not None


def parse_target(data: Any) -> EasyStreamTarget | None:
    """Return the preferred valid target from a readmodel ``easystream`` object."""
    if not isinstance(data, dict):
        return None
    stream = data.get("stream")
    if valid_stream_id(stream):
        return EasyStreamTarget(stream=stream)
    sub = data.get("sub")
    if valid_alias(sub):
        return EasyStreamTarget(sub=sub)
    return None


def _b64url_decode(value: str) -> bytes:
    if not isinstance(value, str) or not value:
        raise ValueError("empty base64url value")
    padded = value + "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(padded.encode("ascii"))


def _b64_standard_decode(value: str, *, limit: int | None = None) -> bytes:
    if not isinstance(value, str) or not value:
        raise ValueError("empty base64 value")
    if BASE64_RE.fullmatch(value) is None:
        raise ValueError("value is not standard base64")
    if len(value) % 4:
        raise ValueError("base64 value is not padded")
    data = base64.b64decode(value, validate=True)
    if limit is not None and len(data) > limit:
        raise ValueError("base64 value is too large")
    return data


def _decode_b64_any(value: str) -> bytes:
    """Decode the base64/base64url form used inside ClearKey JSON."""
    if not isinstance(value, str) or not value:
        raise ValueError("empty base64 value")
    text = value.strip()
    try:
        return _b64_standard_decode(text)
    except (ValueError, binascii.Error):
        padded = text + "=" * (-len(text) % 4)
        return _b64url_decode(padded)


def jwt_expiry(token: str) -> float | None:
    """Return a token's ``exp`` claim, without trusting or verifying it."""
    try:
        parts = token.split(".")
        if len(parts) < 2:
            return None
        payload = json.loads(_b64url_decode(parts[1]))
    except (ValueError, TypeError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    exp = payload.get("exp")
    if isinstance(exp, bool) or not isinstance(exp, (int, float)) or exp <= 0:
        return None
    return float(exp)


def _private_host(host: str) -> bool:
    host = host.lower().rstrip(".")
    if host == "localhost" or host.endswith(".localhost") or host.endswith(".local"):
        return True
    try:
        import ipaddress

        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    return not address.is_global


def _valid_edge_url(value: Any, *, allow_private_hosts: bool) -> bool:
    if not isinstance(value, str) or len(value) > 2048:
        return False
    try:
        parsed = urlparse(value)
    except ValueError:
        return False
    if parsed.scheme != "wss" or not parsed.hostname:
        return False
    if parsed.username or parsed.password:
        return False
    if parsed.path != "/v1/viewer-connections":
        return False
    if not allow_private_hosts and _private_host(parsed.hostname):
        return False
    return True


def parse_viewer_token(payload: Any, *, now: float, allow_private_hosts: bool = False) -> EasyStreamCredentials:
    """Validate a viewer-token response and build bounded credentials."""
    if not isinstance(payload, dict):
        raise EasyStreamProtocolError("viewer-token response is not an object")
    result = payload.get("result")
    if result is not None and result != "OK":
        raise EasyStreamProtocolError(f"viewer-token result is {result!r}")
    body = payload.get("body", payload)
    if not isinstance(body, dict):
        raise EasyStreamProtocolError("viewer-token body is not an object")

    token = body.get("token")
    if not isinstance(token, str) or not token or len(token) > 16384:
        raise EasyStreamProtocolError("viewer-token is missing or invalid")

    edges_data = body.get("edges")
    if not isinstance(edges_data, list) or not edges_data:
        raise EasyStreamProtocolError("viewer-token has no edges")
    edges: list[EasyStreamEdge] = []
    for edge in edges_data:
        if not isinstance(edge, dict):
            raise EasyStreamProtocolError("viewer-token edge is not an object")
        url = edge.get("url")
        weight = edge.get("weight")
        if isinstance(weight, bool) or not isinstance(weight, (int, float)):
            raise EasyStreamProtocolError("viewer-token edge has no numeric weight")
        weight = float(weight)
        if not math.isfinite(weight) or weight < 0:
            raise EasyStreamProtocolError("viewer-token edge has an invalid weight")
        if not _valid_edge_url(url, allow_private_hosts=allow_private_hosts):
            raise EasyStreamProtocolError("viewer-token edge URL is outside the EasyStream contract")
        edges.append(EasyStreamEdge(url=url, weight=weight))

    raw_pick = body.get("pickK")
    if isinstance(raw_pick, bool) or not isinstance(raw_pick, int) or not 1 <= raw_pick <= len(edges):
        pick_k = len(edges)
    else:
        pick_k = raw_pick

    expiry = jwt_expiry(token)
    if expiry is None:
        expires_in = body.get("expiresIn")
        if isinstance(expires_in, (int, float)) and not isinstance(expires_in, bool) and expires_in > 0:
            expiry = now + float(expires_in)
        else:
            # The frontend's token lifetime was 900s.  A conservative default
            # keeps renewal bounded when the response omits both fields.
            expiry = now + 900.0
    return EasyStreamCredentials(token=token, edges=tuple(edges), pick_k=pick_k, expires_at=expiry)


# ---------------------------------------------------------------------------
# Control messages


def decode_message(raw: Any) -> dict[str, Any]:
    if isinstance(raw, (bytes, bytearray)):
        if len(raw) > _MAX_MESSAGE_BYTES:
            raise EasyStreamProtocolError("control message is too large")
        try:
            raw = raw.decode("utf-8")
        except UnicodeDecodeError as error:
            raise EasyStreamProtocolError("control message is not UTF-8") from error
    if not isinstance(raw, str) or len(raw) > _MAX_MESSAGE_BYTES:
        raise EasyStreamProtocolError("control message is not a bounded string")
    try:
        message = json.loads(raw)
    except json.JSONDecodeError as error:
        raise EasyStreamProtocolError("control message is not JSON") from error
    if not isinstance(message, dict) or not isinstance(message.get("type"), str):
        raise EasyStreamProtocolError("control message has no type")
    return message


def _require_str(message: dict[str, Any], key: str, *, allow_empty: bool = False) -> str:
    value = message.get(key)
    if not isinstance(value, str) or (not allow_empty and not value) or len(value) > 4096:
        raise EasyStreamProtocolError(f"{message.get('type')}: invalid {key}")
    return value


def _optional_int(message: dict[str, Any], key: str, low: int, high: int) -> int | None:
    if key not in message:
        return None
    value = message[key]
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise EasyStreamProtocolError(f"{message.get('type')}: invalid {key}")
    return value


def _layer_list(value: Any, label: str = "subscribed") -> list[int]:
    """Validate an ascending layer-id list (0..7).

    The wire field lists layer *ids*, not resolutions; a parallel
    ``resolutions`` array maps each id to a ``WIDTHxHEIGHT`` label.
    """
    if not isinstance(value, list) or not value or len(value) > 8:
        raise EasyStreamProtocolError(f"{label}: invalid layers")
    layers = []
    for index, layer in enumerate(value):
        if isinstance(layer, bool) or not isinstance(layer, int) or not 0 <= layer <= 7:
            raise EasyStreamProtocolError(f"{label}: invalid layer")
        if index == 0:
            if layer != 0:
                raise EasyStreamProtocolError(f"{label}: layers must start at 0")
        elif layer <= layers[-1]:
            raise EasyStreamProtocolError(f"{label}: layers must be ascending")
        layers.append(layer)
    return layers


def _validate_resolutions(value: Any, layers: list[int], label: str) -> None:
    if value is None:
        return
    if not isinstance(value, list) or len(value) != len(layers):
        raise EasyStreamProtocolError(f"{label}: invalid resolutions")
    if any(_resolution_metric(item) is None for item in value):
        raise EasyStreamProtocolError(f"{label}: invalid resolution")


def _require_stream(message: dict[str, Any]) -> str:
    stream = _require_str(message, "stream")
    if not valid_stream_id(stream):
        raise EasyStreamProtocolError(f"{message.get('type')}: invalid stream id")
    return stream


def validate_server_message(message: dict[str, Any]) -> dict[str, Any]:
    """Validate the fields the recorder relies on; unknown events pass through."""
    kind = message["type"]
    if kind == "subscribed":
        stream = _require_str(message, "stream")
        if not valid_stream_id(stream):
            raise EasyStreamProtocolError("subscribed: invalid stream id")
        _require_str(message, "session")
        n = _optional_int(message, "n", 0, 63)
        if n is None:
            raise EasyStreamProtocolError("subscribed: missing n")
        layer = _optional_int(message, "layer", 0, 7)
        if layer is None:
            raise EasyStreamProtocolError("subscribed: missing layer")
        layers = _layer_list(message.get("layers"))
        _require_str(message, "codecs")
        drm = message.get("drm")
        if drm is not None and drm not in ("on", "none"):
            raise EasyStreamProtocolError("subscribed: invalid drm")
        _validate_resolutions(message.get("resolutions"), layers, "subscribed")
    elif kind == "layers":
        _require_stream(message)
        layers = _layer_list(message.get("layers"), "layers")
        _validate_resolutions(message.get("resolutions"), layers, "layers")
    elif kind in ("layer", "gap"):
        _require_stream(message)
        if kind == "layer":
            _optional_int(message, "layer", 0, 7)
            if "layer" not in message:
                raise EasyStreamProtocolError("layer: missing layer")
        _require_str(message, "codecs", allow_empty=True)
    elif kind == "access":
        _require_stream(message)
        access = _require_str(message, "access")
        if access not in ACCESS_VALUES:
            raise EasyStreamProtocolError("access: invalid access")
        if "media" in message and message["media"] not in MEDIA_VALUES:
            raise EasyStreamProtocolError("access: invalid media")
        if "reason" in message:
            reason = message["reason"]
            if not isinstance(reason, str) or ACCESS_RE.fullmatch(reason) is None:
                raise EasyStreamProtocolError("access: invalid reason")
    elif kind == "license":
        _require_stream(message)
        kid = _require_str(message, "kid")
        if KID_RE.fullmatch(kid) is None:
            raise EasyStreamProtocolError("license: invalid kid")
        payload = _require_str(message, "payload")
        if len(payload) > _MAX_LICENSE_BYTES * 2 or BASE64_RE.fullmatch(payload) is None:
            raise EasyStreamProtocolError("license: invalid payload")
        if "system" in message and message["system"] not in DRM_SYSTEMS:
            raise EasyStreamProtocolError("license: invalid system")
    elif kind in ("end", "revoked", "restricted"):
        _require_stream(message)
        if kind == "restricted" and "reason" in message:
            _require_str(message, "reason", allow_empty=True)
    elif kind == "error":
        _require_str(message, "code", allow_empty=True)
        _require_str(message, "message", allow_empty=True)
    elif kind == "ping":
        _optional_int(message, "id", 0, 2**53)
        if "id" not in message:
            raise EasyStreamProtocolError("ping: missing id")
    elif kind in ("warning",):
        warnings = message.get("warnings")
        if warnings is not None and not isinstance(warnings, list):
            raise EasyStreamProtocolError("warning: invalid warnings")
    return message


# ---------------------------------------------------------------------------
# ClearKey


def clearkey_challenge(kid_hex: str) -> str:
    if KID_RE.fullmatch(kid_hex) is None:
        raise LicenseError("invalid ClearKey KID")
    kid = bytes.fromhex(kid_hex)
    encoded = base64.urlsafe_b64encode(kid).rstrip(b"=").decode("ascii")
    body = json.dumps({"kids": [encoded], "type": "temporary"}, separators=(",", ":")).encode("utf-8")
    return base64.b64encode(body).decode("ascii")


def parse_clearkey_license(payload: str, kid_hex: str) -> bytes:
    """Validate a ClearKey JWK response and return its 16-byte AES key."""
    if KID_RE.fullmatch(kid_hex) is None:
        raise LicenseError("invalid ClearKey KID")
    try:
        raw = _b64_standard_decode(payload, limit=_MAX_LICENSE_BYTES)
        data = json.loads(raw)
    except (ValueError, binascii.Error, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise LicenseError("ClearKey license is not valid base64 JSON") from error
    if not isinstance(data, dict) or not isinstance(data.get("keys"), list):
        raise LicenseError("ClearKey license has no keys")
    expected_kid = bytes.fromhex(kid_hex)
    for entry in data["keys"]:
        if not isinstance(entry, dict) or entry.get("kty") != "oct":
            continue
        try:
            kid = _decode_b64_any(entry.get("kid", ""))
            key = _decode_b64_any(entry.get("k", ""))
        except (ValueError, binascii.Error):
            continue
        if kid == expected_kid and len(key) == 16:
            return key
    raise LicenseError("ClearKey license does not contain the requested 16-byte key")


# ---------------------------------------------------------------------------
# MP4 box parsing


@dataclass(frozen=True)
class Box:
    type: str
    start: int
    payload: int
    end: int


def _need(end: int, offset: int, size: int) -> None:
    if offset < 0 or size < 0 or offset + size > end:
        raise EasyStreamProtocolError("MP4 box is truncated")


def _u8(data: memoryview, offset: int, end: int) -> int:
    _need(end, offset, 1)
    return data[offset]


def _u16(data: memoryview, offset: int, end: int) -> int:
    _need(end, offset, 2)
    return struct.unpack_from(">H", data, offset)[0]


def _u32(data: memoryview, offset: int, end: int) -> int:
    _need(end, offset, 4)
    return struct.unpack_from(">I", data, offset)[0]


def _u64(data: memoryview, offset: int, end: int) -> int:
    _need(end, offset, 8)
    return struct.unpack_from(">Q", data, offset)[0]


def boxes(data: bytes | bytearray | memoryview, start: int = 0, end: int | None = None) -> list[Box]:
    """Parse a bounded list of sibling MP4 boxes."""
    view = memoryview(data)
    limit = len(view) if end is None else min(end, len(view))
    if start < 0 or start > limit:
        raise EasyStreamProtocolError("invalid MP4 box range")
    result: list[Box] = []
    offset = start
    while offset < limit:
        if len(result) >= _MAX_BOXES:
            raise EasyStreamProtocolError("too many MP4 boxes")
        _need(limit, offset, 8)
        size = _u32(view, offset, limit)
        box_type = bytes(view[offset + 4 : offset + 8]).decode("latin-1")
        if any(ord(char) < 0x20 or ord(char) > 0x7E for char in box_type):
            raise EasyStreamProtocolError("invalid MP4 box type")
        header = 8
        if size == 1:
            _need(limit, offset, 16)
            size = _u64(view, offset + 8, limit)
            header = 16
        elif size == 0:
            size = limit - offset
        if size < header or offset + size > limit:
            raise EasyStreamProtocolError("invalid MP4 box size")
        result.append(Box(box_type, offset, offset + header, offset + size))
        offset += size
    return result


def _child(view: memoryview, parent: Box, box_type: str) -> Box | None:
    for box in boxes(view, parent.payload, parent.end):
        if box.type == box_type:
            return box
    return None


def _children(view: memoryview, parent: Box, box_type: str) -> list[Box]:
    return [box for box in boxes(view, parent.payload, parent.end) if box.type == box_type]


def _child_required(view: memoryview, parent: Box, box_type: str) -> Box:
    found = _child(view, parent, box_type)
    if found is None:
        raise EasyStreamProtocolError(f"MP4 box {parent.type} has no {box_type}")
    return found


@dataclass(frozen=True)
class TrackInfo:
    track_id: int
    handler: str
    codec: str
    timescale: int
    encrypted: bool
    kid: str | None
    scheme: str | None
    width: int | None = None
    height: int | None = None

    def __str__(self) -> str:
        return (
            f"TrackInfo(id={self.track_id} handler={self.handler} codec={self.codec} "
            f"encrypted={self.encrypted} kid={self.kid or ''})"
        )


@dataclass(frozen=True)
class InitInfo:
    tracks: tuple[TrackInfo, ...]
    pssh_systems: tuple[str, ...] = ()
    width: int | None = None
    height: int | None = None
    has_media: bool = False

    @property
    def video(self) -> TrackInfo | None:
        return next((track for track in self.tracks if track.handler == "vide"), None)

    @property
    def audio(self) -> TrackInfo | None:
        return next((track for track in self.tracks if track.handler == "soun"), None)

    @property
    def kids(self) -> tuple[str, ...]:
        seen: list[str] = []
        for track in self.tracks:
            if track.kid and track.kid not in seen:
                seen.append(track.kid)
        return tuple(seen)

    @property
    def fingerprint(self) -> tuple:
        return tuple(
            (
                track.track_id,
                track.handler,
                track.codec,
                track.timescale,
                track.encrypted,
                track.kid,
                track.scheme,
                track.width,
                track.height,
            )
            for track in self.tracks
        )


def _sample_entry_children(view: memoryview, entry: Box, handler: str) -> list[Box]:
    # Visual sample entries have 78 bytes before their child boxes; audio
    # entries have 28.  ``encv``/``enca`` keep the original entry's layout.
    fixed = 78 if handler == "vide" or entry.type in ("encv", "avc1", "avc3", "hvc1", "hev1") else 28
    start = entry.payload + fixed
    if start > entry.end:
        raise EasyStreamProtocolError("MP4 sample entry is truncated")
    return boxes(view, start, entry.end)


def _parse_track(view: memoryview, trak: Box) -> TrackInfo:
    tkhd = _child_required(view, trak, "tkhd")
    mdia = _child_required(view, trak, "mdia")
    mdhd = _child_required(view, mdia, "mdhd")
    hdlr = _child_required(view, mdia, "hdlr")
    minf = _child_required(view, mdia, "minf")
    stbl = _child_required(view, minf, "stbl")
    stsd = _child_required(view, stbl, "stsd")

    version = _u8(view, tkhd.payload, tkhd.end)
    if version not in (0, 1):
        raise EasyStreamProtocolError("unsupported tkhd version")
    track_id = _u32(view, tkhd.payload + (20 if version == 1 else 12), tkhd.end)

    mdhd_version = _u8(view, mdhd.payload, mdhd.end)
    if mdhd_version not in (0, 1):
        raise EasyStreamProtocolError("unsupported mdhd version")
    timescale = _u32(view, mdhd.payload + (20 if mdhd_version == 1 else 12), mdhd.end)
    if timescale == 0:
        raise EasyStreamProtocolError("MP4 track has a zero timescale")

    _need(hdlr.end, hdlr.payload + 8, 4)
    handler = bytes(view[hdlr.payload + 8 : hdlr.payload + 12]).decode("latin-1")
    if not handler:
        raise EasyStreamProtocolError("MP4 track has no handler")

    entry_count = _u32(view, stsd.payload + 4, stsd.end)
    if entry_count < 1:
        raise EasyStreamProtocolError("MP4 track has no sample entry")
    entry_start = stsd.payload + 8
    entries = boxes(view, entry_start, stsd.end)
    if not entries:
        raise EasyStreamProtocolError("MP4 track has no sample entry")
    entry = entries[0]
    encrypted = entry.type in ("encv", "enca")
    codec = entry.type
    kid: str | None = None
    scheme: str | None = None
    width: int | None = None
    height: int | None = None

    if handler == "vide":
        width = _u16(view, entry.payload + 24, entry.end)
        height = _u16(view, entry.payload + 26, entry.end)
    elif handler == "soun":
        width = None
        height = None

    if encrypted:
        children = _sample_entry_children(view, entry, handler)
        sinf = next((box for box in children if box.type == "sinf"), None)
        if sinf is None:
            raise EasyStreamProtocolError("encrypted sample entry has no sinf")
        sinf_children = boxes(view, sinf.payload, sinf.end)
        frma = next((box for box in sinf_children if box.type == "frma"), None)
        if frma is not None and frma.end - frma.payload >= 4:
            codec = bytes(view[frma.payload : frma.payload + 4]).decode("latin-1")
        schi = next((box for box in sinf_children if box.type == "schi"), None)
        if schi is None:
            raise EasyStreamProtocolError("encrypted sample entry has no schi")
        schm = next((box for box in sinf_children if box.type == "schm"), None)
        if schm is not None:
            _need(schm.end, schm.payload + 8, 4)
            scheme_type = bytes(view[schm.payload + 4 : schm.payload + 8])
            if scheme_type not in (b"cenc", b"cbcs"):
                raise EasyStreamProtocolError("unsupported MP4 encryption scheme")
            scheme = scheme_type.decode("ascii")
        tenc = next((box for box in boxes(view, schi.payload, schi.end) if box.type == "tenc"), None)
        if tenc is None:
            raise EasyStreamProtocolError("encrypted sample entry has no tenc")
        tenc_version = _u8(view, tenc.payload, tenc.end)
        if tenc_version not in (0, 1):
            raise EasyStreamProtocolError("unsupported tenc version")
        is_protected = _u8(view, tenc.payload + 6, tenc.end)
        pattern = _u8(view, tenc.payload + 5, tenc.end)
        if is_protected:
            _need(tenc.end, tenc.payload + 8, 16)
            kid = bytes(view[tenc.payload + 8 : tenc.payload + 24]).hex()
            if KID_RE.fullmatch(kid) is None:
                raise EasyStreamProtocolError("tenc has an invalid KID")
            if scheme is None:
                scheme = "cbcs" if tenc_version == 1 and pattern else "cenc"
        else:
            scheme = None
    return TrackInfo(
        track_id=track_id,
        handler=handler,
        codec=codec,
        timescale=timescale,
        encrypted=encrypted,
        kid=kid,
        scheme=scheme,
        width=width,
        height=height,
    )


def _pssh_systems(view: memoryview, moov: Box) -> tuple[str, ...]:
    systems: list[str] = []
    for pssh in _children(view, moov, "pssh"):
        if pssh.end - pssh.payload < 20:
            continue
        system_id = bytes(view[pssh.payload + 4 : pssh.payload + 20])
        if system_id == CLEARKEY_SYSTEM_ID:
            label = "clearkey"
        elif system_id == WIDEVINE_SYSTEM_ID:
            label = "widevine"
        else:
            label = system_id.hex()
        if label not in systems:
            systems.append(label)
    return tuple(systems)


def parse_init(data: bytes | bytearray | memoryview) -> InitInfo:
    """Parse an EasyStream init segment, including its encrypted tracks."""
    view = memoryview(data)
    top = boxes(view)
    if not top or top[0].type != "ftyp":
        raise EasyStreamProtocolError("EasyStream init does not start with ftyp")
    moov = next((box for box in top if box.type == "moov"), None)
    if moov is None:
        raise EasyStreamProtocolError("EasyStream init has no moov")
    tracks = tuple(_parse_track(view, trak) for trak in _children(view, moov, "trak"))
    if not tracks:
        raise EasyStreamProtocolError("EasyStream init has no tracks")
    video = next((track for track in tracks if track.handler == "vide"), None)
    has_media = any(box.type == "mdat" for box in top)
    return InitInfo(
        tracks=tracks,
        pssh_systems=_pssh_systems(view, moov),
        width=video.width if video else None,
        height=video.height if video else None,
        has_media=has_media,
    )


@dataclass(frozen=True)
class FragmentInfo:
    track_id: int
    start_s: float | None
    end_s: float | None
    keyframe: bool | None
    has_mdat: bool


def _parse_traf(view: memoryview, traf: Box, timescales: dict[int, int]) -> FragmentInfo:
    tfhd = _child_required(view, traf, "tfhd")
    trun = _child_required(view, traf, "trun")
    flags = _u32(view, tfhd.payload, tfhd.end) & 0xFFFFFF
    track_id = _u32(view, tfhd.payload + 4, tfhd.end)
    offset = tfhd.payload + 8
    _need(tfhd.end, offset, 0)
    if flags & 0x1:
        offset += 8
        _need(tfhd.end, offset, 0)
    if flags & 0x2:
        offset += 4
        _need(tfhd.end, offset, 0)
    default_duration = None
    if flags & 0x8:
        default_duration = _u32(view, offset, tfhd.end)
        offset += 4
    if flags & 0x10:
        offset += 4
        _need(tfhd.end, offset, 0)
    default_flags = None
    if flags & 0x20:
        default_flags = _u32(view, offset, tfhd.end)
        offset += 4

    tfdt_value = 0
    tfdt = None
    if (tfdt_box := _child(view, traf, "tfdt")) is not None:
        tfdt = tfdt_box
        tfdt_version = _u8(view, tfdt.payload, tfdt.end)
        if tfdt_version == 1:
            tfdt_value = _u64(view, tfdt.payload + 4, tfdt.end)
        elif tfdt_version == 0:
            tfdt_value = _u32(view, tfdt.payload + 4, tfdt.end)
        else:
            raise EasyStreamProtocolError("unsupported tfdt version")

    trun_flags = _u32(view, trun.payload, trun.end) & 0xFFFFFF
    sample_count = _u32(view, trun.payload + 4, trun.end)
    if sample_count > _MAX_SAMPLES:
        raise EasyStreamProtocolError("fragment has too many samples")
    cursor = trun.payload + 8
    if trun_flags & 0x1:
        cursor += 4
        _need(trun.end, cursor, 0)
    first_flags = None
    if trun_flags & 0x4:
        first_flags = _u32(view, cursor, trun.end)
        cursor += 4
    per_sample = 0
    if trun_flags & 0x100:
        per_sample += 4
    if trun_flags & 0x200:
        per_sample += 4
    if trun_flags & 0x400:
        per_sample += 4
    _need(trun.end, cursor, sample_count * per_sample)
    total_duration = 0
    for index in range(sample_count):
        if trun_flags & 0x100:
            total_duration += _u32(view, cursor, trun.end)
            cursor += 4
        elif default_duration is not None:
            total_duration += default_duration
        if trun_flags & 0x200:
            cursor += 4
        if trun_flags & 0x400:
            if index == 0 and first_flags is None:
                first_flags = _u32(view, cursor, trun.end)
            cursor += 4
    if first_flags is None:
        first_flags = default_flags if default_flags is not None else 0
    keyframe = (first_flags & 0x10000) == 0
    timescale = timescales.get(track_id)
    start_s = end_s = None
    if timescale:
        start_s = tfdt_value / timescale
        end_s = (tfdt_value + total_duration) / timescale
    has_mdat = any(box.type == "mdat" for box in boxes(view))
    return FragmentInfo(
        track_id=track_id,
        start_s=start_s,
        end_s=end_s,
        keyframe=keyframe,
        has_mdat=has_mdat,
    )


def parse_fragment(data: bytes | bytearray | memoryview, timescales: dict[int, int] | None = None) -> list[FragmentInfo]:
    """Parse the ``traf`` records of a media fragment."""
    view = memoryview(data)
    top = boxes(view)
    moof = next((box for box in top if box.type == "moof"), None)
    if moof is None:
        raise EasyStreamProtocolError("EasyStream media fragment has no moof")
    has_mdat = any(box.type == "mdat" for box in top)
    result = []
    for traf in _children(view, moof, "traf"):
        item = _parse_traf(view, traf, timescales or {})
        result.append(
            FragmentInfo(
                track_id=item.track_id,
                start_s=item.start_s,
                end_s=item.end_s,
                keyframe=item.keyframe,
                has_mdat=has_mdat,
            )
        )
    if not result:
        raise EasyStreamProtocolError("EasyStream media fragment has no traf")
    return result


def classify_media(data: bytes | bytearray | memoryview) -> str:
    """Return ``init``, ``media`` or ``unknown`` for one binary message."""
    top = boxes(data)
    if any(box.type == "moof" for box in top):
        return "media"
    if any(box.type == "moov" for box in top):
        return "init"
    return "unknown"


# ---------------------------------------------------------------------------
# Layer selection


@dataclass(frozen=True)
class LayerChoice:
    layer: int
    width: int | None
    height: int | None
    diff: int

    def label(self) -> str:
        if self.width and self.height and self.width != self.height:
            return f"{self.width}x{self.height} (layer {self.layer})"
        if self.height:
            return f"{self.height}p (layer {self.layer})"
        return f"layer {self.layer}"


def _resolution_metric(value: Any) -> tuple[int | None, int | None, int] | None:
    """Map an edge resolution label to ``(width, height, sort dimension)``.

    Edges currently send plain quality numbers (``360, 480, 720, 1080``).
    The number is the short side: the height of a landscape stream and the
    width of a portrait stream.  Test fixtures and some captures used a
    ``WIDTHxHEIGHT`` string, which carries enough information to apply the
    monitor's orientation-aware comparison directly.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        if not 16 <= value <= 1080:
            return None
        return None, value, value
    if not isinstance(value, str):
        return None
    match = re.fullmatch(r"(\d{2,5})x(\d{2,5})", value)
    if match is None:
        return None
    width, height = int(match.group(1)), int(match.group(2))
    if width <= 0 or height <= 0:
        return None
    return width, height, width if width < height else height


def select_layer(
    layers: Sequence[int],
    resolutions: Sequence[Any] | None,
    wanted: int,
    preference: str,
    *,
    current: int | None = None,
) -> LayerChoice | None:
    """Select an entitled layer using the monitor's resolution preference.

    ``layers`` are ascending layer ids; ``resolutions`` is the parallel
    array of quality numbers or ``WIDTHxHEIGHT`` labels.  Without resolutions
    there is no reliable way to map a wanted height to an id, so the edge's
    current layer is kept.
    """
    if not layers or not resolutions or len(resolutions) < len(layers):
        return None
    choices: list[LayerChoice] = []
    for index, layer in enumerate(layers):
        resolution = _resolution_metric(resolutions[index])
        if resolution is None:
            continue
        width, height, dimension = resolution
        choices.append(LayerChoice(layer=layer, width=width, height=height, diff=dimension - wanted))
    if not choices:
        return None
    choices.sort(key=lambda item: (abs(item.diff), item.layer))
    selected: LayerChoice | None = None
    if preference == "exact":
        if choices[0].diff == 0:
            selected = choices[0]
    elif preference == "closest" or len(choices) == 1:
        selected = choices[0]
    elif preference == "exact_or_least_higher":
        selected = next((item for item in choices if item.diff >= 0), None)
    elif preference == "exact_or_highest_lower":
        selected = next((item for item in choices if item.diff <= 0), None)
    else:
        selected = choices[0]
    if current is not None and selected is not None and selected.layer == current:
        return selected
    return selected
