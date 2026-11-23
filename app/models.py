"""Pydantic request/response models (also drive the Swagger schema)."""

from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

ReportStatus = Literal["issued", "pending_recalc"]
OutageOrderStatus = Literal["active", "withdrawn"]
OutageReviewStatus = Literal["pending", "confirmed"]
SegmentRole = Literal["target", "control"]
SavingsEffect = Literal["inflated", "depressed"]
ComparisonPhase = Literal["before", "after"]


class SegmentCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    retrofit_date: date | None = None

    @field_validator("name")
    @classmethod
    def _strip_name(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("name must not be blank")
        return value


class SegmentPatch(BaseModel):
    # Deliberately required: sending {"retrofit_date": null} clears the date.
    retrofit_date: date | None


class ReadingInput(BaseModel):
    date: date
    kwh_per_lamp: float = Field(ge=0)


class ReadingsUpsert(BaseModel):
    readings: list[ReadingInput] = Field(min_length=1, max_length=10000)


class SegmentOut(BaseModel):
    id: int
    name: str
    retrofit_date: date | None
    version: int
    created_at: str
    updated_at: str


class ReadingPoint(BaseModel):
    date: date
    kwh_per_lamp: float


class ReadingsOut(BaseModel):
    segment_id: int
    readings: list[ReadingPoint]


class ComparisonWindow(BaseModel):
    before_start: date
    before_end: date
    after_start: date
    after_end: date
    skipped_date: date


class TargetDetail(BaseModel):
    segment_id: int
    name: str
    retrofit_date: date
    before_avg: float
    after_avg: float
    reduction: float
    readings: dict[str, list[ReadingPoint]]


class ControlDetail(BaseModel):
    segment_id: int
    name: str
    retrofit_date: date | None
    before_avg: float
    after_avg: float
    reduction: float
    readings: dict[str, list[ReadingPoint]]


class ReportDetail(BaseModel):
    id: int
    status: ReportStatus
    target_segment_id: int
    target_name: str
    target_retrofit_date: date
    comparison: ComparisonWindow
    target: TargetDetail
    controls: list[ControlDetail]
    control_count: int
    control_avg_reduction: float
    net_savings_per_lamp_per_day: float
    created_at: str
    stale_at: str | None = None
    stale_reasons: list[dict] = Field(default_factory=list)
    stale_segment_ids: list[int] = Field(default_factory=list)


class ReportSummary(BaseModel):
    id: int
    status: ReportStatus
    target_segment_id: int
    target_name: str
    target_retrofit_date: date
    comparison: ComparisonWindow
    control_count: int
    control_avg_reduction: float
    net_savings_per_lamp_per_day: float
    created_at: str
    stale_at: str | None = None


class ReportIssueResult(BaseModel):
    status: Literal["issued", "deferred"]
    target_segment_id: int
    comparison: ComparisonWindow | None = None
    report: ReportDetail | None = None
    reasons: list[dict] = Field(default_factory=list)


class AffectedReport(BaseModel):
    report_id: int
    previous_status: ReportStatus
    status: ReportStatus


class CorrectionResult(BaseModel):
    segment_id: int
    changed: bool
    changed_readings: list[date] = Field(default_factory=list)
    old_retrofit_date: date | None = None
    new_retrofit_date: date | None = None
    affected_reports: list[AffectedReport] = Field(default_factory=list)
    impact: dict


class HealthOut(BaseModel):
    status: str
    database: str
    version: str


# ---------------------------------------------------------------------------
# Outage re-check
# ---------------------------------------------------------------------------


class OutageOrderCreate(BaseModel):
    """Register an outage work order.

    ``first_date``/``last_date`` are inclusive calendar days on which the
    outage affected the segment's daily reading.
    """

    order_no: str = Field(min_length=1, max_length=100)
    segment_id: int = Field(ge=1)
    first_date: date
    last_date: date

    @field_validator("order_no")
    @classmethod
    def _strip_order_no(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("order_no must not be blank")
        return value

    @model_validator(mode="after")
    def _check_range(self) -> "OutageOrderCreate":
        if self.last_date < self.first_date:
            raise ValueError("last_date must not be earlier than first_date")
        return self


class OutageOrderPatch(BaseModel):
    """Change an order's segment and/or inclusive date range."""

    segment_id: int | None = Field(default=None, ge=1)
    first_date: date | None = None
    last_date: date | None = None

    @model_validator(mode="after")
    def _at_least_one(self) -> "OutageOrderPatch":
        if self.segment_id is None and self.first_date is None and self.last_date is None:
            raise ValueError("provide at least one of segment_id, first_date, last_date")
        return self


class OutageOrderOut(BaseModel):
    id: int
    order_no: str
    segment_id: int
    segment_name: str
    first_date: date
    last_date: date
    status: OutageOrderStatus
    revision: int
    created_at: str
    updated_at: str
    withdrawn_at: str | None = None


class OutageOrderMutationResult(BaseModel):
    order: OutageOrderOut
    changed: bool
    previous_revision: int | None = None
    reviews_marked_pending: list[int] = Field(default_factory=list)


class OutageAffectedDate(BaseModel):
    date: date
    phase: ComparisonPhase
    role: SegmentRole


class OutageImpactedDay(BaseModel):
    """One day of the original comparison, flagged against the outage."""

    date: date
    phase: ComparisonPhase
    role: SegmentRole
    segment_id: int
    segment_name: str
    order_no: str
    order_id: int
    # Value the snapshot used for this day, and the comparison's phase average.
    kwh_per_lamp: float
    phase_average: float
    # 1-based position inside the 7-day phase.
    day_index: int
    # If the outage depresses this day's reading, removing that depression
    # raises the day and pushes the reported net savings in this direction.
    effect_on_net_savings: SavingsEffect


class OutageMatch(BaseModel):
    """One outage order as it bears on the current report revision."""

    order_id: int
    order_no: str
    segment_id: int
    segment_name: str
    role: SegmentRole
    first_date: date
    last_date: date
    status: OutageOrderStatus
    revision: int
    matched_dates: list[OutageAffectedDate]


class OutageReviewState(BaseModel):
    report_id: int
    status: OutageReviewStatus
    # Stable version token for the evidence; the confirm endpoint requires it.
    evidence_version: str
    matched_orders: list[OutageMatch] = Field(default_factory=list)
    impacted_days: list[OutageImpactedDay] = Field(default_factory=list)
    # Distinct segments (with role) touched by at least one matched order.
    matched_segments: list[dict] = Field(default_factory=list)
    review_readings: bool = False
    recomputed_report: bool = False
    confirmed_by: str | None = None
    confirmed_at: str | None = None
    invalidated_at: str | None = None
    invalidated_reason: dict | None = None
    # Prior confirmations (with the evidence each one pinned), newest first.
    confirmation_history: list[dict] = Field(default_factory=list)


class OutageReviewConfirm(BaseModel):
    reviewer: str = Field(min_length=1, max_length=200)
    # Evidence version the reviewer actually saw (from GET .../outage-review).
    evidence_version: str = Field(min_length=1, max_length=200)

    @field_validator("reviewer")
    @classmethod
    def _strip_reviewer(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("reviewer must not be blank")
        return value


class OutageReviewConfirmResult(BaseModel):
    report_id: int
    status: OutageReviewStatus
    changed: bool
    # False when the same evidence version was already confirmed by anyone:
    # the existing confirmation is returned and no second history row is added.
    new_confirmation: bool
    evidence_version: str
    confirmed_by: str | None = None
    confirmed_at: str | None = None
    confirmation_id: int | None = None

