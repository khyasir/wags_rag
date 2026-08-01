"""Date range resolution. Pure — no network, no Odoo, no env reads.

Two jobs, both easy to get wrong and both worth isolating:

1. Turn a named period ("last week") into explicit local date bounds, using a
   caller-supplied ``today`` so tests are deterministic.
2. Convert those local bounds to the UTC strings Odoo expects, because
   ``order_datetime`` is stored in UTC. The conversion happens here, in Python.
   No timezone function is ever applied to the column.

Bounds are half-open: ``local_start`` inclusive, ``local_end`` exclusive. That
removes the whole class of "did we include the last day" bugs.

Week starts **Monday** (ISO), per Yasir.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

ODOO_DT_FMT = "%Y-%m-%d %H:%M:%S"
UTC = ZoneInfo("UTC")

NAMED_PERIODS = (
    "today",
    "yesterday",
    "this_week",
    "last_week",
    "this_month",
    "last_month",
    "last_7_days",
    "last_30_days",
    "this_year",
    "last_year",
    "all_time",
    "custom",
)


class DateRangeError(ValueError):
    """Range cannot be served. Message is safe to show the user."""


@dataclass(frozen=True)
class DateRange:
    """Resolved range. ``utc_*`` are what go into the Odoo domain."""

    period: str
    local_start: Optional[date]   # None only when there is no lower bound
    local_end: Optional[date]     # exclusive; None only for open-ended
    utc_start: Optional[str]
    utc_end: Optional[str]
    clipped: bool                 # True when DATA_START cut the range short
    requested_start: Optional[date]  # what the user asked for, pre-clip

    @property
    def description(self) -> str:
        """Plain-words range, for the answer text."""
        if self.local_start is None and self.local_end is None:
            return "all available data"
        if self.local_start is None:
            return f"everything up to {self._last_day()}"
        if self.local_end is None:
            return f"from {self.local_start} onwards"
        if self.local_start == self._last_day():
            return str(self.local_start)
        return f"{self.local_start} to {self._last_day()}"

    def _last_day(self) -> Optional[date]:
        """Inclusive last day, for humans. Internals stay half-open."""
        return None if self.local_end is None else self.local_end - timedelta(days=1)


# --------------------------------------------------------------- conversions

def local_midnight_to_utc(d: date, tz_name: str) -> str:
    """Local midnight on ``d`` as a naive UTC string for Odoo.

    Riyadh has no DST, but we go through zoneinfo anyway so the same code is
    correct if this is ever pointed at a zone that does.
    """
    aware = datetime.combine(d, time.min, tzinfo=ZoneInfo(tz_name))
    return aware.astimezone(UTC).replace(tzinfo=None).strftime(ODOO_DT_FMT)


def utc_str_to_local_date(s: str, tz_name: str) -> date:
    """Naive UTC string from Odoo back to a local calendar date.

    Used to label read_group buckets from ``__range.from`` rather than trusting
    Odoo's locale-formatted display string.
    """
    aware = datetime.strptime(s, ODOO_DT_FMT).replace(tzinfo=UTC)
    return aware.astimezone(ZoneInfo(tz_name)).date()


# ------------------------------------------------------------ period bounds

def _monday_of(d: date) -> date:
    return d - timedelta(days=d.weekday())


def _first_of_month(d: date) -> date:
    return d.replace(day=1)


def _local_bounds(period: str, today: date,
                  start_date: Optional[date],
                  end_date: Optional[date]) -> tuple[Optional[date], Optional[date]]:
    """Local half-open bounds for a named period. No clipping yet."""
    tomorrow = today + timedelta(days=1)

    if period == "today":
        return today, tomorrow
    if period == "yesterday":
        return today - timedelta(days=1), today
    if period == "this_week":
        return _monday_of(today), tomorrow
    if period == "last_week":
        this_monday = _monday_of(today)
        return this_monday - timedelta(days=7), this_monday
    if period == "this_month":
        return _first_of_month(today), tomorrow
    if period == "last_month":
        this_first = _first_of_month(today)
        return _first_of_month(this_first - timedelta(days=1)), this_first
    if period == "last_7_days":
        return today - timedelta(days=6), tomorrow
    if period == "last_30_days":
        return today - timedelta(days=29), tomorrow
    if period == "this_year":
        return date(today.year, 1, 1), tomorrow
    if period == "last_year":
        return date(today.year - 1, 1, 1), date(today.year, 1, 1)
    if period == "all_time":
        return None, None
    if period == "custom":
        if start_date is None and end_date is None:
            raise DateRangeError(
                "A custom range needs at least a start date or an end date.")
        if start_date and end_date and end_date < start_date:
            raise DateRangeError(
                f"The end date {end_date} is before the start date {start_date}.")
        # end_date is inclusive from the user's point of view
        return start_date, (end_date + timedelta(days=1)) if end_date else None

    raise DateRangeError(f"Unknown period '{period}'.")


# -------------------------------------------------------------------- public

def resolve_range(
    period: str,
    *,
    today: date,
    tz_name: str,
    data_start: Optional[date],
    start_date: Optional[date] = None,
    end_date: Optional[date] = None,
) -> DateRange:
    """Resolve a period into UTC bounds, applying the optional data floor.

    ``data_start`` is the whole point of this function's complexity:

    * ``None``  -> no floor. Full history. This is the local testing setting.
    * a date    -> a range entirely before it is refused outright, and a range
                   that straddles it is clipped with ``clipped=True`` so the
                   answer is obliged to mention it.
    """
    if period not in NAMED_PERIODS:
        raise DateRangeError(
            f"Unknown period '{period}'. Allowed: {', '.join(NAMED_PERIODS)}.")

    local_start, local_end = _local_bounds(period, today, start_date, end_date)
    requested_start = local_start
    clipped = False

    if data_start is not None:
        # Entirely before the floor: refuse without querying.
        if local_end is not None and local_end <= data_start:
            raise DateRangeError(
                f"Data before {data_start} is not available, and the range you "
                f"asked for ends before that date.")
        if local_start is None or local_start < data_start:
            local_start = data_start
            clipped = True

    return DateRange(
        period=period,
        local_start=local_start,
        local_end=local_end,
        utc_start=local_midnight_to_utc(local_start, tz_name) if local_start else None,
        utc_end=local_midnight_to_utc(local_end, tz_name) if local_end else None,
        clipped=clipped,
        requested_start=requested_start,
    )
