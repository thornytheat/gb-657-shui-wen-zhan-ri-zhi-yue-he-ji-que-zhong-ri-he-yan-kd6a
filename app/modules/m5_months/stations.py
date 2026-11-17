"""跨别名日值裁决：合并后的月量按「根站号 + 自然日」去重，按版本取数。

三条必须同时成立的约束：

- 合并只改变「哪些站号算同一个测站」，不复制、不搬移任何日值。月量回读每次
  都从原始日值重算，所以重试、恢复、回读都不会新增日值，也不会整月重复计雨。
- 每条日值始终保留来源站号（`station`）与来源单据（`source_ref`）；月量同时
  给出按来源的归属。撤销后按原站分别重算，直接拆合计不会丢归属。
- 同根同日的多条日值：版本高者胜；同版同值只留一条；同版异值整日隔离，既不
  猜哪条为准，也不把隔离日混进缺测或零雨。

纯函数，不读写数据库；存储与事务在 `app/modules/m8_stations/repository.py`。
"""
from __future__ import annotations

import calendar
import datetime as dt
import hashlib
import re
from decimal import Decimal, InvalidOperation

from ... import config
from ..m8_stations import ledger

DATE_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")
MISSING_KEY = "__missing__"


class DailyInvalid(ValueError):
    code = "E_DAILY_INVALID"

    def __init__(self, message: str, *, field: str = "", code: str | None = None):
        super().__init__(message)
        self.message = message
        self.field = field
        if code:
            self.code = code


def value_key(value: Decimal | None) -> str:
    """0 / 0.0 / 0.00 是同一个值；None 与 0 明确分开。"""
    if value is None:
        return MISSING_KEY
    return format(value.normalize(), "f")


def display_value(value: Decimal | None) -> str | None:
    return None if value is None else format(value, "f")


def parse_station(raw) -> str:
    station = "" if raw is None else str(raw).strip()
    if not station:
        raise DailyInvalid("站号为空", field="station")
    if len(station) > 64:
        raise DailyInvalid("站号过长", field="station")
    return station


def parse_date(raw) -> str:
    text = str(raw or "").strip()
    match = DATE_RE.match(text)
    if not match:
        raise DailyInvalid(f"日期无法解析：{raw!r}", field="date")
    try:
        value = dt.date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
    except ValueError as exc:
        raise DailyInvalid(f"日期无效：{raw!r}（{exc}）", field="date") from exc
    return value.isoformat()


def parse_rainfall(raw) -> Decimal | None:
    """空值/哨兵 → None（缺测）；0 → Decimal(0)（有效零雨）。"""
    if raw is None:
        return None
    if isinstance(raw, Decimal):
        value = raw
    elif isinstance(raw, bool):
        raise DailyInvalid(f"雨量无法解析：{raw!r}", field="rainfall_mm")
    elif isinstance(raw, (int, float)):
        value = Decimal(str(raw))
    else:
        text = str(raw).strip()
        if text == "" or text.lower() in config.SENTINELS:
            return None
        try:
            value = Decimal(text)
        except InvalidOperation as exc:
            raise DailyInvalid(f"雨量无法解析：{raw!r}", field="rainfall_mm") from exc
    if not value.is_finite():
        raise DailyInvalid(f"雨量无法解析：{raw!r}", field="rainfall_mm")
    if value < 0:
        raise DailyInvalid(f"雨量为负：{value}", field="rainfall_mm")
    return value


def parse_version(raw) -> int:
    try:
        if isinstance(raw, bool):
            raise ValueError
        if isinstance(raw, float):
            if not raw.is_integer():
                raise ValueError
            value = int(raw)
        else:
            value = int(str(raw).strip())
    except (TypeError, ValueError) as exc:
        raise DailyInvalid(f"版本号必须是整数：{raw!r}", field="version") from exc
    if value < 0:
        raise DailyInvalid(f"版本号不能为负：{value}", field="version")
    return value


def parse_received_at(raw, *, now: dt.datetime | None = None) -> dt.datetime:
    if raw is None or str(raw).strip() == "":
        return now or dt.datetime.now(dt.timezone.utc)
    if isinstance(raw, dt.datetime):
        value = raw
    else:
        text = str(raw).strip().replace("Z", "+00:00")
        try:
            value = dt.datetime.fromisoformat(text)
        except ValueError as exc:
            raise DailyInvalid(f"接收时间无法解析：{raw!r}", field="received_at") from exc
    if value.tzinfo is None:
        value = value.replace(tzinfo=config.CHINA_TZ)
    return value.astimezone(dt.timezone.utc)


def derive_record_id(*, station: str, date: str, version: int,
                     rainfall: Decimal | None, source_ref: str) -> str:
    """不含接收时间：同一份补报重试得到同一个 record_id，不会新增日值。"""
    parts = [station, date, str(version), value_key(rainfall), source_ref]
    digest = hashlib.sha256("\x00".join(parts).encode("utf-8")).hexdigest()
    return "sd-" + digest[:20]


def normalize_daily(raw: dict, *, now: dt.datetime | None = None) -> dict:
    if not isinstance(raw, dict):
        raise DailyInvalid("日值记录必须是对象", field="record")
    station = parse_station(raw.get("station"))
    date = parse_date(raw.get("date"))
    rainfall = parse_rainfall(raw.get("rainfall_mm", raw.get("rainfall")))
    version = parse_version(raw.get("version", 0))
    received_at = parse_received_at(raw.get("received_at"), now=now)
    source_ref = "" if raw.get("source_ref") is None else str(raw.get("source_ref")).strip()
    record_id = str(raw.get("record_id") or "").strip()
    if not record_id:
        record_id = derive_record_id(station=station, date=date, version=version,
                                     rainfall=rainfall, source_ref=source_ref)
    if len(record_id) > 128:
        raise DailyInvalid("record_id 过长", field="record_id")
    return {
        "record_id": record_id,
        "station": station,
        "date": date,
        "rainfall_mm": rainfall,
        "version": version,
        "source_ref": source_ref,
        "received_at": received_at.isoformat(),
    }


def hydrate(row: dict) -> dict:
    """存储行 → 计算用记录。"""
    record = dict(row)
    value = row.get("rainfall_mm")
    record["rainfall_mm"] = None if value is None else Decimal(str(value))
    received = row.get("received_at")
    if isinstance(received, str):
        received = dt.datetime.fromisoformat(received)
    record["received_at"] = received
    record["version"] = int(row.get("version") or 0)
    return record


def root_of(stations: list[str], decisions: list[dict]) -> dict[str, str]:
    return ledger.root_map(decisions, stations)


def _evidence(row: dict) -> dict:
    received = row["received_at"]
    return {
        "record_id": row["record_id"],
        "station": row["station"],
        "rainfall_mm": display_value(row["rainfall_mm"]),
        "version": row["version"],
        "source_ref": row.get("source_ref", ""),
        "received_at": received.isoformat() if isinstance(received, dt.datetime)
        else received,
    }


def arbitrate(records: list[dict], decisions: list[dict],
              *, roots_map: dict[str, str]) -> dict:
    """按 (根站号, 日期) 裁决；返回 selected / duplicates / conflicts。

    同一最高版本内：
    - 有多个不同的实测值 → 整日冲突，全部进隔离，不猜哪条为准；
    - 只有一个实测值 → 采信它，同日缺测记录只算重复，不占测量日；
    - 全是缺测 → 采信一条缺测记录，月量记缺测而不是零雨。
    """
    groups: dict[tuple[str, str], list[dict]] = {}
    for row in records:
        item = dict(row, root=roots_map[row["station"]])
        groups.setdefault((item["root"], row["date"]), []).append(item)

    selected: list[dict] = []
    duplicates: list[dict] = []
    conflicts: list[dict] = []
    for (root, date), rows in sorted(groups.items()):
        ranked = sorted(rows, key=lambda row: (-row["version"], row["received_at"],
                                               row["record_id"]))
        top_version = ranked[0]["version"]
        top = [row for row in ranked if row["version"] == top_version]
        measured = [row for row in top if row["rainfall_mm"] is not None]
        values = {value_key(row["rainfall_mm"]) for row in measured}
        if len(values) > 1:
            conflicts.append({
                "root": root,
                "date": date,
                "month": date[:7],
                "version": top_version,
                "reason": "VERSION_CONFLICT",
                "values": sorted(values),
                # 整日隔离：当天所有记录都留在证据里，低版本也不例外，
                # 这样「来源 = 采信 + 重复 + 隔离」逐行对得上。
                "records": [_evidence(row) for row in ranked],
            })
            continue
        chosen = measured[0] if measured else top[0]
        selected.append(chosen)
        for other in ranked:
            if other is chosen:
                continue
            if other["version"] != top_version:
                reason = "LOWER_VERSION"
            elif other["rainfall_mm"] is None:
                reason = "MISSING_IGNORED"
            else:
                reason = "DUPLICATE_IDENTICAL"
            duplicates.append({
                "record_id": other["record_id"],
                "root": root,
                "station": other["station"],
                "date": date,
                "rainfall_mm": display_value(other["rainfall_mm"]),
                "version": other["version"],
                "reason": reason,
                "superseded_by": chosen["record_id"],
            })
    selected.sort(key=lambda row: (row["root"], row["date"], row["station"]))
    return {"selected": selected, "duplicates": duplicates, "conflicts": conflicts}


def _merge_windows(decisions: list[dict], roots_map: dict[str, str]) -> dict[str, str]:
    """当前仍生效的合并里，每个根站号最近一次生效时间。"""
    windows: dict[str, str] = {}
    for row in decisions:
        if row.get("status") != "applied" or not row.get("applied_at"):
            continue
        root = roots_map.get(str(row["target"]))
        if root is None:
            continue
        stamp = str(row["applied_at"])
        if root not in windows or stamp > windows[root]:
            windows[root] = stamp
    return windows


def _undone_roots(decisions: list[dict], roots_map: dict[str, str]) -> set[str]:
    roots: set[str] = set()
    for row in decisions:
        if row.get("status") != "undone":
            continue
        for station in (str(row.get("source")), str(row.get("target"))):
            root = roots_map.get(station)
            if root:
                roots.add(root)
    return roots


def tag_selected(selected: list[dict], decisions: list[dict],
                 groups: dict[tuple[str, str], list[dict]],
                 roots_map: dict[str, str]) -> None:
    """给采信日值打「补报 / 更正」标记，只作证据，不改数值。"""
    windows = _merge_windows(decisions, roots_map)
    for row in selected:
        tags: list[str] = []
        window = windows.get(row["root"])
        siblings = groups.get((row["root"], row["date"]), [])
        if window is not None:
            cutoff = dt.datetime.fromisoformat(window)
            received = row["received_at"]
            if isinstance(received, str):
                received = dt.datetime.fromisoformat(received)
            if received > cutoff:
                # 合并生效之后才到的记录：同日此前已有记录 → 更正，否则补报。
                earlier = [other for other in siblings
                           if other["record_id"] != row["record_id"]
                           and isinstance(other["received_at"], dt.datetime)
                           and other["received_at"] <= cutoff]
                tags.append("K_MERGE_CORRECTION" if earlier else "K_MERGE_BACKFILL")
        elif any(other["version"] != row["version"] for other in siblings
                 if other["record_id"] != row["record_id"]):
            tags.append("K_MERGE_CORRECTION")
        row["tags"] = tags


def month_days(month: str) -> int:
    """当地自然日天数（含闰年 2 月）。"""
    return calendar.monthrange(int(month[:4]), int(month[5:7]))[1]


def _month_days(month: str) -> int:
    return month_days(month)


def _money_string(value: Decimal) -> str:
    text = format(value.normalize(), "f")
    return "0" if text in ("", "-0") else text


def month_totals(records: list[dict], conflicts: list[dict], decisions: list[dict],
                 *, roots_map: dict[str, str], roots: set[str],
                 months: list[str] | None = None,
                 expected_days: dict[str, int] | None = None) -> list[dict]:
    """按 (根站号, 月) 汇总；零雨、缺测、冲突日各占一列，互不顶替。"""
    expected_days = expected_days or {}
    buckets: dict[tuple[str, str], dict] = {}

    def bucket(root: str, month: str) -> dict:
        key = (root, month)
        if key not in buckets:
            buckets[key] = {"root": root, "month": month, "exact": Decimal("0"),
                            "measured_days": 0, "zero_rain_days": 0,
                            "reported_missing_days": 0, "sources": {}}
        return buckets[key]

    for row in records:
        root = row["root"]
        if root not in roots:
            continue
        item = bucket(root, row["date"][:7])
        if row["rainfall_mm"] is None:
            item["reported_missing_days"] += 1
            continue
        item["exact"] += row["rainfall_mm"]
        item["measured_days"] += 1
        is_zero = row["rainfall_mm"] == 0
        if is_zero:
            item["zero_rain_days"] += 1
        source = item["sources"].setdefault(
            row["station"], {"exact": Decimal("0"), "measured_days": 0,
                             "zero_rain_days": 0})
        source["exact"] += row["rainfall_mm"]
        source["measured_days"] += 1
        if is_zero:
            source["zero_rain_days"] += 1

    conflict_counts: dict[tuple[str, str], int] = {}
    for conflict in conflicts:
        if conflict["root"] not in roots:
            continue
        key = (conflict["root"], conflict["month"])
        conflict_counts[key] = conflict_counts.get(key, 0) + 1
        bucket(*key)

    undone = _undone_roots(decisions, roots_map)
    month_list = sorted(set(months or []) | {month for _, month in buckets})
    for root in sorted(roots):
        for month in month_list:
            bucket(root, month)

    output = []
    for (root, month), item in sorted(buckets.items()):
        expected = int(expected_days.get(month, _month_days(month)))
        conflict_days = conflict_counts.get((root, month), 0)
        missing = max(0, expected - item["measured_days"] - conflict_days)
        unreported = max(0, missing - item["reported_missing_days"])
        tags = ["K_MERGE_UNDONE"] if root in undone else []
        if conflict_days:
            tags.append("K_MERGE_CONFLICT")
        output.append({
            "root": root,
            "station": root,
            "month": month,
            "rainfall_mm": str(item["exact"].quantize(Decimal("0.1"))),
            "rainfall_exact": _money_string(item["exact"]),
            "expected_days": expected,
            "measured_days": item["measured_days"],
            "zero_rain_days": item["zero_rain_days"],
            "reported_missing_days": item["reported_missing_days"],
            "unreported_missing_days": unreported,
            "missing_days": missing,
            "conflict_days": conflict_days,
            "state": "complete" if missing == 0 and conflict_days == 0 else "partial",
            "by_source": [{
                "station": station,
                "rainfall_mm": str(values["exact"].quantize(Decimal("0.1"))),
                "rainfall_exact": _money_string(values["exact"]),
                "measured_days": values["measured_days"],
                "zero_rain_days": values["zero_rain_days"],
            } for station, values in sorted(item["sources"].items())],
            "tags": tags,
        })
    return output


def build_readback(records: list[dict], decisions: list[dict], *,
                   stations: list[str] | None = None,
                   months: list[str] | None = None,
                   expected_days: dict[str, int] | None = None,
                   revision: int | None = None) -> dict:
    """一次回读：别名分组、日值裁决、月量归属、冲突隔离与两边对账。"""
    requested = [parse_station(item) for item in (stations or [])]
    all_stations = sorted({row["station"] for row in records}
                          | {str(row["source"]) for row in decisions}
                          | {str(row["target"]) for row in decisions}
                          | set(requested))
    roots_map = root_of(all_stations, decisions)
    if requested:
        roots = {roots_map[item] for item in requested}
    else:
        roots = set(roots_map.values())

    scoped = [row for row in records if roots_map.get(row["station"]) in roots]
    arbitration = arbitrate(scoped, decisions, roots_map=roots_map)

    grouped: dict[tuple[str, str], list[dict]] = {}
    for row in scoped:
        item = dict(row, root=roots_map[row["station"]])
        grouped.setdefault((item["root"], item["date"]), []).append(item)

    conflict_rows = []
    for conflict in arbitration["conflicts"]:
        for record in conflict["records"]:
            conflict_rows.append({**record, "conflict": True, "root": conflict["root"]})

    tag_selected(arbitration["selected"], decisions, grouped, roots_map)
    month_rows = month_totals(arbitration["selected"], arbitration["conflicts"],
                              decisions, roots_map=roots_map, roots=roots,
                              months=months, expected_days=expected_days)

    members: dict[str, set[str]] = {}
    for node in all_stations:
        root = roots_map[node]
        if root in roots:
            members.setdefault(root, set()).add(node)
    group_rows = [{"root": root, "stations": sorted(group),
                   "aliases": sorted(group - {root})}
                  for root, group in sorted(members.items())]

    partition = (len(arbitration["selected"]) + len(arbitration["duplicates"])
                 + len(conflict_rows))
    attribution_balanced = all(
        Decimal(row["rainfall_exact"])
        == sum((Decimal(item["rainfall_exact"]) for item in row["by_source"]),
               Decimal("0"))
        for row in month_rows)
    return {
        "revision": revision,
        "roots": sorted(roots),
        "groups": group_rows,
        "months": month_rows,
        "selected": [{
            "record_id": row["record_id"],
            "root": row["root"],
            "station": row["station"],
            "date": row["date"],
            "month": row["date"][:7],
            "rainfall_mm": display_value(row["rainfall_mm"]),
            "version": row["version"],
            "source_ref": row.get("source_ref", ""),
            "received_at": (row["received_at"].isoformat()
                            if isinstance(row["received_at"], dt.datetime)
                            else row["received_at"]),
            "tags": row.get("tags", []),
        } for row in arbitration["selected"]],
        "duplicates": arbitration["duplicates"],
        "conflicts": arbitration["conflicts"],
        "accounts": {
            "source": len(scoped),
            "selected": len(arbitration["selected"]),
            "duplicates": len(arbitration["duplicates"]),
            "conflict_records": len(conflict_rows),
            "conflict_days": len(arbitration["conflicts"]),
            "partition": partition,
            "balanced": partition == len(scoped),
            "attribution_balanced": attribution_balanced,
        },
    }
