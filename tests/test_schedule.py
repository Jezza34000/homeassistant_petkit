"""Unit tests for OpenPetBowl schedule helpers (no Home Assistant)."""

from __future__ import annotations

from datetime import datetime, timedelta
import importlib.util
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest

import pytest

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
amount_config_for_feeder = _mod.amount_config_for_feeder
plan_weekdays_iso = _mod.plan_weekdays_iso
set_plan_weekdays = _mod.set_plan_weekdays
get_openpetbowl_attributes = _mod.get_openpetbowl_attributes
validate_row = _mod.validate_row
device_now = _mod.device_now
feeding_plan_state = _mod.feeding_plan_state
find_scheduled_feed = _mod.find_scheduled_feed
scheduled_feed_refusal = _mod.scheduled_feed_refusal
set_plan_suspended = _mod.set_plan_suspended
_status_from_record = _mod._status_from_record  # noqa: SLF001


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
    """OEM ``repeats`` count 1=Sunday; the card contract is ISO 1=Monday."""

    def test_oem_sunday_is_iso_sunday(self):
        """OEM day 1 is ISO day 7."""
        assert oem_weekday_to_iso(1) == 7
        assert iso_weekday_to_oem(7) == 1

    def test_oem_monday_is_iso_monday(self):
        """OEM day 2 is ISO day 1."""
        assert oem_weekday_to_iso(2) == 1
        assert iso_weekday_to_oem(1) == 2

    def test_round_trip(self):
        """Every OEM day survives a conversion to ISO and back."""
        for oem in range(1, 8):
            assert iso_weekday_to_oem(oem_weekday_to_iso(oem)) == oem


class TestFlatten(unittest.TestCase):
    """Plans become card rows: ISO weekdays, display amounts, stable keys."""

    def test_mini_shape_a_iso_weekdays(self):
        """A shape-A plan yields one row with no weekdays and a display amount."""
        plan = _shape_a_plan(
            [{"id": 100001, "time": 18000, "name": "Breakfast", "amount": 15}],
            repeats="2,3,4,5,6",  # Mon–Fri OEM
        )
        feeder = _feeder(device_type="feedermini", plan=plan)
        rows = flatten_schedule(feeder)
        assert len(rows) == 1
        assert rows[0]["weekdays"] is None
        assert rows[0]["hour"] == 5
        # Mini stores display x 5, so wire 15 is 3 portions.
        assert rows[0]["values"] == [3]
        assert rows[0]["label"] == "Breakfast"
        assert rows[0]["key"] == "18000"

    def test_d3_per_day_groups_weekdays(self):
        """Identical per-day items collapse into one row listing both ISO days."""
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
        assert len(rows) == 1
        assert rows[0]["weekdays"] == [1, 2]
        assert rows[0]["values"] == [20]

    def test_dual_values(self):
        """A dual-hopper item exposes both amounts and two compartments."""
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
        assert rows[0]["values"] == [2, 3]
        assert capabilities_for_feeder(feeder)["compartments"] == 2


class TestInterpolators(unittest.TestCase):
    """Add, edit and remove rewrite the feedDailyList the cloud expects."""

    def test_mini_add_joins_existing_mask_and_allocates_id(self):
        """A new mini meal joins the existing mask and gets a fresh client id."""
        plan = _shape_a_plan(
            [{"id": 100001, "time": 18000, "name": "A", "amount": 10}],
            repeats="2",  # Monday
        )
        feeder = _feeder(device_type="feedermini", plan=plan)
        days = add_schedule_entry(
            feeder, hour=12, minute=0, values=[15], weekdays=[3], label="Lunch"
        )
        mon = next(d for d in days if str(d["repeats"]) in ("2", 2))
        times = sorted(it["time"] for it in mon["items"])
        assert times == [18000, 43200]
        new = next(it for it in mon["items"] if it["time"] == 43200)
        assert int(new["id"]) >= 100002
        # Days outside the mask carry no repeats, so the mask survives the save.
        assert len([d for d in days if d["repeats"] == ""]) == 6

    def test_mini_duplicate_time_rejected(self):
        """Two meals may not share an hour and minute."""
        plan = _shape_a_plan(
            [{"id": 100001, "time": 18000, "name": "A", "amount": 10}],
            repeats="2",
        )
        feeder = _feeder(device_type="feedermini", plan=plan)
        with pytest.raises(ValueError):
            add_schedule_entry(feeder, hour=5, minute=0, values=[10], weekdays=[1])

    def test_mini_gap_rejected(self):
        """A meal inside the 5-minute gap of another is refused."""
        plan = _shape_a_plan(
            [{"id": 100001, "time": 18000, "name": "A", "amount": 10}],
            repeats="2",
        )
        feeder = _feeder(device_type="feedermini", plan=plan)
        with pytest.raises(ValueError):
            add_schedule_entry(feeder, hour=5, minute=2, values=[10], weekdays=[1])

    def test_d3_add_patches_selected_days_only(self):
        """A D3 add touches only the OEM days behind the chosen ISO weekdays."""
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
        assert len(by_oem["2"]["items"]) == 1
        assert len(by_oem["3"]["items"]) == 1
        assert len(by_oem["1"]["items"]) == 0
        assert by_oem["2"]["items"][0]["amount"] == 25

    def test_d3_edit_moves_weekdays(self):
        """Editing weekdays moves the item between per-day buckets."""
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
        assert len(by_oem["2"]["items"]) == 0
        assert len(by_oem["6"]["items"]) == 1  # ISO 5 = Friday = OEM 6
        assert by_oem["6"]["items"][0]["amount"] == 30

    def test_remove_by_key(self):
        """Removing by key drops the item from every day."""
        plan = _shape_a_plan(
            [
                {"id": 100001, "time": 18000, "name": "A", "amount": 10},
                {"id": 100002, "time": 43200, "name": "B", "amount": 15},
            ],
            repeats="2,3,4,5,6,7,1",
        )
        feeder = _feeder(device_type="feedermini", plan=plan)
        days = remove_schedule_entry(feeder, "18000")
        for day in days:
            ids = [str(it.get("id")) for it in day["items"]]
            assert "100001" not in ids

    def test_dual_values_length_enforced(self):
        """Dual hoppers need exactly two values, and both reach the wire."""
        empty = [
            SimpleNamespace(repeats=str(n), suspended=0, items=[]) for n in range(1, 8)
        ]
        plan = SimpleNamespace(
            items=None, feed_daily_list=empty, repeats=None, suspended=None
        )
        feeder = _feeder(device_type="d4s", plan=plan)
        with pytest.raises(ValueError):
            add_schedule_entry(
                feeder, hour=8, minute=0, values=[3], weekdays=[1], label="A"
            )
        days = add_schedule_entry(
            feeder, hour=8, minute=0, values=[3, 4], weekdays=[1], label="A"
        )
        mon = next(d for d in days if str(d["repeats"]) in ("2", 2))
        assert mon["items"][0]["amount1"] == 3
        assert mon["items"][0]["amount2"] == 4

    def test_openpetbowl_set_converts_iso_weekdays(self):
        """A full set converts ISO weekdays to the matching OEM day bucket."""
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
        assert len(sun["items"]) == 1
        mon = next(d for d in days if str(d["repeats"]) == "2")
        assert len(mon["items"]) == 0

    def test_mini_set_and_edit_keep_plan_mask(self):
        """Set and edit preserve the plan mask and the server-assigned meal id."""
        plan = _shape_a_plan(
            [{"id": 100001, "time": 18000, "name": "A", "amount": 10}],
            repeats="2",  # Monday
        )
        feeder = _feeder(device_type="feedermini", plan=plan)
        previous = [
            {
                "repeats": "2",
                "suspended": 0,
                "count": 1,
                "items": [{"id": 100001, "time": 18000, "amount": 10}],
            },
        ] + [
            {"repeats": str(n), "suspended": 0, "count": 0, "items": []}
            for n in (1, 3, 4, 5, 6, 7)
        ]
        days = schedule_to_feed_daily_list(
            [{"key": "18000", "hour": 6, "minute": 0, "values": [12], "label": "A"}],
            feeder,
            previous=previous,
        )
        mon = next(d for d in days if str(d["repeats"]) in ("2", 2))
        assert len(mon["items"]) == 1
        assert mon["items"][0]["time"] == 21600
        assert mon["items"][0]["id"] == 100001
        days = edit_schedule_entry(
            feeder, key="18000", hour=7, minute=0, values=[10], label="A"
        )
        mon = next(d for d in days if str(d["repeats"]) in ("2", 2))
        assert mon["items"][0]["time"] == 25200
        # The edited meal keeps its server id.
        assert mon["items"][0]["id"] == 100001
        assert len([d for d in days if d["repeats"] == ""]) == 6


class TestCapabilities(unittest.TestCase):
    """Per-family capability flags the card reads off the device."""

    def test_mini_is_shared_mask_not_weekly(self):
        """The mini shares one mask, so it is not weekly and needs labels."""
        caps = capabilities_for_feeder(_feeder(device_type="feedermini"))
        assert not caps["weekly"]
        assert "shared_weekdays" not in caps
        assert caps["global_toggle"]
        assert caps["amount"]["unit"] == "portions"
        assert caps["amount"]["alternate_unit"] == {
            "unit_of_measurement": "cup",
            "conversion_factor": 0.05,
        }
        assert "min_gap_seconds" not in caps
        assert caps["labels"] == {"required": True}
        assert "skip_today" in caps["actions"]

    def test_d1_requires_labels(self):
        """D1 requires meal labels and keeps the global toggle."""
        caps = capabilities_for_feeder(_feeder(device_type="feeder"))
        assert caps["labels"] == {"required": True}
        assert not caps["weekly"]
        assert "shared_weekdays" not in caps
        assert caps["global_toggle"]

    def test_d3_grams_and_independent_days(self):
        """D3 is grams with independently editable days."""
        caps = capabilities_for_feeder(_feeder(device_type="d3"))
        assert caps["amount"]["unit"] == "g"
        assert "alternate_unit" not in caps["amount"]
        assert caps["weekly"]
        assert "shared_weekdays" not in caps
        assert caps["global_toggle"]
        assert "min_gap_seconds" not in caps

    def test_d4s_keeps_global_toggle_and_requires_labels(self):
        """D4S/D4SH/D4H pause by re-saving every day, so the toggle stays."""
        for device_type in ("d4s", "d4sh", "d4h"):
            caps = capabilities_for_feeder(_feeder(device_type=device_type))
            assert caps["global_toggle"], device_type
            assert caps["weekly"], device_type
            assert caps["labels"] == {"required": True}, device_type
            assert "shared_weekdays" not in caps

    def test_d4_keeps_global_toggle(self):
        """D4 keeps the global toggle and shows portions."""
        caps = capabilities_for_feeder(_feeder(device_type="d4"))
        assert caps["global_toggle"]
        assert caps["amount"]["unit"] == "portions"
        assert caps["amount"]["alternate_unit"] == {
            "unit_of_measurement": "cup",
            "conversion_factor": 0.1,
        }

    def test_cup_fraction_follows_the_device_factor(self):
        """D4/D4h read the portion size off ``settings.factor``; D1 is fixed."""
        feeder = _feeder(device_type="d4h")
        feeder.settings = SimpleNamespace(factor=25)
        caps = capabilities_for_feeder(feeder)
        assert caps["amount"]["alternate_unit"]["conversion_factor"] == 0.25
        caps = capabilities_for_feeder(_feeder(device_type="feeder"))
        assert caps["amount"]["alternate_unit"]["conversion_factor"] == 0.2

    def test_dual_hoppers_show_bare_portions(self):
        """D4s/D4sh render the tick count as-is, so no cup fraction is offered."""
        for device_type in ("d4s", "d4sh"):
            caps = capabilities_for_feeder(_feeder(device_type=device_type))
            assert caps["amount"]["unit"] == "portions", device_type
            assert "alternate_unit" not in caps["amount"], device_type


def _record(**kw):
    """One daily-feed record item; ``state`` is a plain namespace."""
    state = kw.pop("state", None)
    return SimpleNamespace(state=state, **kw)


class TestStatus(unittest.TestCase):
    """D1/D2 have no ``result``; D3-class families do, with family codes."""

    def test_shape_a_success_needs_no_result_field(self):
        """Shape A reports a scheduled dispense from ``completed_at`` alone."""
        item = _record(
            status=0,
            src=1,
            time=18000,
            state=SimpleNamespace(err_code=None, completed_at="2026-09-05 05:00:12"),
        )
        assert _status_from_record(item, 40000, device_type="feedermini") == (
            "dispensed",
            "dispensed_schedule",
        )

    def test_shape_a_string_err_code_is_a_failure(self):
        """A non-empty ``err_code`` string marks the meal failed."""
        item = _record(
            status=0,
            src=1,
            time=18000,
            state=SimpleNamespace(err_code="ia-1", completed_at=None),
        )
        assert _status_from_record(item, 40000, device_type="feeder") == (
            "failed",
            "error",
        )

    def test_shape_a_manual_offline_needs_no_completed_at(self):
        """A manual dispense counts even without ``completed_at``."""
        item = _record(
            status=0,
            src=4,
            time=18000,
            state=SimpleNamespace(err_code=None, completed_at=None),
        )
        assert _status_from_record(item, 40000, device_type="feeder") == (
            "dispensed",
            "dispensed_local",
        )

    def test_d3_surplus_is_result_six(self):
        """D3 signals a surplus skip with result 6."""
        item = _record(
            status=0, src=1, time=18000, state=SimpleNamespace(result=6, real_amount=0)
        )
        assert _status_from_record(item, 40000, device_type="d3") == (
            "skipped",
            "surplus_skipped",
        )

    def test_d4sh_surplus_is_result_eight(self):
        """D4sh signals a surplus skip with result 8."""
        item = _record(
            status=0, src=1, time=18000, state=SimpleNamespace(result=8, real_amount1=0)
        )
        assert _status_from_record(item, 40000, device_type="d4sh") == (
            "skipped",
            "surplus_skipped",
        )

    def test_d4_result_six_is_not_surplus(self):
        """Only D3/D4h/D4sh have surplusControl; on D4 result 6 is a dispense."""
        item = _record(
            status=0, src=1, time=18000, state=SimpleNamespace(result=6, real_amount=20)
        )
        assert _status_from_record(item, 40000, device_type="d4") == (
            "dispensed",
            "dispensed_schedule",
        )

    def test_d3_nonzero_result_with_food_is_dispensed(self):
        """A non-zero result that still moved food is a dispense."""
        item = _record(
            status=0, src=1, time=18000, state=SimpleNamespace(result=1, real_amount=15)
        )
        assert _status_from_record(item, 40000, device_type="d3")[0] == "dispensed"

    def test_overdue_meal_is_not_reported_pending(self):
        """A meal past its time with no record reads unknown, not pending."""
        item = _record(status=2, src=1, time=18000, state=None)
        assert _status_from_record(item, 40000, device_type="feeder") == (
            "unknown",
            "past_unknown",
        )


class TestAmountScaling(unittest.TestCase):
    """Wire ``amount`` is display x a per-family divisor."""

    def test_scaled_families_are_shown_in_portions(self):
        """Portion families share the same 1-10 display range."""
        for device_type in ("feeder", "feedermini", "d4", "d4h"):
            with self.subTest(device_type=device_type):
                amount = amount_config_for_feeder(_feeder(device_type=device_type))
                assert (amount["min"], amount["max"], amount["step"]) == (1, 10, 1)
                assert amount["unit"] == "portions"

    def test_d1_round_trips_one_portion_as_twenty_wire_units(self):
        """D1 divides the wire amount by 20 and multiplies back on save."""
        feeder = _feeder(
            device_type="feeder",
            plan=_shape_a_plan(
                [{"id": 100001, "time": 18000, "name": "A", "amount": 60}],
                repeats="2",
            ),
        )
        assert flatten_schedule(feeder)[0]["values"] == [3]
        days = edit_schedule_entry(
            feeder, key="18000", hour=5, minute=0, values=[4], label="A"
        )
        mon = next(d for d in days if str(d["repeats"]) == "2")
        assert mon["items"][0]["amount"] == 80

    def test_d4_uses_the_device_factor(self):
        """D4 scales by ``settings.factor`` rather than a fixed divisor."""
        feeder = _feeder(
            device_type="d4",
            plan=SimpleNamespace(
                feed_daily_list=[
                    SimpleNamespace(
                        repeats="2",
                        suspended=0,
                        items=[
                            SimpleNamespace(
                                id=5,
                                time=18000,
                                name="A",
                                amount=75,
                                amount1=None,
                                amount2=None,
                                device_id=1,
                                device_type=11,
                                pet_amount=None,
                            )
                        ],
                    )
                ]
            ),
        )
        feeder.settings = SimpleNamespace(factor=25)
        assert flatten_schedule(feeder)[0]["values"] == [3]

    def test_d3_is_grams_and_does_not_scale(self):
        """D3 amounts are grams and pass through unscaled."""
        amount = amount_config_for_feeder(_feeder(device_type="d3"))
        assert amount["unit"] == "g"
        assert amount["step"] == 1

    def test_dual_hoppers_are_raw_ticks(self):
        """Dual-hopper amounts are raw picker ticks starting at zero."""
        amount = amount_config_for_feeder(_feeder(device_type="d4s"))
        assert (amount["min"], amount["max"], amount["step"]) == (0, 10, 1)


class TestRowValidation(unittest.TestCase):
    """Every family's meal editor applies the same four checks."""

    DAYS = [
        {"repeats": "2", "suspended": 0, "items": [{"id": 1, "time": 18000}]},
    ]

    def _validate(self, device_type, values, **kw):
        kw.setdefault("hour", 5)
        kw.setdefault("minute", 2)
        kw.setdefault("label", "Meal")
        validate_row(
            _feeder(device_type=device_type),
            self.DAYS,
            values=values,
            weekdays=[1],
            **kw,
        )

    def test_gap_applies_to_every_family(self):
        """A meal inside the 5-minute window is refused on all seven families."""
        for device_type, values in (
            ("feeder", [10]),
            ("feedermini", [10]),
            ("d3", [10]),
            ("d4", [10]),
            ("d4h", [10]),
            ("d4s", [1, 1]),
            ("d4sh", [1, 1]),
        ):
            with self.subTest(device_type=device_type), pytest.raises(ValueError):
                self._validate(device_type, values)

    def test_only_the_new_meal_is_checked(self):
        """Two existing meals already close together do not block an add."""
        days = [
            {
                "repeats": "2",
                "suspended": 0,
                "items": [{"id": 1, "time": 18000}, {"id": 2, "time": 18060}],
            },
        ]
        validate_row(
            _feeder(device_type="d4s"),
            days,
            hour=12,
            minute=0,
            values=[1, 1],
            weekdays=[1],
            label="Noon",
        )

    def test_meal_needs_a_name(self):
        """A missing or blank name is refused on every family."""
        for label in (None, "", "  "):
            with self.subTest(label=label), pytest.raises(ValueError):
                self._validate("d4s", [1, 1], hour=12, minute=0, label=label)
        self._validate("d4s", [1, 1], hour=12, minute=0, label="Lunch")

    def test_zero_amount_is_refused(self):
        """A meal must dispense something from at least one hopper."""
        with pytest.raises(ValueError):
            self._validate("d4s", [0, 0], hour=12, minute=0)
        with pytest.raises(ValueError):
            self._validate("d3", [0], hour=12, minute=0)
        self._validate("d4s", [0, 2], hour=12, minute=0)


class TestEmptyPlanMask(unittest.TestCase):
    """A D1/Mini plan with no meals still remembers its weekday mask."""

    def _empty_mini(self, repeats="2,4,6"):
        return _feeder(
            device_type="feedermini", plan=_shape_a_plan([], repeats=repeats)
        )

    def test_mask_is_read_from_an_empty_plan(self):
        """The mask reads back as ISO weekdays with no meals present."""
        assert plan_weekdays_iso(self._empty_mini()) == [1, 3, 5]

    def test_first_add_keeps_the_mask(self):
        """The first meal added to an empty plan joins the existing mask."""
        days = add_schedule_entry(
            self._empty_mini(), hour=8, minute=0, values=[2], label="A"
        )
        in_mask = sorted(str(d["repeats"]) for d in days if d["items"])
        assert in_mask == ["2", "4", "6"]
        assert len([d for d in days if d["repeats"] == ""]) == 4


class TestPlanPause(unittest.TestCase):
    """The feeding-schedule switch: its state and the per-day pause."""

    @staticmethod
    def _per_day_plan(suspended):
        item = SimpleNamespace(
            id=0, time=28800, name="A", amount=0, amount1=2, amount2=1
        )
        return SimpleNamespace(
            items=None,
            repeats=None,
            suspended=None,
            feed_daily_list=[
                SimpleNamespace(repeats=str(n), suspended=suspended, items=[item])
                for n in range(1, 8)
            ],
        )

    def test_state_is_none_without_a_plan(self):
        """No plan means no switch."""
        assert feeding_plan_state(_feeder(device_type="d4s", plan=None)) is None

    def test_state_follows_the_days(self):
        """All days paused reads 0; any open day reads 1."""
        assert (
            feeding_plan_state(_feeder(device_type="d4s", plan=self._per_day_plan(1)))
            == 0
        )
        assert (
            feeding_plan_state(_feeder(device_type="d4s", plan=self._per_day_plan(0)))
            == 1
        )

    def test_shape_a_state_follows_the_plan_flag(self):
        """D1/Mini carry one whole-plan flag."""
        plan = _shape_a_plan([{"id": 100001, "time": 18000, "name": "A", "amount": 10}])
        plan.suspended = 1
        assert feeding_plan_state(_feeder(device_type="feedermini", plan=plan)) == 0

    def test_pause_marks_every_day_and_recomputes_totals(self):
        """Pausing sets ``suspended`` on all seven days and keeps the meals."""
        feeder = _feeder(device_type="d4s", plan=self._per_day_plan(0))
        days = set_plan_suspended(feeder, True)
        assert len(days) == 7
        assert all(d["suspended"] == 1 for d in days)
        assert all(d["count"] == 1 for d in days)
        assert days[0]["totalAmount1"] == 2
        assert days[0]["totalAmount2"] == 1
        assert days[0]["items"][0]["time"] == 28800

    def test_resume_clears_every_day(self):
        """Resuming clears ``suspended`` on all seven days."""
        feeder = _feeder(device_type="d4h", plan=self._per_day_plan(1))
        assert all(d["suspended"] == 0 for d in set_plan_suspended(feeder, False))


def _records(day, *items):
    return SimpleNamespace(feed=[SimpleNamespace(day=day, items=list(items))])


class TestSkipGuard(unittest.TestCase):
    """Skip/restore is refused whenever the app would hide its checkbox."""

    def test_finds_todays_plan_meal_by_time(self):
        """Only today's src-1 meal at that time matches."""
        meal = _record(id="s1", src=1, time=18000, status=0)
        manual = _record(id="m1", src=2, time=18000, status=0)
        feeder = _feeder(
            device_type="d4",
            records=SimpleNamespace(
                feed=[
                    SimpleNamespace(day=20260101, items=[meal]),
                    SimpleNamespace(day=20260102, items=[manual]),
                ]
            ),
        )
        assert find_scheduled_feed(feeder, 20260101, 18000) is meal
        assert find_scheduled_feed(feeder, 20260102, 18000) is None
        assert find_scheduled_feed(feeder, 20260101, 18060) is None

    def test_pending_meal_can_be_skipped_but_not_restored(self):
        """A waiting meal skips; restoring it makes no sense."""
        meal = _record(src=1, time=18000, status=0)
        assert scheduled_feed_refusal(meal, skip=True, now_secs=10000) is None
        assert scheduled_feed_refusal(meal, skip=False, now_secs=10000)

    def test_skipped_meal_can_be_restored_but_not_skipped_again(self):
        """A cancelled meal restores; skipping it again is refused."""
        meal = _record(src=1, time=18000, status=1)
        assert scheduled_feed_refusal(meal, skip=False, now_secs=10000) is None
        assert scheduled_feed_refusal(meal, skip=True, now_secs=10000)

    def test_overdue_and_dispensing_meals_are_untouchable(self):
        """Status 2 (not started) and 3 (dispensing) refuse both actions."""
        for status in (2, 3):
            meal = _record(src=1, time=18000, status=status)
            for skip in (True, False):
                with self.subTest(status=status, skip=skip):
                    assert scheduled_feed_refusal(meal, skip=skip, now_secs=10000)

    def test_past_time_is_refused_even_without_a_result(self):
        """Once the device clock passes the meal time, the app hides the toggle."""
        meal = _record(src=1, time=18000, status=0)
        assert scheduled_feed_refusal(meal, skip=True, now_secs=18001)
        cancelled = _record(src=1, time=18000, status=1)
        assert scheduled_feed_refusal(cancelled, skip=False, now_secs=18001)

    def test_meal_with_a_result_has_run(self):
        """A record with a state has already run."""
        meal = _record(src=1, time=18000, status=0, state=SimpleNamespace(result=0))
        assert scheduled_feed_refusal(meal, skip=True, now_secs=10000)

    def test_device_clock_uses_the_reported_offset(self):
        """``timezone`` is the device's UTC offset in hours."""
        feeder = _feeder(device_type="d4")
        feeder.timezone = 5.5
        now = device_now(feeder)
        assert now.utcoffset() == timedelta(hours=5, minutes=30)
        feeder.timezone = None
        assert device_now(feeder).utcoffset() is None


class TestShapeAToday(unittest.TestCase):
    """The mask is invisible to the card, but it still gates skip-today."""

    @staticmethod
    def _rows_for(mask_oem):
        plan = _shape_a_plan(
            [{"id": 100001, "time": 18000, "name": "A", "amount": 10}],
            repeats=mask_oem,
        )
        return flatten_schedule(_feeder(device_type="feedermini", plan=plan))

    def test_weekdays_stay_hidden_whatever_the_mask(self):
        """The card never sees the mask, whatever it contains."""
        for mask in ("2", "1,2,3,4,5,6,7", "2,4,6"):
            with self.subTest(mask=mask):
                assert self._rows_for(mask)[0]["weekdays"] is None

    def test_today_follows_the_plan_mask(self):
        """``today`` is true only when the mask covers the current day."""
        today_oem = iso_weekday_to_oem(datetime.now().isoweekday())
        other_oem = 1 + (today_oem % 7)
        assert self._rows_for(str(today_oem))[0]["today"]
        assert not self._rows_for(str(other_oem))[0]["today"]

    def test_everyday_plan_always_runs_today(self):
        """A full mask always runs today."""
        assert self._rows_for("1,2,3,4,5,6,7")[0]["today"]


class TestPlanWeekdays(unittest.TestCase):
    """The plan-level mask: readable and settable, but never on the card."""

    def _mini(self, repeats="2,4,6"):
        return _feeder(
            device_type="feedermini",
            plan=_shape_a_plan(
                [{"id": 100001, "time": 18000, "name": "A", "amount": 10}],
                repeats=repeats,
            ),
        )

    def test_reads_mask_as_iso_weekdays(self):
        """The OEM mask reads back as ISO weekdays."""
        # OEM 2,4,6 = Mon, Wed, Fri = ISO 1,3,5
        assert plan_weekdays_iso(self._mini()) == [1, 3, 5]

    def test_sunday_round_trips(self):
        """Sunday survives both directions of the conversion."""
        assert plan_weekdays_iso(self._mini(repeats="1")) == [7]
        assert set_plan_weekdays(self._mini(), [7]) == "1"

    def test_set_converts_iso_to_oem_csv(self):
        """Setting ISO weekdays writes an OEM CSV mask."""
        assert set_plan_weekdays(self._mini(), [1, 3, 5]) == "2,4,6"

    def test_set_sorts_and_deduplicates(self):
        """The written mask is sorted and free of duplicates."""
        assert set_plan_weekdays(self._mini(), [5, 1, 1, 7]) == "1,2,6"

    def test_set_rejects_an_empty_mask(self):
        """An empty weekday list is refused."""
        with pytest.raises(ValueError):
            set_plan_weekdays(self._mini(), [])

    def test_shape_b_has_no_plan_mask(self):
        """Shape B has no plan-level mask to read or write."""
        d3 = _feeder(device_type="d3", plan=None)
        assert plan_weekdays_iso(d3) is None
        with pytest.raises(ValueError):
            set_plan_weekdays(d3, [1, 3, 5])

    def test_attribute_is_present_for_mini_and_absent_for_d3(self):
        """Only shape-A feeders expose ``plan_weekdays``."""
        assert get_openpetbowl_attributes(self._mini())["plan_weekdays"] == [1, 3, 5]
        assert "plan_weekdays" not in get_openpetbowl_attributes(
            _feeder(device_type="d3", plan=None)
        )

    def test_mask_never_leaks_into_the_card_rows(self):
        """Card rows carry no weekdays for a shape-A feeder."""
        rows = flatten_schedule(self._mini())
        assert rows[0]["weekdays"] is None


if __name__ == "__main__":
    unittest.main()
