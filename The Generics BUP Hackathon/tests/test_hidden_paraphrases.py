"""Robustness tests for the three Section 11.4 paraphrase forms and edge cases.

These tests exercise the LLM interpreter (in deterministic rule-fallback mode)
to confirm that semantically equivalent operator notes — written in different
surface forms — still produce the *same canonical directive payload*.

The tests run against the live FastAPI app via TestClient and force the
fallback path by clearing any LLM API keys, so they don't require network
access.
"""
from __future__ import annotations

import json
import math
import os
from pathlib import Path
from typing import Any, Dict, Iterable, List

import pytest
from fastapi.testclient import TestClient

# Make sure no live LLM credentials are active for these tests.
for _k in ("OPENAI_API_KEY", "GEMINI_API_KEY", "ANTHROPIC_API_KEY"):
    os.environ.pop(_k, None)
os.environ["LLM_PROVIDER"] = "off"

from app.main import app  # noqa: E402  (import after env setup)

client = TestClient(app)

# Canonical baseline scenario (matches the shape expected by /optimize-energy).
BASE_HOURS: List[Dict[str, Any]] = [
    {
        "hour": h,
        "demand_kwh": 8.0 if 6 <= h <= 21 else 3.0,
        "solar_kwh": 4.0 * max(0.0, 1 - abs(h - 13) / 8.0),
        "tariff_bdt_per_kwh": 12.0 if 17 <= h <= 22 else 6.0,
    }
    for h in range(24)
]


def _payload(notes: str) -> Dict[str, Any]:
    return {
        "scenario_id": "PARA-TEST",
        "notes": notes,
        "hours": [dict(h) for h in BASE_HOURS],  # deep copy
        "battery": {
            "initial_energy_kwh": 12.0,
            "capacity_kwh": 20.0,
            "max_charge_kwh_per_hour": 4.0,
            "max_discharge_kwh_per_hour": 4.0,
            "charge_efficiency": 0.95,
            "discharge_efficiency": 0.95,
        },
    }


def _directive(res: Dict[str, Any], idx: int = 0) -> Dict[str, Any]:
    di = res["directive_interpretation"]
    assert di, "Expected at least one directive_interpretation entry"
    return di[idx]


# ---------------------------------------------------------------------------
# Form A: solar_reduction
# ---------------------------------------------------------------------------

SOLAR_FORMS = [
    ("Reduce solar by 75% during hours 12 and 13.", 12, 13, 0.25),
    ("Drop solar to 25 percent between 12:00 and 14:00.", 12, 13, 0.25),
    ("Solar generation is at 25% for hours 12,13.", 12, 13, 0.25),
    ("Cut solar output to ~25 percent of its rated value for hour 12 and hour 13.",
     12, 13, 0.25),
]


@pytest.mark.parametrize("notes,lo,hi,expected_factor", SOLAR_FORMS)
def test_solar_reduction_paraphrases(notes, lo, hi, expected_factor):
    p = _payload(notes)
    r = client.post("/optimize-energy", json=p)
    assert r.status_code == 200, r.text
    res = r.json()

    d = _directive(res)
    assert d["directive_type"] == "solar_reduction"
    assert d["applies"] is True
    adj = d["structured_adjustment"]
    assert adj is not None
    assert adj["hours"] == [lo, hi]
    assert math.isclose(adj["factor"], expected_factor, abs_tol=1e-2)


# ---------------------------------------------------------------------------
# Form B: no_charge_window
# ---------------------------------------------------------------------------

NOCHARGE_FORMS = [
    ("Block battery charging from hour 18 through 20.", [18, 19, 20]),
    ("No battery charging between hour 18 and hour 20 (inclusive).", [18, 19, 20]),
    ("Don't let the battery charge during 18, 19, 20.", [18, 19, 20]),
    ("Refuse to charge the battery at hours 18, 19, and 20.", [18, 19, 20]),
]


@pytest.mark.parametrize("notes,expected_hours", NOCHARGE_FORMS)
def test_no_charge_window_paraphrases(notes, expected_hours):
    p = _payload(notes)
    r = client.post("/optimize-energy", json=p)
    assert r.status_code == 200, r.text
    res = r.json()

    d = _directive(res)
    assert d["directive_type"] == "no_charge_window"
    assert d["applies"] is True
    adj = d["structured_adjustment"]
    assert adj is not None
    assert adj["hours"] == expected_hours


# ---------------------------------------------------------------------------
# Form C: max_grid_window
# ---------------------------------------------------------------------------

MAXGRID_FORMS = [
    ("Cap grid draw at 6 kWh from hour 18 to 20.", [18, 19, 20], 6.0),
    ("Maximum grid import 6 kWh between hours 18 and 20.", [18, 19, 20], 6.0),
    ("Hold grid usage under 6 kWh for hours 18, 19, 20.", [18, 19, 20], 6.0),
    ("Limit grid pull to 6 kWh at hour 18 through hour 20.", [18, 19, 20], 6.0),
]


@pytest.mark.parametrize("notes,expected_hours,expected_cap", MAXGRID_FORMS)
def test_max_grid_window_paraphrases(notes, expected_hours, expected_cap):
    p = _payload(notes)
    r = client.post("/optimize-energy", json=p)
    assert r.status_code == 200, r.text
    res = r.json()

    d = _directive(res)
    assert d["directive_type"] == "max_grid_window"
    assert d["applies"] is True
    adj = d["structured_adjustment"]
    assert adj is not None
    assert adj["hours"] == expected_hours
    assert math.isclose(adj["max_grid_kwh"], expected_cap, abs_tol=1e-2)


# ---------------------------------------------------------------------------
# Edge case: no_op must yield applies=False and structured_adjustment=None
# ---------------------------------------------------------------------------

NOOP_FORMS = [
    "Just keep things running as normal.",
    "No special instructions today.",
    "Nothing to report.",
    "All systems nominal.",
]


@pytest.mark.parametrize("notes", NOOP_FORMS)
def test_no_op_paraphrases(notes):
    p = _payload(notes)
    r = client.post("/optimize-energy", json=p)
    assert r.status_code == 200, r.text
    res = r.json()

    d = _directive(res)
    assert d["directive_type"] == "no_op"
    assert d["applies"] is False
    assert d["structured_adjustment"] is None
    assert d["reason"]  # non-empty explanation


# ---------------------------------------------------------------------------
# Edge case: multiple directives in one note, separate sentences
# ---------------------------------------------------------------------------

def test_multi_directive_in_one_note():
    notes = "Cap grid draw at 5 kWh from hours 18 to 20. Reduce solar by 50% between hour 12 and hour 13."
    p = _payload(notes)
    r = client.post("/optimize-energy", json=p)
    assert r.status_code == 200, r.text
    res = r.json()
    types = [d["directive_type"] for d in res["directive_interpretation"]]
    assert "max_grid_window" in types
    assert "solar_reduction" in types
    # All canonical invariants still hold.
    assert len(res["hourly_plan"]) == 24
    assert math.isclose(
        res["hourly_plan"][23]["battery_energy_after_kwh"],
        p["battery"]["initial_energy_kwh"],
        abs_tol=1e-3,
    )
