# Migrating off the Select.Live emulation (Grafana / InfluxDB)

Older selpi versions shipped a small set of statistics commands that re-published a
hand-picked subset of SP PRO values:

| Removed command | What it did |
| --- | --- |
| `stat` / `http` | ~12 variables as a descriptive JSON list (served with Basic auth on `:8000`) |
| `stat-select` / `http-select` | ~20 values in **Select.Live's own point-API JSON shape**, so an existing dashboard that scraped a Select.Live device kept working against selpi |

These have been **removed**. Everything they produced (and ~300 fields more) is available
from the add-on's HTTP API, which is now the single source of truth. This document is for
anyone who was feeding the old `http-select` endpoint into Grafana/InfluxDB and needs to
re-point their collector.

> If you are using the Home Assistant add-on + integration (or the ESPHome component), you
> do not need any of this — the sensors are already published to Home Assistant. This page
> is only for a standalone Grafana/InfluxDB pipeline.

## Why it was removed

The Select.Live emulation was lossy and, in places, wrong — the code said so itself:

- **Energies were mislabelled.** The keys were named `*_wh_*` but the values were kWh
  (the payload even carried `"comment": "energies are actually in kWh, not Wh"`).
- **`shunt_w` assumed shunt 1 is always solar** (`# TODO: assumes shunt 1 is always a solar shunt`).
- **`grid_out_wh_*` were guesses** (`# unverified guess`).
- **`grid_w` was the AC-source/generator variable**, not a split grid import/export.
- **`fault_code`, `fault_ts`, `gen_status` were missing.**

The new API exposes the underlying SP PRO values directly, under SP LINK-faithful names, with
the sign and status handling done properly (e.g. grid vs. generator is resolved from Generator
Status + Output Mode, and signed powers are split into non-negative in/out parts).

## The new endpoint

```
GET /api/live[?screens=now,today,tech,dc,ac,ac2,service,settings,unit]
```

Default screens are `now,today`. Response:

```json
{
  "rows": [
    {
      "key": "batt_soc_percent",
      "label": "Battery SoC Percent",
      "splink_label": "...",
      "screen": "NowSubTab",
      "value": 99.08,
      "text": "99 %",
      "unit": "%",
      "words": ["..."],
      "note": ""
    }
  ],
  "version_problems": [],
  "at": "2026-11-24T10:15:00"
}
```

`value` is the numeric/typed value (use this for Grafana); `text` is the formatted display
string; `unit` is the unit as SP LINK shows it (`kW`, `kWh`, `V`, `A`, `%`, …).

### Access

The add-on web server is **gated to Home Assistant ingress** — it refuses any peer that is not
the Supervisor / loopback. For an external collector:

- Set `SELPI_WEB_ALLOW_ANY=1`. A non-ingress peer is then allowed, but **read-only and only for
  the safe endpoints** `GET /api/live` and `GET /api/status`. Everything else — all writes,
  controls, config reads, backup downloads/deletes — still requires Home Assistant ingress and
  is refused with `403` from any other peer, even with this flag set. That is exactly what a
  Grafana/Telegraf collector needs, and nothing more.
- There is still no password on those two read endpoints, so keep this to a trusted LAN and do
  not expose the port to the internet.
- Alternatively, run the collector on the Home Assistant host and hit it over loopback (full
  access, local only), or scrape the TCP/ESPHome side if you run the ESP proxy.

## Field mapping

The old payload only ever had a narrow slice of fields. Here is the equivalent for each.
Note the unit change: the old energies claimed `Wh` but were kWh; the new fields are honestly
`kWh`.

| Old Select.Live key | New `/api/live` key | Unit | Notes |
| --- | --- | --- | --- |
| `battery_soc` | `batt_soc_percent` | % | |
| `battery_w` | `dc_battery_power` | kW | signed; or use `dc_battery_power_in` / `dc_battery_power_out` (non-negative split) |
| `load_w` | `load_ac_power` | kW | |
| `solarinverter_w` | AC-coupled solar (KACO) power — see the `*kaco*` fields in the catalog | kW | the old field was `CombinedKacoAcPowerHiRes` |
| `shunt_w` | `shunt1_power` | kW | old value was **negated** and assumed shunt 1 = solar; the new field is the raw shunt power (check `shunt1_name`) |
| `battery_in_wh_today` | `d_ck_wh_in_today` | kWh | old key said Wh, value was kWh |
| `battery_out_wh_today` | `d_ck_wh_out_today` | kWh | " |
| `load_wh_total` | `ac_load_kwh_total_acc` | kWh | " |
| `grid_w` | `grid_power_in` / `grid_power_out` | kW | old field mapped to the generator variable; the new fields resolve grid vs. generator from status + output mode |
| `grid_out_wh_*` | `ac_export_accumulated_today` (+ the `ac_export_*` catalog fields) | kWh | old values were flagged "unverified guess" |

For anything else, pull the full catalog (`GET /api/live?screens=all`, or `selpi live --list`)
and pick by `key`. `sppro/live_fields.json` is the authoritative list of ~300 fields.

## Grafana (direct, Infinity datasource)

Grafana core cannot scrape arbitrary JSON; install the **Infinity** datasource
(`yesoreyeram-infinity-datasource`) or the JSON API datasource.

Infinity query:

- **Type:** JSON · **Source:** URL · **Method:** GET
- **URL:** `http://<host>:<port>/api/live?screens=now,today`
- **Rows / root:** `rows`
- **Columns:** `key` (string), `label` (string), `value` (number), `unit` (string)
- Filter to the series you want, e.g. `key == "batt_soc_percent"`.

This gives **live values only** — Infinity keeps no history. For time-series graphs you still
need a time-series database (below) with something writing to it.

## Telegraf → InfluxDB (history)

The old pipeline was almost certainly a collector (Telegraf) writing Select.Live-shaped JSON
into InfluxDB, with Grafana reading InfluxDB. Keep InfluxDB + Grafana; re-point the collector
at `/api/live`. Because the response is an array of objects, use Telegraf's `json_v2` parser:

```toml
[[inputs.http]]
  urls = ["http://<host>:<port>/api/live?screens=now,today"]
  interval = "30s"
  data_format = "json_v2"

  [[inputs.http.json_v2]]
    [[inputs.http.json_v2.object]]
      path = "rows"              # the rows array
      tags = ["key", "unit"]     # label each point by field key + unit
      [inputs.http.json_v2.object.fields]
        value = "float"          # numeric value -> the measurement field
```

That writes one point per field per poll, tagged by `key`, so an InfluxDB/Grafana query filters
by e.g. `key = 'load_ac_power'`. Status/text fields (where `value` is not numeric) can be
dropped or captured separately via a second `object` block reading `text` as a string.

Set `interval` to taste; over Select.Live keep it modest (each read ties up the single device
session), over the ESP/TCP link it can be faster.
