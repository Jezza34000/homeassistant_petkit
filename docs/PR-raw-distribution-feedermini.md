# Pull request: RAW distribution data for Fresh Element Mini and inferred schedules

## Summary

- **Issue:** The `raw_distribution_data` sensor did not appear for **Fresh Element Mini** (`FEEDERMINI`) despite active feeding plans in the PetKit app. The same gap could affect other feeders when `multiFeedItem` or the first weekday’s items were empty but schedule data existed elsewhere in the API payloads.
- **Fix:** Resolve schedule **time/amount pairs** through a clear fallback chain in `custom_components/petkit/utils.py`: `**multiFeedItem.feedDailyList`** (any weekday with items) → `**state.feedState.feedTimes**` (dict or list shapes) → `**device_records.feed**` (daily feed / record merge). Expose `**amount`-only** synthetic `feed_daily_list` rows for inferred paths so UIs that infer dual-hopper from populated `amount1`/`amount2` do not misclassify single-hopper Mini devices.

---

## Background

The `raw_distribution_data` sensor is defined in `sensor.py` with `force_add=[D4H, D4SH]`. All other feeders, including **Fresh Element Mini**, are only registered if `PetKitDescSensorBase._check_value_support` returns a **non-`None`** value from `get_raw_feed_plan_from_schedule(device)`.

That helper originally required `feeder.multi_feed_item.feed_daily_list[0].items` and returned `None` if `multi_feed_item` was missing or the first day had no items — so the entity was never added.

---

## Root cause (validated with Home Assistant debug logs)

### H1 — CONFIRMED (`FEEDERMINI`)

Runtime log (entity support check):

```text
reject_reason=multi_feed_item_missing … has_multi=False … pair_count=0
```

PetKit’s `**device_detail**` payload for this device **does not include `multiFeedItem`**, so `feeder.multi_feed_item` stays `None` after Pydantic parsing. The app still shows plans because schedule information is available through other API surfaces (state and/or daily feed records), not through the structured multi-feed block.

### Failed first fallback — `feedTimes`

Initial implementation read only **non-empty dict** `feedState.feedTimes`. On the user’s Mini, that did not produce pairs (`pair_count=0`), likely due to **missing, empty, or non-dict/list** shapes versus the test fixture’s `{"18000": 1, …}` form.

### Working fallback — `device_records.feed`

PetKit client already fetches `**FeederRecord`** in parallel after device data (`pypetkitapi` `get_devices_data`: main tasks then record tasks). Logs after implementing fallback:

```text
schedule_source=device_records … pair_count=3
plan_out … result_segments=3 out_is_none=False
feedermini supports 'RAW distribution data'
```

Schedule slots were correctly derived from `**device_records.feed**` item times and amounts.

### D3 (Fresh Element 3) — related

Some devices expose `multi_feed_item.feed_daily_list` with **seven day entries** while **weekday index 0 has no `items`** even though another day does. The original code only inspected **day `[0]`**, so it behaved like “no schedule”. **Fix:** iterate **all** days in `feedDailyList` and use the **first day that has non-empty `items`**.

---

## Implemented solution

### 1. Schedule pair resolution (`utils.py`)

Introduced small helpers and a single resolver:

| Source                                | Purpose                                                                                                                   |
| ------------------------------------- | ------------------------------------------------------------------------------------------------------------------------- |
| `_schedule_pairs_from_multi_feed`     | Walk **all** `feedDailyList` entries; return pairs from the **first** day with items.                                     |
| `_schedule_pairs_from_feed_times`     | Support `**feedTimes` as dict** (seconds → portion/amount) **or list** of `{time/t, amount/…}` / `[time, amount]` tuples. |
| `_schedule_pairs_from_device_records` | Aggregate **unique times** from `device_records.feed[*].items[*]` with summed amounts per time.                           |
| `resolve_feed_schedule_pairs`         | Ordered chain: multi → feed_times → device_records; returns `list[tuple[int, int]]`.                                      |

`get_raw_feed_plan_from_schedule` and `get_raw_schedule` both use this resolver.

### 2. `get_raw_feed_plan_from_schedule`

Unchanged behavior for status string: build `status_lookup` from `device_records.feed` and append `idx,hours,minutes,amount,state` per resolved pair (under HA state length limits).

### 3. `get_raw_schedule` synthetic `feed_daily_list`

When `multi_feed_item` is absent, build a **seven-day** synthetic list from resolved pairs for **attributes** consumed by schedule UIs.

**Shape decision (single-hopper, PetKit-aligned):** synthetic items include `**time`**, `**name**`, `**amount**`, `**id**` — **no `amount1` / `amount2` keys**. PetKit’s typical single-hopper payloads **omit** unused hopper fields; emitting `amount1`/`amount2` (even as `0`) interacts badly with front-ends that treat “both hopper fields defined” as **dual-hopper** (e.g. `isDualFromItems` using `a1 != null && a2 != null` where numeric `0` is still not `null` in JavaScript).

### 4. No library change required

`pypetkitapi` already models `Feeder.multi_feed_item`, `FeedState.feed_times`, and merges `FeederRecord`; the fix is **entirely** in the Home Assistant integration schedule helpers.

---

## Verification

| Evidence                                                                  | Interpretation                            |
| ------------------------------------------------------------------------- | ----------------------------------------- |
| `hypothesis=H_ok`, `reject_reason=None`, `schedule_source=device_records` | Fallback chain succeeds for Mini.         |
| `pair_count` matches expected slots (e.g. 3), `plan_out` non-empty        | State string populated.                   |
| `feedermini supports 'RAW distribution data'`                             | Entity passes `is_supported`.             |
| Sensor count increases (e.g. 15 → 17)                                     | Entities registered for affected feeders. |
| FE3 (`d3`) same path when multi day 0 empty                               | Multi-day scan + records fallback.        |

---

## Out of scope / explicitly not shipped

- **No** changes to the dispenser schedule **Lovelace card** in this repo for `isDualFromItems`; single-hopper shape is enforced at integration output to match PetKit’s omission pattern.
- **No** synchronous file/debug logging on the HA event loop (debug session moved to integration `LOGGER` only; final code removes temporary debug spam).

---

## Files touched

Primary:

- `custom_components/petkit/utils.py` — resolver helpers, `get_raw_feed_plan_from_schedule`, `get_raw_schedule` synthetic branch, docstrings.

Optional follow-ups (future PRs):

- Document `feed_daily_list` / `amount`-only inferred rows in `**docs/petkit.md`** (dispenser-schedule-card) if maintainers want user-facing wording.
- If PetKit exposes a dedicated “schedule-only” endpoint for Mini with stable structure, optionally prefer it over heuristic record parsing (would need API evidence).

---

## Checklist for reviewers

- Confirm HA reload reloads inferred `feed_daily_list` attributes for Mini **without** `amount1`/`amount2` on synthetic rows.
- Confirm `**raw_distribution_data`** entity exists for **FEEDERMINI** and state updates after coordinator refreshes.
- Sanity-check **dual-hopper** devices still use `**multi`** path first (unchanged when `multi_feed_item` is present).
