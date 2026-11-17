# 数据定义 hydrocalendar

本文件定义每个数字、时间、键的含义。页面只显示字段名与数值，解释在这里。

## 1 读数文件列契约

| 规范列名 | 可接受别名 | 类型 | 单位 | 可空 | 说明 |
|---|---|---|---|---|---|
| `meter_id` | 表号 / 电能表 / 电表编号 / meter | 字符串 | — | 否 | 前导零保形，`007` ≠ `7` |
| `register` | 寄存器 / 分量 / 费率 | 枚举 | — | 否 | `TOTAL` 总 / `PEAK` 峰 / `FLAT` 平 / `VALLEY` 谷，也接受 总/峰/平/谷 |
| `read_at` | 抄表时间 / 抄见时间 / 时间 | 时间 | — | 否 | 带偏移按偏移换算；无偏移按 +08:00；纯日期取当日 00:00 |
| `reading` | 读数 / 示数 / 累计读数 | 十进制 | 见表内 `unit` | 否 | 累计示数，不是用量 |
| `unit` | 单位 / 计量单位 | 枚举 | — | 是 | `kWh` / `度` / `千瓦时` / `MWh` / `兆瓦时`；空则取契约单位 |
| `read_type` | 抄表类型 / 类型 / 方式 | 枚举 | — | 是 | `N`/`正常`/`实抄`、`E`/`估抄`/`估算`；空按 `N` |
| `note` | 备注 / 说明 | 字符串 | — | 是 | 原样保存，只作证据 |

`meter_id / register / read_at / reading` 四列缺失即整文件失败（`E_HEADER_INVALID`）。

## 2 表计契约（meters.csv）

| 列 | 含义 | 约束 |
|---|---|---|
| `capacity` | 满量（寄存器最大值） | > 0；翻转判定的基准 |
| `multiplier` | 综合倍率 | > 0；区间用量 = 差值 × 倍率 |
| `unit` | 契约单位 | 只能是 `kWh` / `MWh`；读数先折到它 |
| `registers` | 寄存器组合，`;` 分隔 | 子集 of TOTAL/PEAK/FLAT/VALLEY |
| `replaced_at` | 换表时点 | 带偏移；与 `replaced_by` 成对出现 |
| `replaced_by` | 新表号 | 必须存在于契约 |

主寄存器：有 `TOTAL` 取 `TOTAL`，否则取第一个分量。区间账与月度账只统计主寄存器，避免与峰平谷重复计数。

## 3 数值与精度

- 用量：`Decimal`，单位 kWh，最小刻度 `0.001`；量化只发生在区间用量、月度分摊、分量闭合三处。
- 金额：`Decimal`，单位元，最小刻度 `0.01`；量化只发生在账单合计与分项补差两处。
- 电价：元/kWh，`prices.csv`（`TOTAL 0.65`、`PEAK 1.05`、`FLAT 0.65`、`VALLEY 0.32`）。
- 对外 JSON 与 CSV：用量三位小数、金额两位小数，全部以字符串输出，禁止 float 往返。

## 4 时间与分桶

- 入库一律 UTC（`+00:00`）；页面展示与月桶按 `Asia/Shanghai (+08:00)`。
- 区间：`[起, 止)`，允许跨月、跨年；上一段止点等于下一段起点。
- 月桶：`[月初 00:00, 下月初 00:00)`，按覆盖天数加权；天数用秒数 / 86400 折算，保留 9 位小数。
- 相邻区间不重不漏由「半开 + 首尾相接」保证，测试 `test_intervals_are_half_open_and_contiguous` 固定。

## 5 空值与三态

- 哨兵清单：`-`、`--`、`—`、`/`、`N/A`、`null`、`未知`、`无`、`暂无`、`待定`、`空`、`未抄`、空串。
- `null`（无值）、`0`（有效零读数）、空串（格式缺陷）语义不同：前两者走不同原因码，空串按哨兵处理。
- 文件级三态：0 字节 → `E_EMPTY_FILE`；仅表头 → `E_HEADER_ONLY`；数据行全空 → `E_ALL_BLANK`。

## 6 去重与冲突键

- 业务键：`(meter_id, register, read_at)`（时间已归一到 UTC）。
- 同键多行且读数完全一致 → 保留行号最小的一条，其余记 `K_DUPLICATE_IDENTICAL`。
- 同键多行且读数不同 → 两条都进隔离区（`Q_DUPLICATE_CONFLICT`），带各自原值，不做「后写覆盖」。
- 不使用全行哈希：备注列变化不代表读数变化。

## 7 判定规则

| 判定 | 条件 | 结果 |
|---|---|---|
| 常规 | 本次 ≥ 上次 | 差值 = 本次 − 上次 |
| 满量翻转 | 本次 < 上次 且 上次 ≥ 满量 × 0.9 且 补满量后差值 ≤ 满量 | 差值 = 满量 − 上次 + 本次 |
| 倒走 | 本次 < 上次 且 不满足翻转前提 | `Q_REVERSE_READING`，整行隔离 |
| 换表末段 | 旧表最后一条读数（≤ 换表时点） | 区间止点 = 换表时点 |
| 新表首读 | 新表第一条读数 | 区间 = [换表时点, 首读时间)，用量从 0 起算 |
| 估抄 | `read_type = E` | 不作锚点、不参与单调判定，落在区间内时给区间打 `K_ESTIMATE_SKIPPED` |

## 8 尾差处理

1. 月度切分：按天数权重得到精确份额，各自向下取整后，余数按小数部分从大到小逐份补 0.001，保证每段之和等于区间用量。
2. 分量闭合：同月「总表 −（峰+平+谷）」若不等于 0，差额并入当月用量最大的分量（并列取 峰 > 平 > 谷），并标 `K_TOU_ADJUSTED`。
3. 账单闭合：账单额 = 先合计再四舍五入；分项金额各自向下截尾，差额按小数部分从大到小逐分补到分项，补完分项之和等于账单额，差额记 `adjust_cents`。

## 9 原因码

文件级 `E_*`：`E_EMPTY_FILE`、`E_HEADER_ONLY`、`E_ALL_BLANK`、`E_HEADER_INVALID`、`E_DECODE`、`E_SAMPLE_UNKNOWN`、`E_CONTRACT_INVALID`、`E_BATCH_NOT_FOUND`、`E_DAILY_INVALID`、`E_DECISION_NOT_FOUND`、`E_MERGE_SELF`、`E_MERGE_ALIAS`、`E_MERGE_CYCLE`、`E_MERGE_SAME_GROUP`、`E_MERGE_PENDING`、`E_MERGE_NOT_APPLIED`、`E_UNDO_OUT_OF_ORDER`、`E_SWITCH_FAILED`、`E_STALE_REVISION`、`E_REQUEST_REUSED`。

行级 `Q_*`：`Q_COLUMN_COUNT`、`Q_TRUNCATED_ROW`、`Q_MISSING_READING`、`Q_BAD_READING`、`Q_BAD_TIME`、`Q_UNKNOWN_METER`、`Q_BAD_REGISTER`、`Q_BAD_UNIT`、`Q_BAD_READ_TYPE`、`Q_NEGATIVE_READING`、`Q_EXCEEDS_CAPACITY`、`Q_REVERSE_READING`、`Q_DUPLICATE_CONFLICT`、`Q_METER_RETIRED`。

标记 `K_*`：`K_ROLLOVER`、`K_REPLACEMENT_FIRST`、`K_ESTIMATE_SKIPPED`、`K_CHARSET_FALLBACK`、`K_BOM_STRIPPED`、`K_SEGMENT_SPLIT`、`K_TRUNCATED_TAIL`、`K_TOU_ADJUSTED`、`K_BILL_ROUNDED`、`K_UNIT_ASSUMED`、`K_UNIT_CONVERTED`、`K_DUPLICATE_IDENTICAL`、`K_MERGE_BACKFILL`、`K_MERGE_CORRECTION`、`K_MERGE_CONFLICT`、`K_MERGE_UNDONE`。

唯一来源：`app/reasons.py`；页面与导出的原因文案都从它取。

## 10 站号日值与合并决定

### 10.1 日值记录（station_dailies）

| 字段 | 类型 | 说明 |
|---|---|---|
| `record_id` | 字符串 | 幂等键。默认由 `(station, date, version, rainfall, source_ref)` 派生，**不含接收时间**：同一份日值换时间戳重发仍是同一条，重试不新增行 |
| `station` | 字符串 | 来源站号，前导零保形，永远不因合并被改写 |
| `date` | `YYYY-MM-DD` | 当地自然日；决定月桶与缺日 |
| `rainfall_mm` | 十进制或空 | `0` 是有效零雨；空 / 哨兵是缺测（`None`），两者不合并、不互替 |
| `version` | 非负整数 | 版本裁决用；同日同根取最高版 |
| `source_ref` | 字符串 | 来源单据号，只作证据与归属 |
| `received_at` | 时间 | 合并窗口内区分「补报 / 更正」；入库归一 UTC |

同日同根（合并后）裁决顺序：最高版本 → 同版同值保留一条 → 同版异值整日隔离
（`VERSION_CONFLICT`，全部原值进 `conflicts`，不计入月量）。同日有缺测与实测时，
实测优先，缺测记为重复（`MISSING_IGNORED`）而不占测量日；同日全是缺测则采信一条缺测。

### 10.2 月量回读（GET /api/stations/readback）

| 字段 | 说明 |
|---|---|
| `root` | 合并后的根站号；未合并时等于原名 |
| `rainfall_mm` | 十进制一位小数展示；精确值见 `rainfall_exact` |
| `expected_days` | 当地自然日天数（默认按日历，可由 `expected_days` 覆盖） |
| `measured_days` / `zero_rain_days` / `reported_missing_days` / `unreported_missing_days` / `missing_days` / `conflict_days` | 六列互不顶替：`missing_days = expected − measured − conflict`，其中已报缺测单列，未报缺日单列 |
| `by_source[]` | 按来源站号的雨量归属；各来源之和逐位等于月量（`attribution_balanced`） |
| `tags` | `K_MERGE_UNDONE`（撤销后回原站重算）、`K_MERGE_CONFLICT`（当月有隔离日） |

`accounts`：`source = selected + duplicates + conflict_records`（`balanced`），
`attribution_balanced` 表示月量等于归属之和。

### 10.3 合并决定（station_merges）

| 字段 | 说明 |
|---|---|
| `decision_id` | 由 `request_id` 派生；同一请求永远指向同一条决定 |
| `seq` | 决定顺序；撤销只能针对最近一条 `applied` |
| `source` → `target` | 别名并入根。一个 source 只能有一条出边；成环、并自身、两站同组都拒绝 |
| `status` | `pending`（决定已落、未切换）→ `applied`（生效）／`aborted`（切换失败）→ `undone`（撤销）。只有 `applied` 折叠进别名图 |
| `revision` / `expected_revision` | 全局单调修订号。写请求带 `expected_revision` 时比较并设置，过期返回 `E_STALE_REVISION` |
| `undo_request_id` | 撤销幂等键；同一请求重发返回同一结果 |
| `abort_code` | 切换失败原因；同一 `request_id` 重试会复用该决定并重新切换 |

回读规则：合并只改别名图，月量每次从原始日值重算；撤销不拆合计，而是把两条
站号分别按原日值重算；重试与恢复都不新增日值，因此不存在「整月复制导致重复计雨」。
