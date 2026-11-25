"""Bounded HTTP helpers shared by downloaders.

Transport inventory (reviewed 2026-11-25)
-----------------------------------------
* Status polling and page/API calls: site adapters use their existing
  ``curl_cffi``/``requests`` clients.  They keep those clients (some sites
  depend on TLS impersonation) and should migrate to :func:`fetch` adapter by
  adapter.  Status polling keeps its existing site-specific cadence.
* HLS playlist retrieval: a short request that is safe to retry with a small
  budget and a finite read timeout.
* HLS segment transfer: a short request that is safe to retry.  Downloaders
  must buffer a complete body before writing, so a retried transfer can never
  duplicate bytes in the output file.
* Long-lived streaming: ffmpeg and the Agora WebRTC worker own their process
  lifetime and deadlines.  A short generic timeout must never be applied to
  them.

Every helper here is deliberately independent of the HTTP library.  A
``curl_cffi`` session and a plain ``requests`` session both work as long as
they expose ``get(url, headers=..., cookies=..., timeout=...)`` and a
``status_code``/``content``/``text`` response.
"""

from __future__ import annotations

import email.utils
import random
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

__all__ = [
    "RetryPolicy",
    "TransferCancelled",
    "TransportError",
    "fetch",
    "redact_headers",
    "redact_url",
    "retry_after_seconds",
]


class TransportError(Exception):
    """A request failed after the retry budget was exhausted.

    ``retryable`` records whether the failure looked transient, which lets
    callers distinguish a dead stream from an authentication or URL error.
    """

    def __init__(
        self,
        message: str,
        *,
        url: Any = None,
        status: int | None = None,
        retryable: bool = False,
        attempts: int = 1,
    ) -> None:
        super().__init__(message)
        self.url = redact_url(url) if url is not None else None
        self.status = status
        self.retryable = retryable
        self.attempts = attempts

    def __str__(self) -> str:
        base = super().__str__()
        suffix = []
        if self.status is not None:
            suffix.append(f"HTTP {self.status}")
        if self.attempts > 1:
            suffix.append(f"{self.attempts} attempts")
        if suffix:
            return f"{base} ({', '.join(suffix)})"
        return base


class TransferCancelled(TransportError):
    """The caller asked to stop while a request or backoff was in progress."""

    def __init__(self, *, url: Any = None) -> None:
        super().__init__("transfer cancelled", url=url, retryable=False)


@dataclass(frozen=True)
class RetryPolicy:
    """Finite retry budget with capped exponential backoff.

    ``timeout`` is passed straight to the HTTP session.  Use a
    ``(connect, read)`` tuple so a stalled socket cannot block forever.
    """

    attempts: int = 3
    timeout: float | tuple[float, float] = (10.0, 60.0)
    base_delay: float = 0.5
    max_delay: float = 8.0
    jitter: float = 0.25
    retry_statuses: frozenset[int] = field(default_factory=lambda: frozenset({408, 425, 429, 500, 502, 503, 504}))

    def delay_for(self, attempt: int) -> float:
        delay = min(self.max_delay, self.base_delay * (2 ** (attempt - 1)))
        if self.jitter:
            delay += random.uniform(0, delay * self.jitter)
        return delay


# Header values that must never reach a log file or the web UI.
_SENSITIVE_HEADERS = {
    "authorization",
    "cookie",
    "proxy-authorization",
    "set-cookie",
    "x-api-key",
}

# Query parameters that carry credentials or signatures in signed media URLs.
_SENSITIVE_PARAMS = {
    "access_token",
    "api_key",
    "apikey",
    "auth",
    "authorization",
    "expires",
    "hash",
    "hdntl",
    "hdnts",
    "key",
    "pkey",
    "policy",
    "psch",
    "pdkey",
    "sig",
    "signature",
    "token",
}

# Exceptions that are worth retrying even when the HTTP library differs.
_RETRYABLE_EXCEPTION_NAMES = {
    "ChunkedEncodingError",
    "ConnectTimeout",
    "ConnectionError",
    "ConnectionResetError",
    "ConnectionRefusedError",
    "IncompleteRead",
    "ProtocolError",
    "ReadTimeout",
    "RemoteDisconnected",
    "Timeout",
    "TimeoutError",
}
# Exceptions that mean the request is wrong and will stay wrong.
_PERMANENT_EXCEPTION_NAMES = {
    "InvalidHeader",
    "InvalidSchema",
    "InvalidURL",
    "MissingSchema",
    "TooManyRedirects",
    "SSLError",
    "SSLCertVerificationError",
}


def redact_url(url: Any, *, max_length: int = 300) -> str:
    """Return a log-safe form of ``url``.

    Userinfo, query strings and sensitive query values are removed or masked.
    Signed CDN URLs are common in this project, so a log line must never leak
    a token or a signing key.
    """
    if url is None:
        return ""
    raw = str(url)
    try:
        parts = urlsplit(raw)
    except ValueError:
        return "<invalid-url>"

    netloc = parts.netloc
    if "@" in netloc:
        host = netloc.rsplit("@", 1)[1]
        netloc = "***@" + host

    query_pairs = []
    for name, value in parse_qsl(parts.query, keep_blank_values=True):
        if name.lower() in _SENSITIVE_PARAMS:
            value = "***"
        query_pairs.append((name, value))
    query = urlencode(query_pairs, doseq=True)

    # Fragments are never needed for diagnostics and can carry tokens too.
    redacted = urlunsplit((parts.scheme, netloc, parts.path, query, ""))
    if len(redacted) > max_length:
        redacted = redacted[:max_length] + "..."
    return redacted


def redact_headers(headers: Any) -> dict[str, str]:
    """Return headers safe to include in diagnostics."""
    if not headers:
        return {}
    try:
        items = headers.items()
    except AttributeError:
        return {"headers": "<unreadable>"}
    return {str(name): ("***" if str(name).lower() in _SENSITIVE_HEADERS else str(value)) for name, value in items}


def _content_length(response: Any) -> int | None:
    headers = getattr(response, "headers", None)
    if not headers:
        return None
    try:
        # A decompressing client returns the expanded body, so the declared
        # length no longer describes it.
        encoding = headers.get("Content-Encoding")
        if encoding and str(encoding).lower() != "identity":
            return None
        value = headers.get("Content-Length")
    except AttributeError:
        return None
    if value in (None, ""):
        return None
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return None


def retry_after_seconds(response: Any) -> float | None:
    """Parse a ``Retry-After`` header into a non-negative delay."""
    headers = getattr(response, "headers", None)
    if not headers:
        return None
    try:
        value = headers.get("Retry-After")
    except AttributeError:
        return None
    if not value:
        return None
    value = str(value).strip()
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    try:
        when = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return max(0.0, (when - datetime.now(timezone.utc)).total_seconds())


def _exception_is_retryable(exc: BaseException) -> bool:
    name = type(exc).__name__
    if name in _PERMANENT_EXCEPTION_NAMES:
        return False
    if name in _RETRYABLE_EXCEPTION_NAMES:
        return True
    if isinstance(exc, (TimeoutError, ConnectionError)):
        return True
    # Many HTTP wrappers (including pycurl_requests) raise their own error
    # class for socket failures.  Retry unknown request errors within the
    # small budget; truly malformed URLs are listed as permanent above.
    return True


def _status_is_retryable(status: int, policy: RetryPolicy) -> bool:
    if status in policy.retry_statuses:
        return True
    return 500 <= status < 600


def _close(response: Any) -> None:
    close = getattr(response, "close", None)
    if callable(close):
        try:
            close()
        except Exception:  # noqa: BLE001 - cleanup must never mask the error
            pass


def _wait(policy: RetryPolicy, attempt: int, cancel: threading.Event | None) -> None:
    delay = policy.delay_for(attempt)
    if cancel is not None:
        if cancel.wait(delay):
            raise TransferCancelled()
    elif delay > 0:
        time.sleep(delay)


def fetch(
    session: Any,
    url: str,
    *,
    headers: Any = None,
    cookies: Any = None,
    policy: RetryPolicy | None = None,
    cancel: threading.Event | None = None,
    logger: Any = None,
    as_text: bool = False,
) -> Any:
    """GET ``url`` with a finite retry budget and return its complete body.

    The body is read (and therefore validated for truncation) before this
    function returns.  Callers can safely treat the result as one complete
    segment and only then write it to disk.
    """
    policy = policy or RetryPolicy()
    attempts = max(1, int(policy.attempts))
    last_message = "request failed"

    for attempt in range(1, attempts + 1):
        if cancel is not None and cancel.is_set():
            raise TransferCancelled(url=url)

        try:
            response = session.get(
                url,
                headers=headers,
                cookies=cookies,
                timeout=policy.timeout,
            )
        except Exception as exc:  # noqa: BLE001 - classified below
            retryable = _exception_is_retryable(exc)
            last_message = f"{type(exc).__name__}: {exc}"
            if not retryable or attempt >= attempts:
                raise TransportError(
                    f"request failed for {redact_url(url)}: {last_message}",
                    url=url,
                    retryable=retryable,
                    attempts=attempt,
                ) from exc
            if logger is not None:
                logger.debug(f"Retrying {redact_url(url)} after {last_message} (attempt {attempt}/{attempts})")
            _wait(policy, attempt, cancel)
            continue

        status = int(getattr(response, "status_code", 200) or 200)
        if 200 <= status < 300:
            try:
                raw = response.content
                if not isinstance(raw, (bytes, bytearray)):
                    raw = str(raw).encode("utf-8", "replace")
                expected = _content_length(response)
                if expected is not None and len(raw) != expected:
                    raise IOError(f"truncated body ({len(raw)} of {expected} bytes)")
                body = response.text if as_text else bytes(raw)
            except Exception as exc:  # noqa: BLE001 - truncated body
                retryable = _exception_is_retryable(exc)
                last_message = f"{type(exc).__name__}: {exc}"
                _close(response)
                if not retryable or attempt >= attempts:
                    raise TransportError(
                        f"could not read response body for {redact_url(url)}: {last_message}",
                        url=url,
                        status=status,
                        retryable=retryable,
                        attempts=attempt,
                    ) from exc
                if logger is not None:
                    logger.debug(f"Retrying {redact_url(url)} after a truncated body (attempt {attempt}/{attempts})")
                _wait(policy, attempt, cancel)
                continue
            return body

        retryable = _status_is_retryable(status, policy)
        retry_after = retry_after_seconds(response)
        _close(response)
        if not retryable or attempt >= attempts:
            raise TransportError(
                f"request failed for {redact_url(url)}",
                url=url,
                status=status,
                retryable=retryable,
                attempts=attempt,
            )
        if logger is not None:
            logger.debug(f"Retrying {redact_url(url)} after HTTP {status} (attempt {attempt}/{attempts})")
        if retry_after is not None:
            # Honour the server's Retry-After, but keep the wait bounded by the
            # policy so a malicious or broken header cannot stall a bot.
            delay = min(policy.max_delay, retry_after) if policy.max_delay else retry_after
            if cancel is not None:
                if cancel.wait(delay):
                    raise TransferCancelled(url=url)
            elif delay > 0:
                time.sleep(delay)
        else:
            _wait(policy, attempt, cancel)

    raise TransportError(
        f"request failed for {redact_url(url)}: {last_message}",
        url=url,
        retryable=True,
        attempts=attempts,
    )
