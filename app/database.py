"""SQLite connection management and schema.

Concurrency model
-----------------
* The database runs in WAL mode so readers (queries) never block a writer
  (batch submission) and vice versa.
* Every write batch runs inside a single ``BEGIN IMMEDIATE`` transaction.
  Either the whole batch is committed or nothing changes ("failure keeps the
  old chain").  Readers always see a fully committed snapshot, never a mix of
  revisions -- including after a process restart, because uncommitted data is
  never persisted.
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from . import config

_SCHEMA = """
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;
PRAGMA busy_timeout = {busy_timeout};

CREATE TABLE IF NOT EXISTS waybills (
    id              TEXT PRIMARY KEY,
    origin_party    TEXT NOT NULL,
    permit_category TEXT NOT NULL DEFAULT 'DANGEROUS_GOODS',
    created_at      TEXT NOT NULL
);

-- One row per (scan_id, revision).  A scan's adopted state is the row with
-- the highest revision for that scan_id; withdrawn rows are tombstones.
CREATE TABLE IF NOT EXISTS scans (
    waybill_id      TEXT NOT NULL REFERENCES waybills(id),
    scan_id         TEXT NOT NULL,
    revision        INTEGER NOT NULL CHECK (revision > 0),
    event_type      TEXT NOT NULL CHECK (event_type IN ('HANDOVER', 'WITHDRAWAL')),
    ts_utc          TEXT,
    giver           TEXT,
    receiver        TEXT,
    payload_hash    TEXT NOT NULL,
    created_at      TEXT NOT NULL,
    PRIMARY KEY (waybill_id, scan_id, revision)
);

CREATE INDEX IF NOT EXISTS idx_scans_waybill ON scans (waybill_id, scan_id, revision);

-- Permit registry: one row per (custodian, permit_category, validity interval).
-- Validity is half-open UTC [valid_from, valid_to).  A replacement deletes and
-- re-inserts all rows of the (custodian, category) key inside one transaction;
-- an empty replacement list clears the registration entirely.
CREATE TABLE IF NOT EXISTS permits (
    custodian       TEXT NOT NULL,
    permit_category TEXT NOT NULL,
    valid_from      TEXT NOT NULL,
    valid_to        TEXT NOT NULL,
    created_at      TEXT NOT NULL,
    PRIMARY KEY (custodian, permit_category, valid_from, valid_to)
);

CREATE INDEX IF NOT EXISTS idx_permits_lookup
    ON permits (custodian, permit_category, valid_from, valid_to);

-- Materialized trusted prefix: one row per party role of each verified
-- handover.  Rebuilt per waybill inside the scan-batch transaction, so permit
-- replacement / pending queries never replay any waybill's custody chain.
CREATE TABLE IF NOT EXISTS trusted_handovers (
    waybill_id      TEXT NOT NULL REFERENCES waybills(id),
    permit_category TEXT NOT NULL,
    scan_id         TEXT NOT NULL,
    revision        INTEGER NOT NULL,
    handover_ts     TEXT NOT NULL,
    party           TEXT NOT NULL,
    role            TEXT NOT NULL CHECK (role IN ('GIVER', 'RECEIVER', 'BOTH')),
    PRIMARY KEY (waybill_id, scan_id, party)
);

CREATE INDEX IF NOT EXISTS idx_trusted_party_ts
    ON trusted_handovers (permit_category, party, handover_ts);

-- Re-audit worklist: verified-prefix handovers whose giver or receiver held
-- no valid permit of the waybill's category at the handover instant.
CREATE TABLE IF NOT EXISTS permit_pending (
    waybill_id      TEXT NOT NULL REFERENCES waybills(id),
    permit_category TEXT NOT NULL,
    scan_id         TEXT NOT NULL,
    revision        INTEGER NOT NULL,
    handover_ts     TEXT NOT NULL,
    missing_party   TEXT NOT NULL,
    role            TEXT NOT NULL CHECK (role IN ('GIVER', 'RECEIVER', 'BOTH')),
    PRIMARY KEY (waybill_id, scan_id, missing_party)
);

CREATE INDEX IF NOT EXISTS idx_pending_party
    ON permit_pending (permit_category, missing_party);
CREATE INDEX IF NOT EXISTS idx_pending_waybill
    ON permit_pending (permit_category, waybill_id);
"""


def _migrate_legacy_schema(conn: sqlite3.Connection) -> bool:
    """Upgrade pre-permit SQLite volumes in place without losing data."""
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(waybills)").fetchall()}
    legacy = "permit_category" not in cols
    if legacy:
        conn.execute(
            "ALTER TABLE waybills "
            "ADD COLUMN permit_category TEXT NOT NULL DEFAULT 'DANGEROUS_GOODS'"
        )
    return legacy


def _backfill_materialized_views(conn: sqlite3.Connection, legacy: bool) -> None:
    """Populate trusted-prefix / pending tables once for a legacy volume.

    Only volumes created before the permit feature need this one-time
    backfill; new volumes maintain both tables incrementally per batch.
    """
    if not legacy:
        return
    waybills = conn.execute("SELECT id, permit_category FROM waybills").fetchall()
    if not waybills:
        return

    # Imported lazily to avoid a circular import at module load time.
    from . import permits, services

    for wb in waybills:
        chain = services.compute_chain(conn, wb["id"])
        permits.rebuild_waybill_materialized(
            conn, wb["id"], wb["permit_category"], chain
        )


def connect(db_path: Path | str | None = None) -> sqlite3.Connection:
    """Open a connection with sane, chain-integrity-preserving pragmas."""
    path = Path(db_path) if db_path is not None else config.DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(
        str(path),
        timeout=config.BUSY_TIMEOUT_MS / 1000.0,
        isolation_level=None,  # we manage transactions explicitly
    )
    conn.row_factory = sqlite3.Row
    conn.execute(f"PRAGMA busy_timeout = {config.BUSY_TIMEOUT_MS};")
    conn.execute("PRAGMA foreign_keys = ON;")
    return conn


def init_db(db_path: Path | str | None = None) -> None:
    """Create the schema (idempotent) and migrate legacy volumes."""
    conn = connect(db_path)
    try:
        conn.executescript(
            _SCHEMA.format(busy_timeout=config.BUSY_TIMEOUT_MS)
        )
        legacy = _migrate_legacy_schema(conn)
        # One-time materialization so re-audit works immediately after a
        # restart on a pre-permit SQLite volume.
        _backfill_materialized_views(conn, legacy)
    finally:
        conn.close()


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Serialize writers with BEGIN IMMEDIATE.

    Any exception rolls the whole batch back, leaving the previously
    committed chain untouched.
    """
    conn.execute("BEGIN IMMEDIATE;")
    try:
        yield conn
    except Exception:
        conn.rollback()
        raise
    else:
        conn.commit()


@contextmanager
def read_snapshot(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Pin one WAL snapshot for every read inside the block.

    Without an explicit transaction, each SELECT under ``isolation_level=None``
    can acquire a fresh snapshot, so two related queries could observe rows
    from different committed states (a mixed-revision / mixed worklist read).
    A deferred ``BEGIN`` binds the whole block to the snapshot of its first
    read; the transaction is released read-only afterwards.
    """
    conn.execute("BEGIN;")
    try:
        yield conn
    finally:
        conn.rollback()
