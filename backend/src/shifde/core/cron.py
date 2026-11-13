"""Five-field cron expressions and next-run computation (pipeline schedules, P6 section 4).

`minute hour day-of-month month day-of-week` with `*`, `a`, `a-b`, `*/n`, `a-b/n` and comma lists;
day-of-week 0-6 (Sunday = 0, 7 is accepted as Sunday). When BOTH day-of-month and day-of-week are
restricted a day matches if EITHER does (standard cron). Times are evaluated in an IANA time zone;
wall-clock times that do not exist (spring-forward gap) are skipped, ambiguous ones fire once.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

FIELDS = (
    ("minute", 0, 59),
    ("hour", 0, 23),
    ("day-of-month", 1, 31),
    ("month", 1, 12),
    ("day-of-week", 0, 7),
)
SEARCH_DAYS = 366 * 5


class CronError(ValueError):
    """The expression or time zone is invalid."""


def _parse_field(text: str, name: str, low: int, high: int) -> frozenset[int]:
    values: set[int] = set()
    for part in text.split(","):
        if not part:
            raise CronError(f"{name}: empty list item")
        step = 1
        rng = part
        if "/" in part:
            rng, _, step_text = part.partition("/")
            if not step_text.isdigit() or int(step_text) < 1:
                raise CronError(f"{name}: invalid step {step_text!r}")
            step = int(step_text)
        if rng == "*":
            start, end = low, high
        elif "-" in rng:
            a, _, b = rng.partition("-")
            if not (a.isdigit() and b.isdigit()):
                raise CronError(f"{name}: invalid range {rng!r}")
            start, end = int(a), int(b)
        elif rng.isdigit():
            start = int(rng)
            end = high if "/" in part else start
        else:
            raise CronError(f"{name}: invalid value {rng!r}")
        if start < low or end > high or start > end:
            raise CronError(f"{name}: {rng!r} is outside {low}-{high}")
        values.update(range(start, end + 1, step))
    return frozenset(values)


@dataclass(frozen=True)
class Cron:
    minutes: frozenset[int]
    hours: frozenset[int]
    days: frozenset[int]
    months: frozenset[int]
    weekdays: frozenset[int]  # 0-6, Sunday = 0
    dom_restricted: bool
    dow_restricted: bool

    @classmethod
    def parse(cls, expression: str) -> Cron:
        parts = expression.split()
        if len(parts) != 5:
            raise CronError("a cron expression has exactly five fields")
        parsed = [
            _parse_field(text, name, low, high)
            for text, (name, low, high) in zip(parts, FIELDS, strict=True)
        ]
        weekdays = frozenset(d % 7 for d in parsed[4])
        return cls(
            parsed[0],
            parsed[1],
            parsed[2],
            parsed[3],
            weekdays,
            dom_restricted=parts[2] != "*",
            dow_restricted=parts[4] != "*",
        )

    def _day_matches(self, day: datetime) -> bool:
        if day.month not in self.months:
            return False
        dom_ok = day.day in self.days
        dow_ok = (day.isoweekday() % 7) in self.weekdays
        if self.dom_restricted and self.dow_restricted:
            return dom_ok or dow_ok
        return dom_ok and dow_ok

    def next_after(self, after: datetime, tz_name: str = "UTC") -> datetime:
        """The first fire time strictly after `after` (timezone-aware, returned in UTC)."""
        try:
            tz = ZoneInfo(tz_name)
        except (ZoneInfoNotFoundError, ValueError, OSError) as exc:
            raise CronError(f"unknown time zone {tz_name!r}") from exc
        if after.tzinfo is None:
            raise CronError("`after` must be timezone-aware")
        local = after.astimezone(tz).replace(second=0, microsecond=0) + timedelta(minutes=1)
        day = local.replace(hour=0, minute=0)
        for offset in range(SEARCH_DAYS):
            current = (day + timedelta(days=offset)).replace(tzinfo=None)
            if not self._day_matches(current):
                continue
            for hour in sorted(self.hours):
                for minute in sorted(self.minutes):
                    naive = current.replace(hour=hour, minute=minute)
                    if naive < local.replace(tzinfo=None):
                        continue
                    candidate = naive.replace(tzinfo=tz)
                    utc = candidate.astimezone(UTC)
                    if utc.astimezone(tz).replace(tzinfo=None) != naive:
                        continue  # the wall-clock time does not exist (DST gap)
                    if utc > after:
                        return utc
        raise CronError("the expression never fires")
