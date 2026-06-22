"""News / web-research signal — the expensive cascade stage.

Ported from Method 2 (legacy Anthropic web-search script). Makes a single Claude
call with the web_search tool to find ONE recent, specific, dated signal about
the company usable as a personalized first line. Date-strict to the brief's
lookback window. Conservative: a missing signal is fine, a wrong one is not.

This stage costs money (Claude + web search), so the runner only calls it on
companies that already passed the cheap ``has_job_signal`` gate.

Contract: ``news_signal(company, brief) -> (passed: bool, one_line_summary: str)``
``research_news`` returns the same plus cost for the runner's cost log.
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import anthropic
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from ..models import Brief, Company

# Haiku keeps the (smarter) agentic web_search flow but at ~4.5x lower token cost
# than Sonnet. Combined with the search cap (3) and the run-stage short-circuit
# (only no-job-signal companies reach this paid stage), it cuts news spend ~80%
# with no precision loss — Haiku stays conservative and still rejects undated
# signals (verified A/B, see scripts/news_ab_test.py).
MODEL = "claude-haiku-4-5-20251001"
MAX_TOKENS = 1500

PRICE_INPUT_PER_MTOK = 0.80
PRICE_OUTPUT_PER_MTOK = 4.00
PRICE_WEB_SEARCH_PER_1K = 10.00

MEANINGFUL_SPECIFICITY_MIN = 3

VALID_SIGNAL_TYPES = {
    "project_win", "hire", "tech_adoption", "press_coverage",
    "federal_contract", "license_filing", "blog_post", "other",
}


# --- Prompt construction -----------------------------------------------------

def _build_system_prompt(brief: Brief, today: datetime) -> str:
    today_str = today.strftime("%Y-%m-%d")
    lookback = brief.news_lookback_days
    cutoff_str = (today - timedelta(days=lookback)).strftime("%Y-%m-%d")
    titles = ", ".join(brief.contact_filters.get("titles_any", [])[:5]) or "decision makers"
    themes = brief.signals.get("news", {}).get("themes_any", [])
    themes_str = ", ".join(str(t) for t in themes) if themes else "any material business event"

    return f"""\
You are a research analyst for a B2B outbound campaign in the {brief.industry} industry. \
Your job is to find ONE genuinely useful, recent signal about a specific company that a \
sales rep can use to write a personalized first line in a cold email.

BUDGET: Maximum 3 web_search calls per company. If no qualifying signal is found after 3 \
searches, return an empty signals array with detailed search_notes.

WHO RECEIVES THE EMAIL
The recipient holds a role such as: {titles}. A good signal proves we actually paid \
attention to THEIR company. Relevant themes for this campaign: {themes_str}.

TODAY'S DATE: {today_str}
HARD DATE FILTER: Only include a signal if its event date is on or after {cutoff_str} \
(within the last {lookback} days). If a signal is older than that, DISCARD it.

SEARCH STRATEGY
Run at least 2 different searches before concluding no signals exist (max 3). Try the company \
name with location, project/award/expansion terms, leadership-hire terms, and trade-press sites.

WHAT TO LOOK FOR (rough priority): recent project wins / awards / expansions; named \
technology adoption; leadership hires or promotions; trade-press coverage; contract awards; \
fresh content from the company's own news/blog page.

EXCLUDE: boilerplate/About-Us content; signals dated before {cutoff_str}; press releases that \
just repeat a tagline; generic directory listings; the company homepage with no dated content.

RULES
BE CONSERVATIVE. If you are not confident a fact is true and about THIS exact company, skip \
it. Never invent or embellish. A missing signal is fine; a wrong signal is not.
EVERY signal MUST have a real, specific source_url you actually saw in search results. If you \
cannot produce one, DROP the signal.
DATE HANDLING: event date as YYYY-MM-DD when known; "fuzzy" with date_confidence "low" if \
approximate. date_confidence is high|medium|low.
SPECIFICITY SCORE (1-5): 1 = generic; 3 = a real but broad event; 5 = highly specific with \
names/places/dates. Score honestly.
Return AT MOST 3 signals. Prefer one strong signal over three weak ones.
suggested_first_line: single lowercase sentence, no exclamation marks, no emojis, under 25 \
words, referencing the specific event detail (not just the signal type).
LOWERCASE RULE applies ONLY to suggested_first_line.

OUTPUT FORMAT — STRICT JSON ONLY, no markdown or commentary. Exactly one object:
{{
  "company_name": "string",
  "company_domain": "string",
  "signals": [
    {{
      "signal_type": "project_win | hire | tech_adoption | press_coverage | federal_contract | license_filing | blog_post | other",
      "summary": "one-sentence factual statement",
      "source_url": "string",
      "source_title": "string",
      "date": "YYYY-MM-DD or 'fuzzy'",
      "date_confidence": "high | medium | low",
      "specificity_score": 1,
      "suggested_first_line": "lowercase casual one-sentence opener"
    }}
  ],
  "no_signals_found": false,
  "search_notes": "what you searched and, if empty, why nothing qualified"
}}
If nothing qualifies, return an empty signals array and set no_signals_found to true."""


def _build_user_prompt(company: Company) -> str:
    return f"""\
Research this company and return signals per your instructions.

company_name: {company.name}
company_domain: {company.domain}
company_city: {company.city}
company_state: {company.state}

Use web search. Confirm the company identity (name + location) before trusting any signal. \
Return strict JSON only."""


# --- Anthropic call ----------------------------------------------------------

class _Transient(Exception):
    pass


@retry(
    retry=retry_if_exception_type(_Transient),
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=2, min=2, max=30),
    reraise=True,
)
def _call_claude(client: anthropic.Anthropic, system_prompt: str, user_prompt: str):
    try:
        return client.messages.create(
            model=MODEL,
            max_tokens=MAX_TOKENS,
            system=system_prompt,
            tools=[{"type": "web_search_20250305", "name": "web_search", "max_uses": 3}],
            messages=[{"role": "user", "content": user_prompt}],
        )
    except (anthropic.APITimeoutError, anthropic.APIConnectionError,
            anthropic.InternalServerError, anthropic.RateLimitError) as exc:
        raise _Transient(str(exc)) from exc
    except anthropic.APIStatusError as exc:
        if getattr(exc, "status_code", 0) >= 500:
            raise _Transient(str(exc)) from exc
        raise


# --- Parsing -----------------------------------------------------------------

def _extract_text(response) -> str:
    return "\n".join(
        b.text for b in response.content if getattr(b, "type", None) == "text"
    ).strip()


def _strip_fences(text: str) -> str:
    text = text.strip()
    fence = re.match(r"^```(?:json)?\s*(.*?)\s*```$", text, re.DOTALL)
    return fence.group(1).strip() if fence else text


def _parse_json_object(text: str) -> dict:
    cleaned = _strip_fences(text)
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start != -1 and end > start:
        return json.loads(cleaned[start:end + 1])
    raise json.JSONDecodeError("No JSON object found", cleaned, 0)


def _web_search_count(usage) -> int:
    stu = getattr(usage, "server_tool_use", None)
    if stu is None:
        return 0
    return getattr(stu, "web_search_requests", 0) or 0


def _is_meaningful(sig: dict, cutoff: datetime) -> bool:
    try:
        spec = int(sig.get("specificity_score", 0))
    except (TypeError, ValueError):
        spec = 0
    if spec < MEANINGFUL_SPECIFICITY_MIN:
        return False
    url = (sig.get("source_url") or "").strip().lower()
    if not url.startswith("http"):
        return False
    date = (str(sig.get("date") or "")).strip()
    if date and date != "fuzzy":
        try:
            d = datetime.fromisoformat(date).replace(tzinfo=timezone.utc)
            if d < cutoff:
                return False
        except (ValueError, AttributeError):
            return False
    return True


def _estimate_cost(usage) -> float:
    n_in = getattr(usage, "input_tokens", 0) or 0
    n_out = getattr(usage, "output_tokens", 0) or 0
    n_search = _web_search_count(usage)
    return (n_in / 1_000_000 * PRICE_INPUT_PER_MTOK
            + n_out / 1_000_000 * PRICE_OUTPUT_PER_MTOK
            + n_search / 1_000 * PRICE_WEB_SEARCH_PER_1K)


# --- Public API --------------------------------------------------------------

def research_news(company: Company, brief: Brief) -> dict[str, Any]:
    """Run the Claude web-search research. Returns a dict:
    {passed, summary, first_line, cost_usd, signal}.

    Never raises on API failure — returns passed=False with the error noted.
    """
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    client = anthropic.Anthropic(api_key=api_key)
    today = datetime.now(timezone.utc)
    cutoff = today - timedelta(days=brief.news_lookback_days)

    system_prompt = _build_system_prompt(brief, today)
    user_prompt = _build_user_prompt(company)

    try:
        response = _call_claude(client, system_prompt, user_prompt)
    except Exception as exc:  # keep the run alive
        return {"passed": False, "summary": f"news research failed: {exc}",
                "first_line": "", "cost_usd": 0.0, "signal": None}

    cost = _estimate_cost(response.usage)
    raw_text = _extract_text(response)

    try:
        parsed = _parse_json_object(raw_text)
    except (json.JSONDecodeError, AttributeError, TypeError):
        return {"passed": False, "summary": "could not parse model output",
                "first_line": "", "cost_usd": cost, "signal": None}

    signals = parsed.get("signals") or []
    # Keep only meaningful signals, then pick the most specific.
    meaningful = [s for s in signals if isinstance(s, dict) and _is_meaningful(s, cutoff)]
    if not meaningful:
        note = (parsed.get("search_notes") or "no qualifying signal").strip()
        return {"passed": False, "summary": note[:200], "first_line": "",
                "cost_usd": cost, "signal": None}

    def _key(s: dict):
        try:
            spec = int(s.get("specificity_score", 0))
        except (TypeError, ValueError):
            spec = 0
        date = (str(s.get("date") or "")).strip()
        try:
            dval = datetime.fromisoformat(date).timestamp() if date and date != "fuzzy" else 0
        except ValueError:
            dval = 0
        return (-spec, -dval)

    meaningful.sort(key=_key)
    best = meaningful[0]
    stype = best.get("signal_type", "other")
    if stype not in VALID_SIGNAL_TYPES:
        stype = "other"
    summary = f"{stype}: {(best.get('summary') or '').strip()}"
    return {
        "passed": True,
        "summary": summary[:300],
        "first_line": (best.get("suggested_first_line") or "").strip(),
        "cost_usd": cost,
        "signal": best,
    }


def news_signal(company: Company, brief: Brief) -> tuple[bool, str]:
    """Contract wrapper: (passed, one_line_summary)."""
    result = research_news(company, brief)
    return result["passed"], result["summary"]

