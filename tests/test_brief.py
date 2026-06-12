"""Brief loading and validation."""

import pytest

from outbound.brief import load_brief, list_briefs, validate_brief_dict, BriefError


def test_construction_brief_loads():
    b = load_brief("construction")
    assert b.industry == "construction"
    assert b.daily_quota == 500
    assert b.target_depth_days == 7
    assert b.contacts_per_company == 3
    assert b.personalization_mode == "first_line"
    assert len(b.hooks) == 3


def test_construction_in_list():
    assert "construction" in list_briefs()


def test_missing_section_rejected():
    with pytest.raises(BriefError):
        validate_brief_dict({"industry": "x"}, "x")


def test_bad_personalization_mode_rejected():
    data = _minimal_brief()
    data["personalization"]["mode"] = "nonsense"
    with pytest.raises(BriefError):
        validate_brief_dict(data, "x")


def test_no_hooks_rejected():
    data = _minimal_brief()
    data["hooks"] = []
    with pytest.raises(BriefError):
        validate_brief_dict(data, "x")


def test_unknown_brief_raises():
    with pytest.raises(BriefError):
        load_brief("does-not-exist-industry")


def _minimal_brief():
    return {
        "industry": "x",
        "company_filters": {
            "industries": ["X"],
            "employees": {"min": 1, "max": 10},
            "countries": ["United States"],
        },
        "contact_filters": {
            "titles_any": ["VP"],
            "countries": ["United States"],
            "contacts_per_company": 3,
        },
        "signals": {"news": {"lookback_days": 120}},
        "hooks": [{"id": "a", "angle": "b"}],
        "personalization": {"mode": "first_line"},
        "sending": {
            "daily_quota": 500, "days": ["Tue"], "window_local": "09:00-11:30",
            "tracking": "off",
        },
        "reservoir": {"target_depth_days": 7},
        "suppression": {"on_unsubscribe": True, "hard_bounce_threshold": 2},
    }
