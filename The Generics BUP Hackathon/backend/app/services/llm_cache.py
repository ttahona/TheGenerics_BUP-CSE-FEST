"""Lightweight content-hashed LRU cache for LLM JSON responses.

The cache is transparent: callers do not see a difference between a cached hit
and a fresh response, except that a hit avoids the upstream HTTP call. Cache
keys are derived from the active provider, model, and the tuple of operator
notes (so two requests with identical notes reuse one upstream call).

This is purely an optimization — never a source of truth. Guardrails always
re-validate cached output before it reaches the optimizer, matching the
canonical contract that LLM output is "untrusted structured data until
deterministic validation passes" (Problem Statement Section 08).
"""
from __future__ import annotations

import hashlib
import threading
import time
from collections import OrderedDict
from typing import Any, Dict, List, Optional, Tuple

from ..config import settings

_CACHE: "OrderedDict[str, Tuple[float, List[Dict[str, Any]]]]" = OrderedDict()
_LOCK = threading.Lock()
_MAX_ENTRIES = 256
_TTL_SECONDS = 900.0  # 15 minutes


def _key(provider: str, model: str, notes: List[str]) -> str:
    h = hashlib.sha256()
    h.update(provider.encode("utf-8"))
    h.update(b"\x00")
    h.update(model.encode("utf-8"))
    h.update(b"\x00")
    for note in notes:
        h.update(note.strip().lower().encode("utf-8"))
        h.update(b"\x01")
    return h.hexdigest()


def get(provider: str, model: str, notes: List[str]) -> Optional[List[Dict[str, Any]]]:
    """Return a cached response if one is fresh; otherwise None."""
    if not settings.enable_llm_cache:
        return None
    key = _key(provider, model, notes)
    now = time.monotonic()
    with _LOCK:
        entry = _CACHE.get(key)
        if entry is None:
            return None
        ts, value = entry
        if now - ts > _TTL_SECONDS:
            _CACHE.pop(key, None)
            return None
        _CACHE.move_to_end(key)
        return value


def put(provider: str, model: str, notes: List[str], value: List[Dict[str, Any]]) -> None:
    """Store a response in the cache."""
    if not settings.enable_llm_cache:
        return
    key = _key(provider, model, notes)
    now = time.monotonic()
    with _LOCK:
        _CACHE[key] = (now, value)
        _CACHE.move_to_end(key)
        while len(_CACHE) > _MAX_ENTRIES:
            _CACHE.popitem(last=False)


def stats() -> Dict[str, int]:
    """Return cache size (used by the diagnostics endpoint)."""
    with _LOCK:
        return {"entries": len(_CACHE), "max_entries": _MAX_ENTRIES}


def clear() -> None:
    """Drop all cached responses (used by tests)."""
    with _LOCK:
        _CACHE.clear()
