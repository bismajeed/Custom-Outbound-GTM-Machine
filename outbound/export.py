"""CSV export of pipeline state — human-readable views of the DB.

The SQLite/Postgres DB is the source of truth (that's what makes the pipeline
idempotent and resumable). These exporters render the current state to CSV so you
can eyeball where every record is in the cascade. The ``status`` column tells you
which step each record reached:

  companies: SOURCED -> VALID -> JOB_SIGNAL -> QUALIFIED  (or DROPPED at any gate)
  contacts:  ENRICHED -> PERSONALIZED -> QUEUED -> LOADED -> ... (or SUPPRESSED)

Files are written under ``output/<industry>/``:
  companies.csv   every company + status + drop_reason + signal_summary
  contacts.csv    every contact + status + hook + first_line
  runs.csv        per-stage run log with counts + cost
"""

from __future__ import annotations

import csv
import json
import os
from pathlib import Path

from sqlalchemy import text

from .db import Database

DEFAULT_DIR = "output"


def _rows(db: Database, sql: str, params: dict) -> list[dict]:
    with db.engine.connect() as conn:
        result = conn.execute(text(sql), params)
        cols = result.keys()
        return [dict(zip(cols, row)) for row in result.fetchall()]


def _write_csv(path: Path, rows: list[dict], columns: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        for r in rows:
            writer.writerow(r)


def export_brief(db: Database, industry: str, out_dir: str = DEFAULT_DIR) -> dict[str, str]:
    """Write companies.csv, contacts.csv, runs.csv for one brief.

    Returns a map of {name: path} for what was written.
    """
    base = Path(out_dir) / industry
    written: dict[str, str] = {}

    companies = _rows(
        db,
        "SELECT domain, name, industry, employees, revenue_usd, technologies, "
        "status, drop_reason, signal_summary, sourced_at, updated_at "
        "FROM companies WHERE brief = :b ORDER BY sourced_at",
        {"b": industry},
    )
    # technologies is stored as a JSON array — flatten for readability.
    for c in companies:
        if c.get("technologies"):
            try:
                c["technologies"] = "|".join(json.loads(c["technologies"]))
            except (ValueError, TypeError):
                pass
    comp_path = base / "companies.csv"
    _write_csv(comp_path, companies, [
        "domain", "name", "industry", "employees", "revenue_usd", "technologies",
        "status", "drop_reason", "signal_summary", "sourced_at", "updated_at",
    ])
    written["companies"] = str(comp_path)

    contacts = _rows(
        db,
        "SELECT email, company_domain, first_name, last_name, title, seniority, "
        "linkedin, cell, first_line, subject, body, status, enriched_at, "
        "loaded_at, updated_at FROM contacts WHERE brief = :b ORDER BY enriched_at",
        {"b": industry},
    )
    contact_path = base / "contacts.csv"
    _write_csv(contact_path, contacts, [
        "email", "company_domain", "first_name", "last_name", "title", "seniority",
        "linkedin", "cell", "first_line", "subject", "body", "status",
        "enriched_at", "loaded_at", "updated_at",
    ])
    written["contacts"] = str(contact_path)

    runs = _rows(
        db,
        "SELECT run_id, stage, status, counts, started_at, finished_at "
        "FROM runs WHERE brief = :b ORDER BY started_at",
        {"b": industry},
    )
    # Unpack the counts JSON into flat columns for quick scanning.
    for r in runs:
        counts = {}
        if r.get("counts"):
            try:
                counts = json.loads(r["counts"])
            except (ValueError, TypeError):
                pass
        r["in"] = counts.get("in", "")
        r["out"] = counts.get("out", "")
        r["dropped"] = counts.get("dropped", "")
        cost = counts.get("cost_usd")
        r["cost_usd"] = round(cost, 4) if isinstance(cost, (int, float)) else ""
    runs_path = base / "runs.csv"
    _write_csv(runs_path, runs, [
        "run_id", "stage", "status", "in", "out", "dropped", "cost_usd",
        "started_at", "finished_at",
    ])
    written["runs"] = str(runs_path)

    return written
