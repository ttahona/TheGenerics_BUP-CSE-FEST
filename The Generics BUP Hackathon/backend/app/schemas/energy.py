"""Pydantic schemas matching the canonical BUP Hackathon GridWise API contract."""
from __future__ import annotations

from typing import Any, Dict, List, Literal, Optional, Union
from pydantic import BaseModel, Field, field_validator, model_validator


# ---------- Battery Specification ---------- #

class BatterySpec(BaseModel):
    """Battery specification for a 24-hour scenario (Problem Statement Section 7.3)."""

    capacity_kwh: float = Field(..., gt=0, description="Total battery capacity (kWh)")
    initial_energy_kwh: float = Field(0.0, ge=0, description="Initial energy at start of hour 0 (kWh)")
    minimum_energy_kwh: float = Field(0.0, ge=0, description="Base minimum reserve level (kWh)")
    max_charge_kwh_per_hour: float = Field(..., gt=0, description="Max charge rate (kWh per hour)")
    max_discharge_kwh_per_hour: float = Field(..., gt=0, description="Max discharge rate (kWh per hour)")

    # Legacy / alias field support
    initial_kwh: Optional[float] = None
    min_reserve_kwh: Optional[float] = None
    charge_limit_kw: Optional[float] = None
    discharge_limit_kw: Optional[float] = None

    @model_validator(mode="before")
    @classmethod
    def _normalize_battery_fields(cls, data: Any) -> Any:
        if isinstance(data, dict):
            if "initial_energy_kwh" not in data and "initial_kwh" in data:
                data["initial_energy_kwh"] = data["initial_kwh"]
            if "minimum_energy_kwh" not in data and "min_reserve_kwh" in data:
                data["minimum_energy_kwh"] = data["min_reserve_kwh"]
            if "max_charge_kwh_per_hour" not in data and "charge_limit_kw" in data:
                data["max_charge_kwh_per_hour"] = data["charge_limit_kw"]
            if "max_discharge_kwh_per_hour" not in data and "discharge_limit_kw" in data:
                data["max_discharge_kwh_per_hour"] = data["discharge_limit_kw"]
        return data

    @model_validator(mode="after")
    def _check_bounds(self) -> "BatterySpec":
        if self.initial_energy_kwh > self.capacity_kwh:
            raise ValueError("initial_energy_kwh cannot exceed capacity_kwh")
        if self.minimum_energy_kwh > self.capacity_kwh:
            raise ValueError("minimum_energy_kwh cannot exceed capacity_kwh")
        return self


# ---------- Hourly Data ---------- #

class HourlyData(BaseModel):
    """One hour of grid data (Problem Statement Section 7.2)."""

    hour: int = Field(..., ge=0, le=23)
    demand_kwh: float = Field(..., ge=0)
    solar_kwh: float = Field(0.0, ge=0)
    tariff_bdt_per_kwh: float = Field(..., ge=0)


class Scenario(BaseModel):
    """Scenario sub-object (optional wrapper for backwards compatibility)."""

    scenario_id: Optional[str] = None
    battery: Optional[BatterySpec] = None
    hours: Optional[List[HourlyData]] = None


# ---------- Canonical Directives & Adjustments ---------- #

class SolarReductionAdjustment(BaseModel):
    hours: List[int] = Field(..., min_length=1)
    factor: float = Field(..., ge=0.0, le=1.0)


class MinimumBatteryReserveAdjustment(BaseModel):
    hours: List[int] = Field(default_factory=lambda: list(range(24)))
    minimum_energy_kwh: float = Field(..., ge=0.0)


class NoChargeWindowAdjustment(BaseModel):
    hours: List[int] = Field(..., min_length=1)


class NoDischargeWindowAdjustment(BaseModel):
    hours: List[int] = Field(..., min_length=1)


class MaxGridWindowAdjustment(BaseModel):
    hours: List[int] = Field(..., min_length=1)
    max_grid_kwh: float = Field(..., ge=0.0)


# ---------- Canonical API Envelopes ---------- #

class DirectiveInterpretation(BaseModel):
    """Canonical directive interpretation entry per Problem Statement Section 10.2."""

    note_index: int = Field(..., ge=0)
    applies: bool
    directive_type: Literal[
        "solar_reduction",
        "minimum_battery_reserve",
        "no_charge_window",
        "no_discharge_window",
        "max_grid_window",
        "no_op",
    ]
    structured_adjustment: Optional[Dict[str, Any]] = None
    explanation: str = ""
    # Alias for `explanation` — surfaces the same content under both keys for
    # compatibility with test harnesses and client-side consumers.
    reason: Optional[str] = None


class HourlyPlanEntry(BaseModel):
    """Canonical hourly plan entry per Problem Statement Section 10.3."""

    hour: int = Field(..., ge=0, le=23)
    grid_kwh: float = Field(..., ge=0)
    solar_used_kwh: float = Field(..., ge=0)
    battery_action: Literal["charge", "discharge", "idle"]
    battery_kwh: float = Field(..., ge=0)
    battery_energy_after_kwh: float = Field(..., ge=0)


class OptimizationRequest(BaseModel):
    """Public request body for POST /optimize-energy matching Section 7.

    Accepts top-level hours and battery as specified in Section 7.1-7.4,
    as well as nested scenario objects for backwards compatibility.

    `optimized_directives` is an *optional* override that lets the client
    (typically the dashboard's directive editor) inject a manually-edited
    interpretation. When omitted, the server-side LLM / rule interpreter
    runs as usual. When present, the override must match the canonical
    DirectiveInterpretation schema.
    """

    scenario_id: str = Field(..., min_length=1)
    operator_notes: Optional[List[str]] = Field(default=None, max_length=3)
    # Alias for top-level "notes" string used by some test harnesses.
    # If `operator_notes` is not supplied but `notes` (string or list) is, the
    # interpreter will use it.
    notes: Optional[Union[str, List[str]]] = None
    hours: Optional[List[HourlyData]] = None
    battery: Optional[BatterySpec] = None
    scenario: Optional[Scenario] = None
    optimized_directives: Optional[List["DirectiveInterpretation"]] = None

    @field_validator("operator_notes")
    @classmethod
    def _validate_notes(cls, v):
        if v is None:
            return v
        if len(v) > 3:
            raise ValueError("operator_notes must contain at most 3 notes")
        for i, note in enumerate(v):
            if not isinstance(note, str) or not note.strip():
                raise ValueError(f"operator_notes[{i}] must be a non-empty string")
        return [n.strip() for n in v]

    @model_validator(mode="before")
    @classmethod
    def _merge_notes_alias(cls, data: Any) -> Any:
        if isinstance(data, dict) and not data.get("operator_notes"):
            notes = data.get("notes")
            if isinstance(notes, str):
                data["operator_notes"] = [notes]
            elif isinstance(notes, list):
                data["operator_notes"] = notes
        return data

    @model_validator(mode="after")
    def _ensure_hours_and_battery(self) -> "OptimizationRequest":
        if self.scenario is not None:
            if not self.hours and self.scenario.hours:
                self.hours = self.scenario.hours
            if not self.battery and self.scenario.battery:
                self.battery = self.scenario.battery

        if not self.hours or len(self.hours) != 24:
            raise ValueError("Must provide exactly 24 hourly entries for hours 0..23")

        # Validate unique ordered 0..23 hours
        hour_indices = [h.hour for h in self.hours]
        if hour_indices != list(range(24)):
            raise ValueError(f"hours must be ordered 0 through 23; got {hour_indices}")

        if not self.battery:
            raise ValueError("Must provide battery specification")
        return self


class OptimizationResponse(BaseModel):
    """Public response body for POST /optimize-energy matching Section 10.1."""

    scenario_id: str
    directive_interpretation: List[DirectiveInterpretation]
    hourly_plan: List[HourlyPlanEntry]
    total_grid_kwh: float
    total_cost_bdt: float
    peak_grid_kwh: float
    plan_summary: str


# Resolve the forward reference in OptimizationRequest for Pydantic v2.
OptimizationRequest.model_rebuild()
