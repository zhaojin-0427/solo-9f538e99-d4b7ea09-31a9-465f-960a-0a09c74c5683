# 空运危险品保管链 API

面向断网后乱序补传场景的保管链（chain of custody）服务。数据员可以据此判断：
在某个时刻之前，哪些交接仍然可信、断链发生在哪里、当前可确认的保管方是谁；
复查危险品交接时，还能找出**签收当时缺少有效许可记录的保管方**并维护待核清单。

技术栈：**FastAPI + SQLite（WAL 模式）+ Uvicorn**，可通过 `docker compose` 一键启动。

## 核心规则

1. **固定起始保管方**：创建运单时登记 `origin_party`，初始可确认保管方即该方。
2. **批量提交**：每笔扫描携带 `scan_id`、正整数 `revision`、UTC 时刻 `ts_utc`、
   交出方 `giver`、接收方 `receiver`。时刻必须带时区偏移（如 `...Z`、`+08:00`），统一归一化为 UTC 存储。
3. **撤回**：只提交 `scan_id` 与更高的 `revision`，`event_type=WITHDRAWAL`，
   不得携带时刻或当事方；撤回只能针对已存在的扫描 ID。
4. **修订裁决**：
   - 同一 `scan_id` 仅采用**最高修订号**的版本；更低修订号提交返回 `409 stale`。
   - 同修订号、异文（时刻/交出方/接收方/类型不同）**整批拒绝**（`409`），已提交数据原样保留。
   - 同修订号、同文重复提交幂等，不计入写入数。
5. **链核对**（按 UTC 时刻升序，乱序补传自动归位）：
   - 交接的 `giver` 必须等于当前保管方（首笔对照起始保管方，其后对照上一笔的 `receiver`），
     不匹配即为 **GAP 断链**。
   - **同一时刻存在多笔交接时不猜测顺序**：该时刻整组标记为 **SIMULTANEOUS** 断链，组内全部未证实。
   - 断链之后的所有扫描一律 `verified=false`，直到补报、更正或撤回补齐缺口；
     重算后断链位置可以前移、后移或消失。
6. **原子性与一致性**：
   - 每个批次在单个 `BEGIN IMMEDIATE` 事务内完成，任何冲突整批回滚，**失败保留旧链**。
   - 查询只读已提交快照（WAL），交错的扫描提交与查询永远看不到半写状态或混合修订。
   - 数据持久化在 SQLite 文件中，进程重启后状态不变。
7. **危险品许可核查（待核清单）**：
   - 每张运单归属一个 `permit_category`（许可类别，创建时可选，默认 `DANGEROUS_GOODS`）。
     在 `/docs` 或复查接口输入运单号，**同一许可类别的所有运单一并纳入核查**。
   - 许可按「保管方 + 许可类别」登记为一组 UTC 半开有效期 `[valid_from, valid_to)`
     （含起点、不含终点）；**整批替换**，传空列表即清除该保管方在该类别下的全部登记。
   - **只核查可信前缀内的交接**：未证实 / 断链 / 同时刻交接不进待核清单；
     缺证交接**照常录入和查链**，不影响保管链本身。
   - 一笔交接的交出方或接收方在交接时刻没有有效许可记录，即按**缺证方**列入待核清单，
     记录运单号、扫描 ID、修订号、交接时刻（UTC）与缺证角色（`GIVER` / `RECEIVER` / `BOTH`）。
   - 补报、更正或撤回改变可信前缀时，该运单的待核清单随扫描在**同一事务**内更新，
     包括未直接修改、但可信状态随之改变的后续交接。
   - 替换许可后，当次响应直接给出因这次替换**新增（`added`）/ 解除（`removed`）**的待核项。
   - 替换许可与按保管方查询清单都基于物化表（`trusted_handovers` / `permit_pending`），
     **无需重放各张运单的保管链**。
   - 交错提交时，清单依据同一已提交状态的扫描与许可裁决（单事务 + WAL 快照）；
     操作失败回滚，**此前的登记与清单原样保留**。
   - 沿用旧版 SQLite 卷可直接启动：启动时自动补列并一次性回填物化表，重启后仍可查询。

## 启动方式

### Docker Compose（推荐）

```bash
docker compose up --build
# 服务： http://localhost:8000
# 文档： http://localhost:8000/docs
# 控制台：http://localhost:8000/
```

数据库保存在命名卷 `chain-data`（容器内 `/data/chain.db`），容器重启不丢数据。

### 本地运行

```bash
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8000
# 可选：APP_DB_PATH=/custom/path/chain.db
```

## API 一览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/waybills` | 创建运单并固定起始保管方（可带 `permit_category`） |
| GET | `/api/waybills` | 列出全部运单 |
| POST | `/api/waybills/{waybill_id}/scans/batch` | 批量提交交接 / 更正 / 撤回 |
| GET | `/api/waybills/{waybill_id}/chain` | 查询可信前缀、断链原因、当前保管方 |
| PUT | `/api/custodians/{custodian}/permits/{permit_category}` | 整批替换保管方许可有效期（空列表清除），返回待核增减 |
| GET | `/api/custodians/{custodian}/permits/{permit_category}` | 查询保管方当前许可登记 |
| GET | `/api/custodians/{custodian}/pending` | 按保管方查询待核清单（可用 `?permit_category=` 限定类别） |
| GET | `/api/waybills/{waybill_id}/permit-audit` | 复查入口：纳入同类别全部运单，按缺证方分组 |
| GET | `/docs` | Swagger UI，可直接录入与查询 |

### 交接批次示例

```json
{
  "scans": [
    {"scan_id": "S1", "revision": 1, "ts_utc": "2026-11-18T08:00:00Z",
     "giver": "HKG-WAREHOUSE", "receiver": "CX-CARGO"},
    {"scan_id": "S2", "revision": 1, "ts_utc": "2026-11-18T10:30:00Z",
     "giver": "CX-CARGO", "receiver": "PVG-WAREHOUSE"}
  ]
}
```

### 撤回示例（修订号必须更高）

```json
{"scans": [{"scan_id": "S2", "revision": 2, "event_type": "WITHDRAWAL"}]}
```

### 查询响应示例（发生断链时）

```json
{
  "waybill_id": "AWB-001",
  "origin_party": "HKG-WAREHOUSE",
  "current_custodian": "CX-CARGO",
  "verified_prefix": [ {"scan_id": "S1", "verified": true, "...": "..."} ],
  "unverified_scans": [ {"scan_id": "S3", "verified": false,
                         "unverified_reason": "GAP"} ],
  "chain_intact": false,
  "break_reason": "GAP",
  "break_detail": "giver 'X' does not hold custody; expected 'CX-CARGO' ...",
  "break_scan_ids": ["S3"]
}
```

`break_reason` 取值：`NONE` / `GAP`（保管方不承接）/ `SIMULTANEOUS`（同时刻多笔，顺序不可判定）。
`current_custodian` 只沿已证实交接推进，因此断链后仍可信地停留在最后一个已证实接收方。

### 许可登记与复查示例

```bash
# 整批替换保管方 CX-CARGO 在 DG-CLASS-3 类别下的 UTC 有效期（半开区间）
curl -X PUT http://localhost:8000/api/custodians/CX-CARGO/permits/DG-CLASS-3 \
  -H 'Content-Type: application/json' \
  -d '{"intervals": [
    {"valid_from": "2026-11-18T00:00:00Z", "valid_to": "2026-11-18T12:00:00Z"},
    {"valid_from": "2026-11-19T00:00:00Z", "valid_to": "2026-11-20T00:00:00Z"}
  ]}'
# -> {"custodian": "CX-CARGO", "permit_category": "DG-CLASS-3",
#     "intervals": [...], "added": [...], "removed": [...]}

# 清除登记（空列表整批替换）
curl -X PUT http://localhost:8000/api/custodians/CX-CARGO/permits/DG-CLASS-3 \
  -H 'Content-Type: application/json' -d '{"intervals": []}'

# 输入运单号复查：自动纳入同一许可类别的全部运单，按缺证方分组
curl http://localhost:8000/api/waybills/AWB-001/permit-audit
```

待核项结构：

```json
{
  "waybill_id": "AWB-001",
  "permit_category": "DG-CLASS-3",
  "items": [
    {"waybill_id": "AWB-001", "permit_category": "DG-CLASS-3", "scan_id": "S2",
     "revision": 1, "handover_ts_utc": "2026-11-18T10:30:00Z",
     "missing_party": "PVG-WAREHOUSE", "role": "RECEIVER"}
  ],
  "by_missing_party": {"PVG-WAREHOUSE": [ "...同上..." ]}
}
```

## 测试

```bash
pip install pytest httpx
pytest -q
```

共 48 个用例，覆盖：乱序补传归位、时区归一化、最高修订裁决、同修订异文整批回滚、
撤回与补报修复断链、GAP/SIMULTANEOUS 检测、输入校验、真实线程下的写串行化、
交错读不到混合修订、进程重启持久化；以及许可半开区间边界、整批替换与清除、
待核清单随可信前缀（补报/更正/撤回）联动、同类别运单纳入复查、按保管方查询、
缺证角色（含自交接 BOTH）、旧 SQLite 卷迁移回填、许可操作不重放保管链、
交错提交快照一致性与失败回滚保留旧登记和清单。

真实 HTTP 端到端冒烟（先启动服务，再执行）：

```bash
uvicorn app.main:app --host 127.0.0.1 --port 8011 &
SMOKE_PORT=8011 python tests/smoke_http.py
```
