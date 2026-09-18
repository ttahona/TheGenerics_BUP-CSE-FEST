# GridWise

### Natural Language → Deterministic Energy Optimization

GridWise is an intelligent microgrid optimization system that converts **free-form operator instructions into a cost-optimal 24-hour energy dispatch plan**.

Instead of requiring operators to manually translate operational notes into numerical constraints, GridWise interprets natural language, validates the resulting directives, and feeds them into a mathematical optimization engine that coordinates **grid power, solar generation, and battery storage**.

> **Built for the BUP CSE Fest 2026 Hackathon Preliminary**

---

## Overview

Energy operators often work with information that is not naturally expressed as structured data:

> *"Solar output will drop to around 25% between noon and 2 PM. Do not charge the battery during the afternoon."*

GridWise turns this kind of instruction into machine-actionable constraints.

The system follows a controlled pipeline:

```text
Operator Notes
      │
      ▼
┌──────────────────────┐
│ Natural Language     │
│ Interpretation       │
└──────────┬───────────┘
           ▼
┌──────────────────────┐
│ Guardrails &         │
│ Normalization        │
└──────────┬───────────┘
           ▼
┌──────────────────────┐
│ Linear Programming   │
│ Optimization         │
└──────────┬───────────┘
           ▼
┌──────────────────────┐
│ 24-Hour Dispatch     │
│ Plan + Explanation   │
└──────────────────────┘
```

The result is a structured, deterministic energy schedule rather than an LLM-generated recommendation.

---

## Why GridWise?

The key design principle is simple:

**Use AI to understand the operator. Use mathematics to make the decision.**

LLMs are useful for interpreting ambiguous human language, but they should not be responsible for calculating an energy dispatch plan.

GridWise therefore separates the two responsibilities:

| Layer           | Responsibility                                           |
| --------------- | -------------------------------------------------------- |
| LLM Interpreter | Understand natural-language operator notes               |
| Guardrails      | Validate, normalize and constrain interpreted directives |
| Optimizer       | Find the mathematically cost-optimal dispatch            |
| API             | Expose a deterministic machine-readable contract         |
| Dashboard       | Make the resulting plan understandable to operators      |

This separation makes the system easier to validate, test and deploy.

---

## Core Capabilities

### Natural-Language Energy Directives

Operators can provide instructions in ordinary language rather than manually constructing structured constraints.

Examples include:

* Solar production reductions
* Battery charge restrictions
* Battery discharge restrictions
* Minimum battery reserve requirements
* Time-specific operating constraints
* Irrelevant operational notes

GridWise converts applicable instructions into structured directives before optimization.

---

### Multi-Provider LLM Interpretation

GridWise supports:

* OpenAI
* Google Gemini
* Anthropic

The provider layer is abstracted from the optimization engine, allowing the interpretation component to change without modifying the mathematical optimization pipeline.

When an API key is unavailable, GridWise can fall back to a deterministic rule-based interpreter for supported canonical and paraphrased directives.

---

### Deterministic Guardrails

LLM output is never passed directly into the optimizer.

The guardrail layer:

* Rejects unknown directive types
* Clamps hour ranges to `0–23`
* Validates numerical factors
* Normalizes structured adjustments
* Safely converts malformed directives into `no_op`

For example:

```text
LLM Output
   │
   ▼
Validation
   │
   ├── Valid ──────────► Optimization
   │
   └── Invalid ────────► Safe no_op
```

This creates a controlled boundary between probabilistic language interpretation and deterministic computation.

---

## Optimization Engine

GridWise formulates the 24-hour dispatch problem as a **Linear Programming optimization problem**.

The objective is to minimize total electricity cost:

$$
\min \sum_{h=0}^{23} tariff[h] \times grid[h]
$$

subject to operational and physical constraints.

### Energy Balance

For every hour:

$$
grid[h] + solar\_used[h] + battery\_discharge[h]
=
demand[h] + battery\_charge[h]
$$

### Solar Constraints

Solar usage cannot exceed the available forecast after directive adjustments:

$$
0 \le solar\_used[h]
\le solar\_forecast[h] \times solar\_factor[h]
$$

### Battery Dynamics

The battery state evolves according to:

$$
SOC[h] = SOC[h-1] + charge[h] - discharge[h]
$$

while respecting capacity and minimum reserve limits.

### End-of-Day Neutrality

GridWise enforces:

$$
SOC[23] = initial\_energy
$$

This ensures the optimizer cannot artificially reduce today's cost by leaving the battery depleted at the end of the planning horizon.

---

## Optimization Stack

The system uses a layered optimization strategy:

```text
SciPy HiGHS Linear Programming
             │
             ▼
     Pure-Python Simplex
             │
             ▼
      Heuristic Fallback
```

The primary solver uses **SciPy HiGHS** for cost-optimal dispatch.

If SciPy is unavailable, GridWise includes a pure-Python Simplex implementation, followed by a heuristic last-resort path. This allows the application to remain usable in constrained evaluation environments.

---

## System Architecture

```text
                    OPERATOR
                       │
                       │ Natural-language notes
                       ▼
              ┌─────────────────┐
              │ LLM Interpreter │
              │                 │
              │ OpenAI          │
              │ Gemini          │
              │ Anthropic       │
              │ Rule Fallback   │
              └────────┬────────┘
                       │
                       ▼
              ┌─────────────────┐
              │   Guardrails    │
              │                 │
              │ Type validation │
              │ Range checks    │
              │ Normalization   │
              └────────┬────────┘
                       │
                       ▼
              ┌─────────────────┐
              │   Optimizer     │
              │                 │
              │ HiGHS LP        │
              │ Simplex         │
              │ Fallback        │
              └────────┬────────┘
                       │
                       ▼
              ┌─────────────────┐
              │ Canonical JSON  │
              │ Response        │
              └────────┬────────┘
                       │
                       ▼
              ┌─────────────────┐
              │ Web Dashboard   │
              │                 │
              │ Cost            │
              │ Grid            │
              │ Solar           │
              │ Battery SOC     │
              └─────────────────┘
```

---

## Reliability by Design

GridWise is designed to remain deterministic even when the language interpretation layer is not.

### LLM Fallback

If no LLM provider is configured, the system automatically falls back to rule-based interpretation for supported directive patterns.

### LLM Response Cache

An optional thread-safe LRU cache reduces repeated interpretation requests.

* 256 entries
* 15-minute TTL
* SHA-256 based cache keys
* Configurable through `ENABLE_LLM_CACHE`

### Request Deadline

The optimization endpoint operates under a configurable hard request deadline and returns HTTP `504` if the complete pipeline exceeds the allowed time.

### Pre-Warmed Optimization

The application pre-warms the optimization backend during startup to reduce first-request overhead.

---

## Dashboard

The frontend is designed around a compact industrial control-room aesthetic inspired by Nothing / CMF design language.

The dashboard provides:

* Energy contribution visualization
* Battery state-of-charge trajectory
* Grid consumption
* Cost information
* Price-arbitrage visualization
* Operator presets
* Quick note templates
* CSV export

The visual system uses a dark interface with restrained industrial accents, segmented readouts and a translucent bento-grid layout.

---

## Technology

| Component       | Technology                  |
| --------------- | --------------------------- |
| Backend         | Python 3.12+                |
| API             | FastAPI                     |
| Validation      | Pydantic v2                 |
| Optimization    | SciPy HiGHS                 |
| Fallback Solver | Pure-Python Simplex         |
| LLM Providers   | OpenAI / Gemini / Anthropic |
| HTTP Client     | HTTPX                       |
| Frontend        | HTML / CSS / JavaScript     |
| Deployment      | Docker / Docker Compose     |
| Testing         | Pytest                      |

---

## Project Structure

```text
GridWise/
│
├── Dockerfile
├── docker-compose.yml
├── README.md
│
├── backend/
│   ├── requirements.txt
│   ├── .env.example
│   │
│   └── app/
│       ├── main.py
│       ├── config.py
│       │
│       ├── routes/
│       │   └── energy.py
│       │
│       ├── schemas/
│       │   └── energy.py
│       │
│       └── services/
│           ├── llm_interpreter.py
│           ├── llm_cache.py
│           ├── guardrails.py
│           └── optimizer.py
│
├── frontend/
│   ├── index.html
│   ├── app.js
│   └── styles.css
│
├── samples/
│
└── tests/
    ├── conftest.py
    ├── test_smoke.py
    ├── test_public_samples.py
    ├── test_standalone.py
    └── test_hidden_paraphrases.py
```

---

## Getting Started

### Requirements

* Python 3.10+
* or Docker

LLM API keys are optional.

Supported environment variables include:

```text
OPENAI_API_KEY
GEMINI_API_KEY
ANTHROPIC_API_KEY
```

Without an API key, the deterministic rule-based fallback can be used for supported directives.

### Local Installation

```bash
git clone <repository-url>
cd "BUP Hackathon"

pip install -r backend/requirements.txt

uvicorn app.main:app \
  --app-dir backend \
  --host 0.0.0.0 \
  --port 8000
```

Then open:

```text
http://localhost:8000
```

---

## Docker

Build the image:

```bash
docker build -t gridwise-optimizer:latest .
```

Run:

```bash
docker run -p 8000:8000 gridwise-optimizer:latest
```

Or use Docker Compose:

```bash
docker-compose up --build
```

The project includes a self-contained fallback path designed to remain functional even when SciPy/PyPI availability is restricted during evaluation.

---

## API

### `GET /health`

Basic readiness and system diagnostics.

```json
{
  "status": "ok"
}
```

### `GET /health/llm`

Reports LLM configuration and provider status.

```json
{
  "status": "ok",
  "llm_enabled": true,
  "provider": "openai",
  "model": "gpt-4o-mini"
}
```

### `POST /optimize-energy`

Runs the complete interpretation → validation → optimization pipeline.

Example:

```json
{
  "scenario_id": "GRID-101",
  "operator_notes": [
    "Solar output will drop to about 20% from 1 PM to 3 PM.",
    "Do not charge the battery between 2 PM and 4 PM."
  ],
  "hours": [
    {
      "hour": 0,
      "demand_kwh": 180,
      "solar_kwh": 0,
      "tariff_bdt_per_kwh": 7
    }
  ],
  "battery": {
    "capacity_kwh": 500,
    "initial_energy_kwh": 200,
    "minimum_energy_kwh": 50,
    "max_charge_kwh_per_hour": 100,
    "max_discharge_kwh_per_hour": 100
  }
}
```

The response contains:

* Directive interpretation
* Structured adjustments
* Hour-by-hour dispatch
* Grid consumption
* Solar utilization
* Battery actions
* Battery state
* Total grid energy
* Total cost
* Peak grid usage
* Optimization summary

The API follows the canonical request and response structure defined for the hackathon problem.

---

## Testing

GridWise includes automated tests covering both correctness and resilience.

Run the complete suite:

```bash
pytest -v
```

Standalone fallback path:

```bash
python tests/test_standalone.py
```

Public sample replay:

```bash
python tests/test_public_samples.py
```

### Test Coverage

The test suite validates:

* Exact hourly energy balance
* Battery SOC progression
* End-of-day battery neutrality
* Solar reduction directives
* Battery charge restrictions
* Canonical API schema
* Paraphrased natural-language directives
* Guardrail handling of invalid input
* No-SciPy execution path

The repository includes canonical and paraphrased sample cases intended to exercise the interpretation layer under alternate phrasings.

---

## Design Principles

### 01 — AI for Interpretation

Natural language is handled by the LLM layer.

### 02 — Mathematics for Decisions

The final energy schedule is produced through constrained optimization rather than generated text.

### 03 — Safety Before Optimization

Every interpreted directive passes through deterministic validation.

### 04 — Graceful Degradation

The system can operate without an LLM API and includes a solver fallback when SciPy is unavailable.

### 05 — Reproducibility

Given the same normalized inputs and optimization constraints, the dispatch calculation is deterministic.

---

## What Makes GridWise Different

Most AI systems stop at:

```text
Natural Language → AI Response
```

GridWise goes further:

```text
Natural Language
       ↓
Structured Directives
       ↓
Validated Constraints
       ↓
Mathematical Optimization
       ↓
Physically Constrained Dispatch
       ↓
Auditable Energy Plan
```

The LLM does not decide how the microgrid should operate.

It translates the operator's intent.

The optimizer then determines the feasible schedule under explicit mathematical constraints.

That distinction is at the core of GridWise.

---

## Current Limitations

* The LLM cache is currently in-memory and per-process.
* Restarting the container clears cached responses.
* The pure-Python Simplex implementation is slower than HiGHS.
* Rule-based interpretation is limited to supported directive patterns when LLM providers are disabled.
* Currently supported LLM providers are OpenAI, Gemini and Anthropic.

---

## License

MIT License.

Built as an academic submission for the **BUP CSE Fest 2026 Hackathon Preliminary**. Third-party dependencies retain their respective MIT, BSD or Apache-2.0 licenses.
