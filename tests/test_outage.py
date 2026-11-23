from __future__ import annotations

from datetime import date, timedelta

import pytest


RETROFIT = date(2026, 3, 10)
BEFORE = [RETROFIT - timedelta(days=i) for i in range(1, 8)]  # 03-03..03-09
AFTER = [RETROFIT + timedelta(days=i) for i in range(1, 8)]  # 03-11..03-17
WINDOW = BEFORE + AFTER


def _readings(values: dict[date | str, float]) -> dict:
    def iso(key):
        return key.isoformat() if isinstance(key, date) else key

    return {
        "readings": [
            {"date": iso(d), "kwh_per_lamp": v}
            for d, v in sorted(values.items(), key=lambda item: iso(item[0]))
        ]
    }


def _flat(days, value):
    return {d: value for d in days}


@pytest.fixture()
def client(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from app.config import Settings
    from app.main import create_app

    db_path = tmp_path / "outage.db"
    monkeypatch.setenv("LIGHTS_DB_PATH", str(db_path))
    with TestClient(create_app(Settings(db_path=str(db_path)))) as c:
        yield c


@pytest.fixture()
def seeded(client):
    """Target + two controls; issue one report and return ids/report."""

    ids = {}
    for name, retrofit in [
        ("T-目标", RETROFIT),
        ("C1-对照", None),
        ("C2-对照", None),
    ]:
        resp = client.post(
            "/api/segments",
            json={
                "name": name,
                "retrofit_date": retrofit.isoformat() if retrofit else None,
            },
        )
        assert resp.status_code == 201, resp.text
        ids[name] = resp.json()["id"]

    values = {**_flat(BEFORE, 10.0), **_flat(AFTER, 8.0)}
    client.put(f"/api/segments/{ids['T-目标']}/readings", json=_readings(values))
    for name in ("C1-对照", "C2-对照"):
        cvals = {**_flat(BEFORE, 10.2), **_flat(AFTER, 9.7)}
        client.put(f"/api/segments/{ids[name]}/readings", json=_readings(cvals))

    report = client.post(
        "/api/reports", params={"segment_id": ids["T-目标"]}
    ).json()["report"]
    return {"ids": ids, "report": report}


def _order(client, order_no, segment_id, first, last):
    resp = client.post(
        "/api/outage-orders",
        json={
            "order_no": order_no,
            "segment_id": segment_id,
            "first_date": first.isoformat(),
            "last_date": last.isoformat(),
        },
    )
    return resp


def _review(client, report_id):
    resp = client.get(f"/api/reports/{report_id}/outage-review")
    assert resp.status_code == 200, resp.text
    return resp.json()


# ---------------------------------------------------------------------------
# Registration and matching
# ---------------------------------------------------------------------------


def test_register_order_matches_only_used_report_days(client, seeded):
    ids, report = seeded["ids"], seeded["report"]
    # Order spans 03-08..03-12: report used 03-08,03-09 (before) and 03-11,
    # 03-12 (after); 03-10 is the skipped retrofit day, never used.
    resp = _order(
        client, "WO-1", ids["T-目标"], date(2026, 3, 8), date(2026, 3, 12)
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["changed"] is True
    assert body["order"]["status"] == "active"
    assert body["order"]["revision"] == 1
    assert body["order"]["segment_name"] == "T-目标"

    state = _review(client, report["id"])
    assert state["status"] == "pending"
    assert [o["order_no"] for o in state["matched_orders"]] == ["WO-1"]
    match = state["matched_orders"][0]
    assert match["role"] == "target"
    assert [d["date"] for d in match["matched_dates"]] == [
        "2026-03-08",
        "2026-03-09",
        "2026-03-11",
        "2026-03-12",
    ]
    # The skipped retrofit day is never reported as affected.
    assert "2026-03-10" not in [d["date"] for d in match["matched_dates"]]


def test_order_outside_used_days_does_not_match(client, seeded):
    ids, report = seeded["ids"], seeded["report"]
    _order(client, "WO-OUT", ids["T-目标"], date(2026, 4, 1), date(2026, 4, 3))
    state = _review(client, report["id"])
    assert state["matched_orders"] == []
    assert state["impacted_days"] == []


def test_control_order_matches_with_control_role(client, seeded):
    ids, report = seeded["ids"], seeded["report"]
    _order(client, "WO-C", ids["C1-对照"], date(2026, 3, 13), date(2026, 3, 13))
    state = _review(client, report["id"])
    assert len(state["matched_orders"]) == 1
    assert state["matched_orders"][0]["role"] == "control"
    assert state["matched_segments"] == [
        {
            "segment_id": ids["C1-对照"],
            "segment_name": "C1-对照",
            "role": "control",
            "order_nos": ["WO-C"],
        }
    ]


def test_matching_uses_snapshot_dates_not_current_readings(client, seeded):
    """Changing/deleting readings after issuance must not change matching."""

    ids, report = seeded["ids"], seeded["report"]
    # Delete 03-15 from the target.  The report still used it.
    client.delete(f"/api/segments/{ids['T-目标']}/readings/2026-03-15")
    # Add a reading the report never used.
    client.put(
        f"/api/segments/{ids['T-目标']}/readings",
        json=_readings({"2026-05-01": 1.0}),
    )
    _order(client, "WO-USED", ids["T-目标"], date(2026, 3, 15), date(2026, 3, 15))
    _order(
        client, "WO-UNUSED", ids["T-目标"], date(2026, 5, 1), date(2026, 5, 1)
    )
    state = _review(client, report["id"])
    assert [o["order_no"] for o in state["matched_orders"]] == ["WO-USED"]


def test_lookup_orders_by_report_number(client, seeded):
    ids, report = seeded["ids"], seeded["report"]
    _order(client, "WO-MATCH", ids["T-目标"], date(2026, 3, 11), date(2026, 3, 11))
    _order(client, "WO-NOMATCH", ids["T-目标"], date(2026, 5, 1), date(2026, 5, 1))
    _order(client, "WO-OTHERSEG", ids["C1-对照"], date(2026, 5, 2), date(2026, 5, 2))
    rows = client.get(
        "/api/outage-orders", params={"report_id": report["id"]}
    ).json()
    assert [r["order_no"] for r in rows] == ["WO-MATCH"]


# ---------------------------------------------------------------------------
# Per-day savings direction under the original formula
# ---------------------------------------------------------------------------


def test_per_day_effect_direction(client, seeded):
    ids, report = seeded["ids"], seeded["report"]
    # One day in each of the four (role, phase) quadrants.
    _order(client, "T-BEFORE", ids["T-目标"], date(2026, 3, 5), date(2026, 3, 5))
    _order(client, "T-AFTER", ids["T-目标"], date(2026, 3, 14), date(2026, 3, 14))
    _order(client, "C-BEFORE", ids["C1-对照"], date(2026, 3, 6), date(2026, 3, 6))
    _order(client, "C-AFTER", ids["C2-对照"], date(2026, 3, 13), date(2026, 3, 13))

    state = _review(client, report["id"])
    by_order = {d["order_no"]: d for d in state["impacted_days"]}
    # Outage lowers the reading.  Removing that depression:
    #   target before up  -> net up   => reported net was depressed
    #   target after up   -> net down => reported net was inflated
    #   control before up -> net down => inflated
    #   control after up  -> net up   => depressed
    assert by_order["T-BEFORE"]["effect_on_net_savings"] == "depressed"
    assert by_order["T-AFTER"]["effect_on_net_savings"] == "inflated"
    assert by_order["C-BEFORE"]["effect_on_net_savings"] == "inflated"
    assert by_order["C-AFTER"]["effect_on_net_savings"] == "depressed"

    assert by_order["T-BEFORE"]["phase"] == "before"
    assert by_order["T-BEFORE"]["day_index"] == 3
    assert by_order["T-BEFORE"]["kwh_per_lamp"] == 10.0
    assert by_order["T-BEFORE"]["phase_average"] == 10.0
    assert by_order["T-AFTER"]["phase"] == "after"
    assert by_order["T-AFTER"]["day_index"] == 4


# ---------------------------------------------------------------------------
# Confirmation semantics
# ---------------------------------------------------------------------------


def test_confirm_current_evidence_then_repeat_is_not_recorded_twice(client, seeded):
    ids, report = seeded["ids"], seeded["report"]
    _order(client, "WO-1", ids["T-目标"], date(2026, 3, 11), date(2026, 3, 11))
    state = _review(client, report["id"])
    version = state["evidence_version"]

    first = client.post(
        f"/api/reports/{report['id']}/outage-review/confirm",
        json={"reviewer": "验收员甲", "evidence_version": version},
    )
    assert first.status_code == 200, first.text
    body = first.json()
    assert body["status"] == "confirmed"
    assert body["changed"] is True
    assert body["new_confirmation"] is True
    assert body["confirmed_by"] == "验收员甲"

    second = client.post(
        f"/api/reports/{report['id']}/outage-review/confirm",
        json={"reviewer": "验收员乙", "evidence_version": version},
    )
    assert second.status_code == 200
    again = second.json()
    assert again["status"] == "confirmed"
    assert again["changed"] is False
    assert again["new_confirmation"] is False
    # The original confirmation stands; no second history row.
    assert again["confirmed_by"] == "验收员甲"

    state = _review(client, report["id"])
    assert state["status"] == "confirmed"
    assert len(state["confirmation_history"]) == 1
    assert state["confirmation_history"][0]["evidence_version"] == version


def test_confirm_rejects_changed_evidence(client, seeded):
    ids, report = seeded["ids"], seeded["report"]
    _order(client, "WO-1", ids["T-目标"], date(2026, 3, 11), date(2026, 3, 11))
    stale_version = _review(client, report["id"])["evidence_version"]

    # Evidence changes between view and submit: shift the order by a day.
    client.patch(
        "/api/outage-orders/1",
        json={"first_date": "2026-03-12", "last_date": "2026-03-12"},
    )
    resp = client.post(
        f"/api/reports/{report['id']}/outage-review/confirm",
        json={"reviewer": "验收员", "evidence_version": stale_version},
    )
    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert "evidence changed" in detail
    # Nothing was recorded.
    assert _review(client, report["id"])["status"] == "pending"


def test_order_change_invalidates_confirmation_even_when_changed_back(client, seeded):
    ids, report = seeded["ids"], seeded["report"]
    _order(client, "WO-1", ids["T-目标"], date(2026, 3, 11), date(2026, 3, 11))
    version = _review(client, report["id"])["evidence_version"]
    client.post(
        f"/api/reports/{report['id']}/outage-review/confirm",
        json={"reviewer": "验收员", "evidence_version": version},
    )
    assert _review(client, report["id"])["status"] == "confirmed"

    # Reschedule away, then back to the exact original values.
    first = client.patch(
        "/api/outage-orders/1",
        json={"first_date": "2026-03-12", "last_date": "2026-03-12"},
    ).json()
    assert first["changed"] is True
    assert first["reviews_marked_pending"] == [report["id"]]
    pending = _review(client, report["id"])
    assert pending["status"] == "pending"
    assert pending["invalidated_reason"]["type"] == "outage_order_changed"

    back = client.patch(
        "/api/outage-orders/1",
        json={"first_date": "2026-03-11", "last_date": "2026-03-11"},
    ).json()
    assert back["order"]["revision"] == 3
    after = _review(client, report["id"])
    # Back to the same dates, but it must NOT silently re-confirm.
    assert after["status"] == "pending"
    assert after["evidence_version"] != version
    assert len(after["confirmation_history"]) == 1
    # The old confirmation is preserved with its evidence for lookup.
    assert after["confirmation_history"][0]["evidence_version"] == version


def test_withdraw_order_invalidates_and_unmatches(client, seeded):
    ids, report = seeded["ids"], seeded["report"]
    created = _order(
        client, "WO-1", ids["T-目标"], date(2026, 3, 11), date(2026, 3, 11)
    ).json()
    version = _review(client, report["id"])["evidence_version"]
    client.post(
        f"/api/reports/{report['id']}/outage-review/confirm",
        json={"reviewer": "验收员", "evidence_version": version},
    )

    resp = client.post(f"/api/outage-orders/{created['order']['id']}/withdraw")
    assert resp.status_code == 200
    body = resp.json()
    assert body["order"]["status"] == "withdrawn"
    assert body["reviews_marked_pending"] == [report["id"]]

    state = _review(client, report["id"])
    assert state["status"] == "pending"
    assert state["matched_orders"] == []
    assert state["invalidated_reason"]["type"] == "outage_order_withdrawn"

    # A withdrawn order cannot be rescheduled.
    assert client.patch(
        "/api/outage-orders/1", json={"first_date": "2026-03-12"}
    ).status_code == 409


def test_adding_order_invalidates_existing_confirmation(client, seeded):
    ids, report = seeded["ids"], seeded["report"]
    # Confirm the empty-evidence review first.
    empty = _review(client, report["id"])
    assert empty["matched_orders"] == []
    client.post(
        f"/api/reports/{report['id']}/outage-review/confirm",
        json={"reviewer": "验收员", "evidence_version": empty["evidence_version"]},
    )
    assert _review(client, report["id"])["status"] == "confirmed"

    _order(client, "WO-LATE", ids["T-目标"], date(2026, 3, 11), date(2026, 3, 11))
    state = _review(client, report["id"])
    assert state["status"] == "pending"
    assert [o["order_no"] for o in state["matched_orders"]] == ["WO-LATE"]


# ---------------------------------------------------------------------------
# Isolation: no reading re-reads, no report recalculation, no history scans
# ---------------------------------------------------------------------------


def test_order_changes_never_read_daily_readings_or_touch_reports(client, seeded, monkeypatch):
    from app import service

    ids, report = seeded["ids"], seeded["report"]
    _order(client, "WO-1", ids["T-目标"], date(2026, 3, 11), date(2026, 3, 11))

    calls: list[str] = []
    original_for_dates = service._readings_for_dates
    original_all = service._all_readings

    def spy_for_dates(conn, segment_id, dates):
        calls.append(f"for_dates:{segment_id}")
        return original_for_dates(conn, segment_id, dates)

    def spy_all(conn, segment_id):
        calls.append(f"all:{segment_id}")
        return original_all(conn, segment_id)

    monkeypatch.setattr(service, "_readings_for_dates", spy_for_dates)
    monkeypatch.setattr(service, "_all_readings", spy_all)

    # Review read, order mutation, review confirmation: none may re-read
    # daily readings.
    state = _review(client, report["id"])
    client.patch(
        "/api/outage-orders/1",
        json={"first_date": "2026-03-12", "last_date": "2026-03-12"},
    )
    client.post(
        f"/api/reports/{report['id']}/outage-review/confirm",
        json={"reviewer": "验收员", "evidence_version": _review(client, report["id"])["evidence_version"]},
    )
    assert calls == []

    # The report itself is untouched: same numbers, still `issued`.
    detail = client.get(f"/api/reports/{report['id']}").json()
    assert detail["net_savings_per_lamp_per_day"] == report["net_savings_per_lamp_per_day"]
    assert detail["status"] == "issued"
    assert state["recomputed_report"] is False
    assert state["review_readings"] is False


def test_order_change_uses_index_not_full_report_scan(client, seeded, monkeypatch):
    """Candidate lookup must go through report_usage, not SELECT * FROM reports."""

    from app import outage as outage_module

    ids = seeded["ids"]
    _order(client, "WO-1", ids["T-目标"], date(2026, 3, 11), date(2026, 3, 11))

    statements: list[str] = []
    original_candidates = outage_module._candidate_reports

    def spy(conn, segment_id, first, last):
        conn.set_trace_callback(statements.append)
        try:
            return original_candidates(conn, segment_id, first, last)
        finally:
            conn.set_trace_callback(None)

    monkeypatch.setattr(outage_module, "_candidate_reports", spy)
    client.patch(
        "/api/outage-orders/1",
        json={"first_date": "2026-03-12", "last_date": "2026-03-12"},
    )
    assert any("FROM report_usage" in s for s in statements)
    assert not any("FROM reports" in s and "WHERE id =" not in s for s in statements)


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def test_order_validation_and_conflicts(client, seeded):
    ids = seeded["ids"]
    assert _order(
        client, "BAD", ids["T-目标"], date(2026, 3, 12), date(2026, 3, 11)
    ).status_code == 422
    assert _order(
        client, "X", 9999, date(2026, 3, 11), date(2026, 3, 11)
    ).status_code == 404
    _order(client, "DUP", ids["T-目标"], date(2026, 3, 11), date(2026, 3, 11))
    assert _order(
        client, "DUP", ids["T-目标"], date(2026, 3, 12), date(2026, 3, 12)
    ).status_code == 409
    # Patch with nothing to change is a validation error.
    assert client.patch("/api/outage-orders/1", json={}).status_code == 422
    # Review of missing report.
    assert client.get("/api/reports/9999/outage-review").status_code == 404


def test_confirm_requires_matching_version_and_reviewer(client, seeded):
    ids, report = seeded["ids"], seeded["report"]
    _order(client, "WO-1", ids["T-目标"], date(2026, 3, 11), date(2026, 3, 11))
    resp = client.post(
        f"/api/reports/{report['id']}/outage-review/confirm",
        json={"reviewer": "验收员", "evidence_version": "not-the-version"},
    )
    assert resp.status_code == 409
    assert client.post(
        f"/api/reports/{report['id']}/outage-review/confirm",
        json={"reviewer": "   ", "evidence_version": "x"},
    ).status_code == 422
