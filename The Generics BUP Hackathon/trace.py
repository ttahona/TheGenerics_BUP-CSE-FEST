from app.services.llm_interpreter import _classify_note, _extract_hours, _match_percent

cases = [
    "Reduce solar by 75% during hours 12 and 13.",
    "Drop solar to 25 percent between 12:00 and 14:00.",
    "Solar generation is at 25% for hours 12,13.",
    "Cut solar output to ~25 percent of its rated value for hour 12 and hour 13.",
    "Block battery charging from hour 18 through 20.",
    "No battery charging between hour 18 and hour 20 (inclusive).",
    "Don't let the battery charge during 18, 19, 20.",
    "Refuse to charge the battery at hours 18, 19, and 20.",
    "Cap grid draw at 6 kWh from hour 18 to 20.",
    "Maximum grid import 6 kWh between hours 18 and 20.",
    "Hold grid usage under 6 kWh for hours 18, 19, 20.",
    "Limit grid pull to 6 kWh at hour 18 through hour 20.",
    "Just keep things running as normal.",
    "No special instructions today.",
    "Nothing to report.",
    "All systems nominal.",
]
for n in cases:
    h = _extract_hours(n)
    p = _match_percent(n)
    d = _classify_note(n)
    print(f"{n[:55]:55s} | hours={str(h):25s} pct={str(p):6s} | type={d['directive_type']:20s}")
