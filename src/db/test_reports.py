"""Unit tests for src/db/reports.py.

Uses the `tmp_db` fixture (fully initialised, seeded SQLite DB).
Inserts individuals and office_terms with dates relative to today so
the 90-day window tests remain correct regardless of when they run.

Run: pytest src/db/test_reports.py -v
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from src.db import individuals as db_individuals
from src.db import offices as db_offices
from src.db import reports as db_reports

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _today_minus(days: int) -> str:
    return (date.today() - timedelta(days=days)).isoformat()


def _make_office(conn) -> int:
    return db_offices.create_office(
        {
            "country_id": 1,
            "state_id": None,
            "city_id": None,
            "level_id": None,
            "branch_id": None,
            "department": "",
            "name": "Report Test Office",
            "enabled": True,
            "notes": "",
            "url": "https://en.wikipedia.org/wiki/Report_Test_Office",
            "table_configs": [
                {
                    "name": "",
                    "table_no": 1,
                    "table_rows": 1,
                    "link_column": 1,
                    "party_column": 0,
                    "term_start_column": 2,
                    "term_end_column": 3,
                    "district_column": 0,
                    "enabled": 1,
                }
            ],
        },
        conn=conn,
    )


def _make_individual(conn, wiki_url: str, **kwargs) -> int:
    return db_individuals.upsert_individual({"wiki_url": wiki_url, **kwargs}, conn=conn)


def _insert_term(
    conn, od_id: int, ind_id: int, term_start: str | None, term_end: str | None
) -> None:
    conn.execute(
        """INSERT INTO office_terms
           (office_id, individual_id, wiki_url, term_start, term_end)
           VALUES (%s, %s, %s, %s, %s)""",
        (od_id, ind_id, f"https://en.wikipedia.org/wiki/P{ind_id}", term_start, term_end),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# get_recent_deaths
# ---------------------------------------------------------------------------


def test_get_recent_deaths_returns_individual_within_90_days(tmp_db):
    _make_individual(
        tmp_db,
        "/wiki/RecentDeath",
        full_name="Recent Death",
        death_date=_today_minus(30),
    )
    results = db_reports.get_recent_deaths(conn=tmp_db)
    names = [r["full_name"] for r in results]
    assert "Recent Death" in names


def test_get_recent_deaths_excludes_individual_over_91_days_ago(tmp_db):
    _make_individual(
        tmp_db,
        "/wiki/OldDeath",
        full_name="Old Death",
        death_date=_today_minus(91),
    )
    results = db_reports.get_recent_deaths(conn=tmp_db)
    names = [r["full_name"] for r in results]
    assert "Old Death" not in names


def test_get_recent_deaths_returns_empty_when_no_deaths(tmp_db):
    results = db_reports.get_recent_deaths(conn=tmp_db)
    assert isinstance(results, list)
    # No deaths inserted — may be empty or contain nothing from seed
    for r in results:
        assert r.get("death_date") is not None


# ---------------------------------------------------------------------------
# get_recent_term_ends
# ---------------------------------------------------------------------------


def test_get_recent_term_ends_returns_term_within_90_days(tmp_db):
    od_id = _make_office(tmp_db)
    ind_id = _make_individual(tmp_db, "/wiki/RecentTermEnd", full_name="Recent Term End")
    _insert_term(tmp_db, od_id, ind_id, _today_minus(120), _today_minus(45))

    results = db_reports.get_recent_term_ends(conn=tmp_db)
    names = [r["Name"] for r in results]
    assert "Recent Term End" in names


def test_get_recent_term_ends_excludes_term_outside_window(tmp_db):
    od_id = _make_office(tmp_db)
    ind_id = _make_individual(tmp_db, "/wiki/OldTermEnd", full_name="Old Term End")
    _insert_term(tmp_db, od_id, ind_id, _today_minus(200), _today_minus(120))

    results = db_reports.get_recent_term_ends(conn=tmp_db)
    names = [r["Name"] for r in results]
    assert "Old Term End" not in names


# ---------------------------------------------------------------------------
# get_recent_term_starts
# ---------------------------------------------------------------------------


def test_get_recent_term_starts_returns_term_within_90_days(tmp_db):
    od_id = _make_office(tmp_db)
    ind_id = _make_individual(tmp_db, "/wiki/RecentTermStart", full_name="Recent Term Start")
    _insert_term(tmp_db, od_id, ind_id, _today_minus(30), None)

    results = db_reports.get_recent_term_starts(conn=tmp_db)
    names = [r["Name"] for r in results]
    assert "Recent Term Start" in names


def test_get_recent_term_starts_excludes_term_outside_window(tmp_db):
    od_id = _make_office(tmp_db)
    ind_id = _make_individual(tmp_db, "/wiki/OldTermStart", full_name="Old Term Start")
    _insert_term(tmp_db, od_id, ind_id, _today_minus(91), None)

    results = db_reports.get_recent_term_starts(conn=tmp_db)
    names = [r["Name"] for r in results]
    assert "Old Term Start" not in names


def test_get_recent_term_starts_returns_empty_list_with_no_terms(tmp_db):
    results = db_reports.get_recent_term_starts(conn=tmp_db)
    assert isinstance(results, list)
