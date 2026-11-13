"""Shared chat pacing and upstream-failure classification primitives."""

from __future__ import annotations

import re
from typing import Any, Mapping


FAILURE_QUOTA_EXHAUSTED = "quota-exhausted"
FAILURE_RATE_LIMIT = "rate-limit"
FAILURE_AUTH_DEAD = "auth-dead"
FAILURE_TRANSIENT = "transient"


def retry_after_seconds(value: Any) -> float | None:
    """Parse the numeric Retry-After form used by Trae gateways."""

    if value is None:
        return None
    try:
        parsed = float(str(value).strip())
    except (TypeError, ValueError):
        return None
    return max(0.0, parsed)


def classify_upstream_failure(
    error: BaseException | str | None = None,
    *,
    status_code: Any = None,
    headers: Mapping[str, Any] | None = None,
) -> str | None:
    """Classify only failures that warrant account-level action.

    Ordinary caller mistakes (400/404/422) deliberately return ``None`` and
    therefore can never park an account.
    """

    if status_code is None:
        status_code = getattr(error, "status_code", None)
    try:
        status = int(status_code) if status_code is not None else 0
    except (TypeError, ValueError):
        status = 0
    code = str(getattr(error, "code", "") or "").strip().lower()
    text = " ".join(
        str(value or "")
        for value in (error, code)
    ).lower()

    if status == 401 or code in {"401", "invalid_token", "token_invalid"} or any(
        phrase in text
        for phrase in ("invalid token", "token invalid", "unauthorized", "not able to authenticate")
    ):
        return FAILURE_AUTH_DEAD
    if code in {"4008", "quota_exhausted", "quota-exhausted"} or any(
        phrase in text
        for phrase in ("quota exhausted", "credits exhausted", "insufficient credits", "requests have exceeded the quota")
    ):
        return FAILURE_QUOTA_EXHAUSTED
    if status == 429 or code in {"429", "3004", "4011", "9074"} or any(
        phrase in text
        for phrase in ("rate limit", "rate-limit", "too many requests", "too many users")
    ):
        return FAILURE_RATE_LIMIT
    if status >= 500 or any(
        phrase in text
        for phrase in ("network", "connection", "timed out", "timeout", "temporarily unavailable")
    ):
        return FAILURE_TRANSIENT
    # Never punish an account for a normal client-side 4xx response.
    if 400 <= status < 500:
        return None
    return None


def cooldown_seconds(
    failure: str | None,
    *,
    retry_after: Any = None,
    consecutive_transient: int = 0,
) -> float:
    """Return the tiered account cooldown for one classified failure."""

    if failure == FAILURE_QUOTA_EXHAUSTED:
        return 30.0 * 60.0
    if failure == FAILURE_RATE_LIMIT:
        advertised = retry_after_seconds(retry_after)
        return max(60.0, advertised or 0.0)
    if failure == FAILURE_AUTH_DEAD:
        return float("inf")
    if failure == FAILURE_TRANSIENT:
        return min(120.0, 5.0 * (2 ** max(0, consecutive_transient)))
    return 0.0


def retry_after_from_error(error: Any) -> float | None:
    """Extract Retry-After from an httpx/curl response or exception."""

    headers = getattr(error, "headers", None)
    response = getattr(error, "response", None)
    if headers is None and response is not None:
        headers = getattr(response, "headers", None)
    if isinstance(headers, Mapping):
        return retry_after_seconds(
            headers.get("Retry-After") or headers.get("retry-after")
        )
    match = re.search(r"retry[- ]after\s*[=:]?\s*(\d+(?:\.\d+)?)", str(error or ""), re.I)
    return retry_after_seconds(match.group(1)) if match else None
