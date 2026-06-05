"""Report queries: recent deaths, recent term ends, recent term starts (last 90 days)."""

from typing import Any

from .connection import get_connection, is_postgres
from .utils import _row_to_dict


def get_recent_deaths(conn=None) -> list[dict[str, Any]]:
    """Individuals with death_date in the last 90 days. Returns list of dicts (full_name, birth_date, death_date)."""
    own_conn = conn is None
    if own_conn:
        conn = get_connection()
    try:
        if is_postgres():
            # Guard the ::date cast — year-only values like '1891' raise InvalidDatetimeFormat.
            where = (
                "death_date ~ '^\\d{4}-\\d{2}-\\d{2}$'"
                " AND death_date::date BETWEEN CURRENT_DATE - INTERVAL '90 days' AND CURRENT_DATE"
            )
        else:
            where = "death_date BETWEEN date('now', '-90 days') AND CURRENT_DATE"
        cur = conn.execute(f"""SELECT full_name, birth_date, death_date
               FROM individuals
               WHERE {where}
               ORDER BY death_date DESC""")
        return [_row_to_dict(r) for r in cur.fetchall()]
    finally:
        if own_conn:
            conn.close()


def _term_report_query(
    conn,
    date_column: str,
    order_column: str,
) -> list[dict[str, Any]]:
    """Shared logic: term report filtered by date_column (term_start or term_end), ordered by order_column."""
    if is_postgres():
        # Guard the ::date cast — year-only values like '1891' raise InvalidDatetimeFormat.
        where = (
            f"ot.{date_column} ~ '^\\d{{4}}-\\d{{2}}-\\d{{2}}$'"
            f" AND ot.{date_column}::date BETWEEN CURRENT_DATE - INTERVAL '90 days' AND CURRENT_DATE"
        )
    else:
        where = f"ot.{date_column} BETWEEN date('now', '-90 days') AND CURRENT_DATE"
    cur = conn.execute(f"""
        SELECT
          i.full_name AS "Name",
          c.name AS "Country Name",
          s.name AS "State Name",
          l.name AS "Level",
          b.name AS "Branch",
          od.name AS "Office Name",
          ot.district AS "Congressional District",
          ot.term_start AS "Term Start",
          ot.term_end AS "Term End"
        FROM office_terms ot
        LEFT JOIN individuals i ON i.id = ot.individual_id
        LEFT JOIN office_details od ON od.id = ot.office_details_id
        LEFT JOIN source_pages sp ON sp.id = od.source_page_id
        LEFT JOIN countries c ON c.id = sp.country_id
        LEFT JOIN states s ON s.id = sp.state_id
        LEFT JOIN levels l ON l.id = sp.level_id
        LEFT JOIN branches b ON b.id = sp.branch_id
        WHERE {where}
        ORDER BY ot.{order_column} DESC
        """)
    return [_row_to_dict(r) for r in cur.fetchall()]


def get_recent_term_ends(conn=None) -> list[dict[str, Any]]:
    """Office terms with term_end in the last 90 days. Returns list of dicts with Name, Country Name, etc."""
    own_conn = conn is None
    if own_conn:
        conn = get_connection()
    try:
        return _term_report_query(conn, "term_end", "term_end")
    finally:
        if own_conn:
            conn.close()


def get_recent_term_starts(conn=None) -> list[dict[str, Any]]:
    """Office terms with term_start in the last 90 days. Returns list of dicts with Name, Country Name, etc."""
    own_conn = conn is None
    if own_conn:
        conn = get_connection()
    try:
        return _term_report_query(conn, "term_start", "term_start")
    finally:
        if own_conn:
            conn.close()
