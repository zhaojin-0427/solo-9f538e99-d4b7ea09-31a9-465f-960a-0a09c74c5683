"""Dangerous-goods permit registry and re-audit worklist behavior."""
from conftest import handover, make_waybill, withdrawal

CAT = "DG-CLASS-3"


def _wb(client, wid="AWB-1", origin="ORIG", category=CAT):
    r = client.post(
        "/api/waybills",
        json={"waybill_id": wid, "origin_party": origin, "permit_category": category},
    )
    assert r.status_code == 201, r.text
    return r.json()


def _put_permits(client, custodian, intervals, category=CAT):
    return client.put(
        f"/api/custodians/{custodian}/permits/{category}",
        json={"intervals": intervals},
    )


def _chain_of(client, wid, scans):
    r = client.post(f"/api/waybills/{wid}/scans/batch", json={"scans": scans})
    assert r.status_code == 200, r.text
    return r.json()


def test_waybill_carries_permit_category_and_defaults(client):
    body = _wb(client)
    assert body["permit_category"] == CAT
    chain = client.get("/api/waybills/AWB-1/chain").json()
    assert chain["permit_category"] == CAT

    # legacy-shaped create without a category still works
    make_waybill(client, wid="AWB-LEGACY", origin="ORIG")
    chain = client.get("/api/waybills/AWB-LEGACY/chain").json()
    assert chain["permit_category"] == "DANGEROUS_GOODS"


def test_no_permits_at_all_every_trusted_handover_is_pending(client):
    _wb(client)
    body = _chain_of(
        client,
        "AWB-1",
        [handover("S1", 1, "2026-11-18T08:00:00Z", "ORIG", "B")],
    )
    pending = body["permit_pending"]
    assert {(p["scan_id"], p["missing_party"]) for p in pending} == {
        ("S1", "ORIG"),
        ("S1", "B"),
    }
    item = next(p for p in pending if p["missing_party"] == "B")
    assert item["waybill_id"] == "AWB-1"
    assert item["permit_category"] == CAT
    assert item["revision"] == 1
    assert item["handover_ts_utc"].startswith("2026-11-18T08:00:00")
    assert item["role"] == "RECEIVER"
    assert next(p for p in pending if p["missing_party"] == "ORIG")["role"] == "GIVER"


def test_valid_permit_at_handover_instant_clears_pending(client):
    _wb(client)
    _chain_of(
        client,
        "AWB-1",
        [handover("S1", 1, "2026-11-18T08:00:00Z", "ORIG", "B")],
    )
    r = _put_permits(
        client,
        "B",
        [{"valid_from": "2026-11-18T00:00:00Z", "valid_to": "2026-11-19T00:00:00Z"}],
    )
    assert r.status_code == 200, r.text
    body = r.json()
    # B's missing record is lifted; ORIG never had a permit and stays pending
    assert [i["scan_id"] for i in body["removed"]] == ["S1"]
    assert body["removed"][0]["missing_party"] == "B"
    assert all(i["missing_party"] != "B" for i in body["added"])
    audit = client.get("/api/waybills/AWB-1/permit-audit").json()
    assert [i["missing_party"] for i in audit["items"]] == ["ORIG"]
    assert list(audit["by_missing_party"].keys()) == ["ORIG"]


def test_half_open_interval_start_included_end_excluded(client):
    _wb(client)
    _chain_of(
        client,
        "AWB-1",
        [
            handover("S1", 1, "2026-11-18T08:00:00Z", "ORIG", "B"),
            handover("S2", 1, "2026-11-18T09:00:00Z", "B", "C"),
        ],
    )
    r = _put_permits(
        client,
        "B",
        [{"valid_from": "2026-11-18T08:00:00Z", "valid_to": "2026-11-18T09:00:00Z"}],
    )
    assert r.status_code == 200
    audit = client.get("/api/waybills/AWB-1/permit-audit").json()
    missing = {(i["scan_id"], i["missing_party"]) for i in audit["items"]}
    # S1@08:00 covered (start inclusive); S2@09:00 not covered (end exclusive)
    assert ("S1", "B") not in missing
    assert ("S2", "B") in missing


def test_unverified_and_simultaneous_handovers_never_enter_worklist(client):
    _wb(client)
    _chain_of(
        client,
        "AWB-1",
        [
            handover("S1", 1, "2026-11-18T08:00:00Z", "ORIG", "B"),
            handover("S2", 1, "2026-11-18T09:00:00Z", "X", "C"),  # GAP -> unverified
            handover("S3", 1, "2026-11-18T09:00:00Z", "C", "D"),  # simultaneous group
        ],
    )
    audit = client.get("/api/waybills/AWB-1/permit-audit").json()
    assert {i["scan_id"] for i in audit["items"]} == {"S1"}


def test_backfill_extends_trusted_prefix_and_pending_follows(client):
    _wb(client)
    # S2 alone: unverified (GAP), nothing pending
    _chain_of(
        client,
        "AWB-1",
        [handover("S2", 1, "2026-11-18T10:00:00Z", "B", "C")],
    )
    assert client.get("/api/waybills/AWB-1/permit-audit").json()["items"] == []

    # backfill S1 -> both become trusted, both land on the worklist
    body = _chain_of(
        client,
        "AWB-1",
        [handover("S1", 1, "2026-11-18T08:00:00Z", "ORIG", "B")],
    )
    assert {i["scan_id"] for i in body["permit_pending"]} == {"S1", "S2"}


def test_correction_and_withdrawal_update_pending_including_later_handovers(client):
    _wb(client)
    _chain_of(
        client,
        "AWB-1",
        [
            handover("S1", 1, "2026-11-18T08:00:00Z", "ORIG", "B"),
            handover("S2", 1, "2026-11-18T09:00:00Z", "B", "C"),
        ],
    )
    # permit for B covering both handovers -> B clean, ORIG and C pending
    _put_permits(
        client,
        "B",
        [{"valid_from": "2026-11-18T08:00:00Z", "valid_to": "2026-11-18T10:00:00Z"}],
    )
    assert {i["missing_party"] for i in client.get("/api/waybills/AWB-1/permit-audit").json()["items"]} == {"ORIG", "C"}

    # withdraw S2: its pending rows (C, and B's receiver role) disappear
    r = client.post(
        "/api/waybills/AWB-1/scans/batch", json={"scans": [withdrawal("S2", 2)]}
    )
    assert r.status_code == 200
    audit = client.get("/api/waybills/AWB-1/permit-audit").json()
    assert {i["scan_id"] for i in audit["items"]} == {"S1"}

    # re-report S2 at a later ts where B's permit no longer covers it
    client.post(
        "/api/waybills/AWB-1/scans/batch",
        json={"scans": [handover("S2", 3, "2026-11-18T11:00:00Z", "B", "C")]},
    )
    audit = client.get("/api/waybills/AWB-1/permit-audit").json()
    missing = {(i["scan_id"], i["missing_party"]) for i in audit["items"]}
    assert ("S2", "B") in missing  # permit expired at 09:00
    assert ("S2", "C") in missing


def test_replacement_reports_added_and_removed_diff(client):
    _wb(client)
    _chain_of(
        client,
        "AWB-1",
        [
            handover("S1", 1, "2026-11-18T08:00:00Z", "ORIG", "B"),
            handover("S2", 1, "2026-11-18T10:00:00Z", "B", "C"),
        ],
    )
    # first registration covers only S1 -> S2(B) added-lifted logic via removal diff
    r = _put_permits(
        client,
        "B",
        [{"valid_from": "2026-11-18T08:00:00Z", "valid_to": "2026-11-18T09:00:00Z"}],
    )
    body = r.json()
    assert [i["scan_id"] for i in body["removed"]] == ["S1"]  # B@S1 lifted
    assert body["added"] == []

    # widen the window to cover S2 as well
    r = _put_permits(
        client,
        "B",
        [{"valid_from": "2026-11-18T08:00:00Z", "valid_to": "2026-11-18T11:00:00Z"}],
    )
    body = r.json()
    assert [i["scan_id"] for i in body["removed"]] == ["S2"]
    assert body["added"] == []

    # shrink again: S2 violation reappears as added
    r = _put_permits(
        client,
        "B",
        [{"valid_from": "2026-11-18T08:00:00Z", "valid_to": "2026-11-18T09:00:00Z"}],
    )
    body = r.json()
    assert [i["scan_id"] for i in body["added"]] == ["S2"]
    assert body["removed"] == []


def test_empty_interval_list_clears_registration(client):
    _wb(client)
    _chain_of(
        client,
        "AWB-1",
        [handover("S1", 1, "2026-11-18T08:00:00Z", "ORIG", "B")],
    )
    _put_permits(
        client,
        "B",
        [{"valid_from": "2026-11-18T00:00:00Z", "valid_to": "2026-11-19T00:00:00Z"}],
    )
    r = _put_permits(client, "B", [])
    assert r.status_code == 200
    assert [i["scan_id"] for i in r.json()["added"]] == ["S1"]
    assert client.get(f"/api/custodians/B/permits/{CAT}").json()["intervals"] == []
    audit = client.get("/api/waybills/AWB-1/permit-audit").json()
    assert {i["missing_party"] for i in audit["items"]} == {"ORIG", "B"}


def test_replacement_is_whole_batch_not_merge(client):
    _wb(client)
    _chain_of(
        client,
        "AWB-1",
        [handover("S1", 1, "2026-11-18T08:00:00Z", "ORIG", "B")],
    )
    _put_permits(
        client,
        "B",
        [{"valid_from": "2026-11-18T00:00:00Z", "valid_to": "2026-11-18T06:00:00Z"}],
    )
    # a second, disjoint window must REPLACE the first one, not extend it
    r = _put_permits(
        client,
        "B",
        [{"valid_from": "2026-11-20T00:00:00Z", "valid_to": "2026-11-21T00:00:00Z"}],
    )
    assert len(r.json()["intervals"]) == 1
    listed = client.get(f"/api/custodians/B/permits/{CAT}").json()["intervals"]
    assert len(listed) == 1
    assert listed[0]["valid_from"].startswith("2026-11-20")
    # S1@11-18 is uncovered again
    audit = client.get("/api/waybills/AWB-1/permit-audit").json()
    assert ("S1", "B") in {(i["scan_id"], i["missing_party"]) for i in audit["items"]}


def test_multiple_intervals_and_timezone_normalization(client):
    _wb(client)
    _chain_of(
        client,
        "AWB-1",
        [
            handover("S1", 1, "2026-11-18T08:00:00Z", "ORIG", "B"),
            handover("S2", 1, "2026-11-18T12:00:00Z", "B", "C"),
        ],
    )
    # 16:00+08:00 == 08:00Z; second window covers 12:00Z
    r = _put_permits(
        client,
        "B",
        [
            {"valid_from": "2026-11-18T16:00:00+08:00", "valid_to": "2026-11-18T17:00:00+08:00"},
            {"valid_from": "2026-11-18T11:30:00Z", "valid_to": "2026-11-18T12:30:00Z"},
        ],
    )
    assert r.status_code == 200
    stored = client.get(f"/api/custodians/B/permits/{CAT}").json()["intervals"]
    assert stored[0]["valid_from"].startswith("2026-11-18T08:00:00")
    audit = client.get("/api/waybills/AWB-1/permit-audit").json()
    assert all(i["missing_party"] != "B" for i in audit["items"])


def test_invalid_interval_shapes_rejected(client):
    # naive timestamps
    r = _put_permits(
        client, "B", [{"valid_from": "2026-11-18T08:00:00", "valid_to": "2026-11-18T09:00:00Z"}]
    )
    assert r.status_code == 422
    # empty/zero-length half-open interval
    r = _put_permits(
        client, "B", [{"valid_from": "2026-11-18T09:00:00Z", "valid_to": "2026-11-18T09:00:00Z"}]
    )
    assert r.status_code == 422
    # inverted interval
    r = _put_permits(
        client, "B", [{"valid_from": "2026-11-18T10:00:00Z", "valid_to": "2026-11-18T09:00:00Z"}]
    )
    assert r.status_code == 422
    # overlapping intervals
    r = _put_permits(
        client,
        "B",
        [
            {"valid_from": "2026-11-18T08:00:00Z", "valid_to": "2026-11-18T10:00:00Z"},
            {"valid_from": "2026-11-18T09:00:00Z", "valid_to": "2026-11-18T11:00:00Z"},
        ],
    )
    assert r.status_code == 422


def test_failed_validation_keeps_old_registry_and_worklist(client):
    _wb(client)
    _chain_of(
        client,
        "AWB-1",
        [handover("S1", 1, "2026-11-18T08:00:00Z", "ORIG", "B")],
    )
    good = [{"valid_from": "2026-11-18T00:00:00Z", "valid_to": "2026-11-19T00:00:00Z"}]
    assert _put_permits(client, "B", good).status_code == 200
    before = client.get("/api/waybills/AWB-1/permit-audit").json()

    r = _put_permits(
        client,
        "B",
        [{"valid_from": "2026-11-18T10:00:00Z", "valid_to": "2026-11-18T09:00:00Z"}],
    )
    assert r.status_code == 422
    # old registration intact
    assert client.get(f"/api/custodians/B/permits/{CAT}").json()["intervals"] == [
        {
            "valid_from": "2026-11-18T00:00:00Z",
            "valid_to": "2026-11-19T00:00:00Z",
        }
    ]
    after = client.get("/api/waybills/AWB-1/permit-audit").json()
    assert after["items"] == before["items"]


def test_same_category_waybills_share_audit_scope(client):
    _wb(client, wid="AWB-1", origin="ORIG", category=CAT)
    _wb(client, wid="AWB-2", origin="ORIG", category=CAT)
    _wb(client, wid="AWB-3", origin="ORIG", category="OTHER-CLASS")
    _chain_of(
        client,
        "AWB-1",
        [handover("S1", 1, "2026-11-18T08:00:00Z", "ORIG", "B")],
    )
    _chain_of(
        client,
        "AWB-2",
        [handover("T1", 1, "2026-11-18T08:30:00Z", "ORIG", "B")],
    )
    _chain_of(
        client,
        "AWB-3",
        [handover("U1", 1, "2026-11-18T08:00:00Z", "ORIG", "B")],
    )
    # permit for B lifts B's rows in both same-category waybills, not AWB-3
    r = _put_permits(
        client,
        "B",
        [{"valid_from": "2026-11-18T00:00:00Z", "valid_to": "2026-11-19T00:00:00Z"}],
    )
    lifted = {i["waybill_id"] for i in r.json()["removed"]}
    assert lifted == {"AWB-1", "AWB-2"}

    # custodian query sees both categories; category filter narrows it
    r = client.get("/api/custodians/ORIG/pending")
    assert {i["waybill_id"] for i in r.json()["items"]} == {"AWB-1", "AWB-2", "AWB-3"}
    r = client.get(f"/api/custodians/ORIG/pending?permit_category={CAT}")
    assert {i["waybill_id"] for i in r.json()["items"]} == {"AWB-1", "AWB-2"}

    # audit endpoint groups the whole category's findings via missing parties
    audit = client.get("/api/waybills/AWB-1/permit-audit").json()
    assert audit["permit_category"] == CAT
    assert {i["waybill_id"] for i in audit["items"]} == {"AWB-1", "AWB-2"}


def test_audit_unknown_waybill_404(client):
    r = client.get("/api/waybills/NOPE/permit-audit")
    assert r.status_code == 404


def test_self_handover_merges_into_both_role(client):
    _wb(client, origin="B")
    _chain_of(
        client,
        "AWB-1",
        [handover("S1", 1, "2026-11-18T08:00:00Z", "B", "B")],
    )
    audit = client.get("/api/waybills/AWB-1/permit-audit").json()
    assert len(audit["items"]) == 1
    assert audit["items"][0]["missing_party"] == "B"
    assert audit["items"][0]["role"] == "BOTH"
    # a single permit row heals the merged record
    _put_permits(
        client,
        "B",
        [{"valid_from": "2026-11-18T00:00:00Z", "valid_to": "2026-11-19T00:00:00Z"}],
    )
    assert client.get("/api/waybills/AWB-1/permit-audit").json()["items"] == []


def test_pending_survives_restart(client, monkeypatch):
    from app import config
    import importlib

    _wb(client)
    _chain_of(
        client,
        "AWB-1",
        [handover("S1", 1, "2026-11-18T08:00:00Z", "ORIG", "B")],
    )
    _put_permits(
        client,
        "B",
        [{"valid_from": "2026-11-18T08:00:00Z", "valid_to": "2026-11-18T09:00:00Z"}],
    )
    db_path = config.DB_PATH

    importlib.reload(importlib.import_module("app.main"))
    from fastapi.testclient import TestClient
    from app import main

    with TestClient(main.app) as new_client:
        audit = new_client.get("/api/waybills/AWB-1/permit-audit").json()
        assert [i["missing_party"] for i in audit["items"]] == ["ORIG"]
        listed = new_client.get(f"/api/custodians/B/permits/{CAT}").json()
        assert len(listed["intervals"]) == 1
    assert db_path.exists()


def test_legacy_sqlite_volume_migrates_with_permit_tables(client, monkeypatch, tmp_path):
    """A volume created by the pre-permit schema keeps its data and gains new tables."""
    import sqlite3

    from app import config

    legacy = tmp_path / "legacy.db"
    raw = sqlite3.connect(str(legacy))
    raw.executescript(
        """
        CREATE TABLE waybills (id TEXT PRIMARY KEY, origin_party TEXT NOT NULL, created_at TEXT NOT NULL);
        CREATE TABLE scans (
            waybill_id TEXT NOT NULL, scan_id TEXT NOT NULL, revision INTEGER NOT NULL,
            event_type TEXT NOT NULL, ts_utc TEXT, giver TEXT, receiver TEXT,
            payload_hash TEXT NOT NULL, created_at TEXT NOT NULL,
            PRIMARY KEY (waybill_id, scan_id, revision));
        INSERT INTO waybills VALUES ('OLD-AWB', 'ORIG', '2026-11-17T00:00:00Z');
        INSERT INTO scans VALUES ('OLD-AWB','S1',1,'HANDOVER',
            '2026-11-18T08:00:00.000000Z','ORIG','B','h','2026-11-17T00:00:00Z');
        """
    )
    raw.commit()
    raw.close()

    monkeypatch.setenv("APP_DB_PATH", str(legacy))
    import importlib
    from app import config as cfg, database, main

    importlib.reload(cfg)
    importlib.reload(database)
    importlib.reload(main)
    database.init_db()

    from fastapi.testclient import TestClient

    with TestClient(main.app) as c2:
        chain = c2.get("/api/waybills/OLD-AWB/chain").json()
        assert chain["permit_category"] == "DANGEROUS_GOODS"
        assert [s["scan_id"] for s in chain["verified_prefix"]] == ["S1"]
        audit = c2.get("/api/waybills/OLD-AWB/permit-audit").json()
        assert {i["missing_party"] for i in audit["items"]} == {"ORIG", "B"}
        # new registrations work on the migrated volume
        r = c2.put(
            "/api/custodians/B/permits/DANGEROUS_GOODS",
            json={"intervals": [
                {"valid_from": "2026-11-18T00:00:00Z", "valid_to": "2026-11-19T00:00:00Z"}
            ]},
        )
        assert r.status_code == 200
        audit = c2.get("/api/waybills/OLD-AWB/permit-audit").json()
        assert [i["missing_party"] for i in audit["items"]] == ["ORIG"]
