"""dt.clock: New York time from UTC, the NYSE calendar and the 5-minute grid. Offline."""

import datetime
import unittest

from _dt_fixtures import ROOT  # noqa: F401  (puts the repo root on sys.path)
from dt import clock

DT = datetime.datetime
D = datetime.date
T = datetime.time


class Dst(unittest.TestCase):
    def test_spring_forward_2026_03_08(self):
        # 2:00 a.m. EST on the second Sunday of March is 07:00Z; the hour before is still -5
        self.assertEqual(clock.eastern_offset(DT(2026, 3, 8, 6, 59)), -5)
        self.assertEqual(clock.eastern_offset(DT(2026, 3, 8, 7, 0)), -4)
        self.assertEqual(clock.to_eastern(DT(2026, 3, 8, 6, 59)), DT(2026, 3, 8, 1, 59))
        self.assertEqual(clock.to_eastern(DT(2026, 3, 8, 7, 0)), DT(2026, 3, 8, 3, 0))

    def test_fall_back_2026_11_01(self):
        # 2:00 a.m. EDT on the first Sunday of November is 06:00Z
        self.assertEqual(clock.eastern_offset(DT(2026, 11, 1, 5, 59)), -4)
        self.assertEqual(clock.eastern_offset(DT(2026, 11, 1, 6, 0)), -5)
        self.assertEqual(clock.to_eastern(DT(2026, 11, 1, 5, 59)), DT(2026, 11, 1, 1, 59))
        self.assertEqual(clock.to_eastern(DT(2026, 11, 1, 6, 0)), DT(2026, 11, 1, 1, 0))

    def test_2027_boundaries_follow_the_same_rule(self):
        # second Sunday of March 2027 is the 14th; first Sunday of November 2027 is the 7th
        self.assertEqual(clock.eastern_offset(DT(2027, 3, 14, 6, 59)), -5)
        self.assertEqual(clock.eastern_offset(DT(2027, 3, 14, 7, 0)), -4)
        self.assertEqual(clock.eastern_offset(DT(2027, 11, 7, 5, 59)), -4)
        self.assertEqual(clock.eastern_offset(DT(2027, 11, 7, 6, 0)), -5)

    def test_eastern_to_utc_round_trips_on_both_sides(self):
        for local, utc in ((DT(2026, 10, 19, 9, 30), DT(2026, 10, 19, 13, 30)),      # EDT
                           (DT(2026, 11, 2, 9, 30), DT(2026, 11, 2, 14, 30)),        # EST, the Monday after
                           (DT(2026, 10, 30, 15, 55), DT(2026, 10, 30, 19, 55)),
                           (DT(2026, 1, 19, 10, 15), DT(2026, 1, 19, 15, 15)),
                           (DT(2026, 3, 9, 9, 30), DT(2026, 3, 9, 13, 30))):
            self.assertEqual(clock.eastern_to_utc(local), utc, local)
            self.assertEqual(clock.to_eastern(utc), local, utc)

    def test_the_ambiguous_and_the_missing_hour(self):
        # 1:30 a.m. on 2026-11-01 happens twice; the daylight reading (first) wins
        self.assertEqual(clock.eastern_to_utc(DT(2026, 11, 1, 1, 30)), DT(2026, 11, 1, 5, 30))
        self.assertEqual(clock.eastern_to_utc(DT(2026, 11, 1, 0, 30)), DT(2026, 11, 1, 4, 30))
        self.assertEqual(clock.eastern_to_utc(DT(2026, 11, 1, 2, 30)), DT(2026, 11, 1, 7, 30))
        # 2:30 a.m. on 2026-03-08 never happens; the fallback is the standard-time reading
        self.assertEqual(clock.eastern_to_utc(DT(2026, 3, 8, 2, 30)), DT(2026, 3, 8, 7, 30))

    def test_the_fixture_bar_is_9_30_eastern(self):
        self.assertEqual(clock.to_eastern(clock.utc_of(1792416600)), DT(2026, 10, 19, 9, 30))
        self.assertEqual(clock.ts_of(DT(2026, 10, 19, 13, 30)), 1792416600)
        self.assertEqual(clock.utc_of(1792416600), DT(2026, 10, 19, 13, 30))
        self.assertEqual(clock.iso(DT(2026, 10, 19, 13, 30)), "2026-10-19T13:30:00Z")
        self.assertEqual(clock.ts_of(clock.utc_of(1792416600.0)), 1792416600)


class Calendar(unittest.TestCase):
    def test_every_holiday_is_closed(self):
        self.assertEqual(len(clock.NYSE_HOLIDAYS), 20)
        for day, name in clock.NYSE_HOLIDAYS.items():
            d = D.fromisoformat(day)
            ok, why = clock.trading_day(d)
            self.assertFalse(ok, day)
            self.assertEqual(why, "NYSE holiday: " + name, day)
            self.assertIsNone(clock.session(d), day)
            self.assertIsNone(clock.slot_of(DT.combine(d, T(10, 30))), day)
            self.assertNotEqual(d.weekday(), 5, day)  # listed holidays fall on weekdays
            self.assertNotEqual(d.weekday(), 6, day)
            self.assertIn(d.year, clock.CALENDAR_YEARS, day)

    def test_weekends_and_ordinary_days(self):
        self.assertEqual(clock.trading_day(D(2026, 10, 17)), (False, "weekend"))
        self.assertEqual(clock.trading_day(D(2026, 10, 18)), (False, "weekend"))
        self.assertEqual(clock.trading_day(D(2026, 10, 19)), (True, ""))
        self.assertEqual(clock.trading_day(D(2026, 11, 26)), (False, "NYSE holiday: Thanksgiving Day"))
        self.assertEqual(clock.trading_day(D(2025, 7, 4)), (True, "holiday list unknown for 2025"))
        self.assertEqual(clock.session(D(2026, 10, 19)), (T(9, 30), T(16, 0)))
        self.assertIsNone(clock.session(D(2026, 10, 17)))
        self.assertEqual(clock.CALENDAR_YEARS, (2026, 2027))

    def test_early_closes_have_42_slots(self):
        self.assertEqual(sorted(clock.NYSE_EARLY_CLOSE), ["2026-11-27", "2026-12-24", "2027-11-26"])
        for day in clock.NYSE_EARLY_CLOSE:
            d = D.fromisoformat(day)
            self.assertEqual(clock.slots_of_day(day), 42, day)
            self.assertEqual(clock.slots_of_day(d), 42, day)
            self.assertEqual(clock.session(d), (T(9, 30), T(13, 0)), day)
            self.assertEqual(clock.slot_of(DT.combine(d, T(12, 55))), 41, day)
            self.assertIsNone(clock.slot_of(DT.combine(d, T(13, 0))), day)
            self.assertEqual(clock.entry_window(d), (2, 35), day)
            self.assertEqual(clock.flat_slot(d), 40, day)
        self.assertEqual(clock.slots_of_day("2026-10-19"), 78)
        self.assertEqual(clock.slots_of_day(D(2026, 10, 19)), 78)
        self.assertEqual((clock.SESSION_SLOTS, clock.EARLY_SLOTS, clock.BAR_MIN, clock.OPEN_MIN), (78, 42, 5, 570))


class Grid(unittest.TestCase):
    def test_slot_of_on_the_grid(self):
        day = D(2026, 10, 19)
        self.assertEqual(clock.slot_of(DT.combine(day, T(9, 30))), 0)
        self.assertEqual(clock.slot_of(DT.combine(day, T(9, 35))), 1)
        self.assertEqual(clock.slot_of(DT.combine(day, T(10, 10))), 8)
        self.assertEqual(clock.slot_of(DT.combine(day, T(15, 55))), 77)
        for k in range(78):
            self.assertEqual(clock.slot_of(clock.slot_time(day, k)), k)

    def test_slot_of_off_the_grid_or_outside_the_session(self):
        day = D(2026, 10, 19)
        self.assertIsNone(clock.slot_of(DT.combine(day, T(16, 0))))        # the 16:00 print
        self.assertIsNone(clock.slot_of(DT.combine(day, T(9, 25))))        # pre-market
        self.assertIsNone(clock.slot_of(DT.combine(day, T(10, 31))))       # off the 5-minute grid
        self.assertIsNone(clock.slot_of(DT.combine(day, T(10, 30, 15))))   # a live tick with seconds
        self.assertIsNone(clock.slot_of(DT.combine(day, T(10, 49, 45))))
        self.assertIsNone(clock.slot_of(DT(2026, 10, 18, 10, 30)))         # Sunday
        self.assertIsNone(clock.slot_of(DT(2026, 12, 25, 10, 30)))         # Christmas

    def test_slot_time_and_close_minutes(self):
        self.assertEqual(clock.slot_time("2026-10-19", 8), DT(2026, 10, 19, 10, 10))
        self.assertEqual(clock.slot_time(D(2026, 10, 19), 77), DT(2026, 10, 19, 15, 55))
        self.assertEqual(clock.slot_time(D(2026, 10, 19), 0), DT(2026, 10, 19, 9, 30))
        self.assertEqual(clock.slot_close_minutes(8), 615)                 # 10:15
        self.assertEqual(clock.clock12(clock.slot_close_minutes(8)), "10:15 AM")
        self.assertEqual(clock.slot_close_minutes(0), 575)                 # 9:35
        self.assertEqual(clock.slot_close_minutes(77), 960)                # 16:00
        self.assertEqual(clock.slot_close_minutes(76), 955)                # 15:55, the flat-by close


class Windows(unittest.TestCase):
    def test_entry_window_defaults(self):
        day = D(2026, 10, 19)
        self.assertEqual(clock.entry_window(day), (2, 71))
        self.assertEqual(clock.entry_window(day, {}), (2, 71))
        self.assertEqual(clock.entry_window(day, None), (2, 71))
        self.assertEqual(clock.clock12(clock.slot_close_minutes(2)), "9:45 AM")
        self.assertEqual(clock.clock12(clock.slot_close_minutes(71)), "3:30 PM")

    def test_entry_window_from_the_config(self):
        day = D(2026, 10, 19)
        self.assertEqual(clock.entry_window(day, {"no_entries_before": "10:00", "no_entries_after": "12:00"}), (5, 29))
        self.assertEqual(clock.entry_window(day, {"flat_by": "15:45"}), (2, 71))
        self.assertEqual(clock.entry_window(day, {"flat_by": "15:30"}), (2, 70))       # always before flat_by
        self.assertEqual(clock.entry_window(day, {"no_entries_before": "09:30"}), (0, 71))
        # a late no_entries_after is capped a bar before flat_by (15:55 -> the 15:50 close, slot 75) ...
        self.assertEqual(clock.entry_window(day, {"no_entries_after": "16:30"}), (2, 75))
        # ... and never past n - 2 even when flat_by is after the close
        self.assertEqual(clock.entry_window(day, {"no_entries_after": "16:30", "flat_by": "16:30"}), (2, 76))
        self.assertEqual(clock.entry_window(day, {"no_entries_before": None, "no_entries_after": ""}), (2, 71))
        with self.assertRaises(clock.ClockError):
            clock.entry_window(day, {"no_entries_before": "ten"})

    def test_entry_window_on_an_early_close(self):
        self.assertEqual(clock.entry_window(D(2026, 11, 27)), (2, 35))
        self.assertEqual(clock.clock12(clock.slot_close_minutes(35)), "12:30 PM")   # 15:30 moved up 3 hours
        self.assertEqual(clock.entry_window(D(2026, 12, 24), {"no_entries_after": "15:00"}), (2, 29))

    def test_flat_slot(self):
        day = D(2026, 10, 19)
        self.assertEqual(clock.flat_slot(day), 76)
        self.assertEqual(clock.flat_slot("2026-11-27"), 40)
        self.assertEqual(clock.flat_slot(day, {"flat_by": "15:45"}), 74)
        self.assertEqual(clock.flat_slot(day, {"flat_by": "09:30"}), 0)
        self.assertEqual(clock.flat_slot(day, {"flat_by": "16:30"}), 77)
        # the config's flat_by is a full-day time: on an early close it moves up with the 1:00 close
        self.assertEqual(clock.flat_slot(D(2026, 11, 27), {"flat_by": "15:30"}), 35)
        self.assertEqual(clock.flat_slot(D(2026, 11, 27), {"flat_by": "12:30"}), 0)


class Parsing(unittest.TestCase):
    def test_parse_now_new_york_and_z(self):
        self.assertEqual(clock.parse_now("2026-10-19 10:15"), DT(2026, 10, 19, 14, 15))
        self.assertEqual(clock.parse_now("2026-10-19T10:15"), DT(2026, 10, 19, 14, 15))
        self.assertEqual(clock.parse_now("2026-10-19T14:15:00Z"), DT(2026, 10, 19, 14, 15))
        self.assertEqual(clock.parse_now("2026-10-19 10:15-04:00"), DT(2026, 10, 19, 14, 15))
        self.assertEqual(clock.parse_now("2026-10-19 10:15+00:00"), DT(2026, 10, 19, 10, 15))
        self.assertEqual(clock.parse_now("2026-01-19 10:15"), DT(2026, 1, 19, 15, 15))   # winter: -5
        self.assertEqual(clock.parse_now("  2026-10-19 10:15  "), DT(2026, 10, 19, 14, 15))

    def test_parse_now_empty_is_now_and_bad_text_refuses(self):
        now = clock.parse_now("")
        self.assertIsInstance(now, DT)
        self.assertIsNone(now.tzinfo)
        self.assertLessEqual(abs((now - clock.utc_now()).total_seconds()), 5)
        self.assertIsNone(clock.parse_now(None).tzinfo)
        for bad in ("yesterday", "10:15", "2026-13-01 10:15", "2026-10-19 25:00"):
            with self.assertRaises(clock.ClockError, msg=bad):
                clock.parse_now(bad)
        try:
            clock.parse_now("tomorrow")
        except clock.ClockError as e:
            self.assertIn("2026-10-19 10:15", str(e))
            self.assertIn("Z", str(e))

    def test_parse_hhmm_and_minutes(self):
        self.assertEqual(clock.parse_hhmm("09:45"), T(9, 45))
        self.assertEqual(clock.parse_hhmm(" 9:45 "), T(9, 45))
        self.assertEqual(clock.parse_hhmm("15:55"), T(15, 55))
        self.assertEqual(clock.minutes_of(T(9, 45)), 585)
        self.assertEqual(clock.minutes_of(T(15, 55)), 955)
        for bad in ("25x", "9", "24:00", "10:60", None, "", "ten past"):
            with self.assertRaises(clock.ClockError, msg=repr(bad)):
                clock.parse_hhmm(bad, "flat_by")
        try:
            clock.parse_hhmm("x", "flat_by")
        except clock.ClockError as e:
            self.assertIn("flat_by wants HH:MM", str(e))


class Labels(unittest.TestCase):
    def test_clock12(self):
        self.assertEqual(clock.clock12(615), "10:15 AM")
        self.assertEqual(clock.clock12(0), "12:00 AM")
        self.assertEqual(clock.clock12(720), "12:00 PM")
        self.assertEqual(clock.clock12(955), "3:55 PM")
        self.assertEqual(clock.clock12(960), "4:00 PM")
        self.assertEqual(clock.clock12(1439), "11:59 PM")
        self.assertEqual(clock.clock12(575.0), "9:35 AM")

    def test_clock_label(self):
        self.assertEqual(clock.clock_label(T(10, 15)), "10:15 AM")
        self.assertEqual(clock.clock_label(DT(2026, 10, 19, 15, 55)), "3:55 PM")
        self.assertEqual(clock.clock_label(T(0, 5)), "12:05 AM")
        self.assertEqual(clock.clock_label(T(12, 0)), "12:00 PM")
        self.assertEqual(clock.clock_label(T(13, 0)), "1:00 PM")

    def test_day_label(self):
        self.assertEqual(clock.day_label(D(2026, 10, 19)), "Mon 2026-10-19")
        self.assertEqual(clock.day_label(D(2026, 11, 27)), "Fri 2026-11-27")


if __name__ == "__main__":
    unittest.main()
