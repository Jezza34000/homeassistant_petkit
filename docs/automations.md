# Local automations and stats

Recipes that complement the integration with locally-computed drink history,
trends, and daily/weekly/monthly stats. Useful in particular if you run with
local BLE polling and want a setup that survives without the PetKit cloud.

The integration already exposes today's pump runtime, battery, filter %, and
a `Pet drinking` binary sensor. What's missing is the per-day/week/month
aggregates that the PetKit app shows. The blueprint and package below add
those.

---

## Quick start: daily drink count only

If you just want a "drinks today" counter and don't care about weekly /
monthly trends:

1. Create a Counter helper: **Settings → Devices & services → Helpers →
   Create helper → Counter**. Name it `Fountain drinks today`.
2. Import the blueprint:

   ```
   https://github.com/Jezza34000/homeassistant_petkit/blob/main/blueprints/automation/petkit/fountain_drink_counter.yaml
   ```

3. Create an automation from it. Pick your fountain's _Pet drinking_ binary
   sensor, the counter you just made, and leave **Reset counter daily**
   enabled.

Done. The counter increments on every drink event and resets at midnight.

---

## Full app parity

This setup mirrors what the PetKit app shows on the fountain page (drinks today / week / month plus drink-time today and this week), entirely locally. Three companion blueprints add anomaly alerts on top.

### What you get

| Entity                                        | What it shows                                                                          |
| --------------------------------------------- | -------------------------------------------------------------------------------------- |
| `counter.petkit_fountain_drinks_total`        | Lifetime drinks since the package was added. Source of truth.                          |
| `sensor.petkit_fountain_drinks_today`         | Drinks today (resets at local midnight).                                               |
| `sensor.petkit_fountain_drinks_this_week`     | Drinks since Monday 00:00.                                                             |
| `sensor.petkit_fountain_drinks_this_month`    | Drinks since the 1st 00:00.                                                            |
| `sensor.petkit_fountain_drink_time_today`     | Total time the pet was drinking today.                                                 |
| `sensor.petkit_fountain_drink_time_this_week` | Drink time since Monday 00:00.                                                         |
| `sensor.petkit_fountain_drinks_final_today`   | Daily snapshot of today's count at 23:55. Used as a stable input to the median sensor. |
| `sensor.petkit_fountain_drinks_median`        | 7-day median of daily drink counts. Used by the anomaly-alert blueprints.              |

The integration already gives you `sensor.<fountain>_today_pump_run_time`
(BLE-reported, firmware-side), so you can compare HA's count to the
firmware's runtime if you want to spot polling gaps.

### Setup

1. Enable packages in `configuration.yaml` if you haven't already:

   ```yaml
   homeassistant:
     packages: !include_dir_named packages
   ```

2. Copy [`docs/examples/petkit_fountain_stats.yaml`][pkg] into
   `<config>/packages/petkit_fountain_stats.yaml`.

3. Edit the file: replace `binary_sensor.REPLACE_ME_pet_drinking` with your
   fountain's actual entity id (twice, both `history_stats` blocks).

4. Restart Home Assistant. The counter, utility meters, history_stats
   sensors, and the daily snapshot + 7-day median sensor will all appear.

5. Import the blueprint and create an automation:
   - **Pet drinking sensor**: your fountain's binary sensor.
   - **Drinks counter**: `counter.petkit_fountain_drinks_total`.
   - **Reset counter daily**: **OFF**. The utility meters handle the
     cycles. Leaving this on would zero the counter every night and break
     the weekly/monthly meters.

[pkg]: examples/petkit_fountain_stats.yaml

### How it fits together

```
binary_sensor.<fountain>_pet_drinking
    │
    ├─→ (drink-counter blueprint) ─→ counter.petkit_fountain_drinks_total ─┐
    │                                                                       │
    │                                    ┌──────────────────────────────────┤
    │                                    ↓                                  ↓
    │                       utility_meter (daily/weekly/monthly)            │
    │                                    │                                  │
    │                                    │  daily (sensor.*_drinks_today)   │
    │                                    ↓                                  │
    │                       trigger-template snapshot at 23:55              │
    │                                    │                                  │
    │                                    ↓                                  │
    │                       statistics sensor (7-day median) ←──────────────┘
    │                                    │
    │                                    ↓
    │                       fountain_*_drinks_alert blueprints
    │
    └─→ history_stats (drink time today / this week)
```

### Anomaly alerts

Three optional blueprints sit on top of the package. Each is a self-contained
automation that calls a notification action (or any action you pick) when the
fountain's drink pattern looks off.

| Blueprint                    | Default trigger                                            | Useful for                                  |
| ---------------------------- | ---------------------------------------------------------- | ------------------------------------------- |
| `fountain_no_drinks_alert`   | `Pet drinking` stayed `off` for 12h continuously.          | Catching a pet that has stopped drinking.   |
| `fountain_low_drinks_alert`  | At 20:00, today's count is below 30% of the 7-day median.  | Reduced intake before it becomes serious.   |
| `fountain_high_drinks_alert` | At 20:00, today's count is above 200% of the 7-day median. | Heat stress, illness, or fresh-water spike. |

Each blueprint exposes its threshold and check time as inputs. Defaults are
ballpark guesses meant for a single-cat household. Adjust to fit yours.

#### Calibration period

The low/high alerts compare today's count to a 7-day median. Until you have
7 days of data the median is unstable, so those alerts will be noisy or wrong
during the first week. The "no drinks" alert works from day one since it only
needs the binary sensor.

#### Setup

For each alert you want:

1. Settings → Automations & Scenes → Blueprints. Import:

   ```
   https://github.com/Jezza34000/homeassistant_petkit/blob/main/blueprints/automation/petkit/fountain_no_drinks_alert.yaml
   https://github.com/Jezza34000/homeassistant_petkit/blob/main/blueprints/automation/petkit/fountain_low_drinks_alert.yaml
   https://github.com/Jezza34000/homeassistant_petkit/blob/main/blueprints/automation/petkit/fountain_high_drinks_alert.yaml
   ```

2. Create an automation from each. The default action is a persistent
   notification in HA. Replace it with `notify.mobile_app_<your_phone>` or
   any other action via the UI.

3. For low/high alerts, the defaults already point at
   `sensor.petkit_fountain_drinks_today` and
   `sensor.petkit_fountain_drinks_median` from the package. If you have
   multiple fountains, point each automation at its respective sensors.

#### Notes

- Each alert fires at most once per check (single mode), so the time-of-day
  alerts can fire at most once per day.
- The "no drinks" alert resets each time the binary sensor goes back to `on`,
  so a single drink event during the day cancels the timer.
- These are intended as informational nudges, not medical guidance. False
  positives happen, especially during the calibration period and on travel
  days when the household pattern changes.

### Multiple fountains

The package and blueprint are written for a single fountain. For a second
one, duplicate the package file with a different name prefix (e.g.
`petkit_fountain2_stats.yaml`, rename all entity ids to `..._fountain2_..`),
and create a second automation from the same blueprint pointing at the
new counter and binary sensor.

### Troubleshooting

**Counter doesn't increment.** Check that your fountain's `Pet drinking`
binary sensor actually toggles when the pet drinks. If it stays `off`, the
issue is upstream of the automation: the integration isn't getting drink
events. Local BLE polling needs to be working, or cloud-relay (with
another PetKit device in the family) needs to be available.

**`utility_meter` shows 0 forever.** The source counter has to _change_ for
the utility meter to record anything. Confirm `counter.petkit_fountain_drinks_total`
is incrementing first. If you previously had the daily reset enabled in the
blueprint, the utility meters may have been confused by the counter going
backwards. Disable the reset, then restart Home Assistant to re-initialize
the meters.

**Drink time today is stuck.** The `history_stats` `start` template only
re-evaluates on a state change of the source binary sensor. If your pet
drinks once at 03:00 and never again that day, the sensor will keep
showing that one event's contribution until midnight rolls over and a new
drink happens. This is `history_stats` working as designed; the displayed
total is correct, just sticky between events.

**Median sensor is `unknown`.** The 7-day median sensor is fed by the
trigger-template snapshot, which only fires at 23:55 each day. So the
median stays `unknown` until at least one snapshot has been recorded
(i.e., the day after you install the package). Low/high alerts won't
fire while the median is `unknown`.

---

## Per-pet drinking (camera fountains)

Fountains with a camera (Eversweet Ultra AI) identify which pet is drinking.
Two sensors expose that:

| Entity                                | State                                 | Attributes                                                                                    |
| ------------------------------------- | ------------------------------------- | --------------------------------------------------------------------------------------------- |
| `sensor.<fountain>_last_pet_to_drink` | Name of the pet who drank last.       | `timestamp`, `pet_id`, `event_id` of that drink.                                              |
| `sensor.<fountain>_raw_drink_data`    | Number of drink events in the record. | `events`: one `timestamp` / `pet_id` / `pet_name` / `event_id` entry per drink, newest first. |

The state of _Last pet to drink_ is `Unknown` when the fountain could not
identify the pet. Refills are not counted as drinks.

PetKit only keeps these records for a limited time (longer with Care+), so
the `events` list is a rolling window, and it is not stored by the recorder.

### Refill after a specific pet drank

The pet name does not change when the same pet drinks twice in a row, so
trigger on the `timestamp` attribute and check the name in a condition:

```yaml
triggers:
  - trigger: state
    entity_id: sensor.fountain_last_pet_to_drink
    attribute: timestamp
conditions:
  - condition: state
    entity_id: sensor.fountain_last_pet_to_drink
    state: "Rex"
actions:
  - action: button.press
    target:
      entity_id: button.fountain_refill
```

### Drinks per pet

```yaml
template:
  - sensor:
      - name: "Moon drinks"
        state: >
          {{ state_attr('sensor.fountain_raw_drink_data', 'events')
             | default([], true)
             | selectattr('pet_name', 'eq', 'Moon') | list | count }}
```
