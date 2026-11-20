"""End-to-end HTTP smoke test against a running uvicorn instance."""
import json
import os
import sys
import urllib.request

BASE = "http://127.0.0.1:" + os.environ.get("SMOKE_PORT", "8011")


def call(method, path, body=None, raw=False):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        BASE + path, data=data, method=method,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            payload = resp.read()
            return resp.status, payload if raw else json.loads(payload)
    except urllib.error.HTTPError as e:
        payload = e.read()
        try:
            return e.code, json.loads(payload)
        except json.JSONDecodeError:
            return e.code, payload


def check(label, cond, extra=""):
    print(("PASS" if cond else "FAIL"), label, extra)
    if not cond:
        sys.exit(1)


st, body = call("POST", "/api/waybills", {"waybill_id": "AWB-SMOKE", "origin_party": "ORIG"})
check("create waybill", st == 201, body)

st, body = call("POST", "/api/waybills/AWB-SMOKE/scans/batch", {"scans": [
    {"scan_id": "S1", "revision": 1, "ts_utc": "2026-11-18T08:00:00Z", "giver": "ORIG", "receiver": "B"},
    {"scan_id": "S2", "revision": 1, "ts_utc": "2026-11-18T09:00:00Z", "giver": "B", "receiver": "C"},
    {"scan_id": "S3", "revision": 1, "ts_utc": "2026-11-18T10:00:00Z", "giver": "X", "receiver": "Y"},
]})
check("batch accepted", st == 200, st)
check("GAP detected", body["chain"]["break_reason"] == "GAP")
check("custodian stays at C", body["chain"]["current_custodian"] == "C")
check("S3 unverified", [s["scan_id"] for s in body["chain"]["unverified_scans"]] == ["S3"])

# same revision divergence -> 409, old chain preserved
st, body = call("POST", "/api/waybills/AWB-SMOKE/scans/batch", {"scans": [
    {"scan_id": "S3", "revision": 1, "ts_utc": "2026-11-18T10:00:00Z", "giver": "X", "receiver": "TAMPERED"},
]})
check("same-revision divergence -> 409", st == 409, body)

st, body = call("POST", "/api/waybills/AWB-SMOKE/scans/batch", {"scans": [
    {"scan_id": "S3", "revision": 2, "event_type": "WITHDRAWAL"},
]})
check("withdrawal accepted", st == 200, body)
st, body = call("POST", "/api/waybills/AWB-SMOKE/scans/batch", {"scans": [
    {"scan_id": "S3", "revision": 3, "ts_utc": "2026-11-18T10:00:00Z", "giver": "C", "receiver": "D"},
]})
check("correction accepted", st == 200, st)
check("chain healed", body["chain"]["chain_intact"] is True)
check("custodian now D", body["chain"]["current_custodian"] == "D")

# simultaneous timestamps
st, body = call("POST", "/api/waybills/AWB-SMOKE/scans/batch", {"scans": [
    {"scan_id": "S4", "revision": 1, "ts_utc": "2026-11-18T11:00:00Z", "giver": "D", "receiver": "E"},
    {"scan_id": "S5", "revision": 1, "ts_utc": "2026-11-18T11:00:00Z", "giver": "E", "receiver": "F"},
]})
check("simultaneous flagged", body["chain"]["break_reason"] == "SIMULTANEOUS")

st, body = call("GET", "/api/waybills/AWB-SMOKE/chain")
check("GET chain", st == 200 and body["waybill_id"] == "AWB-SMOKE")

st, body = call("GET", "/api/waybills")
check("list waybills", st == 200 and any(w["id"] == "AWB-SMOKE" for w in body["waybills"]))

st, body = call("GET", "/docs", raw=True)
check("swagger docs reachable", st == 200 and b"swagger" in body.lower())
st, body = call("GET", "/openapi.json")
check("openapi schema reachable", st == 200 and "paths" in body)
st, body = call("GET", "/", raw=True)
check("console page reachable", st == 200 and b"<!DOCTYPE html>" in body)

# validation: naive datetime
st, body = call("POST", "/api/waybills/AWB-SMOKE/scans/batch", {"scans": [
    {"scan_id": "S9", "revision": 1, "ts_utc": "2026-11-18T12:00:00", "giver": "D", "receiver": "E"},
]})
check("naive timestamp -> 422", st == 422)

# --------------------------------------------------------------------------- #
# Dangerous-goods permit registry & re-audit worklist
# --------------------------------------------------------------------------- #
CAT = "DG-SMOKE"
st, body = call("POST", "/api/waybills", {
    "waybill_id": "AWB-DG", "origin_party": "ORIG", "permit_category": CAT})
check("create categorized waybill", st == 201 and body["permit_category"] == CAT, body)

st, body = call("POST", "/api/waybills/AWB-DG/scans/batch", {"scans": [
    {"scan_id": "P1", "revision": 1, "ts_utc": "2026-11-18T08:00:00Z", "giver": "ORIG", "receiver": "B"},
    {"scan_id": "P2", "revision": 1, "ts_utc": "2026-11-18T10:00:00Z", "giver": "B", "receiver": "C"},
]})
check("dg batch accepted", st == 200, st)
# no permits registered -> every trusted handover party is pending
pending = {(i["scan_id"], i["missing_party"]) for i in body["permit_pending"]}
check("all trusted parties pending without permits",
      pending == {("P1", "ORIG"), ("P1", "B"), ("P2", "B"), ("P2", "C")}, pending)

# half-open permit covering P1@08:00 but not P2@10:00
st, body = call("PUT", f"/api/custodians/B/permits/{CAT}", {"intervals": [
    {"valid_from": "2026-11-18T08:00:00Z", "valid_to": "2026-11-18T09:00:00Z"}]})
check("permit replacement returns diff",
      st == 200 and [i["scan_id"] for i in body["removed"]] == ["P1"], body)

st, body = call("GET", "/api/waybills/AWB-DG/permit-audit")
missing = {(i["scan_id"], i["missing_party"]) for i in body["items"]}
check("audit scopes category and half-open boundary",
      ("P1", "B") not in missing and ("P2", "B") in missing
      and ("P1", "ORIG") in missing and body["permit_category"] == CAT, missing)
check("audit groups by missing party",
      set(body["by_missing_party"].keys()) == {"ORIG", "B", "C"}, body["by_missing_party"])

st, body = call("PUT", f"/api/custodians/B/permits/{CAT}", {"intervals": []})
# B was only still missing on P2; clearing the registration re-adds that one
check("empty list clears registration", st == 200 and len(body["added"]) == 1, body)
st, body = call("GET", f"/api/custodians/B/permits/{CAT}")
check("cleared registry is empty", st == 200 and body["intervals"] == [], body)

st, body = call("PUT", f"/api/custodians/B/permits/{CAT}", {"intervals": [
    {"valid_from": "2026-11-18T10:00:00Z", "valid_to": "2026-11-18T09:00:00Z"}]})
check("inverted interval -> 422", st == 422, body)

st, body = call("GET", f"/api/custodians/ORIG/pending?permit_category={CAT}")
check("custodian pending query", st == 200
      and {i["scan_id"] for i in body["items"]} == {"P1"}, body)

st, body = call("GET", "/api/waybills/NOPE/permit-audit")
check("audit unknown waybill -> 404", st == 404)

print("ALL SMOKE CHECKS PASSED")
