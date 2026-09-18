"""Self-contained test runner that runs without external testing framework dependencies."""
import json
import math
import os
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE_DIR / "backend"))

from app.schemas.energy import (
    BatterySpec,
    DirectiveInterpretation,
    HourlyData,
    Scenario,
)
from app.services.guardrails import (
    GuardrailError,
    validate_directive_dict,
    validate_directives,
)
from app.services.llm_interpreter import interpret_notes
from app.services.optimizer import (
    _apply_directives,
    _solve_pure_python_lp,
    optimize,
)


def load_sample(name: str) -> dict:
    with open(BASE_DIR / "samples" / name, "r", encoding="utf-8") as f:
        return json.load(f)


def test_guardrails():
    print("Testing Guardrails...")
    # Valid solar reduction
    d = validate_directive_dict({"directive_type": "solar_reduction", "structured_adjustment": {"hours": [12, 13], "factor": 0.25}}, index=0)
    assert d["type"] == "solar_reduction" and d["factor"] == 0.25

    # Valid reserve
    d = validate_directive_dict({"directive_type": "minimum_battery_reserve", "structured_adjustment": {"hours": list(range(24)), "minimum_energy_kwh": 6.0}}, index=1)
    assert d["minimum_energy_kwh"] == 6.0

    # Invalid hour
    try:
        validate_directive_dict({"directive_type": "solar_reduction", "structured_adjustment": {"hours": [25], "factor": 0.5}}, index=2)
        assert False, "Expected GuardrailError on hour 25"
    except GuardrailError:
        pass

    # Invalid factor
    try:
        validate_directive_dict({"directive_type": "solar_reduction", "structured_adjustment": {"hours": [12], "factor": 1.5}}, index=3)
        assert False, "Expected GuardrailError on factor 1.5"
    except GuardrailError:
        pass

    print("  Guardrails PASSED")


def test_interpreter_rule_fallback():
    print("Testing Interpreter (Rule-Based NLP)...")
    notes = [
      "Facilities will wash the rooftop solar panels from noon until 2 PM. During cleaning, usable solar should be treated as roughly 25% of the forecast.",
      "Do not charge the battery between 6 PM and 9 PM.",
      "Keep a minimum battery reserve of 6 kWh at all times."
    ]
    results = interpret_notes(notes)
    assert len(results) == 3
    types = [d["directive_type"] for d in results]
    assert "solar_reduction" in types
    assert "no_charge_window" in types
    assert "minimum_battery_reserve" in types

    for d in results:
        if d["directive_type"] == "solar_reduction":
            assert 12 in d["structured_adjustment"]["hours"] and 13 in d["structured_adjustment"]["hours"]
            assert math.isclose(d["structured_adjustment"]["factor"], 0.25, abs_tol=1e-2)
        elif d["directive_type"] == "no_charge_window":
            assert 18 in d["structured_adjustment"]["hours"] and 19 in d["structured_adjustment"]["hours"] and 20 in d["structured_adjustment"]["hours"]
        elif d["directive_type"] == "minimum_battery_reserve":
            assert math.isclose(d["structured_adjustment"]["minimum_energy_kwh"], 6.0, abs_tol=1e-2)

    print("  Interpreter PASSED")


def test_optimization_solar_cleaning():
    print("Testing Optimization Engine (case_solar_cleaning)...")
    data = load_sample("case_solar_cleaning.json")
    notes = data["operator_notes"]
    sc_dict = data["scenario"]
    sc = Scenario(**sc_dict)

    raw_interps = interpret_notes(notes)
    interpretations = [
        DirectiveInterpretation(
            note_index=d["note_index"],
            applies=d["applies"],
            directive_type=d["directive_type"],
            structured_adjustment=d.get("structured_adjustment"),
            explanation=d.get("explanation", ""),
        )
        for d in raw_interps
    ]

    response = optimize(
        battery=sc.battery,
        hours=sc.hours,
        scenario_id=sc.scenario_id or "case-solar-cleaning",
        directive_interpretations=interpretations,
    )
    plan = response.hourly_plan
    hours = sc.hours
    initial_soc = sc.battery.initial_kwh

    print(f"  Optimal cost: {response.total_cost_bdt:.2f} BDT | Grid energy: {response.total_grid_kwh:.2f} kWh | Peak grid: {response.peak_grid_kwh:.2f} kWh")

    # 1. Exact Hourly Energy Balance
    for h in range(24):
        entry = plan[h]
        hd = hours[h]
        chg = entry.battery_kwh if entry.battery_action == "charge" else 0.0
        dis = entry.battery_kwh if entry.battery_action == "discharge" else 0.0
        gen_side = entry.grid_kwh + entry.solar_used_kwh + dis
        load_side = hd.demand_kwh + chg
        assert math.isclose(gen_side, load_side, abs_tol=1e-3), (
            f"Hour {h}: Energy balance failed: {gen_side} != {load_side}"
        )

    # 2. Exact SOC Dynamics
    cur_soc = initial_soc
    for h in range(24):
        entry = plan[h]
        chg = entry.battery_kwh if entry.battery_action == "charge" else 0.0
        dis = entry.battery_kwh if entry.battery_action == "discharge" else 0.0
        cur_soc = cur_soc + chg - dis
        assert math.isclose(entry.battery_energy_after_kwh, cur_soc, abs_tol=1e-3), (
            f"Hour {h}: SOC progression mismatch: expected {cur_soc}, got {entry.battery_energy_after_kwh}"
        )

    # 3. End of day battery neutrality
    assert math.isclose(plan[23].battery_energy_after_kwh, initial_soc, abs_tol=1e-3), (
        f"Neutrality failed: End SOC {plan[23].battery_energy_after_kwh} != Initial {initial_soc}"
    )

    # 4. Directive compliance: No charge window (18, 19, 20)
    for h in [18, 19, 20]:
        chg = plan[h].battery_kwh if plan[h].battery_action == "charge" else 0.0
        assert math.isclose(chg, 0.0, abs_tol=1e-4), (
            f"Hour {h}: Battery charged ({chg} kWh) during no_charge_window!"
        )

    # 5. Directive compliance: Minimum reserve 6 kWh
    for h in range(24):
        assert plan[h].battery_energy_after_kwh >= 6.0 - 1e-3, (
            f"Hour {h}: Battery SOC ({plan[h].battery_energy_after_kwh} kWh) fell below 6.0 kWh min reserve!"
        )

    print("  Optimization Invariants PASSED")


def test_pure_python_simplex_direct():
    print("Testing Pure-Python 2-Phase Simplex Solver Direct...")
    data = load_sample("case_basic.json")
    sc = Scenario(**data["scenario"])
    state = _apply_directives(sc.battery, sc.hours, [])
    plan = _solve_pure_python_lp(state)
    assert plan is not None, "Pure python simplex returned None!"
    assert len(plan) == 24
    print("  Pure-Python Simplex PASSED")


if __name__ == "__main__":
    test_guardrails()
    test_interpreter_rule_fallback()
    test_optimization_solar_cleaning()
    test_pure_python_simplex_direct()
    print("\nALL STANDALONE TESTS PASSED SUCCESSFULLY!")
