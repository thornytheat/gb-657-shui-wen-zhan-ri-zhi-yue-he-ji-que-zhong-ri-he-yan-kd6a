"""M8 入口：站号合并、撤销、恢复与月量回读。

回读永远是「从原始日值重算」，所以读接口可以和写接口共用同一套计算；
页面与 CSV/JSON 接口不会各算一套。
"""
from __future__ import annotations

import json

from fastapi import APIRouter, Depends, Request

from ... import errors
from ...web import Ctx, get_ctx, render
from ..m5_months import stations as hydro_stations
from .service import Service

router = APIRouter(tags=["M8 站号合并"])


def _service(ctx: Ctx) -> Service:
    return Service(ctx.store)


def _optional_int(value) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise errors.BadRequest(f"revision 必须是整数：{value!r}",
                                code="E_BAD_REQUEST") from exc


def _string_list(value) -> list[str] | None:
    if value is None or value == "":
        return None
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    return [item.strip() for item in str(value).split(",") if item.strip()]


def _expected_days(value) -> dict[str, int] | None:
    if value is None or value == "":
        return None
    if isinstance(value, dict):
        raw = value
    else:
        try:
            raw = json.loads(str(value))
        except ValueError as exc:
            raise errors.BadRequest("expected_days 必须是 JSON 对象",
                                    code="E_BAD_REQUEST") from exc
    out: dict[str, int] = {}
    for month, days in raw.items():
        try:
            out[str(month)] = int(days)
        except (TypeError, ValueError) as exc:
            raise errors.BadRequest(f"expected_days 天数无效：{month}",
                                    code="E_BAD_REQUEST") from exc
        if out[str(month)] < 0:
            raise errors.BadRequest(f"expected_days 天数不能为负：{month}",
                                    code="E_BAD_REQUEST")
    return out


@router.post("/api/stations/dailies")
def api_import_dailies(payload: dict | None = None,
                       ctx: Ctx = Depends(get_ctx)) -> dict:
    body = payload or {}
    records = body.get("records")
    if records is None and "station" in body:
        records = [body]
    return _service(ctx).import_dailies(records or [])


@router.get("/api/stations/dailies")
def api_dailies(station: str | None = None, ctx: Ctx = Depends(get_ctx)) -> dict:
    return {"rows": _service(ctx).dailies(station)}


@router.post("/api/stations/merges")
def api_merge(payload: dict | None = None, ctx: Ctx = Depends(get_ctx)) -> dict:
    body = payload or {}
    for field in ("source", "target"):
        if not str(body.get(field) or "").strip():
            raise errors.BadRequest(f"{field} 不能为空", code="E_BAD_REQUEST")
    return _service(ctx).merge(
        source=str(body["source"]).strip(), target=str(body["target"]).strip(),
        expected_revision=_optional_int(body.get("expected_revision")),
        request_id=str(body.get("request_id") or "").strip() or None,
        note=str(body.get("note") or ""), defer=bool(body.get("defer")))


@router.post("/api/stations/merges/recover")
def api_recover(payload: dict | None = None,
                ctx: Ctx = Depends(get_ctx)) -> dict:
    payload = payload or {}
    switch_check = None
    if payload.get("fail_switch"):
        def switch_check(source: str, target: str) -> None:  # noqa: ANN001
            raise errors.Conflict("切换失败（演练）", code="E_SWITCH_FAILED",
                                  detail={"source": source, "target": target})
    return _service(ctx).recover(switch_check=switch_check)


@router.post("/api/stations/merges/{decision_id}/undo")
def api_undo(decision_id: str, payload: dict | None = None,
             ctx: Ctx = Depends(get_ctx)) -> dict:
    body = payload or {}
    return _service(ctx).undo(
        decision_id,
        expected_revision=_optional_int(body.get("expected_revision")),
        request_id=str(body.get("request_id") or "").strip() or None,
        note=str(body.get("note") or ""))


@router.get("/api/stations/merges")
def api_merges(ctx: Ctx = Depends(get_ctx)) -> dict:
    service = _service(ctx)
    return {"revision": service.repo.revision(), "merges": service.decisions()}


@router.get("/api/stations/readback")
def api_readback(stations: str | None = None, months: str | None = None,
                 expected_days: str | None = None,
                 ctx: Ctx = Depends(get_ctx)) -> dict:
    return _service(ctx).readback(
        stations=_string_list(stations), months=_string_list(months),
        expected_days=_expected_days(expected_days))


@router.get("/stations")
def page_stations(request: Request, stations: str | None = None,
                  months: str | None = None,
                  ctx: Ctx = Depends(get_ctx)):
    service = _service(ctx)
    requested = _string_list(stations)
    readback = service.readback(stations=requested, months=_string_list(months))
    merges = service.decisions()
    ledger_errors = []
    try:
        roots = hydro_stations.root_of(readback["roots"], merges)
    except ValueError as exc:
        roots = {}
        ledger_errors.append(str(exc))
    return render(request, "stations.html", page="stations", page_title="站号合并",
                  readback=readback, merges=merges, roots=roots,
                  ledger_errors=ledger_errors,
                  days_calendar={month: hydro_stations.month_days(month)
                                 for month in sorted({row["month"]
                                                      for row in readback["months"]})})
