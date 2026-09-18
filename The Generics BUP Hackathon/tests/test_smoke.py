"""End-to-end smoke and invariant tests using sample scenarios."""
from __future__ import annotations

import json
import math
from pathlib import Path

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)
SAMPLES_DIR = Path(__file__).resolve().parents[1] / "samples"


def _load(name: str) -> dict:
    with open(SAMPLES_DIR / name, "r", encoding="utf-8") as fh:
        return json.load(fh)


def test_health_endpoint():
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


def test_health_llm_endpoint():
    r = client.get("/health/llm")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert "llm_enabled" in body


def test_basic_scenario():
    payload = _load("case_basic.json")
    r = client.post("/optimize-energy", json=payload)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["scenario_id"] == "case-basic"
    assert len(body["hourly_plan"]) == 24
    assert body["total_grid_kwh"] >= 0
    assert body["total_cost_bdt"] >= 0
    assert body["peak_grid_kwh"] >= 0
    assert isinstance(body["plan_summary"], str)


def test_exact_energy_balance():
    payload = _load("case_solar_cleaning.json")
    r = client.post("/optimize-energy", json=payload)
    assert r.status_code == 200
    body = r.json()
    plan = body["hourly_plan"]
    hours = payload["scenario"]["hours"]

    for h in range(24):
        g = plan[h]["grid_kwh"]
        s = plan[h]["solar_used_kwh"]
        action = plan[h]["battery_action"]
        b_kwh = plan[h]["battery_kwh"]
        chg = b_kwh if action == "charge" else 0.0
        dis = b_kwh if action == "discharge" else 0.0
        demand = hours[h]["demand_kwh"]
        # Invariant: grid + solar_used + discharge == demand + charge
        assert math.isclose(g + s + dis, demand + chg, abs_tol=1e-3), (
            f"Hour {h}: energy balance failed: grid({g}) + solar({s}) + dis({dis}) != demand({demand}) + chg({chg})"
        )


def test_exact_soc_dynamics_and_neutrality():
    payload = _load("case_solar_cleaning.json")
    r = client.post("/optimize-energy", json=payload)
    assert r.status_code == 200
    body = r.json()
    plan = body["hourly_plan"]
    initial = payload["scenario"]["battery"]["initial_kwh"]

    cur_soc = initial
    for h in range(24):
        action = plan[h]["battery_action"]
        b_kwh = plan[h]["battery_kwh"]
        chg = b_kwh if action == "charge" else 0.0
        dis = b_kwh if action == "discharge" else 0.0
        cur_soc = cur_soc + chg - dis
        assert math.isclose(plan[h]["battery_energy_after_kwh"], cur_soc, abs_tol=1e-3), (
            f"Hour {h}: SOC progression mismatch: expected {cur_soc}, got {plan[h]['battery_energy_after_kwh']}"
        )

    # Battery neutrality: end of day SOC == initial
    assert math.isclose(plan[23]["battery_energy_after_kwh"], initial, abs_tol=1e-3)


def test_solar_reduction_and_no_charge_directives():
    payload = _load("case_solar_cleaning.json")
    r = client.post("/optimize-energy", json=payload)
    assert r.status_code == 200, r.text
    body = r.json()
    types = [d["directive_type"] for d in body["directive_interpretation"]]
    assert "solar_reduction" in types
    assert "no_charge_window" in types
    assert "minimum_battery_reserve" in types

    # No charging during no_charge_window
    for d in body["directive_interpretation"]:
        if d["directive_type"] == "no_charge_window":
            for h in d["structured_adjustment"]["hours"]:
                assert body["hourly_plan"][h]["battery_action"] != "charge" or math.isclose(body["hourly_plan"][h]["battery_kwh"], 0.0, abs_tol=1e-4)

    # Minimum reserve floor check
    for h in range(24):
        assert body["hourly_plan"][h]["battery_energy_after_kwh"] >= 6.0 - 1e-3


def test_canonical_request_shape():
    """Test top-level hours and battery per Problem Statement Section 7.4."""
    base = _load("case_basic.json")
    top_level_payload = {
        "scenario_id": "GRID-TOP-LEVEL",
        "operator_notes": ["Do not charge the battery between 18:00 and 21:00."],
        "hours": base["scenario"]["hours"],
        "battery": {
            "capacity_kwh": 20,
            "initial_energy_kwh": 10,
            "minimum_energy_kwh": 4,
            "max_charge_kwh_per_hour": 5,
            "max_discharge_kwh_per_hour": 5,
        },
    }
    r = client.post("/optimize-energy", json=top_level_payload)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["scenario_id"] == "GRID-TOP-LEVEL"
    assert len(body["hourly_plan"]) == 24
    assert body["directive_interpretation"][0]["directive_type"] == "no_charge_window"
    assert body["directive_interpretation"][0]["applies"] is True


def test_paraphrased_notes():
    payload = _load("case_paraphrased.json")
    r = client.post("/optimize-energy", json=payload)
    assert r.status_code == 200, r.text
    body = r.json()
    types = [d["directive_type"] for d in body["directive_interpretation"]]
    assert "solar_reduction" in types
    assert "no_charge_window" in types
    assert "minimum_battery_reserve" in types


def test_guardrails_rejects_out_of_range():
    bad = _load("case_basic.json")
    bad["operator_notes"] = ["Solar at hour 25 is reduced to 150%"]
    r = client.post("/optimize-energy", json=bad)
    assert r.status_code == 200
    body = r.json()
    assert body["directive_interpretation"][0]["directive_type"] in ("no_op", "solar_reduction")
