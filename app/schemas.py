"""Pydantic request/response models and payload canonicalization."""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

# Waybills created without an explicit permit category fall back to this one,
# so legacy clients (and pre-permit SQLite volumes) keep working unchanged.
DEFAULT_PERMIT_CATEGORY = "DANGEROUS_GOODS"


# --------------------------------------------------------------------------- #
# Requests
# --------------------------------------------------------------------------- #
class WaybillCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    waybill_id: str = Field(min_length=1, description="运单号，全局唯一")
    origin_party: str = Field(min_length=1, description="固定起始保管方")
    permit_category: str = Field(
        default=DEFAULT_PERMIT_CATEGORY,
        min_length=1,
        description="许可类别；核查时同一类别下的所有运单一并纳入",
    )

    @field_validator("waybill_id", "origin_party", "permit_category")
    @classmethod
    def _strip_nonempty(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("must not be empty")
        return v


class ScanEntry(BaseModel):
    """One scan inside a batch submission.

    ``event_type`` defaults to HANDOVER so the common case only carries
    scan_id / ts_utc / giver / receiver / revision.
    """

    model_config = ConfigDict(extra="forbid")

    scan_id: str = Field(min_length=1)
    revision: int = Field(gt=0, description="正整数修订号；同 ID 仅最高修订生效")
    event_type: Literal["HANDOVER", "WITHDRAWAL"] = "HANDOVER"
    ts_utc: datetime | None = Field(
        default=None, description="ISO-8601，必须带时区偏移（推荐 UTC，如 2026-11-18T08:30:00Z）"
    )
    giver: str | None = None
    receiver: str | None = None

    @field_validator("scan_id")
    @classmethod
    def _strip_scan_id(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("must not be empty")
        return v

    @field_validator("giver", "receiver")
    @classmethod
    def _strip_parties(cls, v: str | None) -> str | None:
        if v is None:
            return None
        v = v.strip()
        return v or None

    @field_validator("ts_utc")
    @classmethod
    def _require_timezone(cls, v: datetime | None) -> datetime | None:
        if v is not None and v.tzinfo is None:
            raise ValueError("ts_utc must carry an explicit timezone offset, e.g. ...Z or +08:00")
        return v

    @model_validator(mode="after")
    def _check_shape(self) -> "ScanEntry":
        if self.event_type == "HANDOVER":
            missing = [
                name
                for name, val in (
                    ("ts_utc", self.ts_utc),
                    ("giver", self.giver),
                    ("receiver", self.receiver),
                )
                if val is None
            ]
            if missing:
                raise ValueError(f"HANDOVER entry requires fields: {', '.join(missing)}")
        else:  # WITHDRAWAL: only id + higher revision are accepted
            extras = [
                name
                for name, val in (
                    ("ts_utc", self.ts_utc),
                    ("giver", self.giver),
                    ("receiver", self.receiver),
                )
                if val is not None
            ]
            if extras:
                raise ValueError(
                    f"WITHDRAWAL entry must only carry scan_id and revision; got extra: {', '.join(extras)}"
                )
        return self


class BatchSubmit(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scans: list[ScanEntry] = Field(min_length=1)

    @model_validator(mode="after")
    def _check_batch_duplicates(self) -> "BatchSubmit":
        # Same revision appearing twice in one batch with different text is a
        # whole-batch rejection; identical text is treated as idempotent.
        seen: dict[tuple[str, int], str] = {}
        for entry in self.scans:
            key = (entry.scan_id, entry.revision)
            h = entry_payload_hash(entry)
            if key in seen and seen[key] != h:
                raise ValueError(
                    f"scan_id={key[0]} revision={key[1]} appears multiple times "
                    "with conflicting content in the same batch"
                )
            seen[key] = h
        return self


# --------------------------------------------------------------------------- #
# Responses
# --------------------------------------------------------------------------- #
class BreakReason(str, Enum):
    none = "NONE"
    gap = "GAP"  # 保管方不承接：giver != 前一 receiver
    simultaneous = "SIMULTANEOUS"  # 同一时刻多笔交接，顺序不可判定


class ScanView(BaseModel):
    scan_id: str
    revision: int
    event_type: Literal["HANDOVER", "WITHDRAWAL"]
    ts_utc: datetime | None
    giver: str | None
    receiver: str | None
    verified: bool
    unverified_reason: BreakReason | None = None


class ChainResponse(BaseModel):
    waybill_id: str
    origin_party: str
    permit_category: str = DEFAULT_PERMIT_CATEGORY
    current_custodian: str
    verified_prefix: list[ScanView]
    unverified_scans: list[ScanView]
    chain_intact: bool
    break_reason: BreakReason
    break_detail: str | None = None
    break_scan_ids: list[str] = Field(default_factory=list)


class BatchResponse(BaseModel):
    waybill_id: str
    applied: int
    idempotent_duplicates: int
    withdrawn_scan_ids: list[str]
    chain: ChainResponse
    # 本次提交后该运单仍在待核清单中的缺证交接（随可信前缀同步更新）
    permit_pending: list["PendingItem"] = Field(default_factory=list)


# --------------------------------------------------------------------------- #
# Permit registry & re-audit (待核清单)
# --------------------------------------------------------------------------- #
class PermitInterval(BaseModel):
    """一个半开 UTC 有效期 [valid_from, valid_to)。"""

    model_config = ConfigDict(extra="forbid")

    valid_from: datetime = Field(description="有效期起点（含），ISO-8601 且必须带时区")
    valid_to: datetime = Field(description="有效期终点（不含），ISO-8601 且必须带时区")

    @field_validator("valid_from", "valid_to")
    @classmethod
    def _require_timezone(cls, v: datetime) -> datetime:
        if v.tzinfo is None:
            raise ValueError("permit timestamps must carry an explicit timezone offset, e.g. ...Z or +08:00")
        return v

    @model_validator(mode="after")
    def _check_half_open(self) -> "PermitInterval":
        if self.valid_to <= self.valid_from:
            raise ValueError("permit interval must be half-open [valid_from, valid_to) with valid_to > valid_from")
        return self


class PermitReplace(BaseModel):
    model_config = ConfigDict(extra="forbid")

    intervals: list[PermitInterval] = Field(
        description="整批替换后的 UTC 有效期区间列表；传空列表清除该保管方在该类别下的全部登记"
    )

    @model_validator(mode="after")
    def _check_no_overlap(self) -> "PermitReplace":
        spans = sorted(
            (iv.valid_from.astimezone(timezone.utc), iv.valid_to.astimezone(timezone.utc))
            for iv in self.intervals
        )
        for (f1, t1), (f2, t2) in zip(spans, spans[1:]):
            if f2 < t1:
                raise ValueError(
                    f"permit intervals overlap: [{f1.isoformat()}, {t1.isoformat()}) "
                    f"vs [{f2.isoformat()}, {t2.isoformat()})"
                )
        return self


class PendingItem(BaseModel):
    """一条缺证交接的待核记录。"""

    waybill_id: str
    permit_category: str
    scan_id: str
    revision: int
    handover_ts_utc: datetime
    missing_party: str
    role: Literal["GIVER", "RECEIVER", "BOTH"] = Field(
        description="缺证方在该笔交接中的角色；BOTH 表示交出方与接收方为同一方且均缺证"
    )


class PermitPendingChange(BaseModel):
    """一次许可替换在待核清单上产生的增量。"""

    added: list[PendingItem] = Field(default_factory=list)
    removed: list[PendingItem] = Field(default_factory=list)


class PermitReplaceResponse(BaseModel):
    custodian: str
    permit_category: str
    intervals: list[PermitInterval]
    added: list[PendingItem]
    removed: list[PendingItem]


class PermitListResponse(BaseModel):
    custodian: str
    permit_category: str
    intervals: list[PermitInterval]


class CustodianPendingResponse(BaseModel):
    custodian: str
    permit_category: str | None = None
    items: list[PendingItem]


class PermitAuditResponse(BaseModel):
    waybill_id: str
    permit_category: str
    items: list[PendingItem]
    # 按缺证方分组的视图，便于复查时直接定位保管方
    by_missing_party: dict[str, list[PendingItem]]


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def to_utc_iso(dt: datetime) -> str:
    """Normalize any timezone-aware datetime to a fixed-width UTC ISO string."""
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def canonical_payload(entry: ScanEntry) -> dict:
    """Deterministic representation used to detect same-revision divergence."""
    if entry.event_type == "WITHDRAWAL":
        return {"event_type": "WITHDRAWAL"}
    return {
        "event_type": "HANDOVER",
        "ts_utc": to_utc_iso(entry.ts_utc),  # type: ignore[arg-type]
        "giver": entry.giver,
        "receiver": entry.receiver,
    }


def payload_hash_of(payload: dict) -> str:
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


def entry_payload_hash(entry: ScanEntry) -> str:
    return payload_hash_of(canonical_payload(entry))
