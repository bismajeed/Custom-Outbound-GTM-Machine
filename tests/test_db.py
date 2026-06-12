"""State store: dedup, suppression, status transitions, cost roll-up."""

import pytest

from outbound.db import Database
from outbound.models import Company, Contact, CompanyStatus, ContactStatus


@pytest.fixture
def db(tmp_path):
    d = Database(f"sqlite:///{tmp_path / 'test.db'}")
    d.init()
    return d


def _company(domain="acme.com"):
    return Company(domain=domain, name="Acme", brief="construction",
                   status=CompanyStatus.SOURCED)


def _contact(email="vp@acme.com"):
    return Contact(email=email, company_domain="acme.com", first_name="Pat",
                   brief="construction")


def test_company_insert_and_dedup(db):
    assert db.insert_company(_company()) is True
    assert db.insert_company(_company()) is False  # idempotent / single-touch


def test_suppressed_company_not_inserted(db):
    db.add_suppression("acme.com", "domain", "do_not_contact")
    assert db.insert_company(_company()) is False


def test_contact_insert_and_dedup(db):
    db.insert_company(_company())
    assert db.insert_contact(_contact()) is True
    assert db.insert_contact(_contact()) is False


def test_suppressed_contact_not_inserted(db):
    db.insert_company(_company())
    db.add_suppression("vp@acme.com", "email", "unsubscribe")
    assert db.insert_contact(_contact()) is False


def test_status_transition_and_reservoir(db):
    db.insert_company(_company())
    db.insert_contact(_contact())
    assert db.reservoir_count("construction") == 0
    db.update_contact("vp@acme.com", status=ContactStatus.QUEUED)
    assert db.reservoir_count("construction") == 1


def test_cost_rollup(db):
    db.start_run("r1", "construction", "news")
    db.finish_run("r1", "done", {"cost_usd": 2.5})
    db.start_run("r2", "construction", "personalize")
    db.finish_run("r2", "done", {"cost_usd": 1.25})
    assert db.total_cost("construction") == pytest.approx(3.75)


def test_counts_by_status(db):
    db.insert_company(_company("a.com"))
    db.insert_company(_company("b.com"))
    db.update_company("a.com", status=CompanyStatus.QUALIFIED)
    counts = db.count_companies_by_status("construction")
    assert counts.get("QUALIFIED") == 1
    assert counts.get("SOURCED") == 1
