"""Step 1 of the eval pipeline: extract the baseline messaging.

Pulls every piece of copy the construction campaigns have used so far and
writes a human-readable ``evals/data/baseline_messages.csv``. The CSV is grouped
into sections by *signal* — the angle a message leans on — so a reviewer can
read all the copy for one angle in one place:

    FREE IMPLEMENTATION   — the free bid teardown / pilot offer
    PAIN POINT            — estimators re-keying bid line items by hand
    ROI                   — hours clawed back per bid cycle ("4 hours")
    COMPANY SIGNAL        — news/personalized openers ("Saw <company> …")
    GENERAL / SHARED      — the base opener everyone receives

Under each signal header, every row pairs the **subject** and **body** used,
with a ``note`` saying what kind of message it is (opener, follow-up, generated
email, …) and ``sent_count`` recipients reached.

``sent_count`` counts *step-1 opener* sends only — that is the volume the send
ledger records (e.g. the 552 "want 4 hours back" openers). Follow-ups go out
later on the 18-day cadence and aren't individually tracked, so they show 0
(queued, not yet delivered) rather than inheriting the opener's volume.

Three sources feed it, in order of trust:

1. **Briefs** (``briefs/construction*.yaml``) — author-written copy: hook angles
   (the signal definitions), the value subject, the step-1 opener template, and
   every follow-up. Same code path the loader uses, so it matches what ships.
2. **Local DB** (``outbound.db``) — the *generated* copy that actually went out
   (per-lead subjects/bodies/first-lines from the ``contacts`` table).
3. **Smartlead** (optional ``--smartlead``) — live deployed sequences. Networked,
   so off by default.

Read-only: no generation, scoring, or rating happens here. Run it with::

    python -m evals.extract                 # briefs + DB -> baseline CSV
    python -m evals.extract --smartlead     # also pull live campaign copy
"""

from __future__ import annotations

import argparse
import csv
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Optional

from sqlalchemy import bindparam, text

from outbound import brief as brief_mod
from outbound.config import get_database_url
from outbound.db import Database
from outbound.load import smartlead
from outbound.models import Brief, ContactStatus

# --- What counts as "sent" / "replied" --------------------------------------
SENT_STATUSES = frozenset({
    ContactStatus.LOADED,
    ContactStatus.SENDING,
    ContactStatus.REPLIED,
    ContactStatus.BOUNCED,
    ContactStatus.DONE,
})
REPLY_STATUSES = frozenset({ContactStatus.REPLIED})
REPLY_EVENT_TYPES = frozenset({"reply", "positive_reply"})

DEFAULT_PREFIX = "construction"
DEFAULT_OUT = Path(__file__).resolve().parent / "data" / "baseline_messages.csv"

CSV_COLUMNS = ["signal", "note", "subject", "body", "sent_count"]

# --- Signals (message angles) -----------------------------------------------
# Ordered for display. The label is the section header; the angle is filled in
# from the briefs' hooks at runtime.
FREE = "free_implementation"
PAIN = "pain_point"
ROI = "roi"
COMPANY = "company_signal"
GENERAL = "general"

SIGNAL_ORDER = [FREE, PAIN, ROI, COMPANY, GENERAL]
SIGNAL_LABELS = {
    FREE: "FREE IMPLEMENTATION",
    PAIN: "PAIN POINT",
    ROI: "ROI / HOURS BACK",
    COMPANY: "COMPANY SIGNAL (personalized)",
    GENERAL: "GENERAL / SHARED",
}
# Fallback angle text if a brief doesn't define the hook.
SIGNAL_DEFAULT_ANGLE = {
    FREE: "Free bid-extraction pilot / teardown offer",
    PAIN: "Estimators re-keying bid line items by hand",
    ROI: "Hours clawed back per bid cycle",
    COMPANY: "News/personalized opener tied to the company",
    GENERAL: "Base opener and shared copy",
}

_FREE_KW = ("free", "teardown", "pilot", "extraction", "run it free",
            "send back", "send a free")
_PAIN_KW = ("re-key", "rekey", "re-keying", "by hand", "manual", "backlog",
            "spreadsheet", "tax on", "line items")
_ROI_KW = ("hours", "hour", "back per bid", "per bid cycle", "time back",
           "clawed back", "4 hours")


@dataclass
class Message:
    """One subject/body pair tied to a signal, with its recipient count."""
    signal: str
    note: str
    subject: str
    body: str
    sent_count: int


# --- Small helpers -----------------------------------------------------------

def _to_text(s: str) -> str:
    """Flatten an HTML email body to readable plain text, keeping merge vars."""
    if not s:
        return ""
    if "<" not in s:
        return s.strip()
    try:
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(s, "lxml")
        for br in soup.find_all("br"):
            br.replace_with("\n")
        paras = [p.get_text(" ", strip=True) for p in soup.find_all("p")]
        if paras:
            return "\n\n".join(p for p in paras if p)
        return soup.get_text(" ", strip=True)
    except Exception:
        return s.strip()


def _classify(text_: str) -> str:
    """Assign a message to a signal by keyword. Heuristic, best-effort.

    Product-angle keywords win first (free > pain > roi); failing those, a
    "Saw …" opener or a ``company's …`` possessive marks a personalized company
    signal; everything else is general.
    """
    t = (text_ or "").lower()
    if any(k in t for k in _FREE_KW):
        return FREE
    if any(k in t for k in _PAIN_KW):
        return PAIN
    if any(k in t for k in _ROI_KW):
        return ROI
    if t.startswith("saw ") or "'s " in t:
        return COMPANY
    return GENERAL


def _load_briefs(prefix: str) -> list[Brief]:
    briefs: list[Brief] = []
    for name in brief_mod.list_briefs():
        if not name.startswith(prefix):
            continue
        try:
            briefs.append(brief_mod.load_brief(name))
        except Exception as exc:
            print(f"  ! skipping brief '{name}': {exc}")
    return briefs


# --- DB access ---------------------------------------------------------------

@dataclass
class _DbMessaging:
    contacts: list[dict[str, Any]]
    reply_emails: set[str]


def _db_messaging(db: Database, industry: str) -> _DbMessaging:
    """Fetch the contact-level copy and reply set for a single brief."""
    with db.engine.connect() as conn:
        rows = conn.execute(text(
            "SELECT email, cell, subject, body, first_line, status "
            "FROM contacts WHERE brief = :b"
        ), {"b": industry}).fetchall()
        contacts = [dict(r._mapping) for r in rows]

        # Emails with a reply event in the ledger (constants, so safe to bind).
        ev = conn.execute(
            text("SELECT DISTINCT email FROM events WHERE type IN :types")
            .bindparams(bindparam("types", expanding=True)),
            {"types": list(REPLY_EVENT_TYPES)},
        ).fetchall()
        reply_emails = {r[0] for r in ev if r[0]}

    reply_emails |= {
        c["email"] for c in contacts if c["status"] in REPLY_STATUSES and c["email"]
    }
    return _DbMessaging(contacts=contacts, reply_emails=reply_emails)


def _sent(contacts: Iterable[dict[str, Any]]) -> int:
    return sum(1 for c in contacts if c["status"] in SENT_STATUSES)


# --- Signal angles from the briefs' hooks -----------------------------------

def _signal_angles(briefs: list[Brief]) -> dict[str, str]:
    """Map each signal to the most descriptive hook angle across the briefs."""
    angles = dict(SIGNAL_DEFAULT_ANGLE)
    for b in briefs:
        for h in b.hooks:
            sig = h.get("id")
            angle = (h.get("angle") or "").strip()
            if sig in angles and angle and len(angle) > len(angles[sig]):
                angles[sig] = angle
    return angles


# --- Gathering messages ------------------------------------------------------

def gather_messages(briefs: list[Brief], db: Optional[Database],
                    include_smartlead: bool = False) -> list[Message]:
    """Collect subject/body pairs from briefs + DB (+ Smartlead), deduped.

    Identical (signal, subject, body) triples are merged and their recipient
    counts summed, so the CSV shows each distinct message once.
    """
    msgs: list[Message] = []

    for b in briefs:
        ind = b.industry
        m = b.messaging
        msg = _db_messaging(db, ind) if db is not None else None
        brief_sent = _sent(msg.contacts) if msg else 0

        # Brief-authored subject lines (the value/B variant + any template).
        for key, note in (("subject_value_fallback", "value subject"),
                          ("subject_template", "subject template")):
            val = (m.get(key) or "").strip()
            if val:
                msgs.append(Message(_classify(val), note, val, "", brief_sent))

        # Step-1 opener body (skip in full mode — it's generated per lead).
        if b.personalization_mode != "full":
            try:
                _, body_html = smartlead._email_template(
                    b, b.hooks[0].get("id") if b.hooks else "all")
                opener = _to_text(body_html)
            except Exception:
                opener = ""
            if opener:
                msgs.append(Message(GENERAL, "opener body", "", opener, brief_sent))

        # Automated follow-ups (sequence steps 2+). These go out days after the
        # opener on the 18-day cadence; the send ledger only records step-1
        # opener sends, and none of these have fired yet, so sent_count is 0
        # (queued copy, not yet delivered) — never the step-1 volume.
        for i, fu in enumerate(m.get("follow_ups", []), start=1):
            body = _to_text((fu.get("body") or "").strip())
            if body:
                msgs.append(Message(_classify(body), f"follow-up {i}", "", body, 0))

        # Generated, per-lead copy from the DB, paired subject + body.
        if msg:
            groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
            for c in msg.contacts:
                subj = (c.get("subject") or "").strip()
                body = _to_text((c.get("body") or "").strip())
                # first_line is the personalized opener when there's no full body.
                if not body:
                    body = (c.get("first_line") or "").strip()
                if not (subj or body):
                    continue
                groups.setdefault((subj, body), []).append(c)
            for (subj, body), contacts in groups.items():
                note = "generated email" if subj and body else (
                    "generated subject" if subj else "personalized opener")
                signal = _classify(subj or body)
                msgs.append(Message(signal, note, subj, body, _sent(contacts)))

    if include_smartlead:
        msgs += _gather_smartlead([b.industry for b in briefs])

    return _dedupe(msgs)


def _gather_smartlead(industries: list[str]) -> list[Message]:
    """Live campaign sequences from Smartlead (optional, networked)."""
    if not os.environ.get("SMARTLEAD_API_KEY"):
        print("  ! SMARTLEAD_API_KEY not set — skipping Smartlead source")
        return []
    try:
        campaigns = smartlead._list_campaigns()
    except Exception as exc:
        print(f"  ! could not list Smartlead campaigns: {exc}")
        return []

    out: list[Message] = []
    for camp in campaigns:
        name = str(camp.get("name", ""))
        ind = next((i for i in industries if name.startswith(f"{i} —")), None)
        if not ind:
            continue
        cid = str(camp.get("id"))
        try:
            sent = len(smartlead.fetch_campaign_replies(cid))
        except Exception:
            sent = 0
        try:
            resp = smartlead.request_with_retry(
                "GET", smartlead._url(f"/campaigns/{cid}/sequences"))
            data = resp.json()
            seqs = data if isinstance(data, list) else data.get("data", [])
        except Exception as exc:
            print(f"  ! could not fetch sequence for '{name}': {exc}")
            continue
        for seq in seqs:
            n = seq.get("seq_number")
            subj = (seq.get("subject") or "").strip()
            body = _to_text((seq.get("email_body") or "").strip())
            if subj or body:
                out.append(Message(_classify(subj or body), f"live step {n}",
                                   subj, body, sent))
    return out


def _dedupe(msgs: list[Message]) -> list[Message]:
    """Merge identical (signal, subject, body), summing recipient counts."""
    merged: dict[tuple[str, str, str], Message] = {}
    for msg in msgs:
        key = (msg.signal, msg.subject, msg.body)
        if key in merged:
            merged[key].sent_count += msg.sent_count
        else:
            merged[key] = Message(**vars(msg))
    return list(merged.values())


# --- Output ------------------------------------------------------------------

def write_csv(messages: list[Message], angles: dict[str, str],
              out_path: Path) -> Path:
    """Write the grouped CSV: a header row per signal, messages beneath it."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(CSV_COLUMNS)
        for sig in SIGNAL_ORDER:
            rows = [m for m in messages if m.signal == sig]
            if not rows:
                continue
            # Section header row naming the signal + its angle.
            writer.writerow([
                f"━━ {SIGNAL_LABELS[sig]} — {angles.get(sig, '')} ━━",
                "", "", "", "",
            ])
            for m in sorted(rows, key=lambda r: (-r.sent_count, r.note)):
                writer.writerow(["", m.note, m.subject, m.body, m.sent_count])
    return out_path


# --- Orchestration -----------------------------------------------------------

def extract(prefix: str = DEFAULT_PREFIX,
            database_url: Optional[str] = None,
            include_smartlead: bool = False) -> tuple[list[Message], dict[str, str]]:
    briefs = _load_briefs(prefix)
    if not briefs:
        print(f"  ! no briefs matched prefix '{prefix}'")
    db = Database(database_url or get_database_url())
    db.init()
    messages = gather_messages(briefs, db, include_smartlead=include_smartlead)
    return messages, _signal_angles(briefs)


def main(argv: Optional[list[str]] = None) -> None:
    parser = argparse.ArgumentParser(
        description="Extract baseline outbound messaging into a grouped CSV.")
    parser.add_argument("--prefix", default=DEFAULT_PREFIX,
                        help="brief name prefix to include (default: construction)")
    parser.add_argument("--out", default=str(DEFAULT_OUT), help="output CSV path")
    parser.add_argument("--db", default=None, help="database URL override")
    parser.add_argument("--smartlead", action="store_true",
                        help="also pull live campaign copy from Smartlead (network)")
    args = parser.parse_args(argv)

    messages, angles = extract(prefix=args.prefix, database_url=args.db,
                               include_smartlead=args.smartlead)
    out = write_csv(messages, angles, Path(args.out))

    by_sig: dict[str, int] = {}
    for m in messages:
        by_sig[m.signal] = by_sig.get(m.signal, 0) + 1
    summary = ", ".join(f"{SIGNAL_LABELS[s]}={by_sig[s]}"
                        for s in SIGNAL_ORDER if s in by_sig)
    print(f"Wrote {len(messages)} messages to {out}\n  {summary}")


if __name__ == "__main__":
    main()
