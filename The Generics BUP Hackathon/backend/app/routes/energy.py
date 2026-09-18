"""Endpoints implementing the public HTTP API contract."""
from __future__ import annotations

import logging
import time
from typing import List

from fastapi import APIRouter, HTTPException

from ..config import active_llm_provider, settings
from ..schemas.energy import (
    DirectiveInterpretation,
    OptimizationRequest,
    OptimizationResponse,
)
from ..services import llm_cache
from ..services.guardrails import GuardrailError
from ..services.llm_interpreter import interpret_notes
from ..services.optimizer import optimize

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get("/health")
def health() -> dict:
    """Readiness check required by the spec (Problem Statement Section 6.2)."""
    return {"status": "ok"}


@router.get("/health/llm")
def health_llm() -> dict:
    """Diagnostic: report which interpreter is active."""
    provider = active_llm_provider()
    model = None
    if provider == "openai":
        model = settings.openai_model
    elif provider == "gemini":
        model = settings.gemini_model
    elif provider == "anthropic":
        model = settings.anthropic_model

    cache_stats = llm_cache.stats()
    return {
        "status": "ok",
        "llm_enabled": provider is not None,
        "provider": provider or "rules_fallback",
        "model": model,
        "cache": cache_stats,
        "request_timeout_seconds": settings.request_timeout_seconds,
        "llm_timeout_seconds": settings.llm_response_timeout_seconds,
    }


@router.post("/optimize-energy", response_model=OptimizationResponse)
def optimize_energy(payload: OptimizationRequest) -> OptimizationResponse:
    """Main pipeline: interpret notes → validate → optimize → return plan.

    A hard 28-second ceiling (configurable via REQUEST_TIMEOUT_SECONDS) guards
    against a stalled LLM upstream violating the spec's per-request timeout.
    """
    deadline = time.monotonic() + settings.request_timeout_seconds
    try:
        notes = payload.operator_notes or []
        battery = payload.battery or (payload.scenario.battery if payload.scenario else None)
        hours = payload.hours or (payload.scenario.hours if payload.scenario else None)
        scenario_id = payload.scenario_id or (payload.scenario.scenario_id if payload.scenario else "scenario-1")

        if not battery or not hours or len(hours) != 24:
            raise ValueError("Must provide battery specification and exactly 24 hourly entries")

        # Path A: caller supplied a manually-edited interpretation (e.g. from
        # the dashboard's directive editor). Validate it against the canonical
        # schema, apply the same applies clamp, and skip the LLM/rule pass.
        if payload.optimized_directives:
            interpretations: List[DirectiveInterpretation] = []
            for d in payload.optimized_directives:
                struct = d.structured_adjustment if d.applies else None
                explanations = d.explanation or ""
                interpretations.append(
                    DirectiveInterpretation(
                        note_index=d.note_index,
                        applies=d.applies,
                        directive_type=d.directive_type,
                        structured_adjustment=struct,
                        explanation=explanations,
                        reason=explanations,
                    )
                )
        else:
            # Path B: standard interpretation pipeline.
            if not notes:
                notes = ["Run baseline economic dispatch. No special operator directives today."]
            raw_interpreted = interpret_notes(notes, battery=battery)
            interpretations = []
            for d in raw_interpreted:
                dtype = d["directive_type"]
                applies = dtype != "no_op"
                struct = d.get("structured_adjustment") if applies else None
                expl = d.get("explanation", "")
                interpretations.append(
                    DirectiveInterpretation(
                        note_index=d["note_index"],
                        applies=applies,
                        directive_type=dtype,
                        structured_adjustment=struct,
                        explanation=expl,
                        reason=expl,
                    )
                )

        if time.monotonic() > deadline:
            raise TimeoutError("deadline exceeded before optimization")

        response = optimize(
            battery=battery,
            hours=hours,
            scenario_id=scenario_id,
            directive_interpretations=interpretations,
        )
        return response
    except GuardrailError as exc:
        logger.warning("Guardrail failure: %s", exc)
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except TimeoutError as exc:
        logger.warning("Request deadline exceeded: %s", exc)
        raise HTTPException(status_code=504, detail="request deadline exceeded") from exc
    except Exception as exc:  # pragma: no cover - last-resort safety net
        logger.exception("Unhandled error: %s", exc)
        raise HTTPException(status_code=500, detail="internal optimizer error") from exc
