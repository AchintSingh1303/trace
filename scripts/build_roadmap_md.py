"""Regenerate ROADMAP.md from app/roadmap.json so the UI and the docs never drift.

    python scripts/build_roadmap_md.py
"""

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
data = json.loads((ROOT / "app" / "roadmap.json").read_text(encoding="utf-8"))

SECTIONS = [
    ("shipped", "Built in this demo"),
    ("next", "Considering next"),
    ("later", "Later"),
    ("parked", "Tried and parked"),
]

lines = ["# Roadmap", "", data["intro"], ""]
for status, title in SECTIONS:
    items = [i for i in data["items"] if i["status"] == status]
    if not items:
        continue
    lines += [f"## {title}", ""]
    for item in items:
        effort = f" · effort: {item['effort']}" if item.get("effort") else ""
        lines += [
            f"### {item['title']}",
            f"*{item['area']}{effort}*",
            "",
            item["summary"],
            "",
            f"**Why:** {item['why']}",
            "",
        ]
lines += ["## Principles behind this list", ""]
lines += [f"- {principle}" for principle in data["principles"]]
lines.append("")

(ROOT / "ROADMAP.md").write_text("\n".join(lines), encoding="utf-8")
print("Wrote ROADMAP.md")
