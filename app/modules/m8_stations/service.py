"""M8 服务层：日值入库、合并 / 撤销 / 恢复、月量回读。

所有写操作都通过 `Repository` 的事务进行；所有读操作都从原始日值重算，
不缓存任何合并后的月量，因此：
- 重试与恢复都不新增日值；
- 撤销后原站分别重算，不靠拆合计；
- 合并期间读到的月量不会整月重复计雨。
"""
from __future__ import annotations

import datetime as dt

from ... import errors
from ..m5_months import stations as hydro_stations
from . import repository

MERGE_STATUS_LABELS = repository.STATUS_LABELS


class Service:
    def __init__(self, store):
        self.repo = repository.Repository(store)

    # ---------- 日值 ----------
    def import_dailies(self, payload: list[dict], *, now: dt.datetime | None = None) -> dict:
        if isinstance(payload, dict):
            payload = payload.get("records")
        if not isinstance(payload, list):
            raise errors.BadRequest("records 必须是数组", code="E_BAD_REQUEST")
        normalized, invalid = [], []
        for index, raw in enumerate(payload):
            try:
                normalized.append(hydro_stations.normalize_daily(raw, now=now))
            except hydro_stations.DailyInvalid as exc:
                invalid.append({"index": index, "code": exc.code,
                                "field": exc.field, "message": exc.message})
        if not normalized and invalid:
            raise errors.UnprocessableInput(
                "没有任何合法日值", code="E_DAILY_INVALID",
                detail={"invalid": invalid[:20], "invalid_count": len(invalid)})
        result = self.repo.import_dailies(normalized)
        return {"received": len(payload), "valid": len(normalized),
                "invalid": invalid, "invalid_count": len(invalid),
                "inserted": result["inserted"], "existing": result["existing"],
                "revision": self.repo.revision()}

    def dailies(self, station: str | None = None) -> list[dict]:
        rows = [hydro_stations.hydrate(row) for row in self.repo.dailies(station)]
        return [{
            "record_id": row["record_id"], "station": row["station"],
            "date": row["date"], "rainfall_mm": hydro_stations.display_value(
                row["rainfall_mm"]),
            "version": row["version"], "source_ref": row["source_ref"],
            "received_at": (row["received_at"].isoformat()
                            if isinstance(row["received_at"], dt.datetime)
                            else row["received_at"]),
        } for row in rows]

    # ---------- 合并 ----------
    def merge(self, *, source: str, target: str, expected_revision: int | None,
              request_id: str | None, note: str = "",
              defer: bool = False, switch_check=None) -> dict:
        request_id = request_id or self._auto_request_id("merge", source, target)
        result = self.repo.create_merge(
            request_id=request_id, source=source, target=target,
            expected_revision=expected_revision, note=note, defer=defer,
            switch_check=switch_check)
        decision = result["decision"]
        return {
            "status": decision["status"],
            "status_label": MERGE_STATUS_LABELS.get(decision["status"],
                                                    decision["status"]),
            "decision": decision,
            "replayed": result["replayed"],
            "revision": result["revision"],
            "readback": self.readback(stations=[str(source), str(target)]),
        }

    def undo(self, decision_id: str, *, expected_revision: int | None,
             request_id: str | None, note: str = "") -> dict:
        request_id = request_id or self._auto_request_id("undo", decision_id)
        result = self.repo.undo(decision_id, request_id=request_id,
                                expected_revision=expected_revision, note=note)
        decision = result["decision"]
        return {
            "status": decision["status"],
            "status_label": MERGE_STATUS_LABELS.get(decision["status"],
                                                    decision["status"]),
            "decision": decision,
            "replayed": result["replayed"],
            "revision": result["revision"],
            "readback": self.readback(
                stations=[str(decision["source"]), str(decision["target"])]),
        }

    def recover(self, *, switch_check=None) -> dict:
        result = self.repo.recover(switch_check=switch_check)
        return {"recovered": [{**item,
                               "status_label": MERGE_STATUS_LABELS.get(
                                   item["status"], item["status"])}
                              for item in result["recovered"]],
                "revision": result["revision"],
                "readback": self.readback()}

    def decisions(self) -> list[dict]:
        rows = []
        for row in self.repo.decisions():
            payload = repository.Repository._decision_payload(row)
            payload["status_label"] = MERGE_STATUS_LABELS.get(row["status"],
                                                             row["status"])
            rows.append(payload)
        return rows

    # ---------- 回读 ----------
    def readback(self, *, stations: list[str] | None = None,
                 months: list[str] | None = None,
                 expected_days: dict[str, int] | None = None) -> dict:
        records = repository.dailies_for_compute(self.repo)
        decisions = repository.decisions_for_compute(self.repo)
        payload = hydro_stations.build_readback(
            records, decisions, stations=stations, months=months,
            expected_days=expected_days, revision=self.repo.revision())
        payload["decisions"] = self.decisions()
        return payload

    # ---------- 内部 ----------
    @staticmethod
    def _auto_request_id(kind: str, *parts: str) -> str:
        stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%d%H%M%S%f")
        return f"auto-{kind}-{'-'.join(str(part) for part in parts)}-{stamp}"