"""New York time, the NYSE calendar and the 5-minute grid.

Mirrors the upstream tools exactly: DST from the US rule, holidays and 1:00 p.m.
early closes for 2026 and 2027, slot 0 = the 9:30 bar, slot 77 = the 15:55 bar.
"""

import datetime

NYSE_HOLIDAYS = {
    "2026-01-01": "New Year's Day",
    "2026-01-19": "Martin Luther King, Jr. Day",
    "2026-02-16": "Washington's Birthday",
    "2026-04-03": "Good Friday",
    "2026-05-25": "Memorial Day",
    "2026-06-19": "Juneteenth National Independence Day",
    "2026-07-03": "Independence Day (observed)",
    "2026-09-07": "Labor Day",
    "2026-11-26": "Thanksgiving Day",
    "2026-12-25": "Christmas Day",
    "2027-01-01": "New Year's Day",
    "2027-01-18": "Martin Luther King, Jr. Day",
    "2027-02-15": "Washington's Birthday",
    "2027-03-26": "Good Friday",
    "2027-05-31": "Memorial Day",
    "2027-06-18": "Juneteenth National Independence Day (observed)",
    "2027-07-05": "Independence Day (observed)",
    "2027-09-06": "Labor Day",
    "2027-11-25": "Thanksgiving Day",
    "2027-12-24": "Christmas Day (observed)",
}
NYSE_EARLY_CLOSE = {  # 1:00 p.m. ET
    "2026-11-27": "day after Thanksgiving",
    "2026-12-24": "Christmas Eve",
    "2027-11-26": "day after Thanksgiving",
}
CALENDAR_YEARS = (2026, 2027)
OPEN = datetime.time(9, 30)
CLOSE = datetime.time(16, 0)
EARLY = datetime.time(13, 0)
BAR_MIN = 5
SESSION_SLOTS = 78
EARLY_SLOTS = 42
OPEN_MIN = 9 * 60 + 30
EPOCH = datetime.datetime(1970, 1, 1)


class ClockError(Exception):
    pass


def _nth_sunday(year, month, n):
    d = datetime.date(year, month, 1)
    d += datetime.timedelta(days=(6 - d.weekday()) % 7)
    return d + datetime.timedelta(weeks=n - 1)


def eastern_offset(utc):
    """Hours from UTC for New York: -4 in daylight time, -5 otherwise."""
    y = utc.year
    start = datetime.datetime.combine(_nth_sunday(y, 3, 2), datetime.time(7, 0))
    end = datetime.datetime.combine(_nth_sunday(y, 11, 1), datetime.time(6, 0))
    naive = utc.replace(tzinfo=None)
    return -4 if start <= naive < end else -5


def to_eastern(utc):
    return utc.replace(tzinfo=None) + datetime.timedelta(hours=eastern_offset(utc))


def eastern_to_utc(local):
    for off in (-4, -5):
        utc = local - datetime.timedelta(hours=off)
        if eastern_offset(utc) == off:
            return utc
    return local + datetime.timedelta(hours=5)


def utc_now():
    return datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0, tzinfo=None)


def parse_now(text):
    """ISO time -> naive UTC. A time without an offset is New York time."""
    if not text:
        return utc_now()
    t = str(text).strip().replace("Z", "+00:00")
    try:
        dt = datetime.datetime.fromisoformat(t)
    except ValueError:
        raise ClockError("--now wants a time like \"2026-10-19 10:15\" (New York) or ...Z (UTC)")
    if dt.tzinfo is None:
        return eastern_to_utc(dt)
    return (dt - dt.utcoffset()).replace(tzinfo=None)


def utc_of(ts):
    return EPOCH + datetime.timedelta(seconds=int(ts))


def ts_of(utc):
    return int((utc - EPOCH).total_seconds())


def iso(utc):
    return utc.strftime("%Y-%m-%dT%H:%M:%SZ")


def trading_day(d):
    """(is_trading_day, reason)."""
    if d.weekday() >= 5:
        return False, "weekend"
    name = NYSE_HOLIDAYS.get(d.isoformat())
    if name:
        return False, "NYSE holiday: " + name
    if d.year not in CALENDAR_YEARS:
        return True, "holiday list unknown for %d" % d.year
    return True, ""


def session(d):
    """(open_time, close_time) New York, or None when closed all day."""
    ok, _ = trading_day(d)
    if not ok:
        return None
    return OPEN, (EARLY if d.isoformat() in NYSE_EARLY_CLOSE else CLOSE)


def slots_of_day(d):
    """How many 5-minute bars the regular session has: 78, or 42 on an early close."""
    if isinstance(d, str):
        d = datetime.date.fromisoformat(d)
    return EARLY_SLOTS if d.isoformat() in NYSE_EARLY_CLOSE else SESSION_SLOTS


def slot_time(d, k):
    """Naive New York datetime of slot k's START (slot 0 = 9:30)."""
    if isinstance(d, str):
        d = datetime.date.fromisoformat(d)
    return datetime.datetime.combine(d, OPEN) + datetime.timedelta(minutes=BAR_MIN * k)


def slot_close_minutes(k):
    """Minutes after midnight at which slot k's bar CLOSES (k=7 -> 10:15)."""
    return OPEN_MIN + BAR_MIN * (k + 1)


def slot_of(et):
    """Naive New York datetime -> slot index, or None when off the grid or outside the session."""
    if et.second or et.minute % BAR_MIN:
        return None
    s = session(et.date())
    if not s:
        return None
    if et.time() < s[0] or et.time() >= s[1]:
        return None
    return int((et - datetime.datetime.combine(et.date(), s[0])).total_seconds() // (BAR_MIN * 60))


def clock12(minutes):
    h, m = divmod(int(minutes), 60)
    return "%d:%02d %s" % (h % 12 or 12, m, "AM" if h < 12 else "PM")


def clock_label(t):
    """datetime or time -> '10:15 AM'."""
    h = t.hour % 12 or 12
    return "%d:%02d %s" % (h, t.minute, "AM" if t.hour < 12 else "PM")


def parse_hhmm(text, what="a clock time"):
    try:
        h, m = str(text).strip().split(":")
        t = datetime.time(int(h), int(m))
    except (ValueError, AttributeError):
        raise ClockError("%s wants HH:MM (New York), got %r" % (what, text))
    return t


def minutes_of(t):
    return t.hour * 60 + t.minute


def entry_window(d, cfg=None):
    """(first_k, last_k): the slots whose bar CLOSE falls inside the config's entry window
    (no_entries_before 09:45 .. no_entries_after 15:30, New York), shrunk on an early close
    like the upstream tools (the cutoff moves up with the 1:00 close), and always before flat_by."""
    cfg = cfg or {}
    before = parse_hhmm(cfg.get("no_entries_before") or "09:45", "no_entries_before")
    after = parse_hhmm(cfg.get("no_entries_after") or "15:30", "no_entries_after")
    flat = parse_hhmm(cfg.get("flat_by") or "15:55", "flat_by")
    n = slots_of_day(d)
    shift = 0 if n == SESSION_SLOTS else 180
    b = minutes_of(before)
    a = minutes_of(after) - shift
    f = minutes_of(flat) - shift
    last_allowed = min(a, f - BAR_MIN)
    first_k = max(0, (b - OPEN_MIN) // BAR_MIN - 1)
    last_k = min(n - 2, (last_allowed - OPEN_MIN) // BAR_MIN - 1)
    return first_k, last_k


def flat_slot(d, cfg=None):
    """The slot whose CLOSE is the flat-by time (15:55 -> slot 76; 12:55 early -> slot 40)."""
    cfg = cfg or {}
    flat = parse_hhmm(cfg.get("flat_by") or "15:55", "flat_by")
    n = slots_of_day(d)
    shift = 0 if n == SESSION_SLOTS else 180
    k = (minutes_of(flat) - shift - OPEN_MIN) // BAR_MIN - 1
    return max(0, min(n - 1, k))


def day_label(d):
    return "%s %s" % (d.strftime("%a"), d.isoformat())
