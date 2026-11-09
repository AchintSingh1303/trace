"""Requirement import from CSV text.

Columns (header names are case-insensitive, order does not matter):
  title                 required
  type                  Functional, UI, API, Security, Performance or Journey (default Functional)
  description           optional
  acceptance_criteria   optional; separate several with | or new lines
  code                  optional; generated when blank
"""

from __future__ import annotations

import csv
import io
import re

from .agents import REQ_TYPES

TEMPLATE = (
    "code,type,title,description,acceptance_criteria\n"
    'RQ-001,Functional,User login,Registered users sign in with email and password,'
    '"A valid user reaches the dashboard|A wrong password shows an error|The account locks after 5 failed attempts"\n'
)

_TYPES = {t.lower(): t for t in REQ_TYPES}


def parse_requirements(text: str) -> tuple[list[dict], list[dict]]:
    """Return (rows, errors). Each error names the CSV line so it can be fixed in the file."""
    text = text.lstrip("﻿")
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        return [], [{"line": 1, "error": "The file is empty"}]
    headers = {(h or "").strip().lower().replace(" ", "_"): h for h in reader.fieldnames}
    if "title" not in headers:
        return [], [{"line": 1, "error": "A 'title' column is required"}]

    def cell(row: dict, name: str) -> str:
        key = headers.get(name)
        return (row.get(key) or "").strip() if key else ""

    rows, errors = [], []
    seen: set[str] = set()
    for line, row in enumerate(reader, start=2):
        if not any((v or "").strip() for v in row.values() if isinstance(v, str)):
            continue
        title = cell(row, "title")
        if not title:
            errors.append({"line": line, "error": "Title is empty"})
            continue
        req_type = _TYPES.get(cell(row, "type").lower() or "functional")
        if not req_type:
            errors.append({"line": line, "error": f"Unknown type '{cell(row, 'type')}'. Use one of {', '.join(REQ_TYPES)}"})
            continue
        code = cell(row, "code").upper() or None
        if code and code in seen:
            errors.append({"line": line, "error": f"Code {code} appears twice in the file"})
            continue
        if code:
            seen.add(code)
        criteria = [c.strip() for c in re.split(r"[|\n]", cell(row, "acceptance_criteria")) if c.strip()]
        rows.append(
            {
                "code": code,
                "title": title[:300],
                "req_type": req_type,
                "description": cell(row, "description"),
                "acceptance_criteria": criteria,
            }
        )
    return rows, errors
