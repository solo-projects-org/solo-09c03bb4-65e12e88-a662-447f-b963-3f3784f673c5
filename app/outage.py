"""Outage (power-cut) re-check for issued reports.

Design notes
------------
* An outage work order names a segment and an inclusive first/last day.  It is
  *not* bound to a report: the same order may bear on many reports.
* A report's review matches an active order only when the order's segment is
  the report's target or one of its controls **and** the order's day range
  overlaps the daily readings that report actually used.  "Actually used" is
  read from the immutable report snapshot (mirrored in ``report_usage``), never
  from the current ``readings`` table.
* The per-day verdict follows the original accounting formula.  Lowering one
  reading by ``delta`` changes net savings by ``coefficient * -delta`` where the
  coefficient is +1/7 for target-before, -1/7 for target-after, -1/(7N) for
  control-before and +1/(7N) for control-after.  A day is therefore reported as
  inflating or depressing the result if an outage lowered that reading.
* A confirmation pins the exact evidence hash the reviewer saw.  Confirming a
  changed evidence version is rejected.  Re-confirming the *current* version
  adds no second history row.
* Changing/withdrawing an order moves every confirmed review it touches back to
  ``pending`` even if the order is later changed back.  Candidate reports are
  found through ``report_usage`` indexes, so changing an order never scans the
  report table.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import date

from .db import Database, utcnow_iso
from .models import (
    OutageOrderCreate,
    OutageOrderOut,
    OutageOrderPatch,
)
from .service import ApiError, _window_from_row

# d(net savings) / d(reading) sign per (role, phase).
_POSITIVE_SENSITIVITY = {
    ("target", "before"),
    ("control", "after"),
}


# ---------------------------------------------------------------------------
# Reading snapshots (never the live readings table)
# ---------------------------------------------------------------------------


def _report_row(conn: sqlite3.Connection, report_id: int) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM reports WHERE id = ?", (report_id,)).fetchone()
    if row is None:
        raise ApiError(404, f"report {report_id} not found")
    return row


def _used_days(snapshot: dict) -> dict[int, dict]:
    """Segments/readings a report actually used, keyed by segment id."""

    used: dict[int, dict] = {}
    target = snapshot["target"]
    used[target["id"]] = {
        "role": "target",
        "name": target["name"],
        "readings": target["readings"],
        "before_avg": target["before_avg"],
        "after_avg": target["after_avg"],
    }
    for control in snapshot.get("controls", []):
        used[control["id"]] = {
            "role": "control",
            "name": control["name"],
            "readings": control["readings"],
            "before_avg": control["before_avg"],
            "after_avg": control["after_avg"],
        }
    return used


def _phase(window: dict[str, date], day: date) -> str:
    if window["before_start"] <= day <= window["before_end"]:
        return "before"
    return "after"


def _day_index(window: dict[str, date], day: date, phase: str) -> int:
    start = window["before_start"] if phase == "before" else window["after_start"]
    return (day - start).days + 1


def _effect_on_net_savings(role: str, phase: str) -> str:
    """Direction net savings moves when an outage lowers this day's reading."""

    return "depressed" if (role, phase) in _POSITIVE_SENSITIVITY else "inflated"


def _order_rows_for_report(
    conn: sqlite3.Connection, used: dict[int, dict]
) -> list[sqlite3.Row]:
    if not used:
        return []
    placeholders = ",".join("?" for _ in used)
    return conn.execute(
        "SELECT o.*, s.name AS segment_name FROM outage_orders o "
        "JOIN segments s ON s.id = o.segment_id "
        "WHERE o.status = 'active' AND o.segment_id IN "
        f"({placeholders}) ORDER BY o.id",
        tuple(used),
    ).fetchall()


def build_evidence(
    conn: sqlite3.Connection, report_id: int
) -> tuple[dict, str]:
    """Return (evidence dict, stable evidence version hash).

    Reads the report snapshot, active outage orders and the report's review
    epoch only.  The epoch makes the version monotonic: rescheduling an order
    and then restoring the original dates (or withdrawing it) yields a
    different version, so a stale confirmation can never match again.
    """

    report = _report_row(conn, report_id)
    snapshot = json.loads(report["snapshot_json"])
    window = _window_from_row(report)
    used = _used_days(snapshot)

    matched_orders: list[dict] = []
    impacted_days: list[dict] = []
    touched_segments: dict[int, dict] = {}

    for order in _order_rows_for_report(conn, used):
        segment_id = order["segment_id"]
        segment = used[segment_id]
        first = date.fromisoformat(order["first_date"])
        last = date.fromisoformat(order["last_date"])
        matched = sorted(
            d
            for d in segment["readings"]
            if first <= date.fromisoformat(d) <= last
        )
        if not matched:
            continue

        role = segment["role"]
        matched_dates = []
        for raw in matched:
            day = date.fromisoformat(raw)
            phase = _phase(window, day)
            matched_dates.append(
                {
                    "date": raw,
                    "phase": phase,
                    "role": role,
                }
            )
            impacted_days.append(
                {
                    "date": raw,
                    "phase": phase,
                    "role": role,
                    "segment_id": segment_id,
                    "segment_name": segment["name"],
                    "order_no": order["order_no"],
                    "order_id": order["id"],
                    "kwh_per_lamp": segment["readings"][raw],
                    "phase_average": segment[f"{phase}_avg"],
                    "day_index": _day_index(window, day, phase),
                    "effect_on_net_savings": _effect_on_net_savings(role, phase),
                }
            )

        touched_segments[segment_id] = {
            "segment_id": segment_id,
            "segment_name": segment["name"],
            "role": role,
            "order_nos": [],
        }
        touched_segments[segment_id]["order_nos"].append(order["order_no"])

        matched_orders.append(
            {
                "order_id": order["id"],
                "order_no": order["order_no"],
                "segment_id": segment_id,
                "segment_name": segment["name"],
                "role": role,
                "first_date": order["first_date"],
                "last_date": order["last_date"],
                "status": order["status"],
                "revision": order["revision"],
                "matched_dates": matched_dates,
            }
        )

    impacted_days.sort(
        key=lambda d: (d["date"], d["segment_id"], d["order_id"])
    )
    matched_segments = sorted(
        touched_segments.values(), key=lambda s: (s["role"], s["segment_id"])
    )

    review = conn.execute(
        "SELECT epoch FROM outage_reviews WHERE report_id = ?", (report_id,)
    ).fetchone()
    epoch = review["epoch"] if review else 0

    evidence = {
        "report_id": report_id,
        "epoch": epoch,
        "matched_orders": matched_orders,
        "impacted_days": impacted_days,
        "matched_segments": matched_segments,
        # If an order is edited and then changed back, its revision still
        # differs, so the old confirmation cannot be silently reused.
        "order_versions": [
            {"order_id": o["order_id"], "revision": o["revision"]}
            for o in matched_orders
        ],
    }
    version = hashlib.sha256(
        json.dumps(
            evidence,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
    ).hexdigest()
    return evidence, version


# ---------------------------------------------------------------------------
# Review invalidation (indexed by report_usage)
# ---------------------------------------------------------------------------


def _candidate_reports(
    conn: sqlite3.Connection,
    segment_id: int,
    first: date,
    last: date,
) -> set[int]:
    rows = conn.execute(
        "SELECT DISTINCT report_id FROM report_usage "
        "WHERE segment_id = ? AND reading_date BETWEEN ? AND ?",
        (segment_id, first.isoformat(), last.isoformat()),
    ).fetchall()
    return {row["report_id"] for row in rows}


def _apply_order_change(
    conn: sqlite3.Connection, report_ids: set[int], reason: dict
) -> list[int]:
    """Bump evidence epoch and move confirmed reviews back to pending.

    Called for every report whose used days the changed order could touch.
    The epoch bump is unconditional so an order edit followed by an undo still
    yields a fresh evidence version (a stale confirmation can never match).
    """

    now = utcnow_iso()
    marked: list[int] = []
    for report_id in sorted(report_ids):
        row = conn.execute(
            "SELECT status FROM outage_reviews WHERE report_id = ?",
            (report_id,),
        ).fetchone()
        if row is None:
            conn.execute(
                "INSERT INTO outage_reviews (report_id, status, epoch, created_at, "
                "updated_at) VALUES (?, 'pending', 1, ?, ?)",
                (report_id, now, now),
            )
            continue
        conn.execute(
            "UPDATE outage_reviews SET epoch = epoch + 1, updated_at = ? "
            "WHERE report_id = ?",
            (now, report_id),
        )
        if row["status"] != "confirmed":
            continue
        conn.execute(
            "UPDATE outage_reviews SET status = 'pending', invalidated_at = ?, "
            "invalidated_reason_json = ?, updated_at = ? WHERE report_id = ?",
            (now, json.dumps(reason, ensure_ascii=False), now, report_id),
        )
        marked.append(report_id)
    return marked


# ---------------------------------------------------------------------------
# Orders
# ---------------------------------------------------------------------------


def _order_out(conn: sqlite3.Connection, order_id: int) -> dict:
    row = conn.execute(
        "SELECT o.*, s.name AS segment_name FROM outage_orders o "
        "JOIN segments s ON s.id = o.segment_id WHERE o.id = ?",
        (order_id,),
    ).fetchone()
    if row is None:
        raise ApiError(404, f"outage order {order_id} not found")
    return {
        "id": row["id"],
        "order_no": row["order_no"],
        "segment_id": row["segment_id"],
        "segment_name": row["segment_name"],
        "first_date": date.fromisoformat(row["first_date"]),
        "last_date": date.fromisoformat(row["last_date"]),
        "status": row["status"],
        "revision": row["revision"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "withdrawn_at": row["withdrawn_at"],
    }


def _require_segment(conn: sqlite3.Connection, segment_id: int) -> None:
    row = conn.execute("SELECT 1 FROM segments WHERE id = ?", (segment_id,)).fetchone()
    if row is None:
        raise ApiError(404, f"segment {segment_id} not found")


def create_order(db: Database, payload: OutageOrderCreate) -> dict:
    now = utcnow_iso()
    with db.write() as conn:
        _require_segment(conn, payload.segment_id)
        try:
            cur = conn.execute(
                "INSERT INTO outage_orders ("
                "order_no, segment_id, first_date, last_date, status, revision, "
                "created_at, updated_at"
                ") VALUES (?, ?, ?, ?, 'active', 1, ?, ?)",
                (
                    payload.order_no,
                    payload.segment_id,
                    payload.first_date.isoformat(),
                    payload.last_date.isoformat(),
                    now,
                    now,
                ),
            )
        except sqlite3.IntegrityError:
            raise ApiError(409, f"outage order {payload.order_no!r} already exists")
        order_id = int(cur.lastrowid)
        candidates = _candidate_reports(
            conn, payload.segment_id, payload.first_date, payload.last_date
        )
        marked = _apply_order_change(
            conn,
            candidates,
            {
                "type": "outage_order_added",
                "order_id": order_id,
                "order_no": payload.order_no,
                "detected_at": now,
            },
        )
        return {
            "order": OutageOrderOut(**_order_out(conn, order_id)),
            "changed": True,
            "previous_revision": None,
            "reviews_marked_pending": marked,
        }


def patch_order(db: Database, order_id: int, payload: OutageOrderPatch) -> dict:
    now = utcnow_iso()
    with db.write() as conn:
        row = conn.execute(
            "SELECT * FROM outage_orders WHERE id = ?", (order_id,)
        ).fetchone()
        if row is None:
            raise ApiError(404, f"outage order {order_id} not found")
        if row["status"] == "withdrawn":
            raise ApiError(409, "withdrawn orders cannot be changed")

        segment_id = payload.segment_id or row["segment_id"]
        first = payload.first_date or date.fromisoformat(row["first_date"])
        last = payload.last_date or date.fromisoformat(row["last_date"])
        if last < first:
            raise ApiError(422, "last_date must not be earlier than first_date")
        if payload.segment_id is not None:
            _require_segment(conn, segment_id)

        unchanged = (
            segment_id == row["segment_id"]
            and first.isoformat() == row["first_date"]
            and last.isoformat() == row["last_date"]
        )
        if unchanged:
            return {
                "order": OutageOrderOut(**_order_out(conn, order_id)),
                "changed": False,
                "previous_revision": row["revision"],
                "reviews_marked_pending": [],
            }

        conn.execute(
            "UPDATE outage_orders SET segment_id = ?, first_date = ?, last_date = ?, "
            "revision = revision + 1, updated_at = ? WHERE id = ?",
            (segment_id, first.isoformat(), last.isoformat(), now, order_id),
        )
        # Both the old and the new footprint can touch reports: rescheduling
        # away from a report still changes that report's evidence.
        candidates = _candidate_reports(
            conn, row["segment_id"], date.fromisoformat(row["first_date"]),
            date.fromisoformat(row["last_date"]),
        ) | _candidate_reports(conn, segment_id, first, last)
        reason = {
            "type": "outage_order_changed",
            "order_id": order_id,
            "order_no": row["order_no"],
            "old": {
                "segment_id": row["segment_id"],
                "first_date": row["first_date"],
                "last_date": row["last_date"],
            },
            "new": {
                "segment_id": segment_id,
                "first_date": first.isoformat(),
                "last_date": last.isoformat(),
            },
            "detected_at": now,
        }
        marked = _apply_order_change(conn, candidates, reason)
        return {
            "order": OutageOrderOut(**_order_out(conn, order_id)),
            "changed": True,
            "previous_revision": row["revision"],
            "reviews_marked_pending": marked,
        }


def withdraw_order(db: Database, order_id: int) -> dict:
    now = utcnow_iso()
    with db.write() as conn:
        row = conn.execute(
            "SELECT * FROM outage_orders WHERE id = ?", (order_id,)
        ).fetchone()
        if row is None:
            raise ApiError(404, f"outage order {order_id} not found")
        if row["status"] == "withdrawn":
            return {
                "order": OutageOrderOut(**_order_out(conn, order_id)),
                "changed": False,
                "previous_revision": row["revision"],
                "reviews_marked_pending": [],
            }
        conn.execute(
            "UPDATE outage_orders SET status = 'withdrawn', withdrawn_at = ?, "
            "revision = revision + 1, updated_at = ? WHERE id = ?",
            (now, now, order_id),
        )
        candidates = _candidate_reports(
            conn, row["segment_id"], date.fromisoformat(row["first_date"]),
            date.fromisoformat(row["last_date"]),
        )
        marked = _apply_order_change(
            conn,
            candidates,
            {
                "type": "outage_order_withdrawn",
                "order_id": order_id,
                "order_no": row["order_no"],
                "detected_at": now,
            },
        )
        return {
            "order": OutageOrderOut(**_order_out(conn, order_id)),
            "changed": True,
            "previous_revision": row["revision"],
            "reviews_marked_pending": marked,
        }


def get_order(db: Database, order_id: int) -> dict:
    with db.read() as conn:
        return _order_out(conn, order_id)


def list_orders(
    db: Database,
    *,
    segment_id: int | None = None,
    status: str | None = None,
    report_id: int | None = None,
    limit: int = 100,
    offset: int = 0,
) -> list[dict]:
    clauses: list[str] = []
    params: list[object] = []
    if segment_id is not None:
        clauses.append("o.segment_id = ?")
        params.append(segment_id)
    if status is not None:
        clauses.append("o.status = ?")
        params.append(status)
    if report_id is not None:
        # Only orders whose inclusive range touches a day the report used.
        clauses.append(
            "EXISTS (SELECT 1 FROM report_usage u "
            "WHERE u.report_id = ? AND u.segment_id = o.segment_id "
            "AND u.reading_date BETWEEN o.first_date AND o.last_date)"
        )
        params.append(report_id)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    with db.read() as conn:
        rows = conn.execute(
            "SELECT o.*, s.name AS segment_name FROM outage_orders o "
            "JOIN segments s ON s.id = o.segment_id "
            f"{where} ORDER BY o.id DESC LIMIT ? OFFSET ?",
            (*params, limit, offset),
        ).fetchall()
        return [
            {
                "id": r["id"],
                "order_no": r["order_no"],
                "segment_id": r["segment_id"],
                "segment_name": r["segment_name"],
                "first_date": date.fromisoformat(r["first_date"]),
                "last_date": date.fromisoformat(r["last_date"]),
                "status": r["status"],
                "revision": r["revision"],
                "created_at": r["created_at"],
                "updated_at": r["updated_at"],
                "withdrawn_at": r["withdrawn_at"],
            }
            for r in rows
        ]


# ---------------------------------------------------------------------------
# Review state and confirmation
# ---------------------------------------------------------------------------


def _review_row(conn: sqlite3.Connection, report_id: int) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM outage_reviews WHERE report_id = ?", (report_id,)
    ).fetchone()


def _confirmation_history(conn: sqlite3.Connection, report_id: int) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM outage_review_confirmations WHERE report_id = ? "
        "ORDER BY id DESC",
        (report_id,),
    ).fetchall()
    return [
        {
            "confirmation_id": r["id"],
            "reviewer": r["reviewer"],
            "evidence_version": r["evidence_hash"],
            "evidence": json.loads(r["evidence_json"]),
            "confirmed_at": r["confirmed_at"],
        }
        for r in rows
    ]


def _review_payload(conn: sqlite3.Connection, report_id: int) -> dict:
    evidence, version = build_evidence(conn, report_id)
    review = _review_row(conn, report_id)
    status = review["status"] if review else "pending"
    # Defensive read-side guard: even if a mutation path ever missed the
    # write-time invalidation, a confirmation only counts for the exact
    # evidence version it pinned.
    if (
        status == "confirmed"
        and review is not None
        and review["confirmed_evidence_hash"] != version
    ):
        status = "pending"
    return {
        "report_id": report_id,
        "status": status,
        "evidence_version": version,
        "matched_orders": evidence["matched_orders"],
        "impacted_days": evidence["impacted_days"],
        "matched_segments": evidence["matched_segments"],
        "review_readings": False,
        "recomputed_report": False,
        "confirmed_by": review["confirmed_by"] if status == "confirmed" else None,
        "confirmed_at": review["confirmed_at"] if status == "confirmed" else None,
        "invalidated_at": review["invalidated_at"] if review else None,
        "invalidated_reason": (
            json.loads(review["invalidated_reason_json"])
            if review and review["invalidated_reason_json"]
            else None
        ),
        "confirmation_history": _confirmation_history(conn, report_id),
    }


def get_review(db: Database, report_id: int) -> dict:
    with db.read() as conn:
        return _review_payload(conn, report_id)


def confirm_review(
    db: Database, report_id: int, reviewer: str, evidence_version: str
) -> dict:
    now = utcnow_iso()
    with db.write() as conn:
        _report_row(conn, report_id)
        evidence, current_version = build_evidence(conn, report_id)
        if evidence_version != current_version:
            raise ApiError(
                409,
                "evidence changed since it was viewed; reload and confirm again",
                current_evidence_version=current_version,
                submitted_evidence_version=evidence_version,
            )

        review = _review_row(conn, report_id)
        if (
            review is not None
            and review["status"] == "confirmed"
            and review["confirmed_evidence_hash"] == current_version
        ):
            existing = conn.execute(
                "SELECT id FROM outage_review_confirmations "
                "WHERE report_id = ? AND evidence_hash = ? ORDER BY id DESC LIMIT 1",
                (report_id, current_version),
            ).fetchone()
            return {
                "report_id": report_id,
                "status": "confirmed",
                "changed": False,
                "new_confirmation": False,
                "evidence_version": current_version,
                "confirmed_by": review["confirmed_by"],
                "confirmed_at": review["confirmed_at"],
                "confirmation_id": existing["id"] if existing else None,
            }

        evidence_json = json.dumps(evidence, ensure_ascii=False)
        cur = conn.execute(
            "INSERT INTO outage_review_confirmations ("
            "report_id, reviewer, evidence_hash, evidence_json, confirmed_at"
            ") VALUES (?, ?, ?, ?, ?)",
            (report_id, reviewer, current_version, evidence_json, now),
        )
        confirmation_id = int(cur.lastrowid)
        conn.execute(
            "INSERT INTO outage_reviews ("
            "report_id, status, confirmed_by, confirmed_at, "
            "confirmed_evidence_hash, confirmed_evidence_json, "
            "invalidated_at, invalidated_reason_json, created_at, updated_at"
            ") VALUES (?, 'confirmed', ?, ?, ?, ?, NULL, NULL, ?, ?) "
            "ON CONFLICT(report_id) DO UPDATE SET "
            "status = 'confirmed', confirmed_by = excluded.confirmed_by, "
            "confirmed_at = excluded.confirmed_at, "
            "confirmed_evidence_hash = excluded.confirmed_evidence_hash, "
            "confirmed_evidence_json = excluded.confirmed_evidence_json, "
            "invalidated_at = NULL, invalidated_reason_json = NULL, "
            "updated_at = excluded.updated_at",
            (
                report_id,
                reviewer,
                now,
                current_version,
                evidence_json,
                now,
                now,
            ),
        )
        return {
            "report_id": report_id,
            "status": "confirmed",
            "changed": True,
            "new_confirmation": True,
            "evidence_version": current_version,
            "confirmed_by": reviewer,
            "confirmed_at": now,
            "confirmation_id": confirmation_id,
        }
