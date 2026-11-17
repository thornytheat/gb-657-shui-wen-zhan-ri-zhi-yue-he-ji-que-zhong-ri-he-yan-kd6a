"""站号合并的落账入口：决定先落 pending，再切换；失败标 aborted，月量不动。

一次合并的状态机（数据库里可见，进程中断后也能接着走）::

    pending ──切换成功──▶ applied ──撤销──▶ undone
       └────切换失败────▶ aborted

- `pending` 行代表「决定已记下、尚未切换」。中断后 `recover()` 按 seq 继续，
  同一 request_id 重发不会新建决定。
- 回读只把 `applied` 折叠进别名图，所以 pending / aborted / undone 期间读到的
  仍是原月量，且撤销后两条站号各自独立。
- revision 是全局单调修订号；带 expected_revision 的写请求按「比较并设置」
  拒绝过期并发决定，两个并发撤销不会都成功。
"""
from __future__ import annotations

import datetime as dt
import hashlib

from ... import errors, reasons
from ..m5_months import stations as hydro_stations
from . import ledger

PENDING = "pending"
APPLIED = "applied"
ABORTED = "aborted"
UNDONE = "undone"
STATUS_LABELS = {PENDING: "待切换", APPLIED: "已生效",
                 ABORTED: "切换失败", UNDONE: "已撤销"}


def _iso(value: dt.datetime | None = None) -> str:
    return (value or dt.datetime.now(dt.timezone.utc)).astimezone(
        dt.timezone.utc).isoformat()


def decision_id_for(request_id: str) -> str:
    return "mh-" + hashlib.sha256(request_id.encode("utf-8")).hexdigest()[:16]


class Repository:
    def __init__(self, store):
        self.store = store

    # ---------- 读 ----------
    def revision(self, conn=None) -> int:
        sql = "SELECT revision FROM station_state WHERE id=1"
        if conn is not None:
            return int(conn.execute(sql).fetchone()["revision"])
        with self.store.connect() as conn:
            return int(conn.execute(sql).fetchone()["revision"])

    def decisions(self, conn=None) -> list[dict]:
        sql = "SELECT * FROM station_merges ORDER BY seq"
        if conn is not None:
            return [dict(row) for row in conn.execute(sql).fetchall()]
        with self.store.connect() as conn:
            return [dict(row) for row in conn.execute(sql).fetchall()]

    def decision(self, decision_id: str) -> dict | None:
        with self.store.connect() as conn:
            row = conn.execute("SELECT * FROM station_merges WHERE decision_id=?",
                               (decision_id,)).fetchone()
        return dict(row) if row else None

    def dailies(self, station: str | None = None, conn=None) -> list[dict]:
        sql = "SELECT * FROM station_dailies"
        args: tuple = ()
        if station is not None:
            sql += " WHERE station=?"
            args = (station,)
        sql += " ORDER BY station, date, version, record_id"
        if conn is not None:
            return [dict(row) for row in conn.execute(sql, args).fetchall()]
        with self.store.connect() as conn:
            return [dict(row) for row in conn.execute(sql, args).fetchall()]

    # ---------- 日值入库（幂等） ----------
    def import_dailies(self, records: list[dict]) -> dict:
        inserted = existing = 0
        with self.store.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            for record in records:
                cursor = conn.execute(
                    """INSERT INTO station_dailies
                           (record_id,station,date,rainfall_mm,version,source_ref,
                            received_at)
                       VALUES (?,?,?,?,?,?,?)
                       ON CONFLICT(record_id) DO NOTHING""",
                    (record["record_id"], record["station"], record["date"],
                     hydro_stations.display_value(record["rainfall_mm"]),
                     int(record["version"]), record.get("source_ref", ""),
                     record["received_at"]))
                if cursor.rowcount:
                    inserted += 1
                else:
                    existing += 1
        return {"inserted": inserted, "existing": existing}

    # ---------- 合并 ----------
    def create_merge(self, *, request_id: str, source: str, target: str,
                     expected_revision: int | None, note: str = "",
                     defer: bool = False, switch_check=None) -> dict:
        request_id = (request_id or "").strip()
        if not request_id:
            raise errors.BadRequest("request_id 不能为空", code="E_BAD_REQUEST")
        source = hydro_stations.parse_station(source)
        target = hydro_stations.parse_station(target)
        decision_id = decision_id_for(request_id)

        with self.store.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            replay = conn.execute(
                "SELECT * FROM station_merges WHERE request_id=?",
                (request_id,)).fetchone()
            pending = None
            if replay is not None:
                row = dict(replay)
                if row["source"] != source or row["target"] != target:
                    raise errors.Conflict(
                        "同一 request_id 已用于另一组站号",
                        code="E_REQUEST_REUSED",
                        detail={"decision_id": row["decision_id"]})
                if row["status"] in (APPLIED, UNDONE):
                    return {"decision": self._decision_payload(row), "replayed": True,
                            "revision": int(row["revision"] or self.revision(conn))}
                if row["status"] == ABORTED:
                    # 切换失败后重试：复用同一条决定，不新增决定、不新增日值。
                    self._guard_revision(conn, expected_revision)
                    conn.execute(
                        "UPDATE station_merges SET status=?, abort_code=NULL "
                        "WHERE decision_id=? AND status=?",
                        (PENDING, row["decision_id"], ABORTED))
                    row = dict(conn.execute(
                        "SELECT * FROM station_merges WHERE decision_id=?",
                        (row["decision_id"],)).fetchone())
                pending = row
            if pending is None:
                self._guard_revision(conn, expected_revision)
                seq = self._next_seq(conn)
                self._raise_verdict(
                    ledger.check_merge(self._all(conn), source, target,
                                       pending_before=seq), source, target)
                conn.execute(
                    """INSERT INTO station_merges
                           (decision_id,request_id,seq,source,target,status,revision,
                            expected_revision,note,decided_at)
                       VALUES (?,?,?,?,?,?,?,?,?,?)""",
                    (decision_id, request_id, seq, source, target, PENDING, None,
                     -1 if expected_revision is None else int(expected_revision),
                     note or "", _iso()))
                pending = dict(conn.execute(
                    "SELECT * FROM station_merges WHERE decision_id=?",
                    (decision_id,)).fetchone())
        if defer:
            return {"decision": self._decision_payload(pending), "replayed": False,
                    "revision": self.revision()}
        return self._finalize(str(pending["decision_id"]), expected_revision=None,
                              switch_check=switch_check)

    def recover(self, *, switch_check=None) -> dict:
        """按 seq 顺序继续 pending 决定；失败标 aborted，已生效的月量不动。"""
        outcomes = []
        for row in self.decisions():
            if row["status"] != PENDING:
                continue
            try:
                result = self._finalize(row["decision_id"],
                                        expected_revision=self._expected(row),
                                        switch_check=switch_check)
            except errors.MeterbackError as exc:
                if (self.decision(row["decision_id"]) or row)["status"] == PENDING:
                    self._abort(row["decision_id"], exc.code)
                current = self.decision(row["decision_id"]) or row
                outcomes.append({**self._decision_payload(current),
                                 "error": exc.code})
                continue
            outcomes.append(result["decision"])
        return {"recovered": outcomes, "revision": self.revision()}

    def _finalize(self, decision_id: str, *, expected_revision: int | None,
                  switch_check=None) -> dict:
        row = self.decision(decision_id)
        if row is None:
            raise errors.NotFound(f"合并决定 {decision_id} 不存在",
                                  code="E_DECISION_NOT_FOUND")
        if row["status"] == APPLIED:
            return {"decision": self._decision_payload(row), "replayed": True,
                    "revision": int(row["revision"])}
        if row["status"] == ABORTED:
            raise errors.Conflict(
                f"该合并决定已失败：{reasons.label(row.get('abort_code'))}",
                code=row.get("abort_code") or "E_MERGE_NOT_APPLIED",
                detail={"decision_id": decision_id})
        if row["status"] == UNDONE:
            raise errors.Conflict("该合并已撤销", code="E_MERGE_NOT_APPLIED",
                                  detail={"decision_id": decision_id})
        try:
            self._raise_verdict(
                ledger.check_merge(self._applied_now(), str(row["source"]),
                                   str(row["target"])),
                str(row["source"]), str(row["target"]))
            if switch_check is not None:
                switch_check(str(row["source"]), str(row["target"]))
        except errors.MeterbackError as exc:
            self._abort(decision_id, exc.code)
            raise
        except Exception as exc:  # noqa: BLE001 - 切换失败必须留痕且不改月量
            self._abort(decision_id, "E_SWITCH_FAILED")
            raise errors.Conflict(f"合并切换失败：{exc}", code="E_SWITCH_FAILED",
                                  detail={"decision_id": decision_id}) from exc

        pending_error: errors.MeterbackError | None = None
        with self.store.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            current = dict(conn.execute(
                "SELECT * FROM station_merges WHERE decision_id=?",
                (decision_id,)).fetchone())
            if current["status"] != PENDING:
                return {"decision": self._decision_payload(current), "replayed": True,
                        "revision": int(current["revision"] or self.revision(conn))}
            # 事务内复核一次：切换期间可能已有别的决定生效。失败必须在本事务里
            # 标 aborted，不能先抛异常回滚，否则决定会永远停在 pending。
            verdict = ledger.check_merge(self._applied(conn), str(current["source"]),
                                         str(current["target"]))
            try:
                self._raise_verdict(verdict, str(current["source"]),
                                    str(current["target"]))
                self._guard_revision(conn, expected_revision)
            except errors.MeterbackError as exc:
                pending_error = exc
                conn.execute(
                    """UPDATE station_merges SET status=?, abort_code=?
                        WHERE decision_id=? AND status=?""",
                    (ABORTED, exc.code, decision_id, PENDING))
            if pending_error is None:
                revision = self._bump(conn)
                conn.execute(
                    """UPDATE station_merges SET status=?, revision=?, applied_at=?
                        WHERE decision_id=? AND status=?""",
                    (APPLIED, revision, _iso(), decision_id, PENDING))
                final = dict(conn.execute(
                    "SELECT * FROM station_merges WHERE decision_id=?",
                    (decision_id,)).fetchone())
        if pending_error is not None:
            raise pending_error
        return {"decision": self._decision_payload(final), "replayed": False,
                "revision": revision}

    # ---------- 撤销 ----------
    def undo(self, decision_id: str, *, request_id: str,
             expected_revision: int | None, note: str = "") -> dict:
        request_id = (request_id or "").strip()
        if not request_id:
            raise errors.BadRequest("request_id 不能为空", code="E_BAD_REQUEST")
        with self.store.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            replay = conn.execute(
                "SELECT * FROM station_merges WHERE undo_request_id=?",
                (request_id,)).fetchone()
            if replay is not None:
                row = dict(replay)
                if row["decision_id"] != decision_id:
                    raise errors.Conflict(
                        "同一 request_id 已用于撤销另一条决定",
                        code="E_REQUEST_REUSED",
                        detail={"decision_id": row["decision_id"]})
                return {"decision": self._decision_payload(row), "replayed": True,
                        "revision": self.revision(conn)}
            row = conn.execute("SELECT * FROM station_merges WHERE decision_id=?",
                               (decision_id,)).fetchone()
            if row is None:
                raise errors.NotFound(f"合并决定 {decision_id} 不存在",
                                      code="E_DECISION_NOT_FOUND")
            row = dict(row)
            if row["status"] == UNDONE:
                raise errors.Conflict("该合并已撤销", code="E_MERGE_NOT_APPLIED",
                                      detail={"decision_id": decision_id})
            if row["status"] in (ABORTED, PENDING):
                raise errors.Conflict(
                    "该合并尚未生效，不能撤销", code="E_MERGE_NOT_APPLIED",
                    detail={"decision_id": decision_id, "status": row["status"]})
            if ledger.latest_applied_seq(self._applied(conn)) != int(row["seq"]):
                raise errors.Conflict(
                    "只能撤销最近一条生效的合并（链中间不能断）",
                    code="E_UNDO_OUT_OF_ORDER", detail={"decision_id": decision_id})
            self._guard_revision(conn, expected_revision)
            revision = self._bump(conn)
            conn.execute(
                """UPDATE station_merges
                      SET status=?, undo_revision=?, undo_request_id=?, undone_at=?,
                          note=CASE WHEN ?='' THEN note ELSE ? END
                    WHERE decision_id=? AND status=?""",
                (UNDONE, revision, request_id, _iso(), note or "", note or "",
                 decision_id, APPLIED))
            final = dict(conn.execute(
                "SELECT * FROM station_merges WHERE decision_id=?",
                (decision_id,)).fetchone())
        return {"decision": self._decision_payload(final), "replayed": False,
                "revision": revision}

    # ---------- 内部 ----------
    @staticmethod
    def _all(conn) -> list[dict]:
        return [dict(row) for row in conn.execute(
            "SELECT * FROM station_merges ORDER BY seq").fetchall()]

    def _applied(self, conn) -> list[dict]:
        return [dict(row) for row in conn.execute(
            "SELECT * FROM station_merges WHERE status=? ORDER BY seq",
            (APPLIED,)).fetchall()]

    def _applied_now(self) -> list[dict]:
        return [row for row in self.decisions() if row["status"] == APPLIED]

    @staticmethod
    def _next_seq(conn) -> int:
        return int(conn.execute("SELECT COALESCE(MAX(seq), 0) + 1 AS n "
                                "FROM station_merges").fetchone()["n"])

    def _bump(self, conn) -> int:
        conn.execute("UPDATE station_state SET revision=revision+1, updated_at=? "
                     "WHERE id=1", (_iso(),))
        return self.revision(conn)

    def _guard_revision(self, conn, expected: int | None) -> None:
        if expected is None:
            return
        current = self.revision(conn)
        if int(expected) != current:
            raise errors.Conflict(
                f"合并决定已过期：期望修订 {expected}，当前 {current}",
                code="E_STALE_REVISION",
                detail={"expected_revision": int(expected),
                        "current_revision": current})

    @staticmethod
    def _expected(row: dict) -> int | None:
        value = row.get("expected_revision")
        if value is None or int(value) < 0:
            return None
        return int(value)

    @staticmethod
    def _raise_verdict(verdict: str, source: str, target: str) -> None:
        mapping = {
            ledger.VERDICT_SELF: ("E_MERGE_SELF", "不能把站号并入自身"),
            ledger.VERDICT_ALREADY_ALIAS: (
                "E_MERGE_ALIAS", f"来源站 {source} 已是别名，请先撤销旧合并"),
            ledger.VERDICT_CYCLE: ("E_MERGE_CYCLE",
                                   f"{source} → {target} 会形成循环合并"),
            ledger.VERDICT_SAME_GROUP: ("E_MERGE_SAME_GROUP",
                                        f"{source} 与 {target} 已在同一测站组"),
            ledger.VERDICT_PENDING: (
                "E_MERGE_PENDING", f"来源站 {source} 有更早的未完成合并，先恢复或等待其失败"),
        }
        if verdict == ledger.VERDICT_OK:
            return
        code, message = mapping[verdict]
        raise errors.Conflict(message, code=code,
                              detail={"source": source, "target": target})

    def _abort(self, decision_id: str, code: str) -> None:
        with self.store.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                """UPDATE station_merges SET status=?, abort_code=?
                    WHERE decision_id=? AND status=?""",
                (ABORTED, code, decision_id, PENDING))

    @staticmethod
    def _decision_payload(row: dict) -> dict:
        abort_code = row.get("abort_code")
        return {
            "decision_id": row["decision_id"],
            "request_id": row["request_id"],
            "seq": int(row["seq"]),
            "kind": "merge",
            "source": row["source"],
            "target": row["target"],
            "status": row["status"],
            "status_label": STATUS_LABELS.get(row["status"], row["status"]),
            "revision": None if row["revision"] is None else int(row["revision"]),
            "expected_revision": Repository._expected(row),
            "undo_revision": (None if row.get("undo_revision") is None
                              else int(row["undo_revision"])),
            "undo_request_id": row.get("undo_request_id"),
            "abort_code": abort_code,
            "abort_label": reasons.label(abort_code) if abort_code else "—",
            "note": row.get("note") or "",
            "decided_at": row["decided_at"],
            "applied_at": row.get("applied_at"),
            "undone_at": row.get("undone_at"),
        }


def decisions_for_compute(repo: Repository) -> list[dict]:
    """计算层拿全部决定；只有 applied 会被折叠进别名图，其余只作证据。"""
    return repo.decisions()


def dailies_for_compute(repo: Repository, station: str | None = None) -> list[dict]:
    return [hydro_stations.hydrate(row) for row in repo.dailies(station)]


__all__ = ["Repository", "decisions_for_compute", "dailies_for_compute",
           "decision_id_for", "PENDING", "APPLIED", "ABORTED", "UNDONE",
           "STATUS_LABELS"]
