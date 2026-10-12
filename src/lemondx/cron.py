"""Cron expressions: parse one, say when it next fires, describe it.

Five fields -- minute, hour, day of month, month, day of week -- as Vixie
cron reads them, plus the ``@hourly``-style shorthands. A task's schedule is
the one user, and what it needs from this is a single answer every node
agrees on: given the expression, the time zone and "after when", the next
firing as a UTC epoch second. That number is the occurrence's identity across
the cluster (see tasks.py), so it is computed from the record alone and never
from anything local.

Firing times are wall-clock times in the schedule's zone, so a daylight-saving
change moves them the way a person expects: "0 2 * * *" stays at two in the
morning. A wall time that does not exist (the hour skipped in spring) fires at
the moment it maps to, which is just after the gap; one that happens twice (the
hour repeated in autumn) fires once, the first time.
"""

from __future__ import annotations

import datetime
import re

try:
    import zoneinfo
except ImportError:                       # pragma: no cover - 3.9+ always has it
    zoneinfo = None

MACROS = {
    "@yearly": "0 0 1 1 *", "@annually": "0 0 1 1 *",
    "@monthly": "0 0 1 * *", "@weekly": "0 0 * * 0",
    "@daily": "0 0 * * *", "@midnight": "0 0 * * *",
    "@hourly": "0 * * * *",
}
_MONTHS = ("jan", "feb", "mar", "apr", "may", "jun",
           "jul", "aug", "sep", "oct", "nov", "dec")
_DAYS = ("sun", "mon", "tue", "wed", "thu", "fri", "sat")
# (low, high, names, field label)
_FIELDS = (
    (0, 59, None, "minute"),
    (0, 23, None, "hour"),
    (1, 31, None, "day of month"),
    (1, 12, _MONTHS, "month"),
    (0, 7, _DAYS, "day of week"),
)
_TERM = re.compile(r"^(\*|[a-z0-9]+(?:-[a-z0-9]+)?)(?:/([0-9]+))?$")
# Far enough to find "29 February on a Monday" (once in 28 years) and still
# give up on an expression that can never match, like "30 February".
_SEARCH_YEARS = 30


class CronError(ValueError):
    """An expression or time zone that cannot be used; the message says why."""


class Schedule:
    """One parsed expression in one time zone."""

    def __init__(self, expression, timezone="UTC"):
        self.expression = normalise(expression)
        self.timezone = timezone or "UTC"
        self.zone = zone(self.timezone)
        fields = self.expression.split()
        parsed = [_parse_field(text, *spec) for text, spec in zip(fields, _FIELDS)]
        self.minutes, self.hours, self.days, self.months, weekdays = parsed
        # 7 is Sunday as well as 0, as in every cron.
        self.weekdays = {0 if d == 7 else d for d in weekdays}
        # Vixie's rule: with both day fields restricted, either one matching is
        # enough; with one of them `*`, only the other counts.
        self.days_any = fields[2].startswith("*")
        self.weekdays_any = fields[4].startswith("*")

    def _day_matches(self, day):
        in_month = day.day in self.days
        in_week = (day.isoweekday() % 7) in self.weekdays
        if self.days_any and self.weekdays_any:
            return True
        if self.days_any:
            return in_week
        if self.weekdays_any:
            return in_month
        return in_month or in_week

    def next_after(self, after):
        """The first firing strictly after ``after`` (epoch seconds), as epoch seconds.

        Walks wall-clock time in the schedule's zone a field at a time --
        month, then day, then hour, then minute -- so a yearly expression costs
        a few dozen steps, not half a million minutes.
        """
        local = datetime.datetime.fromtimestamp(int(after), self.zone).replace(
            tzinfo=None, second=0, microsecond=0) + datetime.timedelta(minutes=1)
        limit = local.year + _SEARCH_YEARS
        while local.year <= limit:
            if local.month not in self.months:
                year, month = (local.year + 1, 1) if local.month == 12 \
                    else (local.year, local.month + 1)
                local = datetime.datetime(year, month, 1)
                continue
            if not self._day_matches(local):
                local = datetime.datetime(local.year, local.month, local.day) \
                    + datetime.timedelta(days=1)
                continue
            if local.hour not in self.hours:
                local = local.replace(minute=0) + datetime.timedelta(hours=1)
                continue
            if local.minute not in self.minutes:
                local += datetime.timedelta(minutes=1)
                continue
            fires = int(local.replace(tzinfo=self.zone).timestamp())
            if fires > after:
                return fires
            # A skipped wall time maps onto one already passed; keep walking.
            local += datetime.timedelta(minutes=1)
        raise CronError("'%s' never fires." % self.expression)

    def upcoming(self, after, count=5):
        found, at = [], after
        for _ in range(count):
            at = self.next_after(at)
            found.append(at)
        return found

    def describe(self):
        return describe(self.expression)


def normalise(expression):
    """The expression as stored: lower case, single spaces, a macro expanded."""
    text = " ".join(str(expression or "").strip().lower().split())
    if not text:
        raise CronError("Give a schedule, e.g. '0 2 * * *' for 02:00 every day.")
    text = MACROS.get(text, text)
    if text.startswith("@"):
        raise CronError("Unknown shorthand '%s'. Use one of %s."
                        % (text, ", ".join(sorted(MACROS))))
    if len(text.split()) != 5:
        raise CronError("A schedule has five fields -- minute hour day-of-month month "
                        "day-of-week -- e.g. '*/15 * * * *'.")
    return text


def zone(name):
    if not name or name.upper() == "UTC":
        return datetime.timezone.utc
    if zoneinfo is None:
        raise CronError("Time zones other than UTC need Python's zoneinfo.")
    try:
        return zoneinfo.ZoneInfo(name)
    except (zoneinfo.ZoneInfoNotFoundError, ValueError):
        raise CronError("Unknown time zone '%s'. Use a name like 'Europe/Stockholm', "
                        "or UTC." % name)


def _value(text, low, high, names, label):
    if names and text[:3] in names and text.isalpha():
        return names.index(text[:3]) + (1 if label == "month" else 0)
    if not text.isdigit():
        raise CronError("'%s' is not a %s." % (text, label))
    value = int(text)
    if not low <= value <= high:
        raise CronError("%s %d is outside %d-%d." % (label.capitalize(), value, low, high))
    return value


def _parse_field(text, low, high, names, label):
    values = set()
    for term in text.split(","):
        match = _TERM.match(term)
        if not match:
            raise CronError("Cannot read '%s' in the %s field." % (term, label))
        span, step = match.group(1), match.group(2)
        if span == "*":
            start, end = low, high
        elif "-" in span:
            first, last = span.split("-", 1)
            start = _value(first, low, high, names, label)
            end = _value(last, low, high, names, label)
            if end < start:
                raise CronError("The range '%s' in the %s field runs backwards." % (span, label))
        else:
            start = _value(span, low, high, names, label)
            # "5/15" means from 5 to the end in steps of 15, as in Vixie cron.
            end = high if step else start
        increment = int(step) if step else 1
        if increment < 1:
            raise CronError("A step of 0 in the %s field never moves." % label)
        values.update(range(start, end + 1, increment))
    return values


def describe(expression):
    """A short reading of common shapes; the expression itself for the rest."""
    try:
        minute, hour, day, month, weekday = normalise(expression).split()
    except CronError:
        return str(expression or "")
    every = {"*"}
    if (minute, hour, day, month, weekday) == ("*",) * 5:
        return "every minute"
    if minute.startswith("*/") and {hour, day, month, weekday} <= every:
        return "every %s minutes" % minute[2:]
    if minute.isdigit() and hour == "*" and {day, month, weekday} <= every:
        return "hourly at :%02d" % int(minute)
    if minute.isdigit() and hour.startswith("*/") and {day, month, weekday} <= every:
        return "every %s hours at :%02d" % (hour[2:], int(minute))
    if not (minute.isdigit() and hour.isdigit()):
        return expression
    at = "%02d:%02d" % (int(hour), int(minute))
    if {day, month, weekday} <= every:
        return "daily at %s" % at
    if day == "*" and month == "*":
        try:
            days = sorted({0 if d == 7 else d for d in _parse_field(weekday, *_FIELDS[4])})
        except CronError:
            return expression
        if days == [1, 2, 3, 4, 5]:
            return "weekdays at %s" % at
        return "%s at %s" % (", ".join(_DAYS[d].capitalize() for d in days), at)
    if day.isdigit() and month == "*" and weekday == "*":
        return "monthly on day %s at %s" % (day, at)
    return expression
