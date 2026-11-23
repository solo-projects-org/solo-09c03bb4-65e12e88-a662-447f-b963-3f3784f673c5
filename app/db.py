"""SQLite access helpers.

The whole service deliberately uses short, explicit transactions with
``BEGIN IMMEDIATE`` for every write, including report generation.  SQLite then
serialises report generation and corrections at the database level, which is
what guarantees that a correction can never interleave between the snapshot
read and the report insert (and therefore can never miss marking a report as
pending recalculation).

Outage reviews deliberately do not read the mutable ``readings`` table.  A
report's used daily dates are copied once into ``report_usage`` at issuance
(and backfilled for older reports at start-up).  Matching an outage order and
invalidating a confirmed review are therefore index lookups against
``report_usage`` / ``outage_reviews``; changing an order never walks every
historical report and never reads current daily readings.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator


SCHEMA = """
CREATE TABLE IF NOT EXISTS segments (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    name           TEXT NOT NULL UNIQUE,
    retrofit_date  TEXT,
    version        INTEGER NOT NULL DEFAULT 0,
    created_at     TEXT NOT NULL,
    updated_at     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS readings (
    segment_id    INTEGER NOT NULL REFERENCES segments(id) ON DELETE CASCADE,
    reading_date  TEXT NOT NULL,
    kwh_per_lamp  REAL NOT NULL CHECK (kwh_per_lamp >= 0),
    updated_at    TEXT NOT NULL,
    PRIMARY KEY (segment_id, reading_date)
);

CREATE INDEX IF NOT EXISTS idx_readings_date
    ON readings (reading_date);

CREATE TABLE IF NOT EXISTS reports (
    id                              INTEGER PRIMARY KEY AUTOINCREMENT,
    target_segment_id               INTEGER NOT NULL REFERENCES segments(id),
    target_name                     TEXT NOT NULL,
    target_retrofit_date            TEXT NOT NULL,
    before_start                    TEXT NOT NULL,
    before_end                      TEXT NOT NULL,
    after_start                     TEXT NOT NULL,
    after_end                       TEXT NOT NULL,
    skipped_date                    TEXT NOT NULL,
    target_before_avg               REAL NOT NULL,
    target_after_avg                REAL NOT NULL,
    target_reduction                REAL NOT NULL,
    control_avg_reduction           REAL NOT NULL,
    net_savings_per_lamp_per_day    REAL NOT NULL,
    controls_json                   TEXT NOT NULL,
    snapshot_json                   TEXT NOT NULL,
    status                          TEXT NOT NULL
        CHECK (status IN ('issued', 'pending_recalc')),
    stale_at                        TEXT,
    stale_reasons_json              TEXT NOT NULL DEFAULT '[]',
    stale_segment_ids_json          TEXT NOT NULL DEFAULT '[]',
    created_at                      TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_reports_target
    ON reports (target_segment_id, created_at);

-- Which (segment, day) pairs a report's snapshot actually used.  Written once
-- when a report is issued.  Outage work orders match against this table
-- through its indexes, so changing an order never scans report snapshots.
CREATE TABLE IF NOT EXISTS report_usage (
    report_id     INTEGER NOT NULL REFERENCES reports(id),
    segment_id    INTEGER NOT NULL REFERENCES segments(id),
    role          TEXT NOT NULL CHECK (role IN ('target', 'control')),
    reading_date  TEXT NOT NULL,
    PRIMARY KEY (report_id, segment_id, reading_date)
);

CREATE INDEX IF NOT EXISTS idx_report_usage_segment_date
    ON report_usage (segment_id, reading_date, report_id);

-- Outage (power-cut) work orders.  An order is registered independently of
-- any report; a report's review matches it when the order's segment is the
-- report's target or one of its controls and the inclusive date range
-- overlaps the daily readings that report actually used.  ``revision`` grows
-- on every change so a review cannot silently return to an earlier evidence
-- version after an order is changed back.
CREATE TABLE IF NOT EXISTS outage_orders (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    order_no        TEXT NOT NULL UNIQUE,
    segment_id      INTEGER NOT NULL REFERENCES segments(id),
    first_date      TEXT NOT NULL,
    last_date       TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'active'
        CHECK (status IN ('active', 'withdrawn')),
    revision        INTEGER NOT NULL DEFAULT 1,
    withdrawn_at    TEXT,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    CHECK (first_date <= last_date)
);

CREATE INDEX IF NOT EXISTS idx_outage_orders_segment
    ON outage_orders (segment_id, first_date, last_date);

-- Latest review state per report.  Evidence is recomputed lazily from the
-- immutable report snapshot and the outage orders, never from current daily
-- readings.  A confirmation is only valid for the exact evidence hash the
-- reviewer saw; a later evidence change moves the row back to ``pending`` and
-- it never flips back on its own.
CREATE TABLE IF NOT EXISTS outage_reviews (
    report_id                   INTEGER PRIMARY KEY REFERENCES reports(id),
    status                      TEXT NOT NULL
        CHECK (status IN ('pending', 'confirmed')),
    -- Monotonic counter bumped by every order change that can touch this
    -- report.  It is folded into the evidence version so a version string can
    -- never come back after an order is changed back or withdrawn.
    epoch                       INTEGER NOT NULL DEFAULT 0,
    confirmed_by                TEXT,
    confirmed_at                TEXT,
    confirmed_evidence_hash     TEXT,
    confirmed_evidence_json     TEXT,
    invalidated_at              TEXT,
    invalidated_reason_json     TEXT,
    created_at                  TEXT NOT NULL,
    updated_at                  TEXT NOT NULL
);

-- Append-only history of confirmations, each with the exact evidence the
-- reviewer confirmed.  Superseded confirmations remain queryable.
CREATE TABLE IF NOT EXISTS outage_review_confirmations (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    report_id           INTEGER NOT NULL REFERENCES reports(id),
    reviewer            TEXT NOT NULL,
    evidence_hash       TEXT NOT NULL,
    evidence_json       TEXT NOT NULL,
    confirmed_at        TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_outage_confirmations_report
    ON outage_review_confirmations (report_id, id);
"""


def utcnow_iso() -> str:
    """Return an ISO-8601 UTC timestamp with a ``Z`` suffix."""

    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


class Database:
    """Small wrapper around sqlite3 with explicit read/write transactions."""

    def __init__(self, path: str | Path) -> None:
        self.path = str(path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        conn = self.connect()
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(SCHEMA)
            self._backfill_report_usage(conn)
        finally:
            conn.close()

    @staticmethod
    def _backfill_report_usage(conn: sqlite3.Connection) -> None:
        """Populate report_usage for reports issued before the table existed.

        This is a one-off migration over stored report *snapshots* (not current
        readings).  After it runs, order changes never need to touch reports.
        """

        rows = conn.execute(
            "SELECT id, target_segment_id, snapshot_json FROM reports "
            "WHERE id NOT IN (SELECT DISTINCT report_id FROM report_usage)"
        ).fetchall()
        if not rows:
            return
        now_pairs: list[tuple[int, int, str, str]] = []
        for row in rows:
            snapshot = json.loads(row["snapshot_json"])
            target = snapshot["target"]
            for day in target["readings"]:
                now_pairs.append((row["id"], target["id"], "target", day))
            for control in snapshot.get("controls", []):
                for day in control["readings"]:
                    now_pairs.append((row["id"], control["id"], "control", day))
        conn.executemany(
            "INSERT OR IGNORE INTO report_usage "
            "(report_id, segment_id, role, reading_date) VALUES (?, ?, ?, ?)",
            now_pairs,
        )

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(
            self.path,
            timeout=30.0,
            isolation_level=None,  # explicit transactions below
            check_same_thread=False,
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=30000")
        return conn

    @contextmanager
    def read(self) -> Iterator[sqlite3.Connection]:
        conn = self.connect()
        try:
            yield conn
        finally:
            conn.close()

    @contextmanager
    def write(self) -> Iterator[sqlite3.Connection]:
        conn = self.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.execute("COMMIT")
        except BaseException:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        finally:
            conn.close()
