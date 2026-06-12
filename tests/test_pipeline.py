"""Pipeline helpers that need no network or API keys."""

import pytest

from outbound.brief import load_brief
from outbound.db import Database
from outbound.models import Contact
from outbound.pipeline.personalize import pick_hook
from outbound.pipeline.validate import normalize_domain, clean_company_name
from outbound.run import compute_source_limit, reservoir_depth_days


@pytest.fixture
def db(tmp_path):
    d = Database(f"sqlite:///{tmp_path / 'test.db'}")
    d.init()
    return d


def test_normalize_domain():
    assert normalize_domain("https://www.Acme.com/") == "acme.com"
    assert normalize_domain("HTTP://foo.io") == "foo.io"


def test_clean_company_name():
    assert clean_company_name("Acme Construction").lower() == "acme"
    assert clean_company_name("Beta LLC").lower() == "beta"


def test_hook_round_robin_is_deterministic():
    b = load_brief("construction")
    c = Contact(email="a@b.com", company_domain="b.com")
    assert pick_hook(c, b.hooks)["id"] == pick_hook(c, b.hooks)["id"]


def test_hook_round_robin_distributes():
    b = load_brief("construction")
    ids = {pick_hook(Contact(email=f"u{i}@x.com", company_domain="x.com"),
                     b.hooks)["id"] for i in range(30)}
    assert len(ids) > 1  # not all the same hook


def test_source_limit_zero_when_full(db, monkeypatch):
    b = load_brief("construction")
    # Pretend reservoir already at target depth.
    monkeypatch.setattr(db, "reservoir_count",
                        lambda brief: b.target_depth_days * b.daily_quota)
    assert compute_source_limit(db, b) == 0


def test_source_limit_positive_when_empty(db):
    b = load_brief("construction")
    assert compute_source_limit(db, b) > 0


def test_reservoir_depth(db, monkeypatch):
    b = load_brief("construction")
    monkeypatch.setattr(db, "reservoir_count", lambda brief: b.daily_quota)
    assert reservoir_depth_days(db, b) == pytest.approx(1.0)
