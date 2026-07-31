"""Tiny stdlib HTTP client for the cueclock server — no third-party deps.

Every call swallows transport errors into return values: the daemon must keep
running (and rendering "server down" faces) whether or not `cueclock run` is up.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass


@dataclass(frozen=True)
class PostResult:
    ok: bool
    status: int  # HTTP status; 0 = transport error (server down)
    detail: str  # response body or error text


def post(base: str, path: str, timeout: float = 3.0) -> PostResult:
    req = urllib.request.Request(base + path, method="POST")
    try:
        # B310: base is the cueclock server's http:// URL (--server), never file:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # nosec B310
            return PostResult(True, resp.status, resp.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as exc:
        return PostResult(False, exc.code, exc.read().decode("utf-8", "replace"))
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return PostResult(False, 0, str(exc))


def get_json(base: str, path: str, timeout: float = 1.5) -> dict[str, object] | None:
    """Decoded JSON object, or None when the server is down / the body isn't a dict."""
    try:
        with urllib.request.urlopen(base + path, timeout=timeout) as resp:  # nosec B310
            data = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def active_rulesets(base: str) -> tuple[str, list[str]] | None:
    """(active, available) from /api/rulesets, or None when the server is down."""
    data = get_json(base, "/api/rulesets")
    if data is None:
        return None
    active = data.get("active")
    available = data.get("available")
    if not isinstance(active, str) or not isinstance(available, list):
        return None
    return active, [name for name in available if isinstance(name, str)]
