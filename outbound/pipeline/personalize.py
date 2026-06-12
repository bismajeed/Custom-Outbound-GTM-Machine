"""Personalization — turn a qualified contact + company signal into copy.

Ported from the legacy personalization logic. Governed by prompts/messaging.md
(the committed voice + hard rules). Two modes:

- ``first_line`` (default): generate only the {{first_line}} variable; the rest
  of the email is a fixed template in Smartlead.
- ``full``: generate subject + body.

Assigns each contact a hook (``cell``) round-robin across the brief's hooks,
deterministically by email so re-runs are idempotent.

Contract: ``personalize(contact, company, brief) -> Contact``
"""

from __future__ import annotations

import json
import os
import re
import zlib
from functools import lru_cache
from pathlib import Path
from typing import Any

import anthropic
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from ..models import Brief, Company, Contact, ContactStatus

MODEL = "claude-haiku-4-5-20251001"
PRICE_INPUT_PER_MTOK = 0.80
PRICE_OUTPUT_PER_MTOK = 4.00

_PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"


@lru_cache(maxsize=1)
def _messaging_prompt() -> str:
    try:
        return (_PROMPTS_DIR / "messaging.md").read_text(encoding="utf-8")
    except OSError:
        return "Write plain, specific, honest cold-email copy. No hype, no emojis."


def pick_hook(contact: Contact, hooks: list[dict[str, Any]]) -> dict[str, Any]:
    """Deterministic round-robin: stable hash of email -> hook index."""
    if not hooks:
        return {"id": "default", "angle": ""}
    idx = zlib.crc32(contact.email.encode("utf-8")) % len(hooks)
    return hooks[idx]


def subject_variant(email: str) -> str:
    """Deterministic 50/50 A/B split for subject style, by email.

    'A' = signal-anchored (name their thing), 'B' = value/outcome. Stable across
    runs and recomputable at load time, so the variant can be pushed to Smartlead
    as a custom field for reporting without persisting a new column.
    """
    return "A" if zlib.crc32(b"subj:" + email.encode("utf-8")) % 2 == 0 else "B"


class _Transient(Exception):
    pass


@retry(
    retry=retry_if_exception_type(_Transient),
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=2, min=2, max=20),
    reraise=True,
)
def _call(client: anthropic.Anthropic, system: str, user: str, max_tokens: int):
    try:
        return client.messages.create(
            model=MODEL, max_tokens=max_tokens, system=system,
            messages=[{"role": "user", "content": user}],
        )
    except (anthropic.APITimeoutError, anthropic.APIConnectionError,
            anthropic.InternalServerError, anthropic.RateLimitError) as exc:
        raise _Transient(str(exc)) from exc
    except anthropic.APIStatusError as exc:
        if getattr(exc, "status_code", 0) >= 500:
            raise _Transient(str(exc)) from exc
        raise


def _cost(usage) -> float:
    n_in = getattr(usage, "input_tokens", 0) or 0
    n_out = getattr(usage, "output_tokens", 0) or 0
    return n_in / 1_000_000 * PRICE_INPUT_PER_MTOK + n_out / 1_000_000 * PRICE_OUTPUT_PER_MTOK


def _evidence(company: Company) -> str:
    return (company.signal_summary or "").strip() or "(no specific signal on file)"


def _has_signal(company: Company) -> bool:
    return bool((company.signal_summary or "").strip())


def _first_line_prompt(contact: Contact, company: Company, hook: dict,
                       brief: Brief, variant: str) -> str:
    has_signal = _has_signal(company)
    company_short = (company.name.split()[0].lower() if company.name else "their")
    # Variant A wants a signal-anchored subject, but only if a real signal exists;
    # otherwise both variants fall back to the value style.
    if variant == "A" and has_signal:
        subject_instruction = (
            "SUBJECT STYLE: signal — anchor on the COMPANY NAME plus their specific "
            f"thing from the evidence, e.g. \"{company_short}'s addison build\" or "
            f"\"{company_short} precon\". lowercase, 2-5 words. It must be obviously "
            "about THEM — never a bare project name that reads as cryptic."
        )
    else:
        fallback = brief.messaging.get("subject_value_fallback", "")
        subject_instruction = (
            "SUBJECT STYLE: value — a concrete value or free-offer in plain words "
            f"(e.g. \"{fallback}\"). lowercase, 2-5 words." if fallback else
            "SUBJECT STYLE: value — a concrete value or free-offer in plain words. lowercase, 2-5 words."
        )
    if has_signal:
        first_line_instruction = (
            "FIRST LINE: one sentence grounded in the signal evidence above. Reference "
            "the specific event/detail — do not invent anything beyond the evidence."
        )
    else:
        first_line_instruction = (
            "FIRST LINE: there is NO specific signal, so do NOT fabricate news. Instead "
            f"write a relevant cold opener for a {contact.title or 'precon leader'} at "
            f"{company.name} tied to the value angle ({brief.messaging.get('value_angle', 'saving time on bids')}) "
            "or the pain of re-keying Procore data by hand. Never leave it empty."
        )
    return f"""\
Write the SUBJECT and the personalized FIRST LINE of a cold email to \
{contact.first_name or 'the recipient'} ({contact.title or 'a leader'}) at {company.name}.

Signal evidence (use ONLY this — do not invent facts):
{_evidence(company)}

Campaign angle (hook): {hook.get('angle', '')}

{subject_instruction}
Never use a banned subject (e.g. "quick question"). See the subject rules above.

{first_line_instruction}
Normal sentence case (capitalize the first word and proper nouns like Procore/company names — \
NOT all-lowercase), under 25 words.

Return STRICT JSON only: {{"subject": "...", "first_line": "..."}}"""


def _full_prompt(contact: Contact, company: Company, hook: dict, brief: Brief) -> str:
    m = brief.messaging
    signal = company.signal_summary
    signal_block = (f"Recent signal about them (use it if relevant, never invent): {signal}"
                    if signal else
                    "No specific recent signal — write a relevant cold open from their role/company, do NOT fabricate news.")
    return f"""\
Write a CREATIVE cold email to {contact.first_name or 'the recipient'} \
({contact.title or 'a leader'}) at {company.name}.

What we do: {m.get('offer', '')}.
Value angle to orbit (don't quote verbatim): {m.get('value_angle', '')}.
This email's angle (vary your structure to fit it): {hook.get('angle', '')}.
Soft offer to close on: {m.get('cta', 'happy to show you')}.
{signal_block}

Make it feel hand-written and specific to a {contact.title or 'precon leader'} — no boilerplate, \
no two emails alike. Lead with a hook that earns the open, not a generic intro.

Return STRICT JSON only: {{"subject": "...", "body": "..."}}.
- subject: lowercase, value- or curiosity-driven so they WANT to open (a short question is great, \
e.g. "want 4 hours back per bid cycle?"). Never "quick question", never a bare descriptor, no emojis, no exclamation marks.
- body: PROFESSIONAL sentence case — capitalize the first word of every sentence, the recipient's \
name in the greeting (e.g. "Hi {contact.first_name or 'there'},"), and proper nouns (Procore, company \
names). Do NOT write the body in all-lowercase. Under 90 words, conversational, one clear idea, ONE \
soft ask, reference their world (role/company), no signature."""


def _capitalize_first(text: str) -> str:
    """Uppercase the first alphabetic character, leaving the rest untouched."""
    for i, ch in enumerate(text):
        if ch.isalpha():
            return text[:i] + ch.upper() + text[i + 1:]
    return text


def _clean_line(text: str) -> str:
    text = text.strip().strip('"').strip()
    # Drop any model preamble like "First line:".
    text = re.sub(r"^(first line|opener)\s*[:\-]\s*", "", text, flags=re.I)
    return _capitalize_first(text.strip())


# Subjects the model must never produce — last-resort guard at the code layer.
_BANNED_SUBJECT_RE = re.compile(
    r"^(quick question|checking in|touching base|following up|re:)\b", re.I)


def _clean_subject(text: str) -> str:
    text = text.strip().strip('"').strip()
    text = re.sub(r"^(subject)\s*[:\-]\s*", "", text, flags=re.I)
    text = text.rstrip("!").strip()
    if _BANNED_SUBJECT_RE.match(text):
        return ""  # caller falls back to the value subject
    return text.lower()


def personalize_detailed(contact: Contact, company: Company, brief: Brief) -> tuple[Contact, float]:
    """Personalize a contact and return (contact, cost_usd)."""
    hook = pick_hook(contact, brief.hooks)
    contact.cell = hook.get("id")

    client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))
    system = _messaging_prompt()
    cost = 0.0

    if brief.personalization_mode == "full":
        try:
            resp = _call(client, system, _full_prompt(contact, company, hook, brief), 600)
            cost = _cost(resp.usage)
            raw = "\n".join(b.text for b in resp.content
                            if getattr(b, "type", None) == "text").strip()
            raw = re.sub(r"^```(?:json)?|```$", "", raw, flags=re.M).strip()
            data = json.loads(raw)
            contact.subject = _clean_subject(data.get("subject") or "")
            contact.body = (data.get("body") or "").strip()
            contact.first_line = contact.body.split("\n", 1)[0] if contact.body else ""
        except Exception:
            contact.subject = contact.subject or ""
            contact.body = contact.body or ""
        if not contact.subject:
            contact.subject = (brief.messaging.get("subject_value_fallback")
                               or "worth a look?").lower()
    else:
        variant = subject_variant(contact.email)
        try:
            resp = _call(client, system,
                         _first_line_prompt(contact, company, hook, brief, variant), 160)
            cost = _cost(resp.usage)
            raw = "\n".join(b.text for b in resp.content
                            if getattr(b, "type", None) == "text").strip()
            raw = re.sub(r"^```(?:json)?|```$", "", raw, flags=re.M).strip()
            data = json.loads(raw)
            contact.first_line = _clean_line(data.get("first_line") or "")
            contact.subject = _clean_subject(data.get("subject") or "")
        except Exception:
            contact.first_line = contact.first_line or ""
            contact.subject = contact.subject or ""
        # Never ship an empty subject — fall back to the brief's value subject.
        if not contact.subject:
            contact.subject = (brief.messaging.get("subject_value_fallback")
                               or f"{brief.industry} — quick note").lower()
        # Never ship an empty first line either — use a value opener.
        if not contact.first_line:
            offer = brief.messaging.get("offer", "")
            contact.first_line = _capitalize_first(
                offer + "." if offer else
                "Wanted to reach out about speeding up your bid process.")

    contact.status = ContactStatus.PERSONALIZED
    return contact, cost


def personalize(contact: Contact, company: Company, brief: Brief) -> Contact:
    """Contract wrapper: returns the personalized contact."""
    updated, _ = personalize_detailed(contact, company, brief)
    return updated
