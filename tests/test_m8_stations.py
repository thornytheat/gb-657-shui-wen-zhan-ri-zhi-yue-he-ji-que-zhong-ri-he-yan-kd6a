"""M8 站号合并：保留来源身份、按日裁决、撤销重算、恢复与并发。"""
from __future__ import annotations

import pytest

from app import errors
from app.modules.m5_months import stations as hydro
from app.modules.m8_stations import ledger, service
from app.modules.m8_stations.repository import Repository

BASE = "https://hydrocalendar.test"


def _daily(station, date, rainfall, version=1, source_ref="", received_at=None):
    row = {"station": station, "date": date, "rainfall_mm": rainfall,
           "version": version, "source_ref": source_ref}
    if received_at:
        row["received_at"] = received_at
    return row


def _import(client, *rows):
    response = client.post("/api/stations/dailies", json={"records": list(rows)})
    assert response.status_code == 200, response.text
    return response.json()


def _merge(client, source, target, request_id, expected_revision=None, **extra):
    body = {"source": source, "target": target, "request_id": request_id, **extra}
    if expected_revision is not None:
        body["expected_revision"] = expected_revision
    return client.post("/api/stations/merges", json=body)


def _readback(client, **params):
    response = client.get("/api/stations/readback", params=params)
    assert response.status_code == 200, response.text
    return response.json()


def _month(readback, root, month):
    return next(row for row in readback["months"]
                if row["root"] == root and row["month"] == month)


# ---------- 日值三态 ----------

def test_zero_rain_missing_and_blank_are_three_states(client):
    _import(client,
            _daily("A1", "2024-02-01", "0.0"),
            _daily("A1", "2024-02-02", "12.5"),
            _daily("A1", "2024-02-03", ""),
            _daily("A1", "2024-02-04", "未知"))
    readback = _readback(client)
    row = _month(readback, "A1", "2024-02")
    assert row["rainfall_mm"] == "12.5"
    assert row["measured_days"] == 2
    assert row["zero_rain_days"] == 1
    assert row["reported_missing_days"] == 2
    assert row["missing_days"] == 29 - 2
    assert row["state"] == "partial"


def test_invalid_rainfall_rejected_without_writing(client):
    response = client.post("/api/stations/dailies",
                           json={"records": [_daily("A1", "2024-02-30", "1.0")]})
    assert response.status_code == 422
    assert response.json()["error"] == "E_DAILY_INVALID"
    assert _readback(client)["accounts"]["source"] == 0


def test_partial_import_keeps_valid_rows(client):
    payload = _import(client, _daily("A1", "2024-02-01", "-1.0"),
                      _daily("A1", "2024-02-02", "1.0"))
    assert payload["invalid_count"] == 1
    assert payload["inserted"] == 1


def test_duplicate_retry_does_not_add_daily(client):
    row = _daily("A1", "2024-02-01", "4.0", source_ref="form-1")
    first = _import(client, row)
    second = _import(client, row)
    assert first["inserted"] == 1
    assert second["inserted"] == 0
    assert second["existing"] == 1
    assert _readback(client)["accounts"]["source"] == 1


def test_same_logical_record_with_new_received_at_is_not_duplicated(client):
    """record_id 不含接收时间：同一份日值换一个时间戳重发仍是同一条。"""
    first = _import(client, _daily("A1", "2024-02-01", "4.0",
                                   received_at="2024-03-01T00:00:00+00:00"))
    second = _import(client, _daily("A1", "2024-02-01", "4.0",
                                    received_at="2024-03-02T00:00:00+00:00"))
    assert first["inserted"] == 1
    assert second["inserted"] == 0
    assert _readback(client)["accounts"]["source"] == 1


# ---------- 合并与来源身份 ----------

def test_merge_keeps_source_identity_and_does_not_duplicate_rain(client):
    _import(client,
            _daily("A1", "2024-02-01", "0.0", source_ref="form-a"),
            _daily("A1", "2024-02-02", "10.0", source_ref="form-a"),
            _daily("B2", "2024-02-01", "0.0", source_ref="form-b"),
            _daily("B2", "2024-02-02", "10.0", source_ref="form-b"))
    before = _readback(client)
    assert _month(before, "A1", "2024-02")["rainfall_mm"] == "10.0"
    assert _month(before, "B2", "2024-02")["rainfall_mm"] == "10.0"

    merged = _merge(client, "A1", "B2", "req-merge-1")
    assert merged.status_code == 200, merged.text
    payload = merged.json()
    assert payload["status"] == "applied"
    assert payload["decision"]["source"] == "A1"
    assert payload["decision"]["target"] == "B2"

    readback = payload["readback"]
    assert [group["root"] for group in readback["groups"]] == ["B2"]
    month = _month(readback, "B2", "2024-02")
    # 同一天两边报同一个值：只算一次，绝不合出 20.0。
    assert month["rainfall_mm"] == "10.0"
    assert month["measured_days"] == 2
    assert month["zero_rain_days"] == 1
    # 归属按「谁的值被计入」：每天一条，未计入的那条仍留在同名重复里。
    assert sum(item["measured_days"] for item in month["by_source"]) == 2
    assert {item["station"] for item in readback["duplicates"]
            + readback["selected"]} == {"A1", "B2"}
    assert all(item["reason"] == "DUPLICATE_IDENTICAL"
               for item in readback["duplicates"])
    assert sum(float(row["rainfall_mm"]) for row in readback["selected"]) == 10.0
    assert readback["accounts"]["duplicates"] == 2
    assert readback["accounts"]["balanced"] is True
    assert readback["accounts"]["attribution_balanced"] is True


def test_merge_without_duplicate_days_sums_both_sources(client):
    _import(client, _daily("A1", "2024-02-01", "1.0"),
            _daily("B2", "2024-02-02", "2.0"))
    _merge(client, "A1", "B2", "req-merge-2")
    month = _month(_readback(client), "B2", "2024-02")
    assert month["rainfall_mm"] == "3.0"
    assert month["measured_days"] == 2
    assert {item["station"] for item in month["by_source"]} == {"A1", "B2"}


def test_cross_alias_version_wins_per_day(client):
    _import(client,
            _daily("A1", "2024-02-05", "7.0", version=1, source_ref="old"),
            _daily("B2", "2024-02-05", "9.0", version=3, source_ref="correction"))
    _merge(client, "A1", "B2", "req-merge-3")
    readback = _readback(client)
    month = _month(readback, "B2", "2024-02")
    assert month["rainfall_mm"] == "9.0"
    assert readback["duplicates"][0]["reason"] == "LOWER_VERSION"
    assert readback["duplicates"][0]["station"] == "A1"


def test_same_version_different_value_is_isolated_not_summed(client):
    _import(client,
            _daily("A1", "2024-02-06", "3.0", version=2),
            _daily("B2", "2024-02-06", "8.0", version=2),
            _daily("B2", "2024-02-07", "1.0", version=2))
    _merge(client, "A1", "B2", "req-merge-4")
    readback = _readback(client)
    month = _month(readback, "B2", "2024-02")
    assert month["rainfall_mm"] == "1.0"
    assert month["conflict_days"] == 1
    assert month["measured_days"] == 1
    assert month["missing_days"] == 29 - 1 - 1
    conflict = readback["conflicts"][0]
    assert conflict["reason"] == "VERSION_CONFLICT"
    assert conflict["values"] == ["3", "8"]
    assert {item["station"] for item in conflict["records"]} == {"A1", "B2"}
    assert readback["accounts"]["balanced"] is True


def test_missing_against_zero_is_not_summed_away(client):
    """同一天一条缺测、一条零雨：零雨是有效测量，缺测是 None，不能互相顶替。"""
    _import(client,
            _daily("A1", "2024-02-08", ""),
            _daily("B2", "2024-02-08", "0.0"))
    _merge(client, "A1", "B2", "req-merge-5")
    readback = _readback(client)
    month = _month(readback, "B2", "2024-02")
    assert month["measured_days"] == 1
    assert month["zero_rain_days"] == 1
    assert month["reported_missing_days"] == 0
    assert month["rainfall_mm"] == "0.0"


# ---------- 补报与更正 ----------

def test_backfill_during_merge_is_arbitrated_per_day(client):
    _import(client, _daily("A1", "2024-02-01", "2.0", version=1))
    _merge(client, "A1", "B2", "req-merge-6")
    _import(client, _daily("A1", "2024-02-09", "6.0", version=1,
                           source_ref="backfill",
                           received_at="2099-01-01T00:00:00+00:00"))
    readback = _readback(client)
    month = _month(readback, "B2", "2024-02")
    assert month["rainfall_mm"] == "8.0"
    assert month["measured_days"] == 2
    backfill = next(row for row in readback["selected"]
                    if row["date"] == "2024-02-09")
    assert backfill["station"] == "A1"
    assert "K_MERGE_BACKFILL" in backfill["tags"]


def test_correction_after_merge_uses_higher_version(client):
    _import(client, _daily("A1", "2024-02-03", "5.0", version=1))
    _merge(client, "A1", "B2", "req-merge-7")
    _import(client, _daily("B2", "2024-02-03", "6.5", version=2,
                           source_ref="correction",
                           received_at="2099-01-01T00:00:00+00:00"))
    readback = _readback(client)
    assert _month(readback, "B2", "2024-02")["rainfall_mm"] == "6.5"
    selected = next(row for row in readback["selected"]
                    if row["date"] == "2024-02-03")
    assert selected["version"] == 2
    assert "K_MERGE_CORRECTION" in selected["tags"]


def test_retry_does_not_add_rows_or_change_month(client):
    _import(client, _daily("A1", "2024-02-01", "2.0"))
    first = _merge(client, "A1", "B2", "req-merge-8").json()
    second = _merge(client, "A1", "B2", "req-merge-8").json()
    assert second["replayed"] is True
    assert second["decision"]["decision_id"] == first["decision"]["decision_id"]
    assert second["readback"]["months"] == first["readback"]["months"]
    assert len(_readback(client)["selected"]) == 1


# ---------- 撤销 ----------

def test_undo_recomputes_each_original_station(client):
    _import(client,
            _daily("A1", "2024-02-01", "1.0"),
            _daily("A1", "2024-02-02", "2.0"),
            _daily("B2", "2024-02-03", "9.0"))
    applied = _merge(client, "A1", "B2", "req-undo-1").json()
    merged_month = _month(applied["readback"], "B2", "2024-02")
    assert merged_month["rainfall_mm"] == "12.0"
    assert merged_month["measured_days"] == 3

    response = client.post(
        f"/api/stations/merges/{applied['decision']['decision_id']}/undo",
        json={"request_id": "req-undo-1-u"})
    assert response.status_code == 200, response.text
    undone = response.json()
    assert undone["decision"]["status"] == "undone"
    readback = undone["readback"]
    assert [group["root"] for group in readback["groups"]] == ["A1", "B2"]
    assert _month(readback, "A1", "2024-02")["rainfall_mm"] == "3.0"
    assert _month(readback, "B2", "2024-02")["rainfall_mm"] == "9.0"
    assert all(len(row["by_source"]) == 1 for row in readback["months"])


def test_undo_keeps_source_identity_after_restore(client):
    _import(client, _daily("A1", "2024-02-01", "1.0", source_ref="a-form"),
            _daily("B2", "2024-02-01", "2.0", source_ref="b-form"))
    applied = _merge(client, "A1", "B2", "req-undo-2").json()
    client.post(f"/api/stations/merges/{applied['decision']['decision_id']}/undo",
                json={"request_id": "req-undo-2-u"})
    readback = _readback(client)
    refs = {row["station"]: row["source_ref"] for row in readback["selected"]}
    assert refs == {"A1": "a-form", "B2": "b-form"}


def test_undo_is_idempotent_by_request_id(client):
    _import(client, _daily("A1", "2024-02-01", "1.0"))
    applied = _merge(client, "A1", "B2", "req-undo-3").json()
    url = f"/api/stations/merges/{applied['decision']['decision_id']}/undo"
    first = client.post(url, json={"request_id": "req-undo-3-u"})
    second = client.post(url, json={"request_id": "req-undo-3-u"})
    assert first.status_code == 200 and second.status_code == 200
    assert second.json()["replayed"] is True
    assert second.json()["revision"] == first.json()["revision"]


def test_undo_twice_with_other_request_is_rejected(client):
    _import(client, _daily("A1", "2024-02-01", "1.0"))
    applied = _merge(client, "A1", "B2", "req-undo-4").json()
    url = f"/api/stations/merges/{applied['decision']['decision_id']}/undo"
    assert client.post(url, json={"request_id": "u-1"}).status_code == 200
    again = client.post(url, json={"request_id": "u-2"})
    assert again.status_code == 409
    assert again.json()["error"] == "E_MERGE_NOT_APPLIED"


# ---------- 拒绝非法合并 ----------

def test_merge_self_is_rejected(client):
    response = _merge(client, "A1", "A1", "req-bad-1")
    assert response.status_code == 409
    assert response.json()["error"] == "E_MERGE_SELF"


def test_cycle_is_rejected(client):
    _import(client, _daily("A1", "2024-02-01", "1.0"))
    assert _merge(client, "A1", "B2", "req-cycle-1").status_code == 200
    response = _merge(client, "B2", "A1", "req-cycle-2")
    assert response.status_code == 409
    assert response.json()["error"] == "E_MERGE_CYCLE"


def test_merge_of_alias_source_is_rejected(client):
    _merge(client, "A1", "B2", "req-alias-1")
    response = _merge(client, "A1", "C3", "req-alias-2")
    assert response.status_code == 409
    assert response.json()["error"] == "E_MERGE_ALIAS"


def test_merging_same_group_is_rejected(client):
    _merge(client, "A1", "B2", "req-same-1")
    _merge(client, "C3", "B2", "req-same-2")
    response = _merge(client, "A1", "C3", "req-same-3")
    assert response.status_code == 409


def test_chained_merge_resolves_to_root(client):
    _import(client, _daily("A1", "2024-02-01", "1.0"),
            _daily("B2", "2024-02-02", "2.0"),
            _daily("C3", "2024-02-03", "4.0"))
    _merge(client, "A1", "B2", "req-chain-1")
    _merge(client, "B2", "C3", "req-chain-2")
    readback = _readback(client)
    groups = {group["root"]: group for group in readback["groups"]}
    assert list(groups) == ["C3"]
    assert groups["C3"]["stations"] == ["A1", "B2", "C3"]
    month = _month(readback, "C3", "2024-02")
    assert month["rainfall_mm"] == "7.0"
    assert {item["station"] for item in month["by_source"]} == {"A1", "B2", "C3"}


def test_undo_requires_latest_applied(client):
    _import(client, _daily("A1", "2024-02-01", "1.0"))
    first = _merge(client, "A1", "B2", "req-order-1").json()
    _merge(client, "B2", "C3", "req-order-2")
    response = client.post(
        f"/api/stations/merges/{first['decision']['decision_id']}/undo",
        json={"request_id": "req-order-1-u"})
    assert response.status_code == 409
    assert response.json()["error"] == "E_UNDO_OUT_OF_ORDER"


# ---------- 并发与过期 ----------

def test_stale_expected_revision_is_rejected(client):
    _import(client, _daily("A1", "2024-02-01", "1.0"))
    before = _readback(client)["revision"]
    assert _merge(client, "A1", "B2", "req-rev-1",
                  expected_revision=before).status_code == 200
    stale = _merge(client, "C3", "D4", "req-rev-2", expected_revision=before)
    assert stale.status_code == 409
    assert stale.json()["error"] == "E_STALE_REVISION"


def test_two_concurrent_merges_only_one_wins(client):
    _import(client, _daily("A1", "2024-02-01", "1.0"))
    revision = _readback(client)["revision"]
    first = _merge(client, "A1", "B2", "req-race-1", expected_revision=revision)
    second = _merge(client, "C3", "D4", "req-race-2", expected_revision=revision)
    assert first.status_code == 200
    assert second.status_code == 409
    assert second.json()["error"] == "E_STALE_REVISION"
    roots = [group["root"] for group in _readback(client)["groups"]]
    assert len(roots) == len([row for row in _readback(client)["groups"]])


def test_concurrent_undo_only_one_succeeds(client):
    _merge(client, "A1", "B2", "req-undo-race")
    decision_id = client.get("/api/stations/merges").json()["merges"][0]["decision_id"]
    revision = _readback(client)["revision"]
    url = f"/api/stations/merges/{decision_id}/undo"
    first = client.post(url, json={"request_id": "ru-1", "expected_revision": revision})
    second = client.post(url, json={"request_id": "ru-2", "expected_revision": revision})
    assert first.status_code == 200
    assert second.status_code == 409


# ---------- 恢复 ----------

def test_pending_decision_does_not_change_month_until_recovered(fixture_store):
    repo = Repository(fixture_store)
    hydro_service = service.Service(fixture_store)
    hydro_service.import_dailies([_daily("A1", "2024-02-01", "1.0"),
                                  _daily("B2", "2024-02-02", "2.0")])
    deferred = repo.create_merge(request_id="req-defer-1", source="A1", target="B2",
                                 expected_revision=None, defer=True)
    assert deferred["decision"]["status"] == "pending"
    before = hydro_service.readback()
    assert {row["root"] for row in before["months"]} == {"A1", "B2"}

    recovered = repo.recover()
    assert recovered["recovered"][0]["status"] == "applied"
    after = hydro_service.readback()
    assert {row["root"] for row in after["months"]} == {"B2"}
    assert _month(after, "B2", "2024-02")["rainfall_mm"] == "3.0"


def test_switch_failure_aborts_and_keeps_original_month(fixture_store):
    repo = Repository(fixture_store)
    hydro_service = service.Service(fixture_store)
    hydro_service.import_dailies([_daily("A1", "2024-02-01", "1.0"),
                                  _daily("B2", "2024-02-02", "2.0")])

    def explode(source, target):
        raise RuntimeError("storage torn")

    with pytest.raises(errors.Conflict) as caught:
        repo.create_merge(request_id="req-fail-1", source="A1", target="B2",
                          expected_revision=None, switch_check=explode)
    assert caught.value.code == "E_SWITCH_FAILED"
    decision = repo.decision(repo.decisions()[0]["decision_id"])
    assert decision["status"] == "aborted"
    readback = hydro_service.readback()
    assert {row["root"] for row in readback["months"]} == {"A1", "B2"}

    # 同一 request_id 重试：复用决定并落生效，不新增日值。
    before = len(hydro_service.readback()["selected"])
    retried = repo.create_merge(request_id="req-fail-1", source="A1", target="B2",
                                expected_revision=None)
    assert retried["decision"]["status"] == "applied"
    assert len(hydro_service.readback()["selected"]) == before
    assert len(hydro_service.readback()["months"]) == 1


def test_recover_is_idempotent(fixture_store):
    repo = Repository(fixture_store)
    repo.create_merge(request_id="req-idem-1", source="A1", target="B2",
                      expected_revision=None, defer=True)
    first = repo.recover()
    second = repo.recover()
    assert first["recovered"][0]["status"] == "applied"
    assert second["recovered"] == []
    assert len(repo.decisions()) == 1


def test_recover_endpoint_reports_state(client):
    _merge(client, "A1", "B2", "req-recover-api", defer=True)
    response = client.post("/api/stations/merges/recover", json={})
    assert response.status_code == 200
    assert response.json()["recovered"][0]["status"] == "applied"


def test_recover_marks_failed_switch_aborted_without_touching_month(client):
    """恢复时切换失败：决定落 aborted 而不是永远 pending，月量仍读原站。"""
    _import(client, _daily("A1", "2024-02-01", "1.0"),
            _daily("B2", "2024-02-02", "2.0"))
    _merge(client, "A1", "B2", "req-recover-fail", defer=True)
    failed = client.post("/api/stations/merges/recover", json={"fail_switch": True})
    assert failed.status_code == 200
    recovered = failed.json()["recovered"][0]
    assert recovered["status"] == "aborted"
    assert recovered["abort_code"] == "E_SWITCH_FAILED"
    readback = client.get("/api/stations/readback").json()
    assert {row["root"] for row in readback["months"]} == {"A1", "B2"}

    # 修好后用同一 request_id 重试：复用决定，落生效，日值行数不变。
    before = len(readback["selected"])
    retried = _merge(client, "A1", "B2", "req-recover-fail")
    assert retried.status_code == 200
    assert retried.json()["status"] == "applied"
    after = client.get("/api/stations/readback").json()
    assert len(after["selected"]) == before
    assert [row["root"] for row in after["months"]] == ["B2"]


# ---------- 既有功能不受影响 ----------

def test_existing_batch_endpoints_untouched(client):
    first = client.post("/api/runs", json={"sample": "readings_dirty.csv"}).json()
    _import(client, _daily("A1", "2024-02-01", "1.0"))
    _merge(client, "A1", "B2", "req-isolation")
    second = client.post("/api/runs", json={"sample": "readings_dirty.csv"}).json()
    assert second["batch_id"] == first["batch_id"]
    assert second["counts"] == first["counts"]
    assert client.get("/api/reconcile").json()["all_balanced"] is True


def test_stations_page_renders(client):
    _import(client, _daily("A1", "2024-02-01", "1.0"),
            _daily("B2", "2024-02-02", "2.0"))
    _merge(client, "A1", "B2", "req-page")
    page = client.get("/stations")
    assert page.status_code == 200
    assert "站号合并" in page.text
    assert "B2" in page.text


def test_readback_scoped_to_requested_stations(client):
    _import(client, _daily("A1", "2024-02-01", "1.0"),
            _daily("B2", "2024-02-02", "2.0"),
            _daily("C3", "2024-02-03", "4.0"))
    scoped = _readback(client, stations="A1")
    assert scoped["roots"] == ["A1"]
    assert [row["root"] for row in scoped["months"]] == ["A1"]
    assert scoped["accounts"]["source"] == 1


def test_readback_expected_days_can_be_overridden(client):
    _import(client, _daily("A1", "2024-02-01", "1.0"))
    row = _month(_readback(client, expected_days='{"2024-02": 1}'), "A1", "2024-02")
    assert row["missing_days"] == 0
    assert row["state"] == "complete"
