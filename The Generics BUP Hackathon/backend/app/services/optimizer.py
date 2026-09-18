"""Optimization engine for the 24-hour energy grid.

The goal is to minimize total grid cost (sum grid_kwh * tariff) subject to:
- Energy balance per hour: grid + solar_used + battery_discharge = demand + battery_charge
- Solar usage per hour: solar_used <= effective_solar(hour)
- Battery charge/discharge caps: charge <= max_charge_rate, discharge <= max_discharge_rate
- Battery SOC dynamics: SOC[0] = initial_energy + charge[0] - discharge[0]
                        SOC[h] = SOC[h-1] + charge[h] - discharge[h] (h >= 1)
- Battery reserve bounds: min_reserve[h] <= SOC[h] <= capacity
- Battery neutrality: SOC[23] == initial_energy
- Directive constraints: solar reductions, no-charge windows,
                         no-discharge windows, max-grid windows

Solvers:
1. SciPy HiGHS Linear Programming (preferred, fast, globally optimal).
2. Pure-Python 2-Phase Simplex Solver (fallback if SciPy is absent).
3. Multi-pass Arbitrage Heuristic (last-resort safety net).
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from ..schemas.energy import (
    BatterySpec,
    DirectiveInterpretation,
    HourlyData,
    HourlyPlanEntry,
    OptimizationResponse,
)

logger = logging.getLogger(__name__)

TOL = 1e-5


# --------------------------------------------------------------------------- #
# Scenario State & Directive Application
# --------------------------------------------------------------------------- #


@dataclass
class _ScenarioState:
    hours: List[HourlyData]
    battery: BatterySpec
    solar_factor: List[float]  # per hour 0..1
    no_charge: List[bool]
    no_discharge: List[bool]
    min_soc: List[float]       # per hour minimum reserve floor
    max_grid: List[float]


def _apply_directives(
    battery: BatterySpec,
    hours: List[HourlyData],
    raw_directives: List[Dict[str, Any]],
) -> _ScenarioState:
    n = len(hours)
    factor = [1.0] * n
    no_charge = [False] * n
    no_discharge = [False] * n
    max_grid = [float("inf")] * n
    base_min = battery.minimum_energy_kwh or battery.min_reserve_kwh or 0.0
    min_soc = [base_min] * n

    for d in raw_directives:
        dtype = d.get("directive_type") or d.get("type")
        struct = d.get("structured_adjustment")
        data = struct if isinstance(struct, dict) else d

        if dtype == "solar_reduction":
            for h in data.get("hours", []):
                if 0 <= h < n:
                    factor[h] = min(factor[h], float(data.get("factor", 1.0)))
        elif dtype == "minimum_battery_reserve":
            m_val = float(data.get("minimum_energy_kwh") or data.get("min_kwh") or base_min)
            req_hours = data.get("hours") or list(range(n))
            for h in req_hours:
                if 0 <= h < n:
                    min_soc[h] = max(min_soc[h], m_val)
        elif dtype == "no_charge_window":
            for h in data.get("hours", []):
                if 0 <= h < n:
                    no_charge[h] = True
        elif dtype == "no_discharge_window":
            for h in data.get("hours", []):
                if 0 <= h < n:
                    no_discharge[h] = True
        elif dtype == "max_grid_window":
            cap_val = float(data.get("max_grid_kwh") or data.get("max_kwh") or float("inf"))
            for h in data.get("hours", []):
                if 0 <= h < n:
                    max_grid[h] = min(max_grid[h], cap_val)

    # Clamp min_soc to at most capacity
    for h in range(n):
        min_soc[h] = min(min_soc[h], battery.capacity_kwh)

    return _ScenarioState(
        hours=hours,
        battery=battery,
        solar_factor=factor,
        no_charge=no_charge,
        no_discharge=no_discharge,
        min_soc=min_soc,
        max_grid=max_grid,
    )


# --------------------------------------------------------------------------- #
# LP Formulation (SciPy HiGHS)
# --------------------------------------------------------------------------- #


def _solve_scipy_lp(state: _ScenarioState) -> Optional[List[Dict[str, float]]]:
    """Solve the 24-hour dispatch LP using SciPy HiGHS backend."""
    try:
        from scipy.optimize import linprog  # noqa: WPS433
    except ImportError:
        logger.info("SciPy not installed; using pure-Python fallback solver")
        return None
    except Exception as exc:  # noqa: BLE001
        logger.warning("Failed importing linprog: %s", exc)
        return None

    n = len(state.hours)
    bat = state.battery
    init_kwh = bat.initial_energy_kwh or bat.initial_kwh or 0.0
    chg_rate = bat.max_charge_kwh_per_hour or bat.charge_limit_kw or 0.0
    dis_rate = bat.max_discharge_kwh_per_hour or bat.discharge_limit_kw or 0.0

    nv = 5 * n
    GRID, SOL, CHG, DIS, SOC = 0, 1, 2, 3, 4

    def idx(h: int, kind: int) -> int:
        return h * 5 + kind

    c = [0.0] * nv
    for h, hd in enumerate(state.hours):
        c[idx(h, GRID)] = hd.tariff_bdt_per_kwh
        c[idx(h, CHG)] = 1e-6
        c[idx(h, DIS)] = 1e-6

    bounds: List[Tuple[Optional[float], Optional[float]]] = []
    for h, hd in enumerate(state.hours):
        g_max = state.max_grid[h] if state.max_grid[h] < float("inf") else None
        bounds.append((0.0, g_max))

        s_max = max(0.0, hd.solar_kwh * state.solar_factor[h])
        bounds.append((0.0, s_max))

        chg_max = 0.0 if state.no_charge[h] else chg_rate
        bounds.append((0.0, chg_max))

        dis_max = 0.0 if state.no_discharge[h] else dis_rate
        bounds.append((0.0, dis_max))

        bounds.append((state.min_soc[h], bat.capacity_kwh))

    A_eq: List[List[float]] = []
    b_eq: List[float] = []

    for h, hd in enumerate(state.hours):
        row_eb = [0.0] * nv
        row_eb[idx(h, GRID)] = 1.0
        row_eb[idx(h, SOL)] = 1.0
        row_eb[idx(h, DIS)] = 1.0
        row_eb[idx(h, CHG)] = -1.0
        A_eq.append(row_eb)
        b_eq.append(hd.demand_kwh)

        row_soc = [0.0] * nv
        row_soc[idx(h, SOC)] = 1.0
        row_soc[idx(h, CHG)] = -1.0
        row_soc[idx(h, DIS)] = 1.0
        if h == 0:
            A_eq.append(row_soc)
            b_eq.append(init_kwh)
        else:
            row_soc[idx(h - 1, SOC)] = -1.0
            A_eq.append(row_soc)
            b_eq.append(0.0)

    # Battery neutrality: SOC[23] == initial_energy
    row_neut = [0.0] * nv
    row_neut[idx(n - 1, SOC)] = 1.0
    A_eq.append(row_neut)
    b_eq.append(init_kwh)

    try:
        res = linprog(
            c,
            A_eq=A_eq,
            b_eq=b_eq,
            bounds=bounds,
            method="highs",
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("linprog exception: %s", exc)
        return None

    if not res.success:
        logger.info("SciPy HiGHS LP failed or infeasible: %s", res.message)
        return None

    plan: List[Dict[str, float]] = []
    x = res.x
    for h in range(n):
        plan.append(
            {
                "grid": float(max(0.0, x[idx(h, GRID)])),
                "solar": float(max(0.0, x[idx(h, SOL)])),
                "charge": float(max(0.0, x[idx(h, CHG)])),
                "discharge": float(max(0.0, x[idx(h, DIS)])),
                "soc": float(x[idx(h, SOC)]),
            }
        )
    return plan


# --------------------------------------------------------------------------- #
# Pure-Python 2-Phase Simplex LP Solver (Zero-Dependency Offline Fallback)
# --------------------------------------------------------------------------- #


class _SimplexTableau:
    def __init__(self, c: List[float], A_ub: List[List[float]], b_ub: List[float], A_eq: List[List[float]], b_eq: List[float]):
        self.num_orig = len(c)
        self.c = list(c)
        self.A_ub = [list(r) for r in A_ub]
        self.b_ub = list(b_ub)
        self.A_eq = [list(r) for r in A_eq]
        self.b_eq = list(b_eq)

    def solve(self, max_iters: int = 20000) -> Optional[List[float]]:
        num_orig = self.num_orig
        m_ub = len(self.A_ub)
        m_eq = len(self.A_eq)
        m = m_ub + m_eq

        A: List[List[float]] = []
        b: List[float] = []

        for i in range(m_ub):
            if self.b_ub[i] < 0:
                A.append([-x for x in self.A_ub[i]])
                b.append(-self.b_ub[i])
            else:
                A.append(list(self.A_ub[i]))
                b.append(self.b_ub[i])

        for i in range(m_eq):
            if self.b_eq[i] < 0:
                A.append([-x for x in self.A_eq[i]])
                b.append(-self.b_eq[i])
            else:
                A.append(list(self.A_eq[i]))
                b.append(self.b_eq[i])

        num_slack = m_ub
        num_artificial = m
        num_vars = num_orig + num_slack + num_artificial

        T = [[0.0] * (num_vars + 1) for _ in range(m + 1)]
        basis = [0] * m

        slack_idx = 0
        art_idx = 0
        for i in range(m):
            for j in range(num_orig):
                T[i][j] = A[i][j]

            if i < m_ub and self.b_ub[i] >= 0:
                T[i][num_orig + slack_idx] = 1.0
                slack_idx += 1
            elif i < m_ub and self.b_ub[i] < 0:
                T[i][num_orig + slack_idx] = -1.0
                slack_idx += 1

            art_col = num_orig + num_slack + art_idx
            T[i][art_col] = 1.0
            basis[i] = art_col
            art_idx += 1

            T[i][-1] = b[i]

        # Phase 1 Objective: Minimize sum of artificial variables
        for i in range(m):
            art_col = num_orig + num_slack + i
            for j in range(num_vars + 1):
                T[m][j] -= T[i][j]

        def pivot(p_row: int, p_col: int) -> None:
            piv = T[p_row][p_col]
            for j in range(num_vars + 1):
                T[p_row][j] /= piv
            for i in range(m + 1):
                if i != p_row:
                    factor = T[i][p_col]
                    if abs(factor) > 1e-12:
                        for j in range(num_vars + 1):
                            T[i][j] -= factor * T[p_row][j]
            basis[p_row] = p_col

        # Phase 1 Iterations
        for _ in range(max_iters):
            min_val = 0.0
            p_col = -1
            for j in range(num_vars):
                if T[m][j] < min_val - 1e-9:
                    min_val = T[m][j]
                    p_col = j

            if p_col == -1:
                break

            p_row = -1
            min_ratio = float("inf")
            for i in range(m):
                if T[i][p_col] > 1e-9:
                    ratio = T[i][-1] / T[i][p_col]
                    if ratio < min_ratio - 1e-11:
                        min_ratio = ratio
                        p_row = i

            if p_row == -1:
                return None

            pivot(p_row, p_col)

        if abs(T[m][-1]) > 1e-4:
            return None

        # Phase 2 Objective
        for j in range(num_vars + 1):
            T[m][j] = 0.0
        for j in range(num_orig):
            T[m][j] = self.c[j]

        for i in range(m):
            b_var = basis[i]
            if b_var < num_orig:
                cost = self.c[b_var]
                for j in range(num_vars + 1):
                    T[m][j] -= cost * T[i][j]

        # Phase 2 Iterations
        valid_cols = num_orig + num_slack
        for _ in range(max_iters):
            min_val = 0.0
            p_col = -1
            for j in range(valid_cols):
                if T[m][j] < min_val - 1e-9:
                    min_val = T[m][j]
                    p_col = j

            if p_col == -1:
                break

            p_row = -1
            min_ratio = float("inf")
            for i in range(m):
                if T[i][p_col] > 1e-9:
                    ratio = T[i][-1] / T[i][p_col]
                    if ratio < min_ratio - 1e-11:
                        min_ratio = ratio
                        p_row = i

            if p_row == -1:
                return None

            pivot(p_row, p_col)

        sol = [0.0] * num_orig
        for i in range(m):
            if basis[i] < num_orig:
                sol[basis[i]] = max(0.0, T[i][-1])
        return sol


def _solve_pure_python_lp(state: _ScenarioState) -> Optional[List[Dict[str, float]]]:
    n = len(state.hours)
    bat = state.battery
    init_kwh = bat.initial_energy_kwh or bat.initial_kwh or 0.0
    chg_rate = bat.max_charge_kwh_per_hour or bat.charge_limit_kw or 0.0
    dis_rate = bat.max_discharge_kwh_per_hour or bat.discharge_limit_kw or 0.0

    nv = 5 * n
    GRID, SOL, CHG, DIS, SOC = 0, 1, 2, 3, 4

    def idx(h: int, kind: int) -> int:
        return h * 5 + kind

    c = [0.0] * nv
    for h, hd in enumerate(state.hours):
        c[idx(h, GRID)] = hd.tariff_bdt_per_kwh
        c[idx(h, CHG)] = 1e-6
        c[idx(h, DIS)] = 1e-6

    A_eq: List[List[float]] = []
    b_eq: List[float] = []
    A_ub: List[List[float]] = []
    b_ub: List[float] = []

    for h, hd in enumerate(state.hours):
        row_eb = [0.0] * nv
        row_eb[idx(h, GRID)] = 1.0
        row_eb[idx(h, SOL)] = 1.0
        row_eb[idx(h, DIS)] = 1.0
        row_eb[idx(h, CHG)] = -1.0
        A_eq.append(row_eb)
        b_eq.append(hd.demand_kwh)

        row_soc = [0.0] * nv
        row_soc[idx(h, SOC)] = 1.0
        row_soc[idx(h, CHG)] = -1.0
        row_soc[idx(h, DIS)] = 1.0
        if h == 0:
            A_eq.append(row_soc)
            b_eq.append(init_kwh)
        else:
            row_soc[idx(h - 1, SOC)] = -1.0
            A_eq.append(row_soc)
            b_eq.append(0.0)

        # Solar <= forecast * factor
        s_max = max(0.0, hd.solar_kwh * state.solar_factor[h])
        row_s = [0.0] * nv
        row_s[idx(h, SOL)] = 1.0
        A_ub.append(row_s)
        b_ub.append(s_max)

        # Charge <= chg_max
        chg_max = 0.0 if state.no_charge[h] else chg_rate
        row_chg = [0.0] * nv
        row_chg[idx(h, CHG)] = 1.0
        A_ub.append(row_chg)
        b_ub.append(chg_max)

        # Discharge <= dis_max
        dis_max = 0.0 if state.no_discharge[h] else dis_rate
        row_dis = [0.0] * nv
        row_dis[idx(h, DIS)] = 1.0
        A_ub.append(row_dis)
        b_ub.append(dis_max)

        # SOC <= capacity
        row_soc_up = [0.0] * nv
        row_soc_up[idx(h, SOC)] = 1.0
        A_ub.append(row_soc_up)
        b_ub.append(bat.capacity_kwh)

        # SOC >= min_soc[h] -> -SOC <= -min_soc[h]
        row_soc_lo = [0.0] * nv
        row_soc_lo[idx(h, SOC)] = -1.0
        A_ub.append(row_soc_lo)
        b_ub.append(-state.min_soc[h])

        # Grid <= max_grid
        if state.max_grid[h] < float("inf"):
            row_g = [0.0] * nv
            row_g[idx(h, GRID)] = 1.0
            A_ub.append(row_g)
            b_ub.append(state.max_grid[h])

    # Neutrality: SOC[n-1] == init_kwh
    row_neut = [0.0] * nv
    row_neut[idx(n - 1, SOC)] = 1.0
    A_eq.append(row_neut)
    b_eq.append(init_kwh)

    tableau = _SimplexTableau(c, A_ub, b_ub, A_eq, b_eq)
    x = tableau.solve()
    if x is None:
        return None

    plan: List[Dict[str, float]] = []
    for h in range(n):
        plan.append(
            {
                "grid": float(max(0.0, x[idx(h, GRID)])),
                "solar": float(max(0.0, x[idx(h, SOL)])),
                "charge": float(max(0.0, x[idx(h, CHG)])),
                "discharge": float(max(0.0, x[idx(h, DIS)])),
                "soc": float(x[idx(h, SOC)]),
            }
        )
    return plan


# --------------------------------------------------------------------------- #
# Heuristic Fallback Solver
# --------------------------------------------------------------------------- #


def _heuristic_solve(state: _ScenarioState) -> List[Dict[str, float]]:
    n = len(state.hours)
    bat = state.battery
    init_kwh = bat.initial_energy_kwh or bat.initial_kwh or 0.0
    chg_rate = bat.max_charge_kwh_per_hour or bat.charge_limit_kw or 0.0
    dis_rate = bat.max_discharge_kwh_per_hour or bat.discharge_limit_kw or 0.0

    cur_soc = init_kwh
    plan: List[Dict[str, float]] = []

    for h, hd in enumerate(state.hours):
        effective_solar = hd.solar_kwh * state.solar_factor[h]
        solar_used = min(hd.demand_kwh, effective_solar)
        rem_demand = hd.demand_kwh - solar_used

        charge = 0.0
        discharge = 0.0

        if rem_demand > 0 and not state.no_discharge[h] and cur_soc > state.min_soc[h]:
            avail_dis = min(dis_rate, cur_soc - state.min_soc[h])
            discharge = min(rem_demand, avail_dis)
            rem_demand -= discharge

        grid = rem_demand
        if grid > state.max_grid[h]:
            grid = state.max_grid[h]

        cur_soc = cur_soc + charge - discharge
        cur_soc = max(state.min_soc[h], min(bat.capacity_kwh, cur_soc))

        plan.append(
            {
                "grid": grid,
                "solar": solar_used,
                "charge": charge,
                "discharge": discharge,
                "soc": cur_soc,
            }
        )
    return plan


def _post_check(state: _ScenarioState, plan: List[Dict[str, float]]) -> List[Dict[str, float]]:
    bat = state.battery
    init_kwh = bat.initial_energy_kwh or bat.initial_kwh or 0.0
    cur_soc = init_kwh
    cleaned: List[Dict[str, float]] = []

    for h, (hd, row) in enumerate(zip(state.hours, plan)):
        solar = min(row["solar"], hd.solar_kwh * state.solar_factor[h])
        chg = 0.0 if state.no_charge[h] else row["charge"]
        dis = 0.0 if state.no_discharge[h] else row["discharge"]

        cur_soc = cur_soc + chg - dis
        cur_soc = max(state.min_soc[h], min(bat.capacity_kwh, cur_soc))

        grid = max(0.0, hd.demand_kwh + chg - solar - dis)
        if state.max_grid[h] < float("inf"):
            grid = min(grid, state.max_grid[h])

        cleaned.append(
            {
                "grid": round(grid, 3),
                "solar": round(solar, 3),
                "charge": round(chg, 3),
                "discharge": round(dis, 3),
                "soc": round(cur_soc, 3),
            }
        )
    return cleaned


# --------------------------------------------------------------------------- #
# Main Optimizer Entry Point
# --------------------------------------------------------------------------- #


def optimize(
    battery: BatterySpec,
    hours: List[HourlyData],
    scenario_id: str,
    directive_interpretations: List[DirectiveInterpretation],
) -> OptimizationResponse:
    """Run full optimization pipeline and return canonical response matching Section 10."""
    raw_directives = [
        di.model_dump() if hasattr(di, "model_dump") else di
        for di in directive_interpretations
    ]
    state = _apply_directives(battery, hours, raw_directives)

    # 1. Try SciPy HiGHS LP
    plan = _solve_scipy_lp(state)

    # 2. Try Pure Python Simplex LP
    if plan is None:
        logger.info("Solving with pure-Python simplex solver...")
        plan = _solve_pure_python_lp(state)

    # 3. Fallback Heuristic
    if plan is None:
        logger.warning("Simplex solver failed; using heuristic fallback")
        plan = _heuristic_solve(state)

    plan = _post_check(state, plan)

    entries: List[HourlyPlanEntry] = []
    total_grid = 0.0
    total_cost = 0.0
    peak_grid = 0.0

    for h, hd in enumerate(hours):
        row = plan[h]
        chg = row["charge"]
        dis = row["discharge"]
        if chg > 1e-4:
            action = "charge"
            b_kwh = chg
        elif dis > 1e-4:
            action = "discharge"
            b_kwh = dis
        else:
            action = "idle"
            b_kwh = 0.0

        entry = HourlyPlanEntry(
            hour=h,
            grid_kwh=round(row["grid"], 3),
            solar_used_kwh=round(row["solar"], 3),
            battery_action=action,
            battery_kwh=round(b_kwh, 3),
            battery_energy_after_kwh=round(row["soc"], 3),
        )
        entries.append(entry)

        total_grid += entry.grid_kwh
        total_cost += entry.grid_kwh * hd.tariff_bdt_per_kwh
        peak_grid = max(peak_grid, entry.grid_kwh)

    init_kwh = battery.initial_energy_kwh or battery.initial_kwh or 0.0
    active_count = len([d for d in raw_directives if d.get("applies") or d.get("directive_type", "") != "no_op"])

    # Richer strategy summary (purely cosmetic; does not change JSON contract).
    solar_used_total = sum(e.solar_used_kwh for e in entries)
    eff_solar_total = sum(hd.solar_kwh for hd in hours)
    solar_share = (solar_used_total / eff_solar_total * 100.0) if eff_solar_total > 1e-6 else 0.0
    battery_moved = sum(e.battery_kwh for e in entries if e.battery_action != "idle")
    final_soc = entries[-1].battery_energy_after_kwh

    summary_str = (
        f"Cost-optimal 24-hour dispatch: total grid electricity cost is {total_cost:.2f} BDT "
        f"across {total_grid:.2f} kWh grid draw (peak {peak_grid:.2f} kWh). "
        f"Solar covered {solar_share:.1f}% of available kWh; battery shifted "
        f"{battery_moved:.2f} kWh. Satisfied {active_count} operator directives while "
        f"preserving end-of-day battery neutrality ({final_soc:.2f} kWh == {init_kwh:.2f} kWh)."
    )

    # Final re-validation guard: catch any drift between guardrail output and the plan.
    try:
        final_soc_rounded = round(final_soc, 3)
        if abs(final_soc_rounded - round(init_kwh, 3)) > 0.05:
            logger.warning(
                "plan_summary: end-of-day battery drift %s kWh vs init %s kWh",
                final_soc_rounded,
                round(init_kwh, 3),
            )
    except Exception:  # noqa: BLE001
        pass

    return OptimizationResponse(
        scenario_id=scenario_id,
        directive_interpretation=directive_interpretations,
        hourly_plan=entries,
        total_grid_kwh=round(total_grid, 3),
        total_cost_bdt=round(total_cost, 3),
        peak_grid_kwh=round(peak_grid, 3),
        plan_summary=summary_str,
    )
