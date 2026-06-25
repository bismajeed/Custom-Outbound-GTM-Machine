"""Signal-scan companies from a captured CSV — NO Apollo (works while credits are out).

Reads a previously-captured company list (domain/name/location/tech), skips ones
already in the DB, runs the free job-scrape signal + DuckDuckGo/Haiku news signal on
the rest, and inserts them as QUALIFIED with a signal_summary so they're ready for
enrichment later. Technology is read from the CSV (already tagged), so no Apollo call
is made at all.

Usage: python scripts/signal_scan_csv.py [industry] [csv_path] [workers]
"""

from __future__ import annotations

import csv
import json
import os
import sys
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

from dotenv import load_dotenv

load_dotenv()

from outbound.brief import load_brief                       # noqa: E402
from outbound.db import Database                            # noqa: E402
from outbound.models import Company, CompanyStatus          # noqa: E402
from outbound.pipeline import signals_jobs, signals_news    # noqa: E402

_lock = threading.Lock()
_done = 0


def main() -> None:
    industry = sys.argv[1] if len(sys.argv) > 1 else "construction"
    csv_path = sys.argv[2] if len(sys.argv) > 2 else \
        f"output/{industry}/companies_with_tech_signal.csv"
    workers = int(sys.argv[3]) if len(sys.argv) > 3 else 8
    brief = load_brief(industry)

    db = Database(os.environ.get("DATABASE_URL", "sqlite:///outbound.db"))
    db.init()
    with db.engine.connect() as conn:
        from sqlalchemy import text
        in_db = {r[0] for r in conn.execute(
            text("SELECT domain FROM companies WHERE brief=:b"), {"b": industry})}

    rows = list(csv.DictReader(open(csv_path, encoding="utf-8-sig")))
    todo = [r for r in rows if r["domain"] not in in_db]
    total = len(todo)
    print(f"{len(rows)} in CSV, {len(in_db)} already in DB -> {total} to scan "
          f"({workers} workers, no Apollo)\n", flush=True)

    def work(r):
        global _done
        techs = [t for t in (r.get("technologies_matched") or "").split("; ") if t]
        c = Company(
            domain=r["domain"], name=r.get("name", ""),
            city=r.get("city", ""), state=r.get("state", ""),
            founded_year=int(r["founded_year"]) if r.get("founded_year") else None,
            technologies=techs, brief=industry, status=CompanyStatus.QUALIFIED,
        )
        job = news = ""
        cost = 0.0
        try:
            passed, ev = signals_jobs.has_job_signal(c, brief)
            if passed:
                job = ev
            nr = signals_news.research_news(c, brief)
            cost = nr.get("cost_usd", 0.0)
            if nr["passed"]:
                news = nr["summary"]
        except Exception as exc:
            job = job or f"(error: {str(exc)[:60]})"
        # best signal for messaging: news > job > tech
        best = news or job or (f"tech_adoption: uses {', '.join(techs)}" if techs else "")
        c.signal_summary = best or None
        with _lock:
            _done += 1
            d = _done
        flags = "+".join(f for f, v in (("T", techs), ("J", job), ("N", news)) if v) or "none"
        print(f"[{d}/{total}] {c.name[:34]:34} {flags}", flush=True)
        return c, news, job, techs, cost

    results = []
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = [ex.submit(work, r) for r in todo]
        for f in as_completed(futs):
            results.append(f.result())

    # Insert into DB (single-threaded).
    inserted = news_n = job_n = tech_n = any_n = 0
    news_cost = 0.0
    for c, news, job, techs, cost in results:
        news_cost += cost
        if news: news_n += 1
        if job: job_n += 1
        if techs: tech_n += 1
        if c.signal_summary: any_n += 1
        if db.insert_company(c):
            inserted += 1
        else:
            db.update_company(c.domain, status=CompanyStatus.QUALIFIED,
                              technologies=json.dumps(c.technologies),
                              signal_summary=c.signal_summary)
        inserted = inserted  # noqa

    print("\n=== APOLLO-FREE SIGNAL SCAN COMPLETE ===")
    print(f"companies scanned + loaded: {len(results)}")
    print(f"  technology signal: {tech_n}")
    print(f"  job signal:        {job_n}")
    print(f"  news signal:       {news_n}")
    print(f"  ANY signal:        {any_n}  | no signal -> free_implementation: {len(results)-any_n}")
    print(f"news cost (Haiku): ${news_cost:.2f}  | Apollo cost: $0")
    print("These are now QUALIFIED in the DB, ready for enrichment when credits return.")


if __name__ == "__main__":
    main()
