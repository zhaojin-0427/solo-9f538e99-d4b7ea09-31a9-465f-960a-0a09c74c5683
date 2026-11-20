"""Concurrency and atomicity of the permit re-audit worklist."""
import threading
from datetime import datetime, timezone

from conftest import handover, make_waybill

CAT = "DG-CLASS-3"


def _wb(client, wid="AWB-1", origin="ORIG"):
    r = client.post(
        "/api/waybills",
        json={"waybill_id": wid, "origin_party": origin, "permit_category": CAT},
    )
    assert r.status_code == 201, r.text


def _permit(window_start="2026-11-18T00:00:00Z", window_end="2026-11-19T00:00:00Z"):
    return {"intervals": [{"valid_from": window_start, "valid_to": window_end}]}


def test_interleaved_scan_and_permit_writes_never_mix_snapshots(client):
    """Real threads: every observed worklist is consistent with one committed state."""
    from app import config, database, schemas, services
    from app import permits as permit_svc

    _wb(client)
    db_path = config.DB_PATH
    errors: list[str] = []
    observations: list[tuple[tuple, tuple]] = []
    stop = threading.Event()
    lock = threading.Lock()

    def reader() -> None:
        conn = database.connect(db_path)
        try:
            while not stop.is_set():
                # One explicit snapshot for both reads so they always decide
                # against the same committed state of scans and permits.
                with database.read_snapshot(conn):
                    chain = services._compute_chain_locked(conn, "AWB-1")
                    audit = permit_svc._audit_waybill_locked(conn, "AWB-1")
                trusted_ids = tuple(s.scan_id for s in chain.verified_prefix)
                pending_scans = {(i.waybill_id, i.scan_id) for i in audit.items}
                ok = all(
                    wid == "AWB-1" and sid in trusted_ids for wid, sid in pending_scans
                )
                with lock:
                    observations.append((trusted_ids, tuple(sorted(pending_scans))))
                    if not ok:
                        errors.append(
                            f"pending references non-trusted scan: {pending_scans} vs {trusted_ids}"
                        )
        finally:
            conn.close()

    reader_thread = threading.Thread(target=reader)
    reader_thread.start()

    # S1..S4 form one continuous chain ORIG -> B1 -> B2 -> B3 -> B4.
    chain_specs = [
        (1, "ORIG", "B1"),
        (2, "B1", "B2"),
        (3, "B2", "B3"),
        (4, "B3", "B4"),
    ]

    def write_scan(rev: int, giver: str, receiver: str) -> None:
        conn = database.connect(db_path)
        try:
            batch = schemas.BatchSubmit(
                scans=[
                    schemas.ScanEntry(
                        scan_id=f"S{rev}",
                        revision=1,
                        ts_utc=datetime(2026, 11, 18, 8 + rev, tzinfo=timezone.utc),
                        giver=giver,
                        receiver=receiver,
                    )
                ]
            )
            try:
                services.apply_batch(conn, "AWB-1", batch)
            except services.DomainConflict:
                pass
        except Exception as exc:  # pragma: no cover
            with lock:
                errors.append(str(exc))
        finally:
            conn.close()

    def write_permit(custodian: str) -> None:
        conn = database.connect(db_path)
        try:
            body = schemas.PermitReplace(
                intervals=[
                    schemas.PermitInterval(
                        valid_from=datetime(2026, 11, 18, tzinfo=timezone.utc),
                        valid_to=datetime(2026, 11, 20, tzinfo=timezone.utc),
                    )
                ]
            )
            permit_svc.replace_permits(conn, custodian, CAT, body)
        except Exception as exc:  # pragma: no cover
            with lock:
                errors.append(str(exc))
        finally:
            conn.close()

    workers = [
        threading.Thread(target=write_scan, args=spec) for spec in chain_specs
    ]
    workers += [
        threading.Thread(target=write_permit, args=(c,))
        for c in ("ORIG", "B1", "B2", "B3", "B4")
    ]
    for t2 in workers:
        t2.start()
    for t2 in workers:
        t2.join(timeout=30)
    stop.set()
    reader_thread.join(timeout=30)

    assert not errors, errors
    assert observations, "reader never observed a snapshot"

    # Final committed state: all four scans trusted, every party permitted ->
    # the worklist must be empty and consistent with that snapshot.
    final_audit = client.get("/api/waybills/AWB-1/permit-audit").json()
    assert final_audit["items"] == []
    final_chain = client.get("/api/waybills/AWB-1/chain").json()
    assert len(final_chain["verified_prefix"]) == 4


def test_failed_permit_replace_rolls_back_registry_and_worklist(client):
    from app import config, database, schemas
    from app import permits as permit_svc

    _wb(client)
    client.post(
        "/api/waybills/AWB-1/scans/batch",
        json={"scans": [handover("S1", 1, "2026-11-18T08:00:00Z", "ORIG", "B")]},
    )
    db_path = config.DB_PATH

    good = schemas.PermitReplace(
        intervals=[
            schemas.PermitInterval(
                valid_from=datetime(2026, 11, 18, tzinfo=timezone.utc),
                valid_to=datetime(2026, 11, 19, tzinfo=timezone.utc),
            )
        ]
    )
    conn = database.connect(db_path)
    permit_svc.replace_permits(conn, "B", CAT, good)
    conn.close()

    before_audit = client.get("/api/waybills/AWB-1/permit-audit").json()
    assert [i["missing_party"] for i in before_audit["items"]] == ["ORIG"]

    # Bypass pydantic validation to prove the write transaction itself rolls
    # back when an invalid (inverted) interval reaches the service layer.
    conn = database.connect(db_path)
    raised = False
    try:
        bad = schemas.PermitReplace.model_construct(
            intervals=[
                schemas.PermitInterval.model_construct(
                    valid_from=datetime(2026, 11, 20, tzinfo=timezone.utc),
                    valid_to=datetime(2026, 11, 18, tzinfo=timezone.utc),
                )
            ]
        )
        permit_svc.replace_permits(conn, "B", CAT, bad)
    except Exception:
        raised = True
    finally:
        conn.close()
    assert raised

    listed = client.get(f"/api/custodians/B/permits/{CAT}").json()
    assert len(listed["intervals"]) == 1
    after_audit = client.get("/api/waybills/AWB-1/permit-audit").json()
    assert after_audit["items"] == before_audit["items"]


def test_permit_replace_and_custodian_query_do_not_replay_chains(client, monkeypatch):
    """Permit operations read materialized tables; chain recomputation never runs."""
    from app import permits as permit_svc
    from app import services

    _wb(client, wid="AWB-1")
    _wb(client, wid="AWB-2")
    for wid in ("AWB-1", "AWB-2"):
        client.post(
            f"/api/waybills/{wid}/scans/batch",
            json={"scans": [handover("S1", 1, "2026-11-18T08:00:00Z", "ORIG", "B")]},
        )

    calls: list[str] = []
    original = services.compute_chain

    def spy(conn, waybill_id):
        calls.append(waybill_id)
        return original(conn, waybill_id)

    # services.apply_batch calls compute_chain via its own module namespace.
    monkeypatch.setattr(services, "compute_chain", spy)

    r = client.put(f"/api/custodians/B/permits/{CAT}", json=_permit())
    assert r.status_code == 200
    client.get(f"/api/custodians/B/pending?permit_category={CAT}")
    client.get("/api/custodians/ORIG/pending")
    client.get("/api/waybills/AWB-1/permit-audit")
    assert calls == [], f"chain was replayed {calls}"
