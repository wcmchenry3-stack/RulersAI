"""Database connection and initialization."""

import logging
import os
import sqlite3
import threading
from pathlib import Path

_log = logging.getLogger(__name__)

# Exception type tuples for use in CRUD modules — work for both backends.
# Catch _DB_UNIQUE_ERRORS and check "UNIQUE" / "duplicate key" in str(e).
# Catch _DB_OPERATIONAL_ERRORS for "table may not exist" guards (SQLite only in practice).
_DB_UNIQUE_ERRORS: tuple = (sqlite3.IntegrityError,)
_DB_OPERATIONAL_ERRORS: tuple = (sqlite3.OperationalError,)
try:
    import psycopg2
    import psycopg2.errors

    _DB_UNIQUE_ERRORS = _DB_UNIQUE_ERRORS + (psycopg2.errors.UniqueViolation,)
    _DB_OPERATIONAL_ERRORS = _DB_OPERATIONAL_ERRORS + (psycopg2.OperationalError,)
except ImportError:
    pass

# PostgreSQL connection pool — initialized lazily on first use.
# SimpleConnectionPool is sufficient; the scraper runs single-threaded but the
# web app serves concurrent requests via threads, so ThreadedConnectionPool is used.
_pg_pool = None
_pg_pool_lock = threading.Lock()
_PG_POOL_MIN = 1
_PG_POOL_MAX = 5


def _get_pg_pool():
    """Return the shared PostgreSQL connection pool, creating it if needed."""
    global _pg_pool
    if _pg_pool is None:
        with _pg_pool_lock:
            if _pg_pool is None:
                import psycopg2.pool
                import psycopg2.extras

                _pg_pool = psycopg2.pool.ThreadedConnectionPool(
                    _PG_POOL_MIN,
                    _PG_POOL_MAX,
                    os.environ["DATABASE_URL"],
                    cursor_factory=psycopg2.extras.DictCursor,
                )
                _log.info(
                    "PostgreSQL connection pool created (min=%d max=%d)",
                    _PG_POOL_MIN,
                    _PG_POOL_MAX,
                )
    return _pg_pool


PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
DATA_DIR = PROJECT_ROOT / "data"
DB_PATH = DATA_DIR / "office_holder.db"  # test-only default; production uses DATABASE_URL
_DEFAULT_LOG_DIR = DATA_DIR / "logs"
_DEFAULT_CACHE_DIR = DATA_DIR / "wiki_cache"


def is_postgres() -> bool:
    """Return True when DATABASE_URL is set (deployed PostgreSQL)."""
    return bool(os.environ.get("DATABASE_URL"))


class _PGSavepointContext:
    """Context manager for a PostgreSQL savepoint within a shared transaction.

    Use this to wrap an INSERT that may raise a UniqueViolation inside a function
    that accepts a caller-owned connection.  Without a savepoint, any error puts
    the entire outer transaction in an aborted state and all subsequent statements
    fail with InFailedSqlTransaction.

    SQLite does NOT need this: an IntegrityError on SQLite does not abort the
    surrounding connection.  The context manager is a no-op when is_postgres()
    is False so the same calling code works for both backends.

    Usage::

        with _PGSavepointContext(conn, "_my_insert"):
            conn.execute("INSERT ...")
            # If UniqueViolation is raised here the savepoint is rolled back;
            # the outer transaction continues unharmed.
        # After the with-block the savepoint is released (success path).
    """

    def __init__(self, conn, name: str) -> None:
        self._conn = conn
        self._name = name
        self._active = is_postgres()

    def __enter__(self) -> "_PGSavepointContext":
        if self._active:
            self._conn.execute(f"SAVEPOINT {self._name}")
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> bool:
        if self._active:
            if exc_type is None:
                self._conn.execute(f"RELEASE SAVEPOINT {self._name}")
            else:
                self._conn.execute(f"ROLLBACK TO SAVEPOINT {self._name}")
                self._conn.execute(f"RELEASE SAVEPOINT {self._name}")
        return False  # never suppress the exception


def get_db_path() -> Path:
    """Return the SQLite DB path (test use only). Uses OFFICE_HOLDER_DB_PATH env var when set."""
    env_path = os.environ.get("OFFICE_HOLDER_DB_PATH")
    if env_path:
        return Path(env_path)
    return DB_PATH


def get_log_dir() -> Path:
    """Return the logs directory path."""
    env_path = os.environ.get("LOG_DIR")
    if env_path:
        return Path(env_path)
    return _DEFAULT_LOG_DIR


def get_cache_dir() -> Path:
    """Return the wiki cache directory path."""
    env_path = os.environ.get("WIKI_CACHE_DIR")
    if env_path:
        return Path(env_path)
    return _DEFAULT_CACHE_DIR


def ensure_data_dir() -> None:
    """Create cache and log directories if they don't exist."""
    get_log_dir().mkdir(parents=True, exist_ok=True)
    get_cache_dir().mkdir(parents=True, exist_ok=True)
    DATA_DIR.mkdir(parents=True, exist_ok=True)


class _PGConnWrapper:
    """Thin wrapper around a psycopg2 connection that adds .execute() and
    .executemany() shortcuts matching the sqlite3 connection API, so all CRUD
    modules can call conn.execute() / conn.executemany() without changes.
    """

    def __init__(self, conn, pool=None):
        self._conn = conn
        self._pool = pool

    def execute(self, sql, params=None):
        cur = self._conn.cursor()
        if params is not None:
            cur.execute(sql, params)
        else:
            # Do NOT pass an empty tuple — psycopg2 would scan the SQL for '%'
            # placeholders and raise IndexError when it finds one (e.g. inside a
            # DO $$ RAISE NOTICE '% rows ...' block) with nothing to substitute.
            cur.execute(sql)
        return cur

    def executemany(self, sql, seq_of_params):
        cur = self._conn.cursor()
        cur.executemany(sql, seq_of_params)
        return cur

    def commit(self):
        self._conn.commit()

    def rollback(self):
        self._conn.rollback()

    def close(self):
        if self._pool is not None:
            self._pool.putconn(self._conn)
        else:
            self._conn.close()

    def cursor(self):
        return self._conn.cursor()

    @property
    def closed(self):
        return self._conn.closed

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if exc_type is None:
            self._conn.commit()
        else:
            self._conn.rollback()
        self.close()
        return False  # do not suppress exceptions


class _PrefetchedCursor:
    """Wraps a pre-fetched result set so callers can fetch after commit()."""

    def __init__(self, rows, rowcount, description):
        self._rows = list(rows)
        self._idx = 0
        self.rowcount = rowcount
        self.description = description

    def fetchone(self):
        if self._idx >= len(self._rows):
            return None
        row = self._rows[self._idx]
        self._idx += 1
        return row

    def fetchall(self):
        rows = self._rows[self._idx :]
        self._idx = len(self._rows)
        return rows


class _SQLiteConnWrapper:
    """Wraps a sqlite3 connection to accept PostgreSQL-style SQL so CRUD modules
    only need one SQL dialect.  Translations applied to every statement:
      • %s  → ?          (psycopg2 → sqlite3 placeholder)
      • NOW() → CURRENT_TIMESTAMP   (timestamp function)
    SQLite 3.35+ RETURNING and 3.24+ ON CONFLICT upserts are supported natively.
    RETURNING results are pre-fetched so commit() can be called before fetchone().
    """

    def __init__(self, conn):
        self._conn = conn

    @staticmethod
    def _adapt(sql: str) -> str:
        """Translate PostgreSQL-style SQL to SQLite-compatible SQL.

        Translations:
          %s      → ?                  (placeholders)
          %%      → %                  (escaped literal % for psycopg2 → SQLite modulo)
          NOW()   → CURRENT_TIMESTAMP  (timestamp)
          ::TEXT  → (removed)          (PostgreSQL explicit cast — SQLite is dynamically typed)
          ::integer → (removed)        (same)
          ::date  → (removed)          (same)
        """
        import re

        return (
            sql.replace("NOW()", "CURRENT_TIMESTAMP")
            .replace("%s", "?")
            .replace("%%", "%")
            # Strip PostgreSQL type casts (::type) — SQLite is dynamically typed
            .replace("::TEXT", "")
            .replace("::text", "")
            .replace("::integer", "")
            .replace("::INTEGER", "")
            .replace("::date", "")
        )

    def execute(self, sql, params=None):
        adapted = self._adapt(sql)
        cur = (
            self._conn.execute(adapted, params)
            if params is not None
            else self._conn.execute(adapted)
        )
        # Pre-fetch RETURNING results so callers can do commit() before fetchone()
        if "RETURNING" in sql.upper():
            return _PrefetchedCursor(cur.fetchall(), cur.rowcount, cur.description)
        return cur

    def executemany(self, sql, seq_of_params):
        return self._conn.executemany(self._adapt(sql), seq_of_params)

    def executescript(self, sql):
        return self._conn.executescript(sql)

    def commit(self):
        self._conn.commit()

    def rollback(self):
        self._conn.rollback()

    def close(self):
        self._conn.close()

    def cursor(self):
        return self._conn.cursor()

    @property
    def row_factory(self):
        return self._conn.row_factory

    @row_factory.setter
    def row_factory(self, value):
        self._conn.row_factory = value

    @property
    def closed(self):
        return not bool(self._conn)

    def __enter__(self):
        self._conn.__enter__()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        return self._conn.__exit__(exc_type, exc_val, exc_tb)


def get_connection(path: Path | None = None):
    """Return a database connection.

    Production (DATABASE_URL set, no path): returns a _PGConnWrapper (psycopg2).
    Tests (path provided, or DATABASE_URL not set): returns a _SQLiteConnWrapper.
    """
    if is_postgres() and path is None:
        pool = _get_pg_pool()
        conn = pool.getconn()
        return _PGConnWrapper(conn, pool=pool)

    # Test-only: SQLite path — wrapped to accept PostgreSQL-style SQL from CRUD modules
    ensure_data_dir()
    db_path = path if path is not None else get_db_path()
    conn = sqlite3.connect(str(db_path), timeout=10.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    return _SQLiteConnWrapper(conn)


def init_db(path: Path | None = None) -> None:
    """Create / migrate the database on startup."""
    if is_postgres():
        _init_postgres()
    else:
        _init_sqlite(path)


def _init_postgres() -> None:
    """Apply schema and seed data to a fresh or existing PostgreSQL database."""
    from .schema import SCHEMA_PG_SQL, OFFICES_PARTIES_INDEX_PG_SQL
    from .seed import seed_reference_data
    from .seed_reference_docs import seed_wikipedia_mos
    from . import test_scripts as db_test_scripts

    conn = get_connection()
    try:
        # Execute each DDL statement individually (_PGConnWrapper.execute handles this)
        for statement in _split_sql(SCHEMA_PG_SQL):
            conn.execute(statement)
        for statement in _split_sql(OFFICES_PARTIES_INDEX_PG_SQL):
            conn.execute(statement)
        conn.commit()

        seed_reference_data(conn=conn)
        seed_wikipedia_mos(conn=conn)
        conn.commit()

        db_test_scripts.seed_db_from_manifest_if_empty(conn=conn)
        conn.commit()

        _run_pg_migrations(conn)

        from .scheduler_settings import seed_scheduler_settings

        seed_scheduler_settings(conn=conn)

        from .app_settings import seed_app_settings

        seed_app_settings(conn=conn)

        # migrate_to_fk() is deliberately NOT called — PostgreSQL starts with the final schema
    finally:
        conn.close()


def _run_pg_migrations(conn) -> None:
    """Apply idempotent PostgreSQL-only schema corrections that cannot be expressed as
    CREATE TABLE IF NOT EXISTS (e.g. dropping stale FK constraints)."""
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations "
        "(id TEXT PRIMARY KEY, applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW())"
    )
    conn.commit()

    cur = conn.execute("SELECT id FROM schema_migrations")
    applied = {row[0] for row in cur.fetchall()}

    def _apply(name: str, sql: str) -> None:
        if name in applied:
            return
        conn.execute(sql)
        conn.execute("INSERT INTO schema_migrations (id) VALUES (%s)", (name,))
        conn.commit()

    # office_terms.office_id previously had REFERENCES offices(id), but in hierarchy
    # mode it stores office_table_config_id values — violating the FK. Drop it; the
    # office_table_config_id column already carries the proper referential integrity.
    _apply(
        "pg_drop_office_terms_office_id_fkey",
        "ALTER TABLE office_terms DROP CONSTRAINT IF EXISTS office_terms_office_id_fkey",
    )

    # source_pages.url must be unique — prevent duplicate pages from AI-driven inserts.
    # First reassign child rows and remove duplicates, keeping the row with the lowest id.
    _apply(
        "pg_source_pages_dedup",
        """WITH keeper AS (
               SELECT url, MIN(id) AS keep_id FROM source_pages GROUP BY url HAVING COUNT(*) > 1
           )
           UPDATE office_details SET source_page_id = k.keep_id
           FROM keeper k
           JOIN source_pages sp ON sp.url = k.url AND sp.id != k.keep_id
           WHERE office_details.source_page_id = sp.id""",
    )
    _apply(
        "pg_source_pages_dedup_delete",
        """DELETE FROM source_pages
           WHERE id NOT IN (
               SELECT MIN(id) FROM source_pages GROUP BY url
           )""",
    )
    _apply(
        "pg_source_pages_url_unique",
        "ALTER TABLE source_pages ADD CONSTRAINT source_pages_url_key UNIQUE (url)",
    )

    # parse_error_reports: new table for ParseErrorReporter deduplication.
    # Already created by SCHEMA_PG_SQL on fresh installs; this migration adds it
    # to existing production databases that pre-date this table.
    _apply(
        "pg_create_parse_error_reports",
        """
        CREATE TABLE IF NOT EXISTS parse_error_reports (
            id SERIAL PRIMARY KEY,
            fingerprint TEXT NOT NULL UNIQUE,
            function_name TEXT NOT NULL,
            error_type TEXT NOT NULL,
            wiki_url TEXT,
            office_name TEXT,
            github_issue_url TEXT,
            github_issue_number INTEGER,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """,
    )
    _apply(
        "pg_parse_error_reports_fingerprint_idx",
        "CREATE INDEX IF NOT EXISTS idx_parse_error_reports_fingerprint"
        " ON parse_error_reports(fingerprint)",
    )
    _apply(
        "pg_scraper_jobs_queued_at",
        "ALTER TABLE scraper_jobs ADD COLUMN IF NOT EXISTS queued_at TIMESTAMPTZ",
    )
    _apply(
        "pg_scraper_jobs_job_params_json",
        "ALTER TABLE scraper_jobs ADD COLUMN IF NOT EXISTS job_params_json TEXT",
    )
    _apply(
        "pg_individuals_insufficient_vitals_checked_at",
        "ALTER TABLE individuals ADD COLUMN IF NOT EXISTS insufficient_vitals_checked_at TIMESTAMPTZ",
    )
    _apply(
        "pg_individuals_insuf_vitals_checked_at_idx",
        "CREATE INDEX IF NOT EXISTS idx_individuals_insuf_vitals_checked_at"
        " ON individuals(insufficient_vitals_checked_at)",
    )
    _apply(
        "pg_individuals_gemini_research_checked_at",
        "ALTER TABLE individuals ADD COLUMN IF NOT EXISTS gemini_research_checked_at TIMESTAMPTZ",
    )
    _apply(
        "pg_individuals_gemini_research_checked_at_idx",
        "CREATE INDEX IF NOT EXISTS idx_individuals_gemini_research_checked_at"
        " ON individuals(gemini_research_checked_at)",
    )
    _apply(
        "pg_create_individual_research_sources",
        "CREATE TABLE IF NOT EXISTS individual_research_sources ("
        " id SERIAL PRIMARY KEY,"
        " individual_id INTEGER NOT NULL REFERENCES individuals(id),"
        " source_url TEXT NOT NULL,"
        " source_type TEXT,"
        " found_data_json TEXT,"
        " created_at TIMESTAMPTZ NOT NULL DEFAULT NOW())",
    )
    _apply(
        "pg_create_individual_research_sources_idx",
        "CREATE INDEX IF NOT EXISTS idx_individual_research_sources_individual_id"
        " ON individual_research_sources(individual_id)",
    )
    _apply(
        "pg_create_wiki_draft_proposals",
        "CREATE TABLE IF NOT EXISTS wiki_draft_proposals ("
        " id SERIAL PRIMARY KEY,"
        " individual_id INTEGER NOT NULL REFERENCES individuals(id),"
        " proposal_text TEXT NOT NULL,"
        " status TEXT NOT NULL DEFAULT 'pending',"
        " created_at TIMESTAMPTZ NOT NULL DEFAULT NOW())",
    )
    _apply(
        "pg_create_wiki_draft_proposals_individual_idx",
        "CREATE INDEX IF NOT EXISTS idx_wiki_draft_proposals_individual_id"
        " ON wiki_draft_proposals(individual_id)",
    )
    _apply(
        "pg_create_wiki_draft_proposals_status_idx",
        "CREATE INDEX IF NOT EXISTS idx_wiki_draft_proposals_status"
        " ON wiki_draft_proposals(status)",
    )
    _apply(
        "pg_research_sources_origin",
        "ALTER TABLE individual_research_sources ADD COLUMN IF NOT EXISTS"
        " origin TEXT NOT NULL DEFAULT 'manual'",
    )
    _apply(
        "pg_wiki_drafts_origin",
        "ALTER TABLE wiki_draft_proposals ADD COLUMN IF NOT EXISTS"
        " origin TEXT NOT NULL DEFAULT 'manual'",
    )
    _apply(
        "pg_create_reference_documents",
        "CREATE TABLE IF NOT EXISTS reference_documents ("
        " id SERIAL PRIMARY KEY,"
        " doc_key TEXT NOT NULL UNIQUE,"
        " content TEXT NOT NULL,"
        " fetched_at TIMESTAMPTZ,"
        " created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),"
        " updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW())",
    )
    _apply(
        "pg_create_data_quality_reports",
        "CREATE TABLE IF NOT EXISTS data_quality_reports ("
        " id SERIAL PRIMARY KEY,"
        " fingerprint TEXT NOT NULL UNIQUE,"
        " record_type TEXT NOT NULL,"
        " record_id INTEGER NOT NULL,"
        " check_type TEXT NOT NULL,"
        " flagged_by TEXT NOT NULL,"
        " concern_details TEXT,"
        " github_issue_url TEXT,"
        " github_issue_number INTEGER,"
        " created_at TIMESTAMPTZ NOT NULL DEFAULT NOW())",
    )
    _apply(
        "pg_data_quality_reports_fingerprint_idx",
        "CREATE INDEX IF NOT EXISTS idx_data_quality_reports_fingerprint"
        " ON data_quality_reports(fingerprint)",
    )
    _apply(
        "pg_source_pages_last_quality_checked_at",
        "ALTER TABLE source_pages ADD COLUMN IF NOT EXISTS" " last_quality_checked_at TIMESTAMPTZ",
    )
    _apply(
        "pg_create_page_quality_checks",
        "CREATE TABLE IF NOT EXISTS page_quality_checks ("
        " id SERIAL PRIMARY KEY,"
        " source_page_id INTEGER NOT NULL REFERENCES source_pages(id),"
        " checked_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),"
        " html_char_count INTEGER,"
        " office_terms_count INTEGER,"
        " ai_votes TEXT,"
        " result TEXT NOT NULL DEFAULT 'ok',"
        " gh_issue_url TEXT,"
        " created_at TIMESTAMPTZ NOT NULL DEFAULT NOW())",
    )
    _apply(
        "pg_create_suspect_record_flags",
        "CREATE TABLE IF NOT EXISTS suspect_record_flags ("
        " id SERIAL PRIMARY KEY,"
        " individual_id INTEGER REFERENCES individuals(id),"
        " office_id INTEGER,"
        " full_name TEXT,"
        " wiki_url TEXT,"
        " flag_reasons TEXT,"
        " ai_votes TEXT,"
        " result TEXT NOT NULL DEFAULT 'skipped',"
        " gh_issue_url TEXT,"
        " created_at TIMESTAMPTZ NOT NULL DEFAULT NOW())",
    )
    _apply(
        "pg_office_table_config_last_link_fill_rate",
        "ALTER TABLE office_table_config ADD COLUMN IF NOT EXISTS last_link_fill_rate REAL",
    )
    _apply(
        "pg_individuals_superseded_by_individual_id",
        "ALTER TABLE individuals ADD COLUMN IF NOT EXISTS"
        " superseded_by_individual_id INTEGER REFERENCES individuals(id)",
    )
    _apply(
        "pg_create_scheduled_job_runs",
        "CREATE TABLE IF NOT EXISTS scheduled_job_runs ("
        " id SERIAL PRIMARY KEY,"
        " job_name TEXT NOT NULL,"
        " started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),"
        " finished_at TIMESTAMPTZ,"
        " status TEXT NOT NULL DEFAULT 'running',"
        " duration_s NUMERIC(10,2),"
        " result_json TEXT,"
        " error TEXT);"
        "CREATE INDEX IF NOT EXISTS idx_scheduled_job_runs_started"
        " ON scheduled_job_runs (started_at DESC)",
    )
    _apply(
        "pg_create_nolink_supersede_log",
        "CREATE TABLE IF NOT EXISTS nolink_supersede_log ("
        " id SERIAL PRIMARY KEY,"
        " old_individual_id INTEGER NOT NULL REFERENCES individuals(id),"
        " new_individual_id INTEGER NOT NULL REFERENCES individuals(id),"
        " office_id INTEGER NOT NULL,"
        " old_wiki_url TEXT NOT NULL,"
        " new_wiki_url TEXT NOT NULL,"
        " office_terms_reassigned INTEGER NOT NULL DEFAULT 0,"
        " created_at TIMESTAMPTZ NOT NULL DEFAULT NOW())",
    )
    _apply(
        "pg_create_scheduler_settings",
        "CREATE TABLE IF NOT EXISTS scheduler_settings ("
        " job_id TEXT PRIMARY KEY,"
        " paused BOOLEAN NOT NULL DEFAULT FALSE,"
        " updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW())",
    )
    _apply(
        "pg_create_app_settings",
        "CREATE TABLE IF NOT EXISTS app_settings ("
        " key TEXT PRIMARY KEY,"
        " value TEXT NOT NULL,"
        " value_type TEXT NOT NULL DEFAULT 'int',"
        " description TEXT,"
        " updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW())",
    )

    # Issue #311 residual: deduplicate alt_links before adding the UNIQUE constraint.
    # The preceding 5 migrations (backfill through not-null) already ran on production.
    # Production had duplicate (office_details_id, link_path) pairs (e.g. office_details_id=1256)
    # that blocked the constraint; this step removes them, keeping the max-id row per pair.
    _apply(
        "pg_alt_links_dedup_before_unique",
        """
        DO $$
        DECLARE
            dup_count INTEGER;
            deleted_count INTEGER;
        BEGIN
            SELECT COUNT(*) INTO dup_count
            FROM (
                SELECT office_details_id, link_path
                FROM alt_links
                GROUP BY office_details_id, link_path
                HAVING COUNT(*) > 1
            ) dupes;
            RAISE NOTICE 'pg_alt_links_dedup: % duplicate (office_details_id, link_path) pairs found', dup_count;

            DELETE FROM alt_links
            WHERE id NOT IN (
                SELECT MAX(id)
                FROM alt_links
                GROUP BY office_details_id, link_path
            );
            GET DIAGNOSTICS deleted_count = ROW_COUNT;
            RAISE NOTICE 'pg_alt_links_dedup: % duplicate rows deleted', deleted_count;
        END $$;
        """,
    )
    _apply(
        "pg_alt_links_add_unique_office_details_link_path",
        "ALTER TABLE alt_links ADD CONSTRAINT alt_links_office_details_id_link_path_key"
        " UNIQUE (office_details_id, link_path)",
    )
    # Issue #313: drop the legacy offices table now that all FK references are gone.
    # office_terms.office_id FK was dropped in pg_drop_office_terms_office_id_fkey.
    # alt_links.office_id was dropped in pg_alt_links_drop_office_id.
    _apply(
        "pg_drop_offices_indexes",
        "DROP INDEX IF EXISTS idx_offices_country_id;"
        "DROP INDEX IF EXISTS idx_offices_state_id;"
        "DROP INDEX IF EXISTS idx_offices_level_id;"
        "DROP INDEX IF EXISTS idx_offices_branch_id",
    )
    _apply(
        "pg_drop_offices_table",
        "DROP TABLE IF EXISTS offices",
    )
    _apply(
        "pg_office_table_config_cache_batch",
        "ALTER TABLE office_table_config ADD COLUMN IF NOT EXISTS cache_batch INTEGER NOT NULL DEFAULT 0",
    )
    _apply(
        "pg_office_table_config_cache_batch_backfill",
        "UPDATE office_table_config SET cache_batch = id % 7 WHERE cache_batch = 0",
    )
    _apply(
        "pg_create_structural_change_events",
        "CREATE TABLE IF NOT EXISTS structural_change_events ("
        " id SERIAL PRIMARY KEY,"
        " tc_id INTEGER,"
        " office_name TEXT,"
        " page_url TEXT,"
        " prev_rate REAL,"
        " new_rate REAL,"
        " drop_pp REAL,"
        " resolved BOOLEAN NOT NULL DEFAULT FALSE,"
        " created_at TIMESTAMPTZ NOT NULL DEFAULT NOW())",
    )


def _sqlite_add_columns_if_missing(conn) -> None:
    """Idempotently add new columns to pre-existing SQLite tables.

    Required when an existing DB pre-dates a schema change — CREATE TABLE IF NOT EXISTS
    won't add new columns, but the subsequent CREATE INDEX will fail if they are absent.
    """
    migrations = [
        ("individuals", "insufficient_vitals_checked_at", "TEXT"),
        ("individuals", "gemini_research_checked_at", "TEXT"),
        ("scraper_jobs", "queued_at", "TEXT"),
        ("scraper_jobs", "job_params_json", "TEXT"),
        ("individual_research_sources", "origin", "TEXT DEFAULT 'manual'"),
        ("wiki_draft_proposals", "origin", "TEXT DEFAULT 'manual'"),
        ("individuals", "superseded_by_individual_id", "INTEGER"),
        ("source_pages", "last_quality_checked_at", "TEXT"),
        ("office_table_config", "last_link_fill_rate", "REAL"),
        ("office_table_config", "cache_batch", "INTEGER NOT NULL DEFAULT 0"),
    ]
    for table, column, col_type in migrations:
        try:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {col_type}")
            conn.commit()
        except Exception:
            pass  # Column already exists — safe to ignore
    # Backfill cache_batch for any existing rows that still have the default 0
    # (id % 7 can legitimately be 0, so only backfill where ALL rows are 0)
    try:
        count = conn.execute(
            "SELECT COUNT(*) FROM office_table_config WHERE cache_batch != 0"
        ).fetchone()[0]
        if count == 0:
            conn.execute("UPDATE office_table_config SET cache_batch = id % 7")
            conn.commit()
    except Exception:
        pass


def _init_sqlite(path: Path | None = None) -> None:
    """SQLite init for tests — applies the final schema directly (no migrations needed)."""
    from .schema import SCHEMA_SQL
    from .seed import seed_reference_data
    from .seed_reference_docs import seed_wikipedia_mos
    from . import test_scripts as db_test_scripts

    conn = get_connection(path)
    try:
        _sqlite_add_columns_if_missing(conn)
        conn.executescript(SCHEMA_SQL)
        conn.commit()
        seed_reference_data(conn=conn)
        seed_wikipedia_mos(conn=conn)
        db_test_scripts.seed_db_from_manifest_if_empty(conn=conn)
        from .scheduler_settings import seed_scheduler_settings

        seed_scheduler_settings(conn=conn)

        from .app_settings import seed_app_settings

        seed_app_settings(conn=conn)
        conn.commit()
    finally:
        conn.close()


def _split_sql(sql: str) -> list[str]:
    """Split a multi-statement SQL string into individual statements."""
    return [s.strip() for s in sql.split(";") if s.strip()]
