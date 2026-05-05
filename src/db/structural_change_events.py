# -*- coding: utf-8 -*-
"""CRUD helpers for the structural_change_events table."""

from __future__ import annotations

from src.db.connection import get_connection


def insert_event(
    tc_id: int | None,
    office_name: str | None,
    page_url: str | None,
    prev_rate: float,
    new_rate: float,
    drop_pp: float,
    conn=None,
) -> int:
    """Log one structural change detection. Returns the new row id."""
    own_conn = conn is None
    if own_conn:
        conn = get_connection()
    try:
        cur = conn.execute(
            "INSERT INTO structural_change_events"
            " (tc_id, office_name, page_url, prev_rate, new_rate, drop_pp)"
            " VALUES (%s, %s, %s, %s, %s, %s)"
            " RETURNING id",
            (tc_id, office_name, page_url, prev_rate, new_rate, drop_pp),
        )
        row = cur.fetchone()
        if own_conn:
            conn.commit()
        return row[0] if row else 0
    finally:
        if own_conn:
            conn.close()


def list_unresolved(conn=None) -> list[dict]:
    """Return all unresolved structural change events, newest first."""
    own_conn = conn is None
    if own_conn:
        conn = get_connection()
    try:
        cur = conn.execute(
            "SELECT id, tc_id, office_name, page_url, prev_rate, new_rate, drop_pp, created_at"
            " FROM structural_change_events"
            " WHERE resolved IS NOT TRUE"
            " ORDER BY id DESC"
        )
        keys = [
            "id",
            "tc_id",
            "office_name",
            "page_url",
            "prev_rate",
            "new_rate",
            "drop_pp",
            "created_at",
        ]
        return [dict(zip(keys, row)) for row in cur.fetchall()]
    finally:
        if own_conn:
            conn.close()


def mark_resolved(event_id: int, conn=None) -> None:
    """Mark a structural change event as resolved."""
    own_conn = conn is None
    if own_conn:
        conn = get_connection()
    try:
        conn.execute(
            "UPDATE structural_change_events SET resolved = TRUE WHERE id = %s",
            (event_id,),
        )
        if own_conn:
            conn.commit()
    finally:
        if own_conn:
            conn.close()
