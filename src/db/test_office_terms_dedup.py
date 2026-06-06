"""Regression tests for office_terms dedup behaviour during DB startup.

Covers issue #651: the dedup must clean cross-individual duplicate rows (rows
with different individual_id but the same index key) so that
idx_office_terms_hierarchy_dedup can always be created, even when scrape jobs
have accumulated duplicate rows between deploy retries.

Run: pytest src/db/test_office_terms_dedup.py -v
"""

from __future__ import annotations

import pytest

from src.db import individuals as db_individuals
from src.db import offices as db_offices
from src.db import office_terms as db_office_terms
from src.db.connection import _sqlite_add_columns_if_missing

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_TABLE_CONFIG = {
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


def _make_office_two_tables(conn) -> tuple[int, int, int]:
    """Create an office with two table configs. Returns (od_id, tc_id1, tc_id2)."""
    od_id = db_offices.create_office(
        {
            "country_id": 1,
            "state_id": None,
            "city_id": None,
            "level_id": None,
            "branch_id": None,
            "department": "",
            "name": "Dedup Test Office",
            "enabled": True,
            "notes": "",
            "url": "https://example.test/wiki/Dedup_Test_Office",
            "table_configs": [
                {**_TABLE_CONFIG, "table_no": 1},
                {**_TABLE_CONFIG, "table_no": 2},
            ],
        },
        conn=conn,
    )
    cur = conn.execute(
        "SELECT id FROM office_table_config WHERE office_details_id = %s ORDER BY table_no",
        (od_id,),
    )
    tc_ids = [row[0] for row in cur.fetchall()]
    assert len(tc_ids) == 2
    return od_id, tc_ids[0], tc_ids[1]


def _make_individual(conn, wiki_url: str) -> int:
    return db_individuals.upsert_individual({"wiki_url": wiki_url}, conn=conn)


def _insert_raw(
    conn,
    office_id,
    od_id,
    ind_id,
    wiki_url,
    term_start=None,
    term_end=None,
    term_start_year=None,
    term_end_year=None,
) -> None:
    """Insert a term row directly, bypassing ON CONFLICT (unique index must already be dropped)."""
    conn.execute(
        """INSERT INTO office_terms
           (office_id, office_details_id, office_table_config_id,
            individual_id, wiki_url, term_start, term_end, term_start_year, term_end_year)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)""",
        (
            office_id,
            od_id,
            office_id,
            ind_id,
            wiki_url,
            term_start,
            term_end,
            term_start_year,
            term_end_year,
        ),
    )
    conn.commit()


def _count_terms(conn, wiki_url: str) -> int:
    return conn.execute(
        "SELECT COUNT(*) FROM office_terms WHERE wiki_url = %s", (wiki_url,)
    ).fetchone()[0]


def _index_exists(conn) -> bool:
    return (
        conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND name='idx_office_terms_hierarchy_dedup'"
        ).fetchone()
        is not None
    )


# ---------------------------------------------------------------------------
# Cross-individual dedup (the prod failure scenario)
# ---------------------------------------------------------------------------


def test_cross_individual_duplicates_are_removed(tmp_db):
    """Two rows with different individual_id but the same index key are deduped to one.

    Regression for #651: the dedup was previously grouped by individual_id, so rows
    belonging to two separate individual records pointing at the same person (same
    wiki_url, same office, same term) both survived and caused index creation to fail.
    """
    od_id, tc1, tc2 = _make_office_two_tables(tmp_db)
    ind1 = _make_individual(tmp_db, "https://example.test/wiki/IndA")
    ind2 = _make_individual(tmp_db, "https://example.test/wiki/IndB")
    wiki = "https://example.test/wiki/SharedPerson"

    tmp_db.execute("DROP INDEX IF EXISTS idx_office_terms_hierarchy_dedup")
    tmp_db.commit()

    # Same index key, different individual_id and office_id (multi-table-config scenario)
    _insert_raw(tmp_db, tc1, od_id, ind1, wiki, term_start="1987-01-05", term_end="1995-01-02")
    _insert_raw(tmp_db, tc2, od_id, ind2, wiki, term_start="1987-01-05", term_end="1995-01-02")

    assert _count_terms(tmp_db, wiki) == 2

    _sqlite_add_columns_if_missing(tmp_db)

    assert _count_terms(tmp_db, wiki) == 1, "Cross-individual duplicate must be removed"
    assert _index_exists(tmp_db), "Unique index must be recreated after dedup"


def test_same_individual_multi_config_duplicates_are_removed(tmp_db):
    """Two rows for the same individual via different office_table_config rows are deduped.

    The original prod accumulation scenario: the legacy UNIQUE(office_id, ...) constraint
    never fired across configs because each config has a distinct office_id.
    """
    od_id, tc1, tc2 = _make_office_two_tables(tmp_db)
    ind = _make_individual(tmp_db, "https://example.test/wiki/IndC")
    wiki = "https://example.test/wiki/SharedPerson2"

    tmp_db.execute("DROP INDEX IF EXISTS idx_office_terms_hierarchy_dedup")
    tmp_db.commit()

    _insert_raw(tmp_db, tc1, od_id, ind, wiki, term_start="2000-01-01", term_end="2004-01-01")
    _insert_raw(tmp_db, tc2, od_id, ind, wiki, term_start="2000-01-01", term_end="2004-01-01")

    assert _count_terms(tmp_db, wiki) == 2

    _sqlite_add_columns_if_missing(tmp_db)

    assert _count_terms(tmp_db, wiki) == 1, "Same-individual multi-config duplicate must be removed"


# ---------------------------------------------------------------------------
# Multi-term preservation
# ---------------------------------------------------------------------------


def test_multi_term_person_rows_are_preserved(tmp_db):
    """Rows for the same person/office with different year columns are not collapsed.

    Regression for the v2 index extension: persons who served multiple non-contiguous
    terms (both with NULL text dates but distinct years) must keep one row per term.
    """
    od_id, tc1, tc2 = _make_office_two_tables(tmp_db)
    ind = _make_individual(tmp_db, "https://example.test/wiki/IndD")
    wiki = "https://example.test/wiki/MultiTermPerson"

    id1 = db_office_terms.insert_office_term(
        office_details_id=od_id,
        office_table_config_id=tc1,
        individual_id=ind,
        wiki_url=wiki,
        term_start_year=1777,
        term_end_year=1779,
        conn=tmp_db,
    )
    id2 = db_office_terms.insert_office_term(
        office_details_id=od_id,
        office_table_config_id=tc1,
        individual_id=ind,
        wiki_url=wiki,
        term_start_year=1783,
        term_end_year=1785,
        conn=tmp_db,
    )
    tmp_db.commit()

    assert id1 != id2, "Different terms must produce separate rows"

    _sqlite_add_columns_if_missing(tmp_db)

    assert _count_terms(tmp_db, wiki) == 2, "Both terms must survive the dedup"


# ---------------------------------------------------------------------------
# Retry scenario
# ---------------------------------------------------------------------------


def test_dedup_cleans_duplicates_accumulated_between_deploy_retries(tmp_db):
    """Simulates the prod race: duplicate rows inserted after first init are cleaned on retry.

    The PG migration guards with 'if index_name not in applied' so the dedup re-runs on
    every startup attempt until the index commits. This test verifies the SQLite equivalent
    (_sqlite_add_columns_if_missing called again) also cleans fresh duplicates.
    """
    od_id, tc1, tc2 = _make_office_two_tables(tmp_db)
    ind1 = _make_individual(tmp_db, "https://example.test/wiki/IndE")
    ind2 = _make_individual(tmp_db, "https://example.test/wiki/IndF")
    wiki = "https://example.test/wiki/RetryPerson"

    # Simulate scrape job running without a dedup index — drop index and insert duplicates
    tmp_db.execute("DROP INDEX IF EXISTS idx_office_terms_hierarchy_dedup")
    tmp_db.commit()

    _insert_raw(tmp_db, tc1, od_id, ind1, wiki, term_start_year=1800, term_end_year=1804)
    _insert_raw(tmp_db, tc2, od_id, ind2, wiki, term_start_year=1800, term_end_year=1804)

    assert _count_terms(tmp_db, wiki) == 2

    # Startup retry: _sqlite_add_columns_if_missing runs again
    _sqlite_add_columns_if_missing(tmp_db)

    assert _count_terms(tmp_db, wiki) == 1, "Retry dedup must clean newly accumulated duplicates"
    assert _index_exists(tmp_db), "Index must exist after successful retry dedup"
