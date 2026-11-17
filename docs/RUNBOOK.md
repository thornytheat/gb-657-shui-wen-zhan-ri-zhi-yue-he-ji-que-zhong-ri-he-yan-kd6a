# 运行手册 hydrocalendar

## 1 启动

```bash
cd projects/gb-657
docker compose up -d --build
curl -sS http://localhost:18216/api/health
```

页面：<http://localhost:18216/>。首次打开没有批次，选样例点「开始处理」即可跑通。

容器内端口 8000，宿主 18216；SQLite 落在命名卷 `hydrocalendar-data` 的 `/data/hydrocalendar.sqlite`。

## 2 主流程

1. 总览页选 `readings_dirty.csv`（脏样例，13 类问题），点「开始处理」。
2. 阶段卡显示七段「进 / 出 / 隔离」；任一阶段失败可点该卡的「重试」。
3. 读数诊断页看逐行诊断与隔离区，点「隔离区 CSV」下载证据。
4. 区间还原页看翻转、换表末段、新表首读与标记，可点「重算区间」。
5. 月度分摊页看每月合计、账单与尾差，可点「重算分摊」。
6. 站号合并页合并 / 撤销 / 回读月量（接口见下）。
7. 批次对账页看四账与运行清单；导出页下载五份文件。

命令行同一套接口：

```bash
curl -sS -X POST http://localhost:18216/api/runs \
  -H 'Content-Type: application/json' \
  -d '{"sample":"readings_dirty.csv"}'
curl -sS http://localhost:18216/api/reconcile | python3 -m json.tool
curl -sS -o /tmp/monthly.csv http://localhost:18216/api/export/monthly.csv
```

### 2.1 站号合并

```bash
# 导入站号日值（幂等：同一条重发不会新增行）
curl -sS -X POST http://localhost:18216/api/stations/dailies \
  -H 'Content-Type: application/json' \
  -d '{"records":[{"station":"A1","date":"2024-02-01","rainfall_mm":"0.0","version":1}]}'

# 合并 A1 → B2；expected_revision 可省略，带上则拒绝过期并发
curl -sS -X POST http://localhost:18216/api/stations/merges \
  -H 'Content-Type: application/json' \
  -d '{"source":"A1","target":"B2","request_id":"merge-20240201","expected_revision":0}'

# 回读：别名分组、按日裁决、零雨 / 缺测 / 冲突分列、按来源归属
curl -sS 'http://localhost:18216/api/stations/readback?stations=A1,B2'

# 撤销（只能撤最近一条生效的合并；request_id 保证重试幂等）
curl -sS -X POST http://localhost:18216/api/stations/merges/<decision_id>/undo \
  -H 'Content-Type: application/json' -d '{"request_id":"undo-20240202"}'

# 中断恢复：继续所有 pending 决定；失败的标 aborted，月量保持原样
curl -sS -X POST http://localhost:18216/api/stations/merges/recover -d '{}'
```

约定：

- 合并只改「哪些站号算同一测站」，**不复制月量、不改写来源站号**。回读每次都从原始日值重算。
- 同一天跨别名出现多条：高版本胜；同版同值只算一次；同版异值整日隔离（`conflict_days`），不猜哪条为准。
- 撤销后两条站号分别重算，`by_source` 恢复为各自归属；日值行数与合并前一致。
- 重试、恢复、`request_id` 重发都不新增日值；`pending` / `aborted` 期间读到的仍是原月量。
- 拒绝：并入自身（`E_MERGE_SELF`）、源站已是别名（`E_MERGE_ALIAS`）、成环（`E_MERGE_CYCLE`）、两站同组（`E_MERGE_SAME_GROUP`）、撤销非最近一条（`E_UNDO_OUT_OF_ORDER`）、过期并发（`E_STALE_REVISION`）。

## 3 常见操作

- **同输入重跑**：再点一次「开始处理」或 POST 同一 `sample`，命中批次键，返回同一 `batch_id`，行数与合计不变。
- **强制重算**：POST `/api/batches/{batch_id}/retry`，用同一个 `sample` 重跑并覆盖该批次的行。
- **换样例**：下拉框里其余样例分别是干净、GBK 中文表头、末行截断、空文件、仅表头、全空行、表头缺列。
- **重跑测试**：`docker compose exec -T app python -m pytest -q`。
- **清库**：`docker compose down -v` 删除命名卷；下次启动自动建表。

## 4 失败排查

| 现象 | 原因 | 处理 |
|---|---|---|
| 422 `E_EMPTY_FILE` | 样例文件 0 字节 | 选非空样例 |
| 422 `E_HEADER_ONLY` | 只有表头 | 补数据行 |
| 422 `E_ALL_BLANK` | 数据行全空 | 检查导出是否只带格式 |
| 422 `E_HEADER_INVALID` | 缺 `meter_id/register/read_at/reading` | 按列别名表改名 |
| 422 `E_CONTRACT_INVALID` | `meters.csv` 重复表号、零倍率、换表目标缺失 | 修契约 |
| 400 `E_SAMPLE_UNKNOWN` | 样例名不在白名单 | 用页面下拉框里的名字 |
| 404 `E_BATCH_NOT_FOUND` | 还没有批次就导出 | 先跑一次处理 |
| 409 `E_MERGE_CYCLE` | 合并会成环（B→A 而 A 已是 B 的别名） | 先撤销旧合并，或反向合并 |
| 409 `E_MERGE_ALIAS` | 来源站已并入别的根，一个站只能有一条出边 | 撤销旧合并后再合 |
| 409 `E_UNDO_OUT_OF_ORDER` | 撤销的不是最近一条生效合并 | 从最新一条倒着撤 |
| 409 `E_STALE_REVISION` | 决定基于旧修订号提交 | 重新回读 `revision` 后重提 |
| `pending` 状态一直不变 | 进程在切换前中断 | `POST /api/stations/merges/recover` 续跑；失败会自动标 `aborted` 且月量不变 |
| 四账「不平」 | 逻辑或数据被改 | 看差额字段与说明，逐账核对 |
| 站号账 `balanced=false` | 日值入库被截断或决定台账被手改 | 查 `accounts.source / selected / duplicates / conflict_records` |

日志：`docker compose logs -f app`（uvicorn 访问日志）。运行清单里的 `input_digest` 可用来确认两次运行读的是同一份输入。

## 5 复算

1. 从导出页取 `monthly.csv` 与 `manifest.json`；
2. `manifest.json` 的 `inputs[].sha256` 与样例文件比对，确认输入未变；
3. 逐月加总 `用量kWh` 应等于 `manifest.json` 的 `counters` 与 `accounts[1]`（区间账）两侧；
4. 账单 `金额元` 之和应等于 `accounts[3]` 两侧，分项之和等于账单额。

## 6 环境变量

| 变量 | 默认 | 说明 |
|---|---|---|
| `HYDROCALENDAR_DB` | `/data/hydrocalendar.sqlite` | SQLite 路径 |
| `HYDROCALENDAR_SAMPLE_DIR` | `/app/sample_data` | 样例目录 |

规则版本写在 `app/config.py` 的 `RULE_VERSION`，进入运行清单；改规则必须同时加测试，否则同输入重跑会出现两个批次。
