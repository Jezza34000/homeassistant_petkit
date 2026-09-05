"""Unit tests for OpenPetBowl schedule helpers (no Home Assistant)."""

from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
_SCHEDULE_PATH = ROOT / "custom_components" / "petkit" / "schedule.py"
_spec = importlib.util.spec_from_file_location("petkit_schedule", _SCHEDULE_PATH)
assert _spec and _spec.loader
_mod = importlib.util.module_from_spec(_spec)
sys.modules["petkit_schedule"] = _mod
_spec.loader.exec_module(_mod)

add_schedule_entry = _mod.add_schedule_entry
capabilities_for_feeder = _mod.capabilities_for_feeder
edit_schedule_entry = _mod.edit_schedule_entry
flatten_schedule = _mod.flatten_schedule
iso_weekday_to_oem = _mod.iso_weekday_to_oem
oem_weekday_to_iso = _mod.oem_weekday_to_iso
remove_schedule_entry = _mod.remove_schedule_entry
schedule_to_feed_daily_list = _mod.schedule_to_feed_daily_list


def _feeder(*, device_type: str, plan=None, multi=None, records=None, feeder_id=1):
    return SimpleNamespace(
        id=feeder_id,
        device_nfo=SimpleNamespace(device_type=device_type, type=6),
        feed_plan=plan,
        multi_feed_item=multi,
        device_records=records,
    )


def _shape_a_plan(items, repeats="2,3,4,5,6", suspended=0):
    return SimpleNamespace(
        items=[SimpleNamespace(**it) if isinstance(it, dict) else it for it in items],
        repeats=repeats,
        suspended=suspended,
        feed_daily_list=None,
    )


class TestWeekdays(unittest.TestCase):
    def test_oem_sunday_is_iso_sunday(self):
        self.assertEqual(oem_weekday_to_iso(1), 7)
        self.assertEqual(iso_weekday_to_oem(7), 1)

    def test_oem_monday_is_iso_monday(self):
        self.assertEqual(oem_weekday_to_iso(2), 1)
        self.assertEqual(iso_weekday_to_oem(1), 2)

    def test_round_trip(self):
        for oem in range(1, 8):
            self.assertEqual(iso_weekday_to_oem(oem_weekday_to_iso(oem)), oem)


class TestFlatten(unittest.TestCase):
    def test_mini_shape_a_iso_weekdays(self):
        plan = _shape_a_plan(
            [{"id": 100001, "time": 18000, "name": "Breakfast", "amount": 15}],
            repeats="2,3,4,5,6",  # Mon–Fri OEM
        )
        feeder = _feeder(device_type="feedermini", plan=plan)
        rows = flatten_schedule(feeder)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["weekdays"], [1, 2, 3, 4, 5])
        self.assertEqual(rows[0]["hour"], 5)
        self.assertEqual(rows[0]["values"], [15])
        self.assertEqual(rows[0]["label"], "Breakfast")
        self.assertEqual(rows[0]["key"], "100001")

    def test_d3_per_day_groups_weekdays(self):
        item = SimpleNamespace(id=21600, time=21600, name="A", amount=20)
        plan = SimpleNamespace(
            items=None,
            repeats=None,
            suspended=None,
            feed_daily_list=[
                SimpleNamespace(repeats="1", suspended=0, items=[]),  # Sunday empty
                SimpleNamespace(repeats="2", suspended=0, items=[item]),  # Monday
                SimpleNamespace(repeats="3", suspended=0, items=[item]),  # Tuesday
            ]
            + [
                SimpleNamespace(repeats=str(n), suspended=0, items=[])
                for n in range(4, 8)
            ],
        )
        feeder = _feeder(device_type="d3", plan=plan)
        rows = flatten_schedule(feeder)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["weekdays"], [1, 2])
        self.assertEqual(rows[0]["values"], [20])

    def test_dual_values(self):
        item = SimpleNamespace(
            id="18000", time=18000, name="R1", amount=0, amount1=2, amount2=3
        )
        plan = SimpleNamespace(
            items=None,
            feed_daily_list=[
                SimpleNamespace(repeats="1", suspended=0, items=[item]),
            ]
            + [
                SimpleNamespace(repeats=str(n), suspended=0, items=[])
                for n in range(2, 8)
            ],
        )
        feeder = _feeder(device_type="d4s", plan=plan)
        rows = flatten_schedule(feeder)
        self.assertEqual(rows[0]["values"], [2, 3])
        self.assertEqual(capabilities_for_feeder(feeder)["compartments"], 2)


class TestInterpolators(unittest.TestCase):
    def test_mini_add_merges_mask_and_allocates_id(self):
        plan = _shape_a_plan(
            [{"id": 100001, "time": 18000, "name": "A", "amount": 10}],
            repeats="2",  # Monday
        )
        feeder = _feeder(device_type="feedermini", plan=plan)
        days = add_schedule_entry(
            feeder, hour=12, minute=0, values=[15], weekdays=[3], label="Lunch"
        )
        # OEM 2=Mon, 4=Wed (ISO 3)
        mon = next(d for d in days if str(d["repeats"]) in ("2", 2))
        times = sorted(it["time"] for it in mon["items"])
        self.assertEqual(times, [18000, 43200])
        new = next(it for it in mon["items"] if it["time"] == 43200)
        self.assertGreaterEqual(int(new["id"]), 100002)
        wed = next(d for d in days if str(d["repeats"]) in ("4", 4))
        self.assertEqual(len(wed["items"]), 2)

    def test_mini_duplicate_time_rejected(self):
        plan = _shape_a_plan(
            [{"id": 100001, "time": 18000, "name": "A", "amount": 10}],
            repeats="2",
        )
        feeder = _feeder(device_type="feedermini", plan=plan)
        with self.assertRaises(ValueError):
            add_schedule_entry(feeder, hour=5, minute=0, values=[10], weekdays=[1])

    def test_mini_gap_rejected(self):
        plan = _shape_a_plan(
            [{"id": 100001, "time": 18000, "name": "A", "amount": 10}],
            repeats="2",
        )
        feeder = _feeder(device_type="feedermini", plan=plan)
        with self.assertRaises(ValueError):
            add_schedule_entry(feeder, hour=5, minute=2, values=[10], weekdays=[1])

    def test_d3_add_patches_selected_days_only(self):
        empty = [
            SimpleNamespace(repeats=str(n), suspended=0, items=[]) for n in range(1, 8)
        ]
        plan = SimpleNamespace(
            items=None, feed_daily_list=empty, repeats=None, suspended=None
        )
        feeder = _feeder(device_type="d3", plan=plan)
        days = add_schedule_entry(
            feeder, hour=8, minute=0, values=[25], weekdays=[1, 2], label="AM"
        )
        # ISO 1,2 = OEM 2,3
        by_oem = {str(d["repeats"]): d for d in days}
        self.assertEqual(len(by_oem["2"]["items"]), 1)
        self.assertEqual(len(by_oem["3"]["items"]), 1)
        self.assertEqual(len(by_oem["1"]["items"]), 0)
        self.assertEqual(by_oem["2"]["items"][0]["amount"], 25)

    def test_d3_edit_moves_weekdays(self):
        item = SimpleNamespace(id=28800, time=28800, name="AM", amount=25)
        days = []
        for n in range(1, 8):
            items = [item] if n == 2 else []
            days.append(SimpleNamespace(repeats=str(n), suspended=0, items=items))
        plan = SimpleNamespace(
            items=None, feed_daily_list=days, repeats=None, suspended=None
        )
        feeder = _feeder(device_type="d3", plan=plan)
        out = edit_schedule_entry(
            feeder,
            key="28800",
            hour=8,
            minute=0,
            values=[30],
            weekdays=[5],
            label="AM",
        )
        by_oem = {str(d["repeats"]): d for d in out}
        self.assertEqual(len(by_oem["2"]["items"]), 0)
        self.assertEqual(len(by_oem["6"]["items"]), 1)  # ISO 5 = Friday = OEM 6
        self.assertEqual(by_oem["6"]["items"][0]["amount"], 30)

    def test_remove_by_key(self):
        plan = _shape_a_plan(
            [
                {"id": 100001, "time": 18000, "name": "A", "amount": 10},
                {"id": 100002, "time": 43200, "name": "B", "amount": 15},
            ],
            repeats="2,3,4,5,6,7,1",
        )
        feeder = _feeder(device_type="feedermini", plan=plan)
        days = remove_schedule_entry(feeder, "100001")
        for day in days:
            ids = [str(it.get("id")) for it in day["items"]]
            self.assertNotIn("100001", ids)

    def test_dual_values_length_enforced(self):
        empty = [
            SimpleNamespace(repeats=str(n), suspended=0, items=[]) for n in range(1, 8)
        ]
        plan = SimpleNamespace(
            items=None, feed_daily_list=empty, repeats=None, suspended=None
        )
        feeder = _feeder(device_type="d4s", plan=plan)
        with self.assertRaises(ValueError):
            add_schedule_entry(feeder, hour=8, minute=0, values=[3], weekdays=[1])
        days = add_schedule_entry(
            feeder, hour=8, minute=0, values=[3, 4], weekdays=[1]
        )
        mon = next(d for d in days if str(d["repeats"]) in ("2", 2))
        self.assertEqual(mon["items"][0]["amount1"], 3)
        self.assertEqual(mon["items"][0]["amount2"], 4)

    def test_openpetbowl_set_converts_iso_weekdays(self):
        feeder = _feeder(device_type="d3")
        days = schedule_to_feed_daily_list(
            [
                {
                    "key": "1",
                    "hour": 7,
                    "minute": 0,
                    "values": [10],
                    "weekdays": [7],  # Sunday
                    "label": "Sun",
                }
            ],
            feeder,
        )
        sun = next(d for d in days if str(d["repeats"]) == "1")
        self.assertEqual(len(sun["items"]), 1)
        mon = next(d for d in days if str(d["repeats"]) == "2")
        self.assertEqual(len(mon["items"]), 0)


if __name__ == "__main__":
    unittest.main()
