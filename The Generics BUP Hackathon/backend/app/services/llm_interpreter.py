"""Interpret operator notes into structured directives matching Problem Statement Section 4.1.

Pipeline:
1. If an LLM provider is configured (OpenAI, Google Gemini, Anthropic) and
   `LLM_PROVIDER != "off"`, call the LLM with a strict JSON schema.
2. Otherwise (or on any LLM failure), fall back to a robust rule-based
   interpreter that handles whole-hour intervals, percentages, fractions,
   and operational keywords.
3. Both paths pass through deterministic guardrails ensuring hours in [0..23],
   factor in [0..1], applies semantics, and valid numeric bounds.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any, Dict, List, Optional, Tuple

from ..config import active_llm_provider, settings
from ..schemas.energy import BatterySpec
from . import llm_cache
from .guardrails import GuardrailError, validate_directive_dict, validate_directives


def _timeout() -> float:
    """Per-call upstream timeout (seconds)."""
    return float(getattr(settings, "llm_response_timeout_seconds", 12.0))

logger = logging.getLogger(__name__)


SYSTEM_PROMPT = """You are an expert energy-grid operator-note interpreter for the BUP Hackathon GridWise system.
Convert each natural-language operator note into a structured machine-checkable directive JSON object.

Allowed directive types and their required structured_adjustment shape:
1) solar_reduction        : {"directive_type": "solar_reduction", "applies": true, "structured_adjustment": {"hours": [int,...], "factor": float in [0.0, 1.0]}, "explanation": "..."}
2) minimum_battery_reserve: {"directive_type": "minimum_battery_reserve", "applies": true, "structured_adjustment": {"hours": [int,...], "minimum_energy_kwh": float >= 0}, "explanation": "..."}
3) no_charge_window       : {"directive_type": "no_charge_window", "applies": true, "structured_adjustment": {"hours": [int,...]}, "explanation": "..."}
4) no_discharge_window    : {"directive_type": "no_discharge_window", "applies": true, "structured_adjustment": {"hours": [int,...]}, "explanation": "..."}
5) max_grid_window        : {"directive_type": "max_grid_window", "applies": true, "structured_adjustment": {"hours": [int,...], "max_grid_kwh": float >= 0}, "explanation": "..."}
6) no_op                  : {"directive_type": "no_op", "applies": false, "structured_adjustment": null, "explanation": "..."}

Rules:
- Whole-hour convention: Start hour included, end hour excluded.
  - "1 PM to 3 PM" or "1-3 PM" -> hours [13, 14]
  - "between 6 PM and 9 PM" -> hours [18, 19, 20]
  - "noon until 2 PM" -> hours [12, 13]
  - "2 AM until 5 AM" -> hours [2, 3, 4]
  - "18:00 to 21:00" -> hours [18, 19, 20]
  - "one until three" (in daytime solar context) -> hours [13, 14]
- factor is the usable fraction remaining:
  - "80% reduction" or "reduced by 80%" or "drop by 80%" -> factor 0.20
  - "25% of forecast" or "a quarter" -> factor 0.25
  - "reduced to 20%" or "drop to about 20%" -> factor 0.20
  - "roughly one-fifth" -> factor 0.20
  - "cut by 50%" or "half" -> factor 0.50
- "Keep / hold at least X kWh" or "minimum reserve X kWh" -> minimum_battery_reserve with minimum_energy_kwh = X. If hours not specified, include all hours [0..23].
- "charger isolated", "charging circuit unavailable", "charging disabled", "do not charge", "avoid charging" -> no_charge_window.
- "grid intake must stay at or below X kWh", "cap grid import at X kWh", "limit grid draw to at most X kWh" -> max_grid_window with max_grid_kwh = X.
- Irrelevant notes (e.g. cafeteria menu, weather unrelated to solar) MUST be no_op with applies: false and structured_adjustment: null.
- Return ONLY JSON of shape: {"directives": [ ... ]} with exactly ONE directive per note in note_index order (0, 1, ...).
"""


# --------------------------------------------------------------------------- #
# LLM Providers (OpenAI, Gemini, Anthropic)
# --------------------------------------------------------------------------- #


def _call_openai(notes: List[str]) -> List[Dict[str, Any]]:
    import httpx

    user_msg = "Operator notes:\n" + "\n".join(
        f"{i}. {n}" for i, n in enumerate(notes)
    )
    headers = {
        "Authorization": f"Bearer {settings.openai_api_key}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": settings.openai_model,
        "response_format": {"type": "json_object"},
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_msg + "\n\nReturn JSON of shape {\"directives\": [...]}.\n"},
        ],
        "temperature": 0.0,
    }
    with httpx.Client(timeout=_timeout()) as client:
        resp = client.post("https://api.openai.com/v1/chat/completions", headers=headers, json=payload)
        resp.raise_for_status()
        data = resp.json()
        content = data["choices"][0]["message"]["content"]
        parsed = json.loads(content)
        if isinstance(parsed, dict) and "directives" in parsed:
            return parsed["directives"]
        if isinstance(parsed, list):
            return parsed
        raise GuardrailError("LLM output missing 'directives' array")


def _call_gemini(notes: List[str]) -> List[Dict[str, Any]]:
    import httpx

    user_msg = SYSTEM_PROMPT + "\n\nOperator notes:\n" + "\n".join(
        f"{i}. {n}" for i, n in enumerate(notes)
    ) + "\n\nReturn JSON of shape {\"directives\": [...]}.\n"

    url = f"https://generativelanguage.googleapis.com/v1beta/models/{settings.gemini_model}:generateContent?key={settings.gemini_api_key}"
    payload = {
        "contents": [{"parts": [{"text": user_msg}]}],
        "generationConfig": {"temperature": 0.0, "responseMimeType": "application/json"},
    }
    with httpx.Client(timeout=_timeout()) as client:
        resp = client.post(url, json=payload)
        resp.raise_for_status()
        data = resp.json()
        text = data["candidates"][0]["content"]["parts"][0]["text"]
        parsed = json.loads(text)
        if isinstance(parsed, dict) and "directives" in parsed:
            return parsed["directives"]
        if isinstance(parsed, list):
            return parsed
        raise GuardrailError("Gemini response missing 'directives' array")


def _call_anthropic(notes: List[str]) -> List[Dict[str, Any]]:
    import httpx

    user_msg = "Operator notes:\n" + "\n".join(
        f"{i}. {n}" for i, n in enumerate(notes)
    ) + "\n\nReturn JSON of shape {\"directives\": [...]}.\n"

    headers = {
        "x-api-key": settings.anthropic_api_key,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }
    payload = {
        "model": settings.anthropic_model,
        "max_tokens": 1024,
        "system": SYSTEM_PROMPT,
        "messages": [{"role": "user", "content": user_msg}],
        "temperature": 0.0,
    }
    with httpx.Client(timeout=_timeout()) as client:
        resp = client.post("https://api.anthropic.com/v1/messages", headers=headers, json=payload)
        resp.raise_for_status()
        data = resp.json()
        text = data["content"][0]["text"]
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if match:
            text = match.group(0)
        parsed = json.loads(text)
        if isinstance(parsed, dict) and "directives" in parsed:
            return parsed["directives"]
        if isinstance(parsed, list):
            return parsed
        raise GuardrailError("Anthropic output missing 'directives' array")


def _call_llm(notes: List[str]) -> List[Dict[str, Any]]:
    provider = active_llm_provider()
    if provider == "openai":
        model = settings.openai_model
        cached = llm_cache.get(provider, model, notes)
        if cached is not None:
            return cached
        result = _call_openai(notes)
        llm_cache.put(provider, model, notes, result)
        return result
    if provider == "gemini":
        model = settings.gemini_model
        cached = llm_cache.get(provider, model, notes)
        if cached is not None:
            return cached
        result = _call_gemini(notes)
        llm_cache.put(provider, model, notes, result)
        return result
    if provider == "anthropic":
        model = settings.anthropic_model
        cached = llm_cache.get(provider, model, notes)
        if cached is not None:
            return cached
        result = _call_anthropic(notes)
        llm_cache.put(provider, model, notes, result)
        return result
    raise ValueError(f"No active LLM provider configured (provider={provider})")


# --------------------------------------------------------------------------- #
# Enhanced Rule-Based NLP Fallback
# --------------------------------------------------------------------------- #

_HOUR_PATTERNS = [
    re.compile(r"(?:from\s+)?(?P<a1>\d{1,2})(?::\d{2})?\s*(?P<ap1>am|pm)?\s*(?:until|to|through|till|-|–|—)\s*(?P<a2>\d{1,2})(?::\d{2})?\s*(?P<ap2>am|pm)?", re.IGNORECASE),
    re.compile(r"(?:between\s+)?(?P<a1>\d{1,2})(?::\d{2})?\s*(?P<ap1>am|pm)?\s*(?:and|to|until|-|–|—)\s*(?P<a2>\d{1,2})(?::\d{2})?\s*(?P<ap2>am|pm)?", re.IGNORECASE),
    re.compile(r"(?P<a1>\d{1,2})\s*[-–—]\s*(?P<a2>\d{1,2})\s*(?P<ap2>am|pm)", re.IGNORECASE),
    re.compile(r"(?:hours?\s+(?P<b1>\d{1,2})\s*[-to]+\s*(?P<b2>\d{1,2}))", re.IGNORECASE),
    re.compile(r"(?:(?P<a1>one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve)\s*(?:until|to|-)\s*(?P<a2>one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve))", re.IGNORECASE),
]

_WORD_TO_NUM = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
    "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12,
}


def _to_24h(hour_val: int | str, ampm: str | None, is_solar: bool = False) -> int:
    if isinstance(hour_val, str) and hour_val.lower() in _WORD_TO_NUM:
        h = _WORD_TO_NUM[hour_val.lower()]
        if is_solar and 1 <= h <= 6 and not ampm:
            ampm = "pm"
    else:
        h = int(hour_val) % 24
        if is_solar and 1 <= h <= 6 and not ampm:
            ampm = "pm"
    if not ampm:
        return h % 24
    ampm = ampm.lower()
    if ampm == "am":
        return 0 if h == 12 else h
    if ampm == "pm":
        return 12 if h == 12 else h + 12
    return h


def _preprocess_text(text: str) -> str:
    t = text.lower()
    t = re.sub(r"\bnoon\b", "12pm", t)
    t = re.sub(r"\bmidday\b", "12pm", t)
    t = re.sub(r"\bmidnight\b", "12am", t)
    return t


def _extract_hours(raw_text: str) -> List[int]:
    """Extract canonical hour list from an operator note.

    Supports:
    - ranges with inclusive end: "from X to Y", "X-Y", "X through Y", "between X and Y"
    - single hours: "at hour X", "hour X"
    - comma lists: "X, Y, and Z", "hours X, Y, Z", "during 18, 19, 20"
    - AM/PM markers and 24-hour clock (12:00, 14:00)
    - numeric words (one..twelve)
    - inclusive end markers: "(inclusive)", "and including"
    - solar-context auto-PM heuristic (1..6 → PM)
    - explicit clock-times like "14:00" are treated as the start of that hour,
      so a range "12:00 to 14:00" covers hours 12 and 13 only.
    """
    text = _preprocess_text(raw_text)
    is_solar = any(k in text for k in ["solar", "panel", "pv", "sun", "rooftop", "washing", "clean"])
    hours: List[int] = []
    seen = set()

    def _add_range(a: int, b: int, inclusive_end: bool) -> None:
        lo, hi = sorted((a, b))
        # If inclusive_end, both lo and hi are included; otherwise end-exclusive.
        end = hi if inclusive_end else max(lo, hi - 1)
        for h in range(lo, end + 1):
            if 0 <= h <= 23 and h not in seen:
                seen.add(h)
                hours.append(h)

    # Mask out numbers that belong to a quantity like "6 kWh" or "25 percent"
    # so they don't get picked up as hour references by the range patterns.
    masked = re.sub(r"\b(\d+(?:\.\d+)?)\s*(kwh|kilowatt-hour|kilowatt|kw|percent|%)\b",
                    lambda m: " " * len(m.group(0)), text, flags=re.IGNORECASE)

    # Keep an untouched copy for range matching — we DON'T want "between X
    # and Y" or "X through Y" patterns to lose their range separator when we
    # normalize "and"/"," separators later for list matching.
    range_masked = masked

    # Range patterns. Each returns (start, end, has_clock_time, ampm).
    # A clock time like "14:00" means the start of hour 14, so the range is
    # naturally end-exclusive at that hour.
    # NOTE: text has already been lowercased and "noon"/"midnight" replaced
    # with "12pm"/"12am" by _preprocess_text, so "pm" / "am" suffixes are
    # what we expect.
    range_patterns = [
        # "from X to/through Y" with optional am/pm on either endpoint.
        # Accepts optional "hour" on either side and optional leading
        # preposition (at/from/for).
        re.compile(
            r"(?:\b(?:at|from|for)\s+)?(?:hours?\s+)?(\d{1,2})(?::(\d{2}))?\s*(am|pm)?\s*"
            r"(?:through|till|to|until|up\s+to)\s*"
            r"(?:hours?\s+)?(\d{1,2})(?::(\d{2}))?\s*(am|pm)?",
            re.IGNORECASE,
        ),
        # "between X and Y" / "between X to Y" / "between hours X and Y"
        re.compile(
            r"between\s+(?:hours?\s+)?(\d{1,2})(?::(\d{2}))?\s*(am|pm)?\s*"
            r"(?:and|to|until)\s*"
            r"(?:hours?\s+)?(\d{1,2})(?::(\d{2}))?\s*(am|pm)?",
            re.IGNORECASE,
        ),
        # "X-Y" or "X to Y" (with optional am/pm on the second)
        re.compile(
            r"(?<!\d)(\d{1,2})(?::(\d{2}))?\s*(am|pm)?\s*(?:-|–|—|to|until|through)\s*(\d{1,2})(?::(\d{2}))?\s*(am|pm)?\b",
            re.IGNORECASE,
        ),
        # "hours X to Y" (fallback for hours-only ranges)
        re.compile(
            r"hours?\s+(\d{1,2})\s*(?:-|to|until|through)\s*(\d{1,2})",
            re.IGNORECASE,
        ),
    ]

    # Explicit inclusive marker anywhere in the text overrides clock-time logic.
    explicit_inclusive = bool(
        re.search(r"\(\s*inclusive\s*\)|and\s+including|\binclusive\b", text)
    )
    # Explicit exclusive marker.
    explicit_exclusive = bool(
        re.search(r"\(\s*exclusive\s*\)|\bexclusive\b(?=\s|$)", text)
    )

    for pat in range_patterns:
        for m in pat.finditer(range_masked):
            groups = m.groups()
            n = len(groups)
            # Pattern 3 (X-Y, X to Y): 6 groups (a, a_min, a_ampm, b, b_min, b_ampm)
            # Pattern 4 (hours X to Y): 2 groups (a, b)
            # Patterns 1 & 2: 5 groups (a, a_min, a_ampm, b, b_ampm) (no b_min)
            if n == 6:
                a_raw, a_min, a_ampm, b_raw, b_min, b_ampm = groups
            elif n == 2:
                a_raw, b_raw = groups[0], groups[1]
                a_min = b_min = a_ampm = b_ampm = None
            elif n == 5:
                a_raw, a_min, a_ampm, b_raw, b_ampm = groups
                b_min = None
            else:
                a_raw = groups[0]
                a_min = groups[1] if n > 1 else None
                a_ampm = groups[2] if n > 2 else None
                b_raw = groups[3] if n > 3 else groups[0]
                b_min = groups[4] if n > 4 else None
                b_ampm = groups[5] if n > 5 else None

            try:
                a = int(a_raw)
                b = int(b_raw)
            except (ValueError, TypeError):
                continue
            # Convert ampm if needed (e.g., "6 PM" -> 18, "2 PM" -> 14).
            a_24 = _to_24h(a, a_ampm, is_solar)
            b_24 = _to_24h(b, b_ampm, is_solar)
            if not (0 <= a_24 <= 23 and 0 <= b_24 <= 23):
                continue

            # Decide end inclusivity:
            # - if explicit marker wins, use it
            # - if right endpoint has ":00" / ":30" minutes (clock-time),
            #   the right endpoint is the START of that hour, so the range
            #   is naturally end-exclusive at that hour.
            # - if right endpoint has "am"/"pm" suffix (without minutes),
            #   the convention is also end-exclusive: "9 PM" -> 21, so the
            #   range covers up to (but not including) hour 21.
            # - if no clock or ampm info (e.g. "from 18 through 20"), use
            #   whole-hour inclusive semantics.
            if explicit_inclusive:
                inc = True
            elif explicit_exclusive:
                inc = False
            elif b_min == "00" or b_min == "30" or b_ampm:
                inc = False
            else:
                inc = True

            _add_range(a_24, b_24, inclusive_end=inc)

    # Single-hour mentions: "hour 18", "at 7 pm"
    for m in re.finditer(r"\b(?:at|hour)\s*(\d{1,2})\s*(am|pm)?\b", masked, re.IGNORECASE):
        h = _to_24h(int(m.group(1)), m.group(2), is_solar)
        if h not in seen and 0 <= h <= 23:
            seen.add(h)
            hours.append(h)

        # Normalize "X and Y" / "X, and Y" between plain digits to "X, Y" so a
    # simple comma-list regex can match the full sequence (Python's re engine
    # can't backtrack through "and" mixed with commas in a single nested
    # quantifier). Skip sequences where the digit is followed by ":NN" clock
    # minutes, e.g. "12:00 and 14:00" must NOT be merged into "12, 14:00".
    prev = None
    while prev != masked:
        prev = masked
        masked = re.sub(r"(\d{1,2})(?!\s*:)\s*,\s*and\s+(\d{1,2})(?!\s*:)", r"\1, \2", masked)
        masked = re.sub(r"(\d{1,2})(?!\s*:)\s+and\s+(\d{1,2})(?!\s*:)", r"\1, \2", masked)

    # Comma-separated hour lists preceded by a keyword: "hours 18, 19, 20",
    # "for hours 18, 19, and 20", "during 18, 19, 20".
    list_pattern = re.compile(
        r"\b(?:hours?|for|during|at|on)\s+(?:hours?\s+)?"
        r"(\d{1,2}(?:\s*,\s*\d{1,2})+)",
        re.IGNORECASE,
    )
    for m in list_pattern.finditer(masked):
        nums = re.findall(r"\d{1,2}", m.group(1))
        for n in nums:
            try:
                h = int(n)
            except ValueError:
                continue
            if 0 <= h <= 23 and h not in seen:
                seen.add(h)
                hours.append(h)

    # Comma-separated hour lists without a preceding keyword: "18, 19, and 20"
    # Only accept when 3+ numbers and none already seen (avoid catching unrelated
    # digits).
    if not hours:
        m = re.search(r"((?:\d{1,2}\s*,\s*)+(?:and\s+)?\d{1,2})", masked)
        if m:
            nums = re.findall(r"\d{1,2}", m.group(1))
            if 3 <= len(nums) <= 6 and all(0 <= int(n) <= 23 for n in nums):
                for n in nums:
                    h = int(n)
                    if h not in seen:
                        seen.add(h)
                        hours.append(h)

    # "hours X and Y" enumeration: "for hours 12 and 13", "hours 18 and 20".
    # We want to add the standalone hours that were missed when the range
    # pattern only captured one side.
    if not hours:
        for m in re.finditer(
            r"\b(?:hours?|for)\s+(?:hours?\s+)?(\d{1,2})\s+and\s+(\d{1,2})\b",
            masked,
            re.IGNORECASE,
        ):
            try:
                a = int(m.group(1))
                b = int(m.group(2))
            except ValueError:
                continue
            if 0 <= a <= 23 and a not in seen:
                seen.add(a)
                hours.append(a)
            if 0 <= b <= 23 and b not in seen:
                seen.add(b)
                hours.append(b)

    if hours:
        return sorted(hours)

    presets = {
        "morning": list(range(6, 11)),
        "forenoon": list(range(8, 12)),
        "afternoon": list(range(12, 17)),
        "evening": list(range(17, 22)),
        "night": list(range(21, 24)) + list(range(0, 5)),
        "peak": list(range(17, 22)),
    }
    for k, v in presets.items():
        if k in text:
            return v
    return []


def _match_percent(text: str) -> float | None:
    lowered = text.lower()
    # "80% reduction" / "cut by 80%" -> factor 0.20
    m_red = re.search(r"(\d+(?:\.\d+)?)\s*%\s*(?:reduction|cut|drop|decrease|curtailment)", lowered)
    if m_red:
        return max(0.0, min(1.0, 1.0 - float(m_red.group(1)) / 100.0))
    # "reduce by 75%" / "cut by 80%" / "drop by 80%" -> factor (1 - X/100)
    # Use loose matching so stems like "reduce", "drop", "curtail" all hit.
    m_red_by = re.search(
        r"\b(?:reduce|reduc|curtail|cut|drop|down)\b[^.\d%]*?\bby\b[^.\d%]*?(\d+(?:\.\d+)?)\s*%",
        lowered,
    )
    if m_red_by:
        return max(0.0, min(1.0, 1.0 - float(m_red_by.group(1)) / 100.0))
    # "to 25%" / "to ~25%" / "at 25%" -> factor = X/100
    m_to = re.search(r"\b(?:to|at)\s*~?\s*(\d+(?:\.\d+)?)\s*(?:%|percent)\b", lowered)
    if m_to:
        return max(0.0, min(1.0, float(m_to.group(1)) / 100.0))
    # "25 percent of forecast/rated/value" -> factor = X/100
    m_pct_of = re.search(r"(\d+(?:\.\d+)?)\s*(?:%|percent)\s+of\s+(?:the\s+)?(?:forecast|rated|value|generation|output)", lowered)
    if m_pct_of:
        return max(0.0, min(1.0, float(m_pct_of.group(1)) / 100.0))
    # "is at 25%" or "is at 25 percent" -> factor = X/100
    m_is_at = re.search(r"\bis\s+at\s+(\d+(?:\.\d+)?)\s*(?:%|percent)\b", lowered)
    if m_is_at:
        return max(0.0, min(1.0, float(m_is_at.group(1)) / 100.0))
    # "25 percent" (bare) -> factor = X/100
    m_pct_word = re.search(r"(\d+(?:\.\d+)?)\s+percent\b", lowered)
    if m_pct_word:
        return max(0.0, min(1.0, float(m_pct_word.group(1)) / 100.0))
    # Bare percent fallback -> treat as factor (X/100)
    m = re.search(r"(\d+(?:\.\d+)?)\s*%", lowered)
    if m:
        return max(0.0, min(1.0, float(m.group(1)) / 100.0))

    fractions = [
        ("one-fifth", 0.2), ("one fifth", 0.2),
        ("a quarter", 0.25), ("one quarter", 0.25), ("quarter", 0.25),
        ("one third", 0.33), ("two thirds", 0.66),
        ("three quarters", 0.75), ("roughly half", 0.5), ("around half", 0.5), ("about half", 0.5), ("half", 0.5),
        ("minimal", 0.1), ("almost none", 0.05),
        ("zero", 0.0), ("none", 0.0),
    ]
    for word, frac in fractions:
        if word in lowered:
            return frac
    return None


def _match_number(text: str, unit_hint: str) -> float | None:
    m = re.search(r"(\d+(?:\.\d+)?)\s*(kwh|kilowatt-hour|kilowatt|kw)", text, re.IGNORECASE)
    if m:
        return float(m.group(1))
    m = re.search(r"(\d+(?:\.\d+)?)\s*" + re.escape(unit_hint), text, re.IGNORECASE)
    if m:
        return float(m.group(1))
    m = re.search(r"(\d+(?:\.\d+)?)", text)
    if m:
        return float(m.group(1))
    return None


def _classify_note(note: str, battery: Optional[BatterySpec] = None) -> Dict[str, Any]:
    text = note.lower()

    # --- solar_reduction ---
    solar_kw = (
        "solar" in text or "panel" in text or "pv" in text
        or "generation" in text or "rooftop" in text or "sun" in text
    )
    if solar_kw and (
        "reduc" in text
        or "clean" in text
        or "wash" in text
        or "dust" in text
        or "shadow" in text
        or "covered" in text
        or "block" in text
        or "less" in text
        or "curtail" in text
        or "quarter" in text
        or "half" in text
        or "third" in text
        or "fifth" in text
        or "%" in text
        or "percent" in text
    ):
        factor = _match_percent(text)
        hours = _extract_hours(text)
        if factor is not None:
            hours = hours if hours else list(range(8, 18))
            return {
                "directive_type": "solar_reduction",
                "applies": True,
                "structured_adjustment": {"hours": hours, "factor": round(factor, 4)},
                "explanation": f"Solar output adjusted by factor {factor:.2f} during specified hours.",
                "reason": f"Solar output adjusted by factor {factor:.2f} during specified hours.",
            }

    # --- no_discharge_window (check before no_charge to avoid collision) ---
    if "discharg" in text and (
        "no " in text or "not" in text or "don't" in text or "dont" in text or "forbid" in text or "avoid" in text or "stop" in text or "idle" in text or "prohibit" in text or "prevent" in text or "never" in text or "isolat" in text or "unavail" in text or "disable" in text or "offline" in text
    ):
        hours = _extract_hours(text)
        if hours:
            return {
                "directive_type": "no_discharge_window",
                "applies": True,
                "structured_adjustment": {"hours": hours},
                "explanation": f"Battery discharging forbidden during hours {hours}.",
                "reason": f"Battery discharging forbidden during hours {hours}.",
            }

    # --- no_charge_window ---
    charge_kw = "charg" in text or "charger" in text or "charging circuit" in text
    if charge_kw and not "discharg" in text and (
        "no " in text or "not" in text or "don't" in text or "dont" in text or "forbid" in text or "avoid" in text or "stop" in text or "idle" in text or "prohibit" in text or "prevent" in text or "never" in text or "pause" in text or "isolat" in text or "unavail" in text or "disable" in text or "offline" in text or "down" in text or "out of service" in text or "refuse" in text or "block" in text or "let " in text
    ):
        hours = _extract_hours(text)
        if hours:
            return {
                "directive_type": "no_charge_window",
                "applies": True,
                "structured_adjustment": {"hours": hours},
                "explanation": f"Battery charging forbidden during hours {hours}.",
                "reason": f"Battery charging forbidden during hours {hours}.",
            }

    # --- minimum_battery_reserve ---
    if ("reserve" in text or "minimum" in text or "maintain" in text or "never drop below" in text or "floor" in text) and (
        "battery" in text or "stored" in text or "charg" in text or "energy" in text or "kwh" in text or "soc" in text or "capacity" in text
    ):
        hours = _extract_hours(text)
        hours = hours if hours else list(range(24))

        # Check percentage of capacity: e.g. "50% of the battery capacity"
        m_cap_pct = re.search(r"(\d+(?:\.\d+)?)\s*%\s*of\s*(?:the\s*)?(?:battery\s*)?capacity", text)
        if m_cap_pct:
            pct = float(m_cap_pct.group(1)) / 100.0
            cap = battery.capacity_kwh if battery else 200.0
            min_energy = pct * cap
            return {
                "directive_type": "minimum_battery_reserve",
                "applies": True,
                "structured_adjustment": {"hours": hours, "minimum_energy_kwh": round(min_energy, 2)},
                "explanation": f"Enforce minimum battery reserve of {min_energy:.1f} kWh ({pct*100:.0f}% capacity).",
                "reason": f"Enforce minimum battery reserve of {min_energy:.1f} kWh ({pct*100:.0f}% capacity).",
            }

        val = _match_number(text, "kwh")
        if val is not None:
            return {
                "directive_type": "minimum_battery_reserve",
                "applies": True,
                "structured_adjustment": {"hours": hours, "minimum_energy_kwh": round(val, 2)},
                "explanation": f"Enforce minimum battery reserve of {val:.1f} kWh.",
                "reason": f"Enforce minimum battery reserve of {val:.1f} kWh.",
            }

    # --- max_grid_window (must come before minimum_battery_reserve to handle "Hold grid usage under X") ---
    grid_kw = (
        "grid" in text or "intake" in text or "import" in text or "draw" in text
        or "buy" in text or "purchase" in text or "pull" in text or "usage" in text
    )
    grid_limit_kw = (
        "max" in text or "cap" in text or "limit" in text or "stay at or below" in text
        or "at or below" in text or "no more than" in text or "no more" in text
        or "above" in text or "ceiling" in text or "not exceed" in text or "up to" in text
        or "below" in text or "restricted" in text or "under" in text or "hold " in text
    )
    if grid_kw and grid_limit_kw and not ("reserve" in text or "minimum" in text):
        hours = _extract_hours(text)
        cap = _match_number(text, "kwh")
        if hours and cap is not None:
            return {
                "directive_type": "max_grid_window",
                "applies": True,
                "structured_adjustment": {"hours": hours, "max_grid_kwh": round(cap, 2)},
                "explanation": f"Cap grid import at {cap:.1f} kWh during hours {hours}.",
                "reason": f"Cap grid import at {cap:.1f} kWh during hours {hours}.",
            }

    return {
        "directive_type": "no_op",
        "applies": False,
        "structured_adjustment": None,
        "explanation": "This note does not affect today's 24-hour energy schedule.",
        "reason": "This note does not affect today's 24-hour energy schedule.",
    }


def _split_into_clauses(note: str) -> List[str]:
    """Split a single note into multiple clauses when it contains several
    distinct directives (e.g. "Cap grid draw at 5 kWh ... . Reduce solar ...").

    We split on sentence boundaries (".", ";") and re-classify each clause
    independently. If a clause itself produces no_op, we keep it only when the
    whole note would have been no_op; otherwise we drop empty clauses.
    """
    cleaned = (note or "").strip()
    if not cleaned:
        return []

    # Look for sentence/semicolon boundaries
    raw_parts = re.split(r"(?<=[.!?])\s+|(?<=;)\s+", cleaned)
    parts = [p.strip().rstrip(".").strip() for p in raw_parts if p.strip()]

    # Heuristic: only split if there are at least two parts AND at least two
    # of them contain an action verb from the directive lexicon.
    action_verbs = (
        "cap", "limit", "reduce", "curtail", "drop", "cut", "block",
        "no ", "not ", "don't", "dont", "forbid", "avoid", "prohibit",
        "prevent", "stop", "never", "hold ", "keep", "maintain",
        "preserve", "reserve", "charge", "discharge", "disable",
        "pause", "isolate", "use", "ensure", "store",
    )
    n_action = sum(1 for p in parts if any(v in p.lower() for v in action_verbs))
    if len(parts) >= 2 and n_action >= 2:
        return parts
    return [cleaned]


def _rule_interpret(notes: List[str], battery: Optional[BatterySpec] = None) -> List[Tuple[str, Dict[str, Any]]]:
    out: List[Tuple[str, Dict[str, Any]]] = []
    for note in notes:
        clauses = _split_into_clauses(note)
        for clause in clauses:
            directive = _classify_note(clause, battery=battery)
            out.append((clause, directive))
    return out


# --------------------------------------------------------------------------- #
# Public Entry Point
# --------------------------------------------------------------------------- #


def interpret_notes(notes: List[str], battery: Optional[BatterySpec] = None) -> List[Dict[str, Any]]:
    """Interpret notes to normalized directive dicts with note_index, applies, etc."""
    notes = notes or []
    raw_pairs: List[Tuple[str, Dict[str, Any]]] = []

    if settings.using_llm():
        try:
            data = _call_llm(notes)
            if len(data) != len(notes):
                raise GuardrailError(
                    f"LLM returned {len(data)} directives for {len(notes)} notes"
                )
            for note, item in zip(notes, data):
                raw_pairs.append((note, item))
        except Exception as exc:  # noqa: BLE001
            logger.warning("LLM interpreter failed (%s); using rule-based parser", exc)
            raw_pairs = []

    if not raw_pairs:
        raw_pairs = _rule_interpret(notes, battery=battery)

    # Apply guardrails to every directive, ensuring note_index order and correct shape
    validated_directives: List[Dict[str, Any]] = []
    for idx, (note, raw) in enumerate(raw_pairs):
        try:
            clean = validate_directive_dict(raw, index=idx)
            dtype = clean["type"]
            applies = dtype != "no_op"
            struct = clean.get("structured_adjustment")
            explanation = raw.get("explanation") or clean.get("reason") or "Interpreted directive"
            validated_directives.append({
                "note_index": idx,
                "applies": applies,
                "directive_type": dtype,
                "structured_adjustment": struct if applies else None,
                "explanation": str(explanation),
                # Internal helper keys for solver
                "type": dtype,
                "hours": clean.get("hours", []),
                "factor": clean.get("factor", 1.0),
                "minimum_energy_kwh": clean.get("minimum_energy_kwh", clean.get("min_kwh", 0.0)),
                "max_grid_kwh": clean.get("max_grid_kwh", clean.get("max_kwh", 0.0)),
            })
        except GuardrailError as exc:
            logger.warning("Guardrail rejected directive for note #%d: %s", idx, exc)
            validated_directives.append({
                "note_index": idx,
                "applies": False,
                "directive_type": "no_op",
                "structured_adjustment": None,
                "explanation": f"Rejected by guardrail: {exc}",
                "type": "no_op",
            })

    return validated_directives