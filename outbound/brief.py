"""Industry brief loading, validation, and the interactive `brief new` wizard.

One YAML file per industry under ``briefs/``. A brief is authored once and then
referenced by name on every command. Validation fails fast with a readable
message naming the offending field.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml

from .models import Brief

BRIEFS_DIR = "briefs"

# Required top-level sections and the fields each must contain.
_REQUIRED_SECTIONS = {
    "company_filters": ["industries", "employees", "countries"],
    "contact_filters": ["titles_any", "countries", "contacts_per_company"],
    "signals": ["news"],
    "hooks": None,            # list, validated separately
    "personalization": ["mode"],
    "sending": ["daily_quota", "days", "window_local", "tracking"],
    "reservoir": ["target_depth_days"],
    "suppression": ["on_unsubscribe", "hard_bounce_threshold"],
}

_VALID_PERSONALIZATION_MODES = {"first_line", "full", "template"}


class BriefError(ValueError):
    """Raised when a brief file is missing or fails validation."""


def brief_path(industry: str, private: bool = False) -> Path:
    sub = os.path.join(BRIEFS_DIR, "private") if private else BRIEFS_DIR
    return Path(sub) / f"{industry}.yaml"


def _resolve_path(industry: str) -> Path:
    """Find a brief by name, preferring public then private."""
    public = brief_path(industry, private=False)
    if public.exists():
        return public
    private = brief_path(industry, private=True)
    if private.exists():
        return private
    raise BriefError(
        f"No brief found for '{industry}'. Looked for {public} and {private}. "
        f"Create one with: outbound brief new {industry}"
    )


def validate_brief_dict(data: dict[str, Any], industry: str) -> None:
    """Validate a parsed brief dict against the schema. Raises BriefError."""
    if not isinstance(data, dict):
        raise BriefError(f"Brief '{industry}' is not a valid YAML mapping.")

    if not data.get("industry"):
        raise BriefError(f"Brief '{industry}' is missing required field: industry")

    for section, fields in _REQUIRED_SECTIONS.items():
        if section not in data or data[section] is None:
            raise BriefError(
                f"Brief '{industry}' is missing required section: {section}"
            )
        if fields is None:
            continue
        if not isinstance(data[section], dict):
            raise BriefError(
                f"Brief '{industry}' section '{section}' must be a mapping."
            )
        for field in fields:
            if field not in data[section]:
                raise BriefError(
                    f"Brief '{industry}' section '{section}' is missing field: {field}"
                )

    # hooks must be a non-empty list of {id, angle}.
    hooks = data.get("hooks")
    if not isinstance(hooks, list) or not hooks:
        raise BriefError(f"Brief '{industry}' must define at least one hook.")
    for i, hook in enumerate(hooks):
        if not isinstance(hook, dict) or "id" not in hook or "angle" not in hook:
            raise BriefError(
                f"Brief '{industry}' hook #{i + 1} must have 'id' and 'angle'."
            )

    mode = data["personalization"].get("mode")
    if mode not in _VALID_PERSONALIZATION_MODES:
        raise BriefError(
            f"Brief '{industry}' personalization.mode must be one of "
            f"{sorted(_VALID_PERSONALIZATION_MODES)}, got '{mode}'."
        )

    quota = data["sending"].get("daily_quota")
    if not isinstance(quota, int) or quota <= 0:
        raise BriefError(
            f"Brief '{industry}' sending.daily_quota must be a positive integer."
        )

    emp = data["company_filters"].get("employees")
    if not isinstance(emp, dict) or "min" not in emp or "max" not in emp:
        raise BriefError(
            f"Brief '{industry}' company_filters.employees must have min and max."
        )


def load_brief(industry: str) -> Brief:
    """Load and validate a brief by name. Raises BriefError on any problem."""
    path = _resolve_path(industry)
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
    except yaml.YAMLError as exc:
        raise BriefError(f"Brief '{industry}' has invalid YAML: {exc}") from exc

    validate_brief_dict(data, industry)

    return Brief(
        industry=data["industry"],
        saved_search=data.get("saved_search", ""),
        company_filters=data["company_filters"],
        contact_filters=data["contact_filters"],
        signals=data["signals"],
        hooks=data["hooks"],
        personalization=data["personalization"],
        sending=data["sending"],
        reservoir=data["reservoir"],
        suppression=data["suppression"],
        messaging=data.get("messaging", {}) or {},
        raw=data,
    )


def list_briefs() -> list[str]:
    """Return the names of all available briefs (public + private)."""
    names: set[str] = set()
    for sub in (BRIEFS_DIR, os.path.join(BRIEFS_DIR, "private")):
        p = Path(sub)
        if p.is_dir():
            for f in p.glob("*.yaml"):
                names.add(f.stem)
    return sorted(names)


# --- Interactive wizard ------------------------------------------------------

def _prompt(label: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    val = input(f"{label}{suffix}: ").strip()
    return val or default


def _prompt_list(label: str, default: list[str] | None = None) -> list[str]:
    default = default or []
    hint = f" [{', '.join(default)}]" if default else ""
    val = input(f"{label} (comma-separated){hint}: ").strip()
    if not val:
        return default
    return [x.strip() for x in val.split(",") if x.strip()]


def _prompt_int(label: str, default: int) -> int:
    val = input(f"{label} [{default}]: ").strip()
    if not val:
        return default
    try:
        return int(val)
    except ValueError:
        return default


def new_brief_interactive(industry: str, private: bool = False) -> Path:
    """Run the interactive wizard and write briefs/<industry>.yaml.

    Returns the written path. Raises BriefError if the file already exists.
    """
    path = brief_path(industry, private=private)
    if path.exists():
        raise BriefError(f"Brief already exists: {path}. Edit it directly.")

    print(f"\nCreating brief for '{industry}'. Press Enter to accept defaults.\n")

    saved_search = _prompt("Saved search label", f"Wave 1 — {industry}")
    industries = _prompt_list("Target industries", [industry])
    emp_min = _prompt_int("Min employees", 100)
    emp_max = _prompt_int("Max employees", 2000)
    rev_min = _prompt_int("Min revenue USD", 50_000_000)
    rev_max = _prompt_int("Max revenue USD", 500_000_000)
    countries = _prompt_list("Countries", ["United States"])
    technologies = _prompt_list("Technologies (any-of)", [])
    exclude = _prompt_list("Exclude keywords", [])

    titles = _prompt_list("Contact titles (any-of)",
                          ["VP", "Director", "Chief Operating Officer"])
    seniority = _prompt_list("Seniority", ["Director", "VP", "C-Suite"])
    per_company = _prompt_int("Contacts per company", 3)

    news_lookback = _prompt_int("News lookback days", 120)
    job_keywords = _prompt_list("Job-posting keywords (any-of)", [])

    daily_quota = _prompt_int("Daily send quota", 500)
    days = _prompt_list("Sending days", ["Tue", "Wed", "Thu"])
    window = _prompt("Sending window (local)", "09:00-11:30")
    mode = _prompt("Personalization mode (first_line/full)", "first_line")
    target_depth = _prompt_int("Reservoir target depth (days)", 7)

    print("\nDefine outreach hooks (angles). Blank id to finish.")
    hooks = []
    while True:
        hid = input(f"  Hook #{len(hooks) + 1} id: ").strip()
        if not hid:
            break
        angle = input("    angle: ").strip()
        hooks.append({"id": hid, "angle": angle})
    if not hooks:
        hooks = [{"id": "value_prop", "angle": "The core value proposition"}]

    data: dict[str, Any] = {
        "industry": industry,
        "saved_search": saved_search,
        "company_filters": {
            "industries": industries,
            "employees": {"min": emp_min, "max": emp_max},
            "revenue_usd": {"min": rev_min, "max": rev_max},
            "countries": countries,
            "technologies_any": technologies,
            "exclude_keywords": exclude,
        },
        "contact_filters": {
            "titles_any": titles,
            "seniority": seniority,
            "countries": countries,
            "email_status": ["verified", "likely"],
            "contacts_per_company": per_company,
        },
        "signals": {
            "job_postings": {"keywords_any": job_keywords},
            "news": {"lookback_days": news_lookback, "themes_any": []},
        },
        "hooks": hooks,
        "personalization": {"mode": mode},
        "sending": {
            "daily_quota": daily_quota,
            "days": days,
            "window_local": window,
            "per_mailbox_cap": {"m365": 30, "gw": 25},
            "tracking": "off",
        },
        "reservoir": {"target_depth_days": target_depth},
        "suppression": {
            "on_unsubscribe": True,
            "hard_bounce_threshold": 2,
            "suppress_company_on_meeting_months": 12,
        },
    }

    # Validate before writing so we never persist a broken brief.
    validate_brief_dict(data, industry)

    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        yaml.safe_dump(data, f, sort_keys=False, allow_unicode=True)
    return path
