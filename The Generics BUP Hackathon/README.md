# ⚡ GridWise — LLM Energy Optimizer

[![FastAPI](https://img.shields.io/badge/FastAPI-0.115.0-009688.svg?style=flat&logo=FastAPI&logoColor=white)](https://fastapi.tiangolo.com)
[![Python](https://img.shields.io/badge/Python-3.12+-3776AB.svg?style=flat&logo=Python&logoColor=white)](https://www.python.org/)
[![Docker](https://img.shields.io/badge/Docker-Ready-2496ED.svg?style=flat&logo=Docker&logoColor=white)](https://www.docker.com/)
[![License](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)



> **Built for the BUP CSE Fest 2026 Hackathon Preliminary.**  
> Compliant with the **Problem Statement** & **Participant Guide & Evaluation Rubric**, and styled with an authentic **Nothing / CMF** industrial aesthetic.

GridWise turns free-form operator notes into a deterministic, cost-optimal 24-hour
microgrid dispatch plan. Operator text is interpreted by a **multi-provider LLM
(OpenAI / Gemini / Anthropic)** — with a **pure-rule deterministic fallback**
when no API key is configured — then fed through a layered guardrail and solved
by an **HiGHS-powered Linear Program** (with a pure-Python Simplex and a
heuristic last-resort fallback if SciPy is unavailable).

The whole service is exposed through **two thin FastAPI endpoints**, ships as a
**single-container Docker image**, and runs identically on Python 3.12 or inside
`docker-compose up`.

---

## 📐 Architecture

```
                ┌───────────────────────────────┐
                │  Operator notes (free text)   │
                └──────────────┬────────────────┘
                               ▼
   ┌────────────────────────────────────────────────────────────┐
   │  1. LLM Interpreter  (services/llm_interpreter.py)         │
   │     • OpenAI / Gemini / Anthropic (HTTP via httpx)         │
   │     • Pure-rule fallback (handles every canonical phrasing)│
   │     • 12 s upstream timeout, transparent on cache hit      │
   └────────────────────────────────────────────────────────────┘
                               ▼
   ┌────────────────────────────────────────────────────────────┐
   │  2. Guardrails  (services/guardrails.py)                   │
   │     • Drops unknown directive types                        │
   │     • Clamps hour ranges to 0-23                           │
   │     • Normalizes structured_adjustment payloads            │
   └────────────────────────────────────────────────────────────┘
                               ▼
   ┌────────────────────────────────────────────────────────────┐
   │  3. Optimizer  (services/optimizer.py)                      │
   │     • SciPy HiGHS Linear Program (cost-optimal dispatch)   │
   │     • Pure-Python Simplex fallback (if SciPy unavailable)  │
   │     • Heuristic last-resort                                │
   │     • Re-validation pass for end-of-day battery neutrality │
   └────────────────────────────────────────────────────────────┘
                               ▼
                ┌──────────────────────────────┐
                │   Canonical JSON response    │
                └──────────────────────────────┘
```

**Cross-cutting layers**

- `services/llm_cache.py` — thread-safe OrderedDict LRU cache, 256 entries ×
  15 min TTL, key = `SHA-256(provider || model || normalized notes)`.
  Opt-in via `ENABLE_LLM_CACHE`.
- `app/main.py` — lifespan context manager pre-warms
  `scipy.optimize.linprog` on boot, removing the first-request JIT cost.
- `routes/energy.py` — hard 28 s request deadline (configurable via
  `REQUEST_TIMEOUT_SECONDS`) returns HTTP 504 on overrun.

---

## 🗂 Project Layout

```
BUP Hackathon/
├── Dockerfile                  # python:3.12-slim, single uvicorn worker
├── docker-compose.yml          # host-env var injection + healthcheck
├── README.md                   # ← this file
├── backend/
│   ├── requirements.txt        # pinned, free-of-charge dependencies
│   ├── .env.example            # every supported env var + provider key
│   └── app/
│       ├── main.py             # FastAPI app + lifespan pre-warm
│       ├── config.py           # typed settings (LLM_PROVIDER, timeouts…)
│       ├── routes/energy.py    # /health, /health/llm, /optimize-energy
│       ├── schemas/energy.py   # Pydantic v2 request/response models
│       └── services/
│           ├── llm_interpreter.py   # multi-provider + rule fallback
│           ├── llm_cache.py         # thread-safe LRU cache
│           ├── guardrails.py        # input validation + semantic clamp
│           └── optimizer.py         # LP dispatch + re-validation
├── frontend/
│   ├── index.html              # Nothing / CMF styled dashboard
│   ├── app.js                  # fetch-driven UI
│   └── styles.css              # monospaced industrial aesthetic
├── samples/                    # 10 canonical + 3 paraphrased cases
└── tests/
    ├── conftest.py             # schema-only validation harness
    ├── test_smoke.py           # /health contract
    ├── test_public_samples.py  # all 10 public samples
    ├── test_standalone.py      # no-SciPy code path
    └── test_hidden_paraphrases.py  # 11.4 paraphrase robustness
```

---

## 🏗️ Architecture & Pipeline

```
┌───────────────────────────────┐
│     Operator Notes (NL)       │ e.g. "Facilities will wash rooftop panels from noon to 2 PM.
└───────────────┬───────────────┘       Solar output is roughly 25% of forecast."
                │
                ▼
┌───────────────────────────────┐
│       LLM Interpreter         │ OpenAI (GPT-4o/mini), Google Gemini, or Anthropic
│   (Multi-Provider + Rules)    │ with pure rule-based fallback for 100% offline reliability
└───────────────┬───────────────┘
                │
                ▼
┌───────────────────────────────┐
│   Deterministic Guardrails    │ Strict range & type checks: hours in [0..23], factor in [0..1]
│        & Normalizer           │ Demotes malformed directives to safe 'no_op'
└───────────────┬───────────────┘
                │
                ▼
┌───────────────────────────────┐
│     Optimization Engine       │ Global cost minimization via Linear Programming:
│ (SciPy HiGHS / Simplex PurePy)│ • Hourly energy balance: Grid + Solar + Dis == Demand + Chg
└───────────────┬───────────────┘ • Battery neutrality: SOC[23] == Initial SOC
                │
                ▼
┌───────────────────────────────┐
│   REST API & CMF Web UI       │ Strict canonical JSON contract + interactive bento dashboard
└───────────────────────────────┘
```

---

## ⚡ Quick Start

### Prerequisites
- Python 3.10+ or Docker
- Optional: `OPENAI_API_KEY`, `GEMINI_API_KEY`, or `ANTHROPIC_API_KEY` (runs with automatic rule-based NLP fallback if keys are omitted).

### 1. Local Setup
```bash
# Clone and enter directory
cd "BUP Hackathon"

# Install dependencies
pip install -r backend/requirements.txt

# Run server
uvicorn app.main:app --app-dir backend --host 0.0.0.0 --port 8000
```
On Windows PowerShell:
```powershell
& "$env:USERPROFILE\python_embed\python.exe" -m uvicorn app.main:app --app-dir backend --host 0.0.0.0 --port 8000
```

Open your browser at **`http://localhost:8000`** to access the web dashboard.

---

## 🔐 Environment Variables

All variables are read from the **process environment** first, then from a
local `.env` if one is mounted (uncomment `env_file:` in
`docker-compose.yml`). `backend/.env.example` is the canonical reference.

| Variable                    | Default                          | Description                                            |
| --------------------------- | -------------------------------- | ------------------------------------------------------ |
| `LLM_PROVIDER`              | `auto`                           | `auto` / `openai` / `gemini` / `anthropic` / `off`     |
| `OPENAI_API_KEY`            | _none_                           | OpenAI key (skipped if absent + provider ≠ openai)     |
| `OPENAI_MODEL`              | `gpt-4o-mini`                    | OpenAI chat model                                      |
| `GEMINI_API_KEY`            | _none_                           | Google AI Studio key                                   |
| `GEMINI_MODEL`              | `gemini-1.5-flash`               | Gemini model                                           |
| `ANTHROPIC_API_KEY`         | _none_                           | Anthropic key                                          |
| `ANTHROPIC_MODEL`           | `claude-3-5-sonnet-latest`       | Anthropic model                                        |
| `LLM_RESPONSE_TIMEOUT_SECONDS` | `12`                           | Per-call upstream budget for each LLM provider         |
| `REQUEST_TIMEOUT_SECONDS`   | `28`                             | Hard deadline for the full `/optimize-energy` pipeline |
| `ENABLE_LLM_CACHE`          | `1`                              | Toggle the response LRU cache (`1` = on, `0` = off)     |
| `HOST`                      | `0.0.0.0`                        | Bind address                                           |
| `PORT`                      | `8000`                           | Bind port                                              |

> **Compatibility note:** every dependency listed in
> `backend/requirements.txt` is permissive-licensed (MIT / BSD / Apache-2.0);
> no commercial-only, paid, or copyleft component is required.

---

## 📡 Endpoints

| Endpoint                | Purpose                                                |
| ----------------------- | ------------------------------------------------------ |
| `GET  /health`          | Liveness probe + SciPy / cache diagnostics             |
| `GET  /health/llm`      | Provider status, cache stats, configurable timeouts    |
| `POST /optimize-energy` | Runs the interpretation + optimization pipeline        |

The request / response schemas are declared in `app/schemas/energy.py` and are
locked to the **Problem Statement** contract.

---

## 🧪 Tests

```bash
# run all unit + schema tests
pytest -v

# run the no-SciPy fallback path
python tests/test_standalone.py

# replay the public-sample corpus
python tests/test_public_samples.py
```

Public sample cases live in `samples/` (10 canonical + 3 paraphrased fixtures
matching the **Hidden Test Set** style guide from the Participant Guide). The
additional `tests/test_hidden_paraphrases.py` covers the three paraphrase forms
described in Section 11.4 of the Participant Guide.

---

## 🐳 Docker Deployment & Fallback Image

GridWise comes with self-contained Docker support:

```bash
# Build Docker image
docker build -t gridwise-optimizer:latest .

# Run container
docker run -p 8000:8000 gridwise-optimizer:latest
```

Using Docker Compose:
```bash
docker-compose up --build
```

### Fallback image (no-PyPI environment)

The image ships a **pure-Python Simplex** path that exercises the optimizer
without SciPy. If `pip install` cannot reach PyPI during evaluation,
`tests/test_standalone.py` still drives a full end-to-end request through the
service:

```bash
python tests/test_standalone.py
```

### Registry build (publishing the image)

```bash
docker tag gridwise-optimizer:latest <registry>/gridwise-optimizer:1.1.0
docker push <registry>/gridwise-optimizer:1.1.0
```

The `Dockerfile` is built on `python:3.12-slim` with a single Uvicorn worker
(LP state is per-process and intentionally not shared between workers).

---

## ⚠️ Known Limitations

- The LLM cache is in-memory only (per process). A `docker restart` clears it.
- The pure-Python Simplex is **slower** than HiGHS — expect ~20–40 ms on
  extreme 24 h scenarios (still well below the 5 s SLA).
- When `LLM_PROVIDER=off`, the rule fallback supports the canonical paraphrases
  in `samples/` and the Section 11.4 paraphrased forms (see
  `tests/test_hidden_paraphrases.py`).
- Only `OPENAI_*`, `GEMINI_*`, and `ANTHROPIC_*` providers are supported — no
  other closed/open LLM endpoints.

---

## 📄 License & Attribution

This is an academic submission for the **BUP CSE Fest 2026 Hackathon
Preliminary**. All third-party dependencies retain their original licenses
(MIT, BSD, Apache-2.0); see `backend/requirements.txt` for the exact pin list.

---

## 🔌 Canonical API Specification

### 1. `GET /health`
Readiness check endpoint for orchestrators and judges (Problem Statement Section 6.2).
- **Response `200 OK`**:
```json
{
  "status": "ok"
}
```

### 2. `GET /health/llm`
Diagnostics endpoint reporting LLM readiness.
- **Response `200 OK`**:
```json
{
  "status": "ok",
  "llm_enabled": true,
  "provider": "openai",
  "model": "gpt-4o-mini"
}
```

### 3. `POST /optimize-energy`
Primary optimization pipeline matching Problem Statement Sections 7 & 10.

#### Request Body
```json
{
  "scenario_id": "GRID-101",
  "operator_notes": [
    "Solar output will drop to about 20% from 1 PM to 3 PM.",
    "Do not charge the battery between 2 PM and 4 PM.",
    "The cafeteria menu changes tomorrow."
  ],
  "hours": [
    { "hour": 0, "demand_kwh": 180, "solar_kwh": 0, "tariff_bdt_per_kwh": 7 },
    { "hour": 1, "demand_kwh": 170, "solar_kwh": 0, "tariff_bdt_per_kwh": 6.5 },
    ... 22 more hourly entries ...,
    { "hour": 23, "demand_kwh": 200, "solar_kwh": 0, "tariff_bdt_per_kwh": 9 }
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

#### Canonical Response Body
```json
{
  "scenario_id": "GRID-101",
  "directive_interpretation": [
    {
      "note_index": 0,
      "applies": true,
      "directive_type": "solar_reduction",
      "structured_adjustment": {
        "hours": [13, 14],
        "factor": 0.2
      },
      "explanation": "Solar output adjusted by factor 0.2 during specified hours."
    },
    {
      "note_index": 1,
      "applies": true,
      "directive_type": "no_charge_window",
      "structured_adjustment": {
        "hours": [14, 15]
      },
      "explanation": "Battery charging forbidden during hours [14, 15]."
    },
    {
      "note_index": 2,
      "applies": false,
      "directive_type": "no_op",
      "structured_adjustment": null,
      "explanation": "This note does not affect today's 24-hour energy schedule."
    }
  ],
  "hourly_plan": [
    {
      "hour": 0,
      "grid_kwh": 180.0,
      "solar_used_kwh": 0.0,
      "battery_action": "idle",
      "battery_kwh": 0.0,
      "battery_energy_after_kwh": 200.0
    }
  ],
  "total_grid_kwh": 2450.0,
  "total_cost_bdt": 18950.0,
  "peak_grid_kwh": 180.0,
  "plan_summary": "Cost-optimal 24-hour dispatch: total grid electricity cost is 18950.00 BDT across 2450.00 kWh grid draw (peak 180.00 kWh). Satisfied 2 operator directives while preserving end-of-day battery neutrality (200.00 kWh == 200.00 kWh)."
}
```

---

## 📏 Mathematical Optimization Formulation

The objective is to minimize total electricity cost from the grid over 24 hours (hours 0 to 23):

$$\min \sum_{h=0}^{23} \text{tariff}[h] \times \text{grid}[h]$$

### Constraints
1. **Hourly Energy Balance**:
   $$\text{grid}[h] + \text{solar\_used}[h] + \text{battery\_discharge}[h] = \text{demand}[h] + \text{battery\_charge}[h]$$
2. **Solar Usability**:
   $$0 \le \text{solar\_used}[h] \le \text{solar\_forecast}[h] \times \text{solar\_factor}[h]$$
3. **Battery State of Charge (SOC) Dynamics**:
   - For $h = 0$:
     $$\text{SOC}[0] = \text{initial\_energy\_kwh} + \text{battery\_charge}[0] - \text{battery\_discharge}[0]$$
   - For $h \ge 1$:
     $$\text{SOC}[h] = \text{SOC}[h-1] + \text{battery\_charge}[h] - \text{battery\_discharge}[h]$$
4. **Reserve & Capacity Bounds**:
   $$\max(\text{minimum\_energy\_kwh}, \text{directive\_minimum\_energy\_kwh}[h]) \le \text{SOC}[h] \le \text{capacity\_kwh}$$
5. **Battery Neutrality (End-of-Day Invariant)**:
   $$\text{SOC}[23] = \text{initial\_energy\_kwh}$$
6. **Operating Limits & Windows**:
   - $0 \le \text{battery\_charge}[h] \le (0 \text{ if in no\_charge\_window else max\_charge\_kwh\_per\_hour})$
   - $0 \le \text{battery\_discharge}[h] \le (0 \text{ if in no\_discharge\_window else max\_discharge\_kwh\_per\_hour})$
   - $0 \le \text{grid}[h] \le \text{max\_grid\_kwh}$

---

## 🧪 Automated Testing

Run the test suite:
```bash
pytest -v
```

Tests include:
- `test_exact_energy_balance`: Checks balance equation for all 24 hours.
- `test_exact_soc_dynamics_and_neutrality`: Validates step-by-step SOC progression and end-of-day neutrality.
- `test_solar_reduction_and_no_charge_directives`: Verifies solar reduction factor and forbidden charge windows.
- `test_canonical_request_shape`: Verifies top-level hours and battery schema matching Section 7.
- `test_paraphrased_notes`: Tests LLM and rule-based resilience to alternate phrasings.
- `test_guardrails_rejects_out_of_range`: Tests graceful handling of malformed input.

---

## 🎨 Nothing / CMF Design Highlights
- **Dot Matrix Display Engine**: LED pip indicators, segmented readouts, and NDot typography.
- **Translucent Bento Grid**: High-contrast dark theme with iconic CMF Orange (`#FF4400`) and Nothing Red (`#D71920`) accents.
- **Interactive Visualizers**:
  - Stacked Energy Contribution Chart (Solar vs. Battery vs. Grid).
  - Continuous Battery SOC Trajectory Curve with Min Floor indicator.
  - Price Arbitrage Matrix aligning grid intake with lowest tariff windows.
- **Operator Utilities**: 1-click presets, quick note templates, and CSV export.

---

## 🛡️ License
MIT License. Built for the BUP Hackathon 2026.
