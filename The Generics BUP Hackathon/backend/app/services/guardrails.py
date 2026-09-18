"""Deterministic validation of LLM-generated directives.

Directives are required to be well-formed JSON with concrete numeric/structural
fields matching Problem Statement Section 4.1 & Section 8. Unsupported types,
out-of-range numbers, out-of-range hours, or invented constraints are safely rejected.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

ALLOWED_TYPES = {
    "solar_reduction",
    "minimum_battery_reserve",
    "no_charge_window",
    "no_discharge_window",
    "max_grid_window",
    "no_op",
}


class GuardrailError(ValueError):
    """Raised when a directive fails validation."""


def _check_hours(hours: Any, where: str, default_all: bool = False) -> List[int]:
    if hours is None or hours == []:
        if default_all:
            return list(range(24))
        raise GuardrailError(f"{where}: 'hours' must be a non-empty list of integers")
    if not isinstance(hours, list):
        raise GuardrailError(f"{where}: 'hours' must be a list of integers")
    out: List[int] = []
    seen = set()
    for h in hours:
        if not isinstance(h, int) or isinstance(h, bool):
            raise GuardrailError(f"{where}: hour entries must be integers")
        if h < 0 or h > 23:
            raise GuardrailError(f"{where}: hour {h} out of 0..23 range")
        if h in seen:
            continue
        seen.add(h)
        out.append(h)
    if not out:
        if default_all:
            return list(range(24))
        raise GuardrailError(f"{where}: 'hours' list cannot be empty")
    return sorted(out)


def validate_directive_dict(payload: Dict[str, Any], *, index: int) -> Dict[str, Any]:
    """Validate and normalize one raw directive dict into the canonical contract.

    Returns a normalized dictionary with 'type', 'applies', 'structured_adjustment', 'explanation'.
    Raises GuardrailError on any structural problem.
    """
    if not isinstance(payload, dict):
        raise GuardrailError(f"directive #{index}: must be a JSON object")

    dtype = payload.get("directive_type") or payload.get("type")
    if not isinstance(dtype, str) or dtype not in ALLOWED_TYPES:
        raise GuardrailError(
            f"directive #{index}: unsupported type {dtype!r}; "
            f"allowed: {sorted(ALLOWED_TYPES)}"
        )

    # Allow nested structured_adjustment or flat fields
    struct = payload.get("structured_adjustment")
    data = struct if isinstance(struct, dict) else payload

    if dtype == "solar_reduction":
        hours = _check_hours(data.get("hours", []), f"directive #{index}")
        factor = data.get("factor")
        if not isinstance(factor, (int, float)) or isinstance(factor, bool):
            raise GuardrailError(f"directive #{index}: 'factor' must be a number")
        factor = float(factor)
        if factor < 0.0 or factor > 1.0:
            raise GuardrailError(f"directive #{index}: 'factor' must be in [0,1]")
        return {
            "type": dtype,
            "hours": hours,
            "factor": factor,
            "structured_adjustment": {"hours": hours, "factor": factor},
        }

    if dtype == "minimum_battery_reserve":
        min_kwh = data.get("minimum_energy_kwh")
        if min_kwh is None:
            min_kwh = data.get("min_kwh")
        if not isinstance(min_kwh, (int, float)) or isinstance(min_kwh, bool):
            raise GuardrailError(f"directive #{index}: 'minimum_energy_kwh' must be a number")
        min_kwh = float(min_kwh)
        if min_kwh < 0:
            raise GuardrailError(f"directive #{index}: 'minimum_energy_kwh' must be >= 0")
        hours = _check_hours(data.get("hours", []), f"directive #{index}", default_all=True)
        return {
            "type": dtype,
            "hours": hours,
            "minimum_energy_kwh": min_kwh,
            "min_kwh": min_kwh,
            "structured_adjustment": {"hours": hours, "minimum_energy_kwh": min_kwh},
        }

    if dtype in {"no_charge_window", "no_discharge_window"}:
        hours = _check_hours(data.get("hours", []), f"directive #{index}")
        return {
            "type": dtype,
            "hours": hours,
            "structured_adjustment": {"hours": hours},
        }

    if dtype == "max_grid_window":
        hours = _check_hours(data.get("hours", []), f"directive #{index}")
        max_kwh = data.get("max_grid_kwh")
        if max_kwh is None:
            max_kwh = data.get("max_kwh")
        if not isinstance(max_kwh, (int, float)) or isinstance(max_kwh, bool):
            raise GuardrailError(f"directive #{index}: 'max_grid_kwh' must be a number")
        max_kwh = float(max_kwh)
        if max_kwh < 0:
            raise GuardrailError(f"directive #{index}: 'max_grid_kwh' must be >= 0")
        return {
            "type": dtype,
            "hours": hours,
            "max_grid_kwh": max_kwh,
            "max_kwh": max_kwh,
            "structured_adjustment": {"hours": hours, "max_grid_kwh": max_kwh},
        }

    # no_op
    reason = payload.get("explanation") or payload.get("reason") or "Irrelevant or unconstrained note"
    return {
        "type": "no_op",
        "reason": str(reason)[:200],
        "structured_adjustment": None,
    }


def validate_directives(raw_list: List[Any]) -> List[Dict[str, Any]]:
    """Validate an ordered list of raw directive dicts."""
    if not isinstance(raw_list, list):
        raise GuardrailError("directives must be a list")
    return [validate_directive_dict(item, index=i) for i, item in enumerate(raw_list)]
