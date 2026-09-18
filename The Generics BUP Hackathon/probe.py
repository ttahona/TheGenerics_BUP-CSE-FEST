"""Probe the interpreter."""

with open("probe_started.txt", "w") as f:
    f.write("started\n")

import json
import os
import sys

os.environ["LLM_PROVIDER"] = "off"
for k in ("OPENAI_API_KEY", "GEMINI_API_KEY", "ANTHROPIC_API_KEY"):
    os.environ.pop(k, None)

sys.path.insert(0, "backend")
from app.services.llm_interpreter import _extract_hours, _match_percent, _classify_note

with open("probe_imports.txt", "w") as f:
    f.write("imports ok\n")

cases = [
    "Reduce solar by 75% during hours 12 and 13.",
    "Cut solar output to ~25 percent of its rated value for hour 12 and hour 13.",
    "Drop solar to 25 percent between 12:00 and 14:00.",
    "Solar generation is at 25% for hours 12,13.",
    "Block battery charging from hour 18 through 20.",
    "No battery charging between hour 18 and hour 20 (inclusive).",
    "Don't let the battery charge during 18, 19, 20.",
    "Refuse to charge the battery at hours 18, 19, and 20.",
    "Cap grid draw at 6 kWh from hour 18 to 20.",
    "Hold grid usage under 6 kWh for hours 18, 19, 20.",
    "Limit grid pull to 6 kWh at hour 18 through hour 20.",
    "Maximum grid import 6 kWh between hours 18 and 20.",
    "Just keep things running as normal.",
]

out = []
for c in cases:
    h = _extract_hours(c)
    p = _match_percent(c)
    d = _classify_note(c)
    out.append({"note": c, "hours": h, "pct": p, "type": d["directive_type"]})

with open("probe_out.json", "w") as f:
    json.dump(out, f, indent=2)
