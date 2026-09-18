"""Test all 10 official public samples (SAMPLE-01 to SAMPLE-10)."""
from __future__ import annotations

import json
import math
from pathlib import Path
from fastapi.testclient import TestClient
from app.main import app

client = TestClient(app)
SAMPLES_DIR = Path(__file__).resolve().parents[1] / "samples"


def test_all_ten_public_samples():
    for i in range(1, 11):
        fname = f"sample_{i:02d}.json"
        with open(SAMPLES_DIR / fname, "r", encoding="utf-8") as f:
            payload = json.load(f)

        r = client.post("/optimize-energy", json=payload)
        assert r.status_code == 200, f"Failed on {fname}: {r.status_code} -> {r.text}"
        res = r.json()

        assert res["scenario_id"] == f"SAMPLE-{i:02d}"
        assert len(res["hourly_plan"]) == 24
        assert res["total_grid_kwh"] >= 0
        assert res["total_cost_bdt"] >= 0
        assert res["peak_grid_kwh"] >= 0
        assert isinstance(res["plan_summary"], str)

        # Invariants on hourly_plan
        init_kwh = payload["battery"]["initial_energy_kwh"]
        cur_soc = init_kwh
        for h, row in enumerate(res["hourly_plan"]):
            # Energy balance
            g = row["grid_kwh"]
            s = row["solar_used_kwh"]
            action = row["battery_action"]
            bk = row["battery_kwh"]
            chg = bk if action == "charge" else 0.0
            dis = bk if action == "discharge" else 0.0
            demand = payload["hours"][h]["demand_kwh"]
            assert math.isclose(g + s + dis, demand + chg, abs_tol=1e-3)

            # SOC progression
            cur_soc = cur_soc + chg - dis
            assert math.isclose(row["battery_energy_after_kwh"], cur_soc, abs_tol=1e-3)

        # End of day neutrality
        assert math.isclose(res["hourly_plan"][23]["battery_energy_after_kwh"], init_kwh, abs_tol=1e-3)

        # Verify specific expected directives
        types = [d["directive_type"] for d in res["directive_interpretation"]]
        if i == 1:
            assert "solar_reduction" in types
            assert res["directive_interpretation"][0]["structured_adjustment"]["hours"] == [12, 13]
            assert math.isclose(res["directive_interpretation"][0]["structured_adjustment"]["factor"], 0.25, abs_tol=1e-2)
        elif i == 2:
            assert "no_charge_window" in types
            assert res["directive_interpretation"][0]["structured_adjustment"]["hours"] == [2, 3, 4]
        elif i == 3:
            assert "minimum_battery_reserve" in types
            assert res["directive_interpretation"][0]["structured_adjustment"]["hours"] == [18, 19, 20]
            assert math.isclose(res["directive_interpretation"][0]["structured_adjustment"]["minimum_energy_kwh"], 10.0, abs_tol=1e-2)
        elif i == 4:
            assert "solar_reduction" in types
            assert res["directive_interpretation"][0]["structured_adjustment"]["hours"] == [13, 14]
            assert math.isclose(res["directive_interpretation"][0]["structured_adjustment"]["factor"], 0.20, abs_tol=1e-2)
        elif i == 5:
            assert "no_discharge_window" in types
            assert res["directive_interpretation"][0]["structured_adjustment"]["hours"] == [10, 11]
        elif i == 6:
            assert "no_charge_window" in types
            assert res["directive_interpretation"][0]["structured_adjustment"]["hours"] == [14, 15]
        elif i == 7:
            assert "no_op" in types
            assert res["directive_interpretation"][0]["applies"] is False
            assert res["directive_interpretation"][0]["structured_adjustment"] is None
        elif i == 8:
            assert "no_charge_window" in types
            assert res["directive_interpretation"][0]["structured_adjustment"]["hours"] == [18, 19, 20]
        elif i == 9:
            assert "solar_reduction" in types
            assert res["directive_interpretation"][0]["structured_adjustment"]["hours"] == [13, 14]
            assert math.isclose(res["directive_interpretation"][0]["structured_adjustment"]["factor"], 0.20, abs_tol=1e-2)
        elif i == 10:
            assert "max_grid_window" in types
            assert res["directive_interpretation"][0]["structured_adjustment"]["hours"] == [8, 9, 10]
            assert math.isclose(res["directive_interpretation"][0]["structured_adjustment"]["max_grid_kwh"], 6.0, abs_tol=1e-2)
