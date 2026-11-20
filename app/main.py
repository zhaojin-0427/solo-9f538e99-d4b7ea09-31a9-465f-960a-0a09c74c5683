"""FastAPI application: dangerous-goods custody chain API."""
from __future__ import annotations

import sqlite3
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import ValidationError

from . import config, permits, services
from .database import connect, init_db
from .schemas import (
    BatchResponse,
    BatchSubmit,
    ChainResponse,
    CustodianPendingResponse,
    PermitAuditResponse,
    PermitListResponse,
    PermitReplace,
    PermitReplaceResponse,
    WaybillCreate,
)


@asynccontextmanager
async def lifespan(app: FastAPI) -> None:
    init_db()
    yield


app = FastAPI(
    title="空运危险品保管链 API",
    description=(
        "运单保管链（扫描交接 / 撤回 / 补报 / 更正）的修订裁决与可信前缀查询。"
        "同 ID 仅最高修订生效；同修订异文整批拒绝；失败保留旧链。"
    ),
    version="1.0.0",
    lifespan=lifespan,
)


def get_conn() -> sqlite3.Connection:  # type: ignore[return]
    conn = connect()
    try:
        yield conn
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# Error mapping: domain errors never leak 500s, and failed batches have
# already been rolled back by the service layer.
# --------------------------------------------------------------------------- #
@app.exception_handler(services.DomainConflict)
@app.exception_handler(permits.DomainConflict)
async def conflict_handler(_: Request, exc: Exception) -> JSONResponse:
    return JSONResponse(status_code=409, content={"error": "conflict", "detail": str(exc)})


@app.exception_handler(services.DomainValidation)
@app.exception_handler(permits.DomainValidation)
async def not_found_handler(_: Request, exc: Exception) -> JSONResponse:
    return JSONResponse(status_code=404, content={"error": "not_found", "detail": str(exc)})


@app.exception_handler(permits.PermitValidationError)
async def permit_validation_handler(_: Request, exc: Exception) -> JSONResponse:
    return JSONResponse(status_code=422, content={"error": "validation_error", "detail": str(exc)})


@app.exception_handler(ValidationError)
async def pydantic_handler(_: Request, exc: ValidationError) -> JSONResponse:
    return JSONResponse(status_code=422, content={"error": "validation_error", "detail": exc.errors()})


# --------------------------------------------------------------------------- #
# Endpoints
# --------------------------------------------------------------------------- #
@app.post("/api/waybills", status_code=201, tags=["waybills"])
def create_waybill(
    body: WaybillCreate, conn: sqlite3.Connection = Depends(get_conn)
) -> dict:
    """创建运单并固定其起始保管方。"""
    return services.create_waybill(conn, body)


@app.get("/api/waybills", tags=["waybills"])
def list_waybills(conn: sqlite3.Connection = Depends(get_conn)) -> dict:
    from .database import read_snapshot

    with read_snapshot(conn):
        rows = conn.execute(
            "SELECT id, origin_party, permit_category, created_at FROM waybills ORDER BY id"
        ).fetchall()
        return {"waybills": [dict(r) for r in rows]}


@app.post(
    "/api/waybills/{waybill_id}/scans/batch",
    response_model=BatchResponse,
    tags=["scans"],
)
def submit_batch(
    waybill_id: str,
    batch: BatchSubmit,
    conn: sqlite3.Connection = Depends(get_conn),
) -> BatchResponse:
    """批量提交交接/撤回。

    * HANDOVER：scan_id、revision、ts_utc（带时区）、giver、receiver
    * WITHDRAWAL：仅 scan_id、revision（必须高于已知最高修订）
    * 同修订异文、过期修订、撤回未知 ID -> 409，整批回滚，旧链保留
    """
    return services.apply_batch(conn, waybill_id, batch)


@app.get(
    "/api/waybills/{waybill_id}/chain",
    response_model=ChainResponse,
    tags=["scans"],
)
def query_chain(
    waybill_id: str, conn: sqlite3.Connection = Depends(get_conn)
) -> ChainResponse:
    """返回可信连续交接前缀、首处断链原因与当前可确认保管方。"""
    return services.compute_chain(conn, waybill_id)


# --------------------------------------------------------------------------- #
# Dangerous-goods permit registry & re-audit worklist
# --------------------------------------------------------------------------- #
@app.put(
    "/api/custodians/{custodian}/permits/{permit_category}",
    response_model=PermitReplaceResponse,
    tags=["permits"],
)
def replace_permits(
    custodian: str,
    permit_category: str,
    body: PermitReplace,
    conn: sqlite3.Connection = Depends(get_conn),
) -> PermitReplaceResponse:
    """按保管方整批替换某许可类别的 UTC 有效期（半开区间 [起点, 终点)）。

    * 区间统一归一化为 UTC；传空列表清除该保管方在该类别下的全部登记。
    * 无需重放任何运单的保管链：只复核该保管方在已物化可信前缀中的交接。
    * 返回因本次替换新增（added）或解除（removed）的待核项；整事务原子提交，
      失败时此前的登记与待核清单原样保留。
    """
    return permits.replace_permits(conn, custodian, permit_category, body)


@app.get(
    "/api/custodians/{custodian}/permits/{permit_category}",
    response_model=PermitListResponse,
    tags=["permits"],
)
def get_permits(
    custodian: str,
    permit_category: str,
    conn: sqlite3.Connection = Depends(get_conn),
) -> PermitListResponse:
    """查询某保管方在某许可类别下当前登记的全部有效期区间。"""
    return permits.list_permits(conn, custodian, permit_category)


@app.get(
    "/api/custodians/{custodian}/pending",
    response_model=CustodianPendingResponse,
    tags=["permits"],
)
def custodian_pending(
    custodian: str,
    permit_category: str | None = None,
    conn: sqlite3.Connection = Depends(get_conn),
) -> CustodianPendingResponse:
    """按保管方查询待核清单（可加 ``permit_category`` 查询参数限定类别）。

    直接读物化待核表，不重放各张运单的保管链。
    """
    return permits.get_custodian_pending(conn, custodian, permit_category)


@app.get(
    "/api/waybills/{waybill_id}/permit-audit",
    response_model=PermitAuditResponse,
    tags=["permits"],
)
def permit_audit(
    waybill_id: str, conn: sqlite3.Connection = Depends(get_conn)
) -> PermitAuditResponse:
    """复查入口：按运单号返回待核项，并自动纳入同一许可类别的全部运单。

    仅核查可信前缀内的交接；缺证交接照常录入和查链，缺证记录按缺证方
    分组（by_missing_party），含运单号、扫描 ID、修订号与交接时刻。
    """
    return permits.audit_waybill(conn, waybill_id)


# --------------------------------------------------------------------------- #
# Minimal console (the interactive /docs remains fully functional too).
# --------------------------------------------------------------------------- #
_STATIC_DIR = Path(__file__).parent / "static"


@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    return FileResponse(_STATIC_DIR / "index.html")
