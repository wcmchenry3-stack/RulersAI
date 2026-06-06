"""Regression tests for office_terms dedup behaviour during DB startup.

Covers issues #651, #654, #655: the dedup must clean cross-individual duplicate rows and
the dedup + CREATE INDEX + migration-record must be atomic (single transaction under a
table lock) so concurrent writers cannot reintroduce duplicates between the steps.
Startup must also degrade gracefully rather than crash-loop when the index step fails.

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


# ---------------------------------------------------------------------------
# PG migration path: atomicity and graceful degradation (issues #654, #655)
# ---------------------------------------------------------------------------


def test_pg_v2_index_failure_degrades_gracefully():
    """If CREATE UNIQUE INDEX fails, _run_pg_migrations must not raise, must call
    rollback(), and must not record the migration row (so the block retries next boot).

    Regression for #654/#655: previously the non-atomic two-step (dedup commit then
    separate index creation) would crash the app, perpetuating a deadlock where the
    old instance kept the new one from ever becoming healthy.
    """
    from unittest.mock import MagicMock
    from src.db.connection import _run_pg_migrations

    mock_cursor = MagicMock()
    mock_cursor.fetchall.return_value = []  # empty applied set — all migrations will run

    mock_conn = MagicMock()
    schema_migrations_inserts: list[str] = []

    def _execute(sql, *args, **kwargs):
        sql_str = sql.strip() if isinstance(sql, str) else ""
        if "SELECT id FROM schema_migrations" in sql_str:
            return mock_cursor
        # Only fail on the v2 index (which includes term_start_year),
        # not the earlier v1 index (4-column key, no year columns).
        if (
            "CREATE UNIQUE INDEX" in sql_str
            and "idx_office_terms_hierarchy_dedup" in sql_str
            and "term_start_year" in sql_str
        ):
            raise RuntimeError("could not create unique index: duplicate key")
        if "INSERT INTO schema_migrations" in sql_str and args:
            schema_migrations_inserts.append(str(args[0]))
        return MagicMock()

    mock_conn.execute.side_effect = _execute

    # Must NOT raise — graceful degradation
    _run_pg_migrations(mock_conn)

    # rollback must be called exactly once (for the failed v2 block)
    mock_conn.rollback.assert_called_once()

    # Migration row must NOT be recorded — ensures retry on next boot
    assert not any(
        "pg_office_terms_hierarchy_dedup_idx_v2" in s for s in schema_migrations_inserts
    ), "v2 migration row must not be recorded when index creation fails"


def test_pg_v2_index_block_is_skipped_when_already_applied():
    """If the v2 migration is already in schema_migrations, the dedup block is not re-run.

    Verifies idempotency: once the index is committed, neither LOCK TABLE nor DELETE
    executes on subsequent startups.
    """
    from unittest.mock import MagicMock, call
    from src.db.connection import _run_pg_migrations

    mock_cursor = MagicMock()
    # Simulate a DB where the v2 migration has already been applied
    mock_cursor.fetchall.return_value = [("pg_office_terms_hierarchy_dedup_idx_v2",)]

    mock_conn = MagicMock()

    def _execute(sql, *args, **kwargs):
        if "SELECT id FROM schema_migrations" in sql.strip():
            return mock_cursor
        return MagicMock()

    mock_conn.execute.side_effect = _execute

    _run_pg_migrations(mock_conn)

    executed_sqls = [str(c.args[0]) for c in mock_conn.execute.call_args_list if c.args]
    assert not any(
        "LOCK TABLE" in s for s in executed_sqls
    ), "LOCK TABLE must not run when v2 migration is already applied"
    # Distinguish the v2-block DELETE (groups by term_start_year) from the earlier
    # one-time dedup migration (groups by individual_id, no year columns).
    assert not any(
        "DELETE FROM office_terms" in s and "term_start_year" in s for s in executed_sqls
    ), "v2 dedup DELETE must not run when migration is already applied"
