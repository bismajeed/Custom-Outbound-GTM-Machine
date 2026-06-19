"""Scoring rubric: load it, run the hard-fail checker, blend an overall score.

The rubric itself lives in versioned YAML under ``evals/rubrics/`` (``v1.yaml``,
``v2.yaml`` after Brad's calibration). This module is the thin, judge-agnostic
layer over it: it does *not* call an LLM. The judge (built later) loads a rubric,
asks the model for per-criterion 1-5 scores, then uses ``check_hard_fails`` and
``compute_overall`` here to turn those into a final, comparable number.

    rubric = load_rubric("v1")
    fails = check_hard_fails(text, rubric)        # [] == clean
    overall = 0.0 if fails else compute_overall(scores, rubric["weights"])
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Optional

import yaml

RUBRICS_DIR = Path(__file__).resolve().parent / "rubrics"
DEFAULT_VERSION = "v1"

_MATCH_TYPES = {"phrase", "word", "regex"}


class RubricError(ValueError):
    """Raised when a rubric file is missing or structurally invalid."""


def rubric_path(version: str) -> Path:
    return RUBRICS_DIR / f"{version}.yaml"


def load_rubric(version: str = DEFAULT_VERSION) -> dict[str, Any]:
    """Load and validate a versioned rubric. Raises RubricError on any problem."""
    path = rubric_path(version)
    if not path.exists():
        raise RubricError(
            f"Rubric '{version}' not found at {path}. "
            f"Available: {', '.join(list_rubrics()) or 'none'}."
        )
    try:
        with path.open("r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
    except yaml.YAMLError as exc:
        raise RubricError(f"Rubric '{version}' has invalid YAML: {exc}") from exc

    _validate(data, version)
    return data


def list_rubrics() -> list[str]:
    """Return the names (stems) of all rubric files, sorted."""
    if not RUBRICS_DIR.is_dir():
        return []
    return sorted(p.stem for p in RUBRICS_DIR.glob("*.yaml"))


def _validate(data: Any, version: str) -> None:
    if not isinstance(data, dict):
        raise RubricError(f"Rubric '{version}' must be a mapping.")
    for key in ("scored_criteria", "weights", "hard_fails"):
        if key not in data:
            raise RubricError(f"Rubric '{version}' is missing '{key}'.")

    criteria = {c["id"] for c in data["scored_criteria"]
                if isinstance(c, dict) and "id" in c}
    if not criteria:
        raise RubricError(f"Rubric '{version}' has no usable scored_criteria.")

    weights = data["weights"]
    if not isinstance(weights, dict) or not weights:
        raise RubricError(f"Rubric '{version}' weights must be a non-empty mapping.")
    unknown = set(weights) - criteria
    if unknown:
        raise RubricError(
            f"Rubric '{version}' weights reference unknown criteria: "
            f"{', '.join(sorted(unknown))}."
        )
    total = sum(float(w) for w in weights.values())
    if abs(total - 1.0) > 1e-6:
        raise RubricError(
            f"Rubric '{version}' weights must sum to 1.0 (got {total:.3f})."
        )

    for rule in data["hard_fails"]:
        if not isinstance(rule, dict) or "id" not in rule:
            raise RubricError(f"Rubric '{version}' hard_fail rules need an 'id'.")
        rtype = rule.get("type", "phrase")
        if rtype not in _MATCH_TYPES:
            raise RubricError(
                f"Rubric '{version}' hard_fail '{rule['id']}' has unknown "
                f"type '{rtype}' (use one of {', '.join(sorted(_MATCH_TYPES))})."
            )


def check_hard_fails(text: str, rubric: Optional[dict[str, Any]] = None) -> list[dict[str, str]]:
    """Return the hard-fail rules a message trips. Empty list == clean pass.

    Each hit is ``{"rule": id, "match": <offending text>, "desc": ...}``. A rule
    can fire at most once (first match wins) so the list reads as one entry per
    violated rule, not per occurrence.
    """
    if rubric is None:
        rubric = load_rubric()
    text = text or ""
    low = text.lower()

    fails: list[dict[str, str]] = []
    for rule in rubric.get("hard_fails", []):
        rid = rule.get("id", "")
        rtype = rule.get("type", "phrase")
        desc = rule.get("desc", "")
        for item in rule.get("items", []):
            match = _first_match(rtype, item, text, low)
            if match is not None:
                fails.append({"rule": rid, "match": match, "desc": desc})
                break  # one flag per rule
    return fails


def _first_match(rtype: str, item: str, text: str, low: str) -> Optional[str]:
    """Return the matched substring for one rule item, or None."""
    if rtype == "phrase":
        return item if item.lower() in low else None
    if rtype == "word":
        # Stem-match so the token also catches its inflections: drop a trailing
        # 'e' and allow any word suffix ("leverage" -> leveraged/leveraging).
        stem = item[:-1] if len(item) > 3 and item.endswith("e") else item
        m = re.search(r"\b" + re.escape(stem) + r"\w*", text, re.IGNORECASE)
        return m.group(0) if m else None
    if rtype == "regex":
        m = re.search(item, text, re.IGNORECASE)
        return m.group(0) if m else None
    return None


def compute_overall(scores: dict[str, float], weights: dict[str, float]) -> float:
    """Blend per-criterion 1-5 scores into a single weighted score.

    Only criteria present in both ``scores`` and ``weights`` count, and the
    result is renormalized by the weight actually applied — so a partial set of
    scores still yields a sensible 1-5 number. Returns 0.0 if nothing applies.
    """
    num = den = 0.0
    for cid, weight in weights.items():
        value = scores.get(cid)
        if value is None:
            continue
        num += float(value) * float(weight)
        den += float(weight)
    return round(num / den, 3) if den else 0.0
