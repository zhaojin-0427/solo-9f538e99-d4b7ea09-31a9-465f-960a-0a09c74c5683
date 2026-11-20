"""Permit registry and re-audit worklist.

Design
------
Two materialized tables keep permit operations independent of chain replay:

* ``trusted_handovers`` — every GIVER/RECEIVER role of every handover inside a
  waybill's verified prefix, rebuilt for that one waybill whenever a scan batch
  changes its prefix.  Only trusted-prefix handovers are therefore eligible for
  re-audit; unverified scans never enter the worklist.
* ``permit_pending`` — verified handovers whose giver/receiver had no valid
  permit of the waybill's category at the handover instant (half-open UTC
  interval ``[valid_from, valid_to)``).

Permit replacement for one custodian only touches rows of that custodian, so
no waybill chain is ever replayed.  Every write runs inside one
``BEGIN IMMEDIATE`` transaction together with the calling write path, meaning
interleaved submissions always decide against the same committed state of
scans and permits, and any failure rolls the old registry and worklist back.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

from .schemas import (
    ChainResponse,
    CustodianPendingResponse,
    PendingItem,
    PermitAuditResponse,
    PermitInterval,
    PermitListResponse,
    PermitReplace,
    PermitReplaceResponse,
    ScanView,
    to_utc_iso,
    utcnow_iso,
)

_STORAGE_FMT = "%Y-%m-%dT%H:%M:%S.%fZ"


class DomainConflict(Exception):
    """HTTP 409: conflicting registry operation."""


class DomainValidation(Exception):
    """HTTP 404: referenced waybill/resource does not exist."""


class PermitValidationError(Exception):
    """HTTP 422: malformed permit intervals (also enforced at the schema layer)."""


# --------------------------------------------------------------------------- #
# Time / row helpers
# --------------------------------------------------------------------------- #
def _parse_ts(text: str) -> datetime:
    return datetime.strptime(text, _STORAGE_FMT).replace(tzinfo=timezone.utc)


def _row_to_pending(row: sqlite3.Row) -> PendingItem:
    return PendingItem(
        waybill_id=row["waybill_id"],
        permit_category=row["permit_category"],
        scan_id=row["scan_id"],
        revision=row["revision"],
        handover_ts_utc=_parse_ts(row["handover_ts"]),
        missing_party=row["missing_party"],
        role=row["role"],
    )


def _sort_items(items: list[PendingItem]) -> list[PendingItem]:
    return sorted(
        items,
        key=lambda i: (i.waybill_id, i.handover_ts_utc, i.scan_id, i.missing_party),
    )


# --------------------------------------------------------------------------- #
# Materialized trusted prefix maintenance (driven by scan-batch commits)
# --------------------------------------------------------------------------- #
def rebuild_waybill_materialized(
    conn: sqlite3.Connection,
    waybill_id: str,
    permit_category: str,
    chain: ChainResponse,
) -> None:
    """Replace trusted-handovers and pending rows of one waybill.

    Only the verified prefix is materialized, and only this waybill's rows are
    touched — no other waybill's chain is recomputed.
    """
    conn.execute(
        "DELETE FROM trusted_handovers WHERE waybill_id = ?", (waybill_id,)
    )
    conn.execute(
        "DELETE FROM permit_pending WHERE waybill_id = ?", (waybill_id,)
    )

    verified: list[ScanView] = chain.verified_prefix
    for scan in verified:
        ts = to_utc_iso(scan.ts_utc)  # type: ignore[arg-type]
        parties = (scan.giver, scan.receiver)
        if scan.giver == scan.receiver:
            conn.execute(
                "INSERT INTO trusted_handovers "
                "(waybill_id, permit_category, scan_id, revision, handover_ts, party, role) "
                "VALUES (?, ?, ?, ?, ?, ?, 'BOTH')",
                (waybill_id, permit_category, scan.scan_id, scan.revision, ts, scan.giver),
            )
        else:
            conn.executemany(
                "INSERT INTO trusted_handovers "
                "(waybill_id, permit_category, scan_id, revision, handover_ts, party, role) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                [
                    (waybill_id, permit_category, scan.scan_id, scan.revision, ts, scan.giver, "GIVER"),
                    (waybill_id, permit_category, scan.scan_id, scan.revision, ts, scan.receiver, "RECEIVER"),
                ],
            )

        for party in set(parties):
            role = (
                "BOTH"
                if scan.giver == scan.receiver
                else ("GIVER" if party == scan.giver else "RECEIVER")
            )
            if not _party_has_valid_permit(conn, party, permit_category, ts):
                conn.execute(
                    "INSERT INTO permit_pending "
                    "(waybill_id, permit_category, scan_id, revision, handover_ts, missing_party, role) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (waybill_id, permit_category, scan.scan_id, scan.revision, ts, party, role),
                )


def _party_has_valid_permit(
    conn: sqlite3.Connection, party: str, permit_category: str, ts_text: str
) -> bool:
    """Half-open validity: valid_from <= ts < valid_to."""
    row = conn.execute(
        "SELECT 1 FROM permits "
        "WHERE custodian = ? AND permit_category = ? "
        "AND valid_from <= ? AND ? < valid_to LIMIT 1",
        (party, permit_category, ts_text, ts_text),
    ).fetchone()
    return row is not None


# --------------------------------------------------------------------------- #
# Permit registry: whole-batch replacement per (custodian, category)
# --------------------------------------------------------------------------- #
def list_permits(
    conn: sqlite3.Connection, custodian: str, permit_category: str
) -> PermitListResponse:
    from .database import read_snapshot

    with read_snapshot(conn):
        rows = conn.execute(
            "SELECT valid_from, valid_to FROM permits "
            "WHERE custodian = ? AND permit_category = ? "
            "ORDER BY valid_from, valid_to",
            (custodian, permit_category),
        ).fetchall()
        return PermitListResponse(
            custodian=custodian,
            permit_category=permit_category,
            intervals=[
                PermitInterval(
                    valid_from=_parse_ts(r["valid_from"]),
                    valid_to=_parse_ts(r["valid_to"]),
                )
                for r in rows
            ],
        )


def replace_permits(
    conn: sqlite3.Connection,
    custodian: str,
    permit_category: str,
    body: PermitReplace,
) -> PermitReplaceResponse:
    from .database import transaction

    custodian = custodian.strip()
    permit_category = permit_category.strip()
    if not custodian or not permit_category:
        raise DomainValidation("custodian and permit_category must not be empty")

    normalized = sorted(
        (
            iv.valid_from.astimezone(timezone.utc),
            iv.valid_to.astimezone(timezone.utc),
        )
        for iv in body.intervals
    )
    # Defense in depth: the pydantic model already rejects these, but a
    # direct service-layer caller must never be able to persist bad intervals.
    for valid_from, valid_to in normalized:
        if valid_to <= valid_from:
            raise PermitValidationError(
                f"permit interval must be half-open [valid_from, valid_to) with "
                f"valid_to > valid_from; got [{valid_from.isoformat()}, {valid_to.isoformat()})"
            )
    for (f1, t1), (f2, _t2) in zip(normalized, normalized[1:]):
        if f2 < t1:
            raise PermitValidationError("permit intervals must not overlap")

    # The whole decision — old worklist snapshot, registry swap, worklist
    # refresh and new/old diff — happens against one committed state inside a
    # single write transaction.  Any failure rolls registry and worklist back.
    with transaction(conn):
        # Snapshot the worklist entries attributable to this custodian before
        # the swap, so the response reports exactly what this replacement changed.
        old_rows = conn.execute(
            "SELECT * FROM permit_pending "
            "WHERE permit_category = ? AND missing_party = ?",
            (permit_category, custodian),
        ).fetchall()
        old_by_key = {
            (r["waybill_id"], r["scan_id"], r["missing_party"]): _row_to_pending(r)
            for r in old_rows
        }

        # Whole-batch replacement: an empty list clears the registration.
        conn.execute(
            "DELETE FROM permits WHERE custodian = ? AND permit_category = ?",
            (custodian, permit_category),
        )
        now = utcnow_iso()
        conn.executemany(
            "INSERT INTO permits (custodian, permit_category, valid_from, valid_to, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            [
                (custodian, permit_category, to_utc_iso(f), to_utc_iso(t), now)
                for f, t in normalized
            ],
        )
        _refresh_pending_for_custodian(conn, custodian, permit_category)

        new_rows = conn.execute(
            "SELECT * FROM permit_pending "
            "WHERE permit_category = ? AND missing_party = ?",
            (permit_category, custodian),
        ).fetchall()
        new_by_key = {
            (r["waybill_id"], r["scan_id"], r["missing_party"]): _row_to_pending(r)
            for r in new_rows
        }

    added = _sort_items([v for k, v in new_by_key.items() if k not in old_by_key])
    removed = _sort_items([v for k, v in old_by_key.items() if k not in new_by_key])

    return PermitReplaceResponse(
        custodian=custodian,
        permit_category=permit_category,
        intervals=[
            PermitInterval(valid_from=f, valid_to=t) for f, t in normalized
        ],
        added=added,
        removed=removed,
    )


def _refresh_pending_for_custodian(
    conn: sqlite3.Connection, custodian: str, permit_category: str
) -> None:
    """Re-evaluate only this custodian's roles in materialized trusted handovers.

    No custody chain is replayed: the trusted-prefix materialization is read
    directly.  Rows for every other custodian stay untouched.
    """
    conn.execute(
        "DELETE FROM permit_pending "
        "WHERE permit_category = ? AND missing_party = ?",
        (permit_category, custodian),
    )
    roles = conn.execute(
        "SELECT waybill_id, permit_category, scan_id, revision, handover_ts, party, role "
        "FROM trusted_handovers "
        "WHERE permit_category = ? AND party = ? "
        "ORDER BY waybill_id, handover_ts, scan_id",
        (permit_category, custodian),
    ).fetchall()
    for r in roles:
        if not _party_has_valid_permit(conn, custodian, permit_category, r["handover_ts"]):
            conn.execute(
                "INSERT INTO permit_pending "
                "(waybill_id, permit_category, scan_id, revision, handover_ts, missing_party, role) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    r["waybill_id"],
                    r["permit_category"],
                    r["scan_id"],
                    r["revision"],
                    r["handover_ts"],
                    r["party"],
                    r["role"],
                ),
            )


# --------------------------------------------------------------------------- #
# Re-audit queries
# --------------------------------------------------------------------------- #
def get_custodian_pending(
    conn: sqlite3.Connection, custodian: str, permit_category: str | None
) -> CustodianPendingResponse:
    from .database import read_snapshot

    custodian = custodian.strip()
    if not custodian:
        raise DomainValidation("custodian must not be empty")

    with read_snapshot(conn):
        if permit_category is not None:
            rows = conn.execute(
                "SELECT * FROM permit_pending "
                "WHERE missing_party = ? AND permit_category = ? "
                "ORDER BY handover_ts, waybill_id, scan_id",
                (custodian, permit_category.strip()),
            ).fetchall()
            category = permit_category.strip()
        else:
            rows = conn.execute(
                "SELECT * FROM permit_pending WHERE missing_party = ? "
                "ORDER BY handover_ts, waybill_id, scan_id",
                (custodian,),
            ).fetchall()
            category = None
    return CustodianPendingResponse(
        custodian=custodian,
        permit_category=category,
        items=[_row_to_pending(r) for r in rows],
    )


def audit_waybill(conn: sqlite3.Connection, waybill_id: str) -> PermitAuditResponse:
    from .database import read_snapshot

    with read_snapshot(conn):
        return _audit_waybill_locked(conn, waybill_id)


def _audit_waybill_locked(
    conn: sqlite3.Connection, waybill_id: str
) -> PermitAuditResponse:
    """Caller must already hold a transaction/snapshot on ``conn``."""
    row = conn.execute(
        "SELECT id, permit_category FROM waybills WHERE id = ?", (waybill_id,)
    ).fetchone()
    if row is None:
        raise DomainValidation(f"waybill {waybill_id!r} not found")
    category = row["permit_category"]

    # Entering one waybill number pulls every waybill of the same permit
    # category into the re-audit scope, not just that waybill's own rows.
    rows = conn.execute(
        "SELECT * FROM permit_pending "
        "WHERE permit_category = ? "
        "ORDER BY handover_ts, waybill_id, scan_id, missing_party",
        (category,),
    ).fetchall()
    items = [_row_to_pending(r) for r in rows]

    grouped: dict[str, list[PendingItem]] = {}
    for item in items:
        grouped.setdefault(item.missing_party, []).append(item)

    return PermitAuditResponse(
        waybill_id=waybill_id,
        permit_category=category,
        items=items,
        by_missing_party=grouped,
    )


__all__ = [
    "DomainConflict",
    "DomainValidation",
    "PermitValidationError",
    "audit_waybill",
    "get_custodian_pending",
    "list_permits",
    "rebuild_waybill_materialized",
    "replace_permits",
]
