# Protocol

The communication protocol with the SP Pro appears to consist of requests for blocks of memory, with knowledge of the structures in that memory to be reimplemented by the requested.

<!-- TOC -->

- [Protocol](#protocol)
- [Memory space](#memory-space)
- [Messages](#messages)
	- [Query](#query)
	- [Write](#write)
	- [Example Messages](#example-messages)
		- [Hello](#hello)
- [Authentication](#authentication)

<!-- /TOC -->

# Memory space

Memory is addressed as 2 byte words.

Memory of interest appears to be within the 0xa000 - 0xafff range.

# Messages

## Query

A query request consists of:

    TLAAAACC

where:

 * T is the message type - Q for Query.
 * L is a byte indicating the number (length) of 2 byte words to return (0x00 will return 1 word).
 * AAAA is a little endian word address (shifting the address by 1 shifts by 2 bytes).
 * CC is a Cyclic Redundancy Check.

A query response consists of:

 * TLAAAACC the exact bytes from the query (including CRC).
 * MM the requested memory (the number of 2 byte words specified by L + 1).
 * CC CRC of the query + memory.

It's possible to request between 0-255 bytes of memory, 

## Write

A write request consists of:

    TLAAAACCDDCC

where:

 * T is the message type - W for Write.
 * L is a byte indicating the number (length) of 2 byte words to be written.
 * AAAA is a little endian word address (shifting the address by 1 shifts by 2 bytes).
 * CC is a Cyclic Redundancy Check (everything to the left).
 * DD is the data to write, between 1 and 256 words (as specified by L + 1).

## Example Messages

### Hello

Prior to authentication SP LINK sends a request for 0 words from `0xa000` and the SP Pro will respond with the appropriate CRC calculation.

E.g:

```
Query:    0x510000a000009d4b
            TTLLAAAAAAAACCCC
Response: 0x510000a000009d4b0100d819
            TTLLAAAAAAAACCCCMMMMCCCC
```

# Authentication

There are two separate mechanisms.

## Connection login

* **Direct serial / TCP:** the SP PRO does not answer until the client logs in: read 8 words at `0x1f0000`,
  append the password space-padded to 32 bytes, MD5 it, and write the (byte-swapped) digest back to `0x1f0000`
  (see `memory/protocol.py`).
* **Select.Live (`select.live:7528`, TLS):** a text handshake authenticates the account, after which the
  connection is a transparent tunnel to the SP PRO's serial port. No `0x1f0000` login is needed (SP LINK never
  sends one over Select.Live, and reads work without it):

```
S: LOGIN
C: USER:<account e-mail>:<account password>
S: OK
C: LIST DEVICES            (optional)
S: DEVICES:1 / DEVICE:<id>
C: CONNECT:<id>
S: READY                   (from here on: raw Q/W frames)
```

SP LINK negotiates TLS 1.0 (AES256-SHA). Select.Live occasionally fails to deliver a reply; it then closes
the connection about 60 s later, so clients should keep transfers short (e.g. download logs by date range)
and reconnect on failure.

## Write unlock (installer code)

Writing configuration needs the installer code. SP LINK does:

1. write `0x0000` to `0xca14` (reads `0xffff` normally)
2. write the installer code (decimal, one word) to `0xa001`, then `0x0000` to `0xa001`
3. write the configuration blocks (below)
4. write `0x0000` to `0xa002`, then `0xffff` to `0xca14`

SP LINK always rewrites every block, but single-word writes work too (tested 17 Nov 2026 with
`tools/write_test.py`, InitialReturnVoltage `0xc185` 2183 → 2184 → 2183): SP LINK also sets download mode
`0xa251` = 1 before step 1 and 0 after step 4; after step 2 `0xa002` reads 1 (unlocked). Each write is
echoed; read-back showed only the target word changed.

Setting the clock (`0x1d0011`, see `commands/settime.py`) and the log range registers below work without it.

# Configuration

SP LINK reads and writes the whole configuration as these blocks (word addresses):

| Block | Words | Content |
|---|---|---|
| `0xc000` | 197 | Common settings, slot 0–196 |
| `0xc0e5` | 18 | Common settings, slot 197–214 (written as `0xc0e5`×9 + `0xc0ef`×8 in SP LINK 15.40) |
| `0xc100` | 125 | Application type settings |
| `0xc180` | 60 | Battery type settings |
| `0xc800` | 193 | System scheduler settings |
| `0xc201` | 40 | Service settings (read only by SP LINK) |

SP LINK's configuration file (`.SPLC`, CSV, "Configuration Settings Version Number: 48") lists the raw
word values under the firmware's variable names, in sections `COMMON` / `APPLICATION TYPE` / `BATTERY TYPE` /
`SYSTEM SCHEDULER`. Slot *n* of a section is at the block base + *n* (Common slots ≥ 197 continue at
`0xc0e5`); unnamed slots are reserved. All 623 named settings of SP PRO firmware 15.47 were verified this way
(mapping in `docs/config-map.json`). Example: `InitialReturnVoltage` = `0xc185`, in mV per cell
(2183 × 24 cells = 52.4 V on a 48 V system).

# Scale factors

`0xa028`–`0xa02d`: `CommonScaleForAcVolts`, `AcCurrent`, `DcVolts`, `DcCurrent`, `Temperature`,
`InternalVoltages`. Log records repeat most of them, so each record can be converted on its own. The
conversions are the ones in `memory/converter.py` (DC V = raw × DcVolts / 327680; DC A = raw × DcCurrent /
327680; AC W = raw × AcV × AcI / (32768 × 800), signed variant / (32768 × 100); Wh = raw × 24 × V × I /
3276800; °C = raw × Temperature / 32768; % = raw / 256).

# Data logs

Four logs, each a ring of fixed-size records spread over one or more memory segments, timestamps as `uint32`
seconds since 2001-01-01 00:00 **local time**.

| Log | Descriptor | Range write | Result | Record | Example segments |
|---|---|---|---|---|---|
| Operational events | `0xa2a7` | `0xa2d4` | `0xa2d8` | 36 words | `0xd8000–0xdffff`, `0xe0000–0xe7fff` |
| Alerts | `0xa266` | `0xa293` | `0xa297` | 36 words | `0xe8000–0xeffff`, `0xf0000–0xf7fff` |
| Detailed (15 min) | `0xa335` | `0xa374` | `0xa378` | 44 words | 12 segments `0x80000–…` |
| Daily summary | `0xa2e8` | `0xa321` | `0xa325` | 36 words | `0xc8000–0xcffff`, `0xd0000–0xd7fff` |

* **Descriptor** (5 words): segment count, record size (words), newest record address (`uint32`), capacity
  (records). It is followed by the segment list: per segment a start and end address (`uint32` each).
* **Download:** write `0x0001` to `0xa251` (download mode), then per log write the range (5 words:
  `uint32` from, `uint32` to, `0x0001`; the range register is followed directly by the result block, so
  the 5th word is the result's status word and writing 1 starts the search), poll the result (6 words: status, `uint32` first record address,
  `uint32` last record address, count; status 1 = searching, 3 = found, 4 = none), read the records from
  first to last (following the segments), and finally write `0x0000` to `0xa251`.
* SP LINK's CSV export encodes each event's record address in the timestamp fraction:
  `816593971.371392` → record at `0x80000 + 371392`.

## Detailed data record (44 words)

| Word | Field | Type / conversion |
|---|---|---|
| 0 | timestamp | uint32 |
| 2 | Inverter AC power (avg) | int32, AC W |
| 4, 6, 8, 10 | DC input, DC output, battery in, battery out (kWh this interval) | uint32, Wh |
| 12–14 | DC voltage avg, min, max | DC V |
| 15–17 | DC mid voltage avg, at min DC V, at max DC V | DC V |
| 18–20 | Inverter DC current, shunt 1, shunt 2 (avg) | int16, DC A |
| 21–23 | Load AC power avg, load max, AC input power | int16, AC W signed |
| 24 | AC load voltage | AC V (raw × AcV / 327680) |
| 25 | AC load frequency | /100 Hz |
| 26–28 | Transformer, heatsink, battery temperature (max) | °C |
| 29, 30 | AC-coupled power (avg), AC-coupled energy | AC W signed, Wh |
| 31 | State of charge | /256 % |
| 32–35 | AC input, AC load, shunt 1, shunt 2 kWh | Wh (AC, AC, DC, DC) |
| 36, 37 | (unknown small counters, 0–10) | |
| 38 | AC export kWh | AC Wh |
| 39–43 | scale factors AcV, AcI, DcV, DcI, Temperature | |

## Event / alert record (36 words)

| Word | Field | Type / conversion |
|---|---|---|
| 0 | timestamp | uint32 |
| 2 | event code (see table) | uint16 |
| 3, 4 | DC voltage, DC mid voltage | DC V |
| 5–7 | Inverter DC current, shunt 1, shunt 2 | int16, DC A |
| 8, 10 | Load AC power, inverter AC power | int32, AC W |
| 12 | AC input power | int16, AC W signed |
| 13 | AC load voltage | AC V |
| 14 | State of charge | /256 % |
| 15 | AC frequency | /100 Hz |
| 16–21 | Transformer, heatsink 1, heatsink 2, battery, internal, power module temperature | °C |
| 22 | Operational mode | 2 = On, 3 = Sync |
| 23 | Battery charge mode | 0 = Initial, 1 = Bulk, 3 = Short Term Float |
| 24 | Contactor state | 1 = AcSourceOpened-InverterClosed, 3 = AcSourceClosed-InverterClosed |
| 25 | Generator status | 0 = Not Running, 10 = AC Source Present |
| 26, 27 | Generator start / running reason | 0 = Not Running |
| 28 | DC battery cable loss | DC W (raw × DcV × DcI / 3276800) |
| 29 | Fan speed | /256 % |
| 30 | BMS state of health | /256 % |
| 31, 33 | Discharge / charge current target | int16 × 100/256 A |
| 32, 34, 35 | scale factors AcI, DcI, Temperature | |

## Daily summary record (36 words)

| Word | Field | Type / conversion |
|---|---|---|
| 0 | timestamp (23:59:59 of the day) | uint32 |
| 2, 4 | DC input, DC output kWh (day) | uint32, DC Wh |
| 6, 8 | Battery in, battery out kWh (lifetime) | uint32, DC Wh |
| 10, 12 | AC input, AC load kWh (lifetime) | uint32, AC Wh |
| 14, 16 | Shunt 1, shunt 2 kWh (lifetime) | int32, DC Wh |
| 18 | AC input hours | uint32 minutes |
| 20 | AC export kWh | uint32, AC Wh |
| 22 | Inverter DC kWh | uint32, DC Wh |
| 24 | Modulation hours | uint32 minutes |
| 26 | Float hours today | uint16 minutes |
| 27–28 | AC-coupled solar kWh (lifetime) | uint32 **high word first**, AC Wh |
| 29 | Battery charge efficiency index | /2048 |
| 30–34 | scale factors AcV, AcI, DcV, DcI, Temperature | |

## Event codes

The complete operational (346) and alert (271) tables and all status enums are in `sppro/splink_tables.json`,
matching SP LINK 15.40's texts; the codes below were seen on a real unit.

### Seen 14–17 Nov

| Code | Type | Description |
|---|---|---|
| 4 | Operational | Charger - Initial Stage Start |
| 5 | Operational | Charger - Initial Stage End |
| 6 | Operational | Charger - Bulk Stage Start |
| 89 | Operational | AC Mode - Support (DC to AC Load) |
| 90 | Operational | AC Mode - Charge (AC Source to DC) |
| 91 | Operational | AC Mode - Export (DC to AC Source) |
| 98 | Operational | Relay 4 - On |
| 99 | Operational | Relay 1 - Off |
| 100 | Operational | Relay 2 - Off |
| 101 | Operational | Relay 3 - Off |
| 106 | Operational | Digital Control Out 1 - Off |
| 107 | Operational | Digital Control Out 2 - Off |
| 108 | Operational | Digital Control Out 3 - Off |
| 109 | Operational | Digital Control Input 1 - On |
| 110 | Alert | Unit - Real Time Clock - Reading newer time |
| 112 | Operational | Digital Control Input 4 - On |
| 113 | Operational | Digital Control Input 1 - Off |
| 116 | Operational | Digital Control Input 4 - Off |
| 148 | Operational | Generator Controller - Remain On - AC load is greater than Generator Remain On setting |
| 149 | Operational | Generator Controller - Remain On - AC load is lower than Generator Remain On setting |
| 178 | Operational | AC Mode - Standalone begin |
| 179 | Operational | AC Mode - Standalone end |
| 201 | Operational | AC Mode - Synchronised begin |
| 202 | Operational | AC Mode - Synchronised end |
| 231 | Operational | Digital Input - Grid Available selected |
| 232 | Operational | Digital Input - Grid Available inactive |
| 238 | Operational | Digital Input - Grid Fail Available selected |
| 239 | Operational | Digital Input - Grid Fail Available inactive |
| 275 | Operational | AC Source - Minimum Sync Voltage detected (S) |
| 304 | Operational | Solar Hybrid Priority 3 - Digital Input Asserted |
| 305 | Operational | Solar Hybrid Priority 3 - Digital Input De-asserted |
| 320 | Operational | Solar Hybrid Priority 3 - Begin |
| 321 | Operational | Solar Hybrid Priority 3 - End |
| 322 | Operational | Solar Hybrid Priority 4 - Begin |
| 323 | Operational | Solar Hybrid Priority 4 - End |
| 324 | Operational | Solar Hybrid Priority 5 - Begin |
| 325 | Operational | Solar Hybrid Priority 5 - End |
| 326 | Operational | Solar Hybrid Priority 6 - Begin |
| 327 | Operational | Solar Hybrid Priority 6 - End |
| 328 | Operational | Solar Hybrid Priority 7 - Begin |
| 329 | Operational | Solar Hybrid Priority 7 - End |
| 330 | Operational | Solar Hybrid Priority 8 - Begin |
| 331 | Operational | Solar Hybrid Priority 8 - End |
| 332 | Operational | Solar Hybrid SoC Recovery Begin |
| 333 | Operational | Solar Hybrid SoC Recovery End |
| 368 | Operational | System - Phase shift above threshold |

# Live data (SP LINK's data screens)

`sppro/live_fields.json` lists every value SP LINK's data screens show (288 fields: 249 numbers, 21 status
codes, 18 raw such as serial numbers), matching SP LINK 15.40, memory map version 31.
`selpi live` reads them; values were checked against the SP PRO's Modbus map on 17 Nov 2026 (load, inverter, grid, solar
power; reactive power; voltages; frequencies; battery V/SoC/power; today's energies; generator status; solar
hybrid priority; run hours all agree).

SP LINK reads one block per screen and indexes into it:

| Screen | Block | Examples |
|---|---|---|
| Now | `0xa058` × 85 | inverter AC power `0xa058` (s32), reactive `0xa05a`, battery V `0xa05c`, solar hybrid active `0xa070`, charge mode `0xa07c`, output mode `0xa07d`, gen start/run reason `0xa07e`/`0xa07f`, AC source status `0xa080`, SoC `0xa081`, source reactive `0xa082`, battery A `0xa084` (s32), shunt A/kW `0xa086`–`0xa089`, AC source V/A/Hz `0xa095`/`0xa08c`/`0xa08d`, load V/Hz `0xa090`/`0xa092`, load power `0xa07a` (s32), generator status `0xa096`, AC solar total `0xa09b`, grid power `0xa0ab` |
| Today | `0xa0af` × 68 | DC in/out `0xa0af`/`0xa0b1` (u32), battery in/out `0xa0b5`/`0xa0b7` (u32), AC load/input/export `0xa0ec`/`0xa0f0`/`0xa0ed`, AC solar `0xa0f2`, run/AC-input/float minutes `0xa0c1`/`0xa0c0`/`0xa0bc` |
| Technical | `0xa015` × 154 | DC inverter V `0xa02e`, battery power `0xa02f` (s32), temperatures `0xa037`–`0xa03e`, fan `0xa042`, midpoint `0xa046`, equalise/recharge days `0xa047`/`0xa048`/`0xa06e`/`0xa06f`, digital/analogue inputs `0xa052`–`0xa057` |
| Service | `0xa03f` × 73 | total run time `0xa03f` (u32, minutes) |
| DC history | `0xa127` × 140 | DC/inverter/shunt/battery kWh: previous day, 7/30/365 day, year, resettable, total |
| AC history | `0xa1ca` × 103, `0xa36e` × 18 | AC load/input/export/solar kWh and AC-input hours, same periods; lifetime totals at `0xa36e`–`0xa37f` |

Conversions (SP LINK shows kW/kWh): AC kW = raw × AcV × AcI / 3276800 / 1000 (16-bit) or / 26214400 / 1000
(32-bit); kWh = raw × 24 × V × I / 3276800 / 1000; V = raw × scale / 327680; A = raw × scale / 327680;
Hz = raw / 100; % = raw / 256; °C = s16 × Temperature / 32768; hours = minutes / 60.

Corrections to the older selpi variable map (`memory/variable.py`, an older memory map): load power is
`0xa07a` (not `0xa093`), grid power `0xa0ab` (not `0xa08a`); `DCkWhIn/OutToday` (`0xa0af`/`0xa0b1`) are the
inverter's DC energy including shunts, battery in/out today are `0xa0b5`/`0xa0b7`.

The memory-map version SP LINK branches on (`InverterMemoryMapVersionNumber`, newest 31) is reported at connect;
fields differ for versions below 20–25.

## Modbus profiles

This SP LINK (15.40, non-installer download) has no Modbus profile setting: only `ModbusRtuPort` (line format)
and `ModbusRtuAddress`. Modbus runs in the comms card (ACC) firmware; SP LINK embeds encrypted firmware images
(`ACC.bin`, `SP_PRO_V14_58.bin`, `SP_PRO_V12_25_*.bin`, `G3Unified.bin`; entropy 8.0) and only CRC-checks and
sends them, so the Modbus map can't be read from SP LINK. An installer build of SP LINK may differ.

# Other registers seen

| Address | Seen as |
|---|---|
| `0xa000` | 1; first request of every session ("hello") |
| `0xa114` | SP LINK writes 3 × `0x0000` on connect |
| `0xdf50` | 128 words; starts with the unit's serial/device number |
| `0xdfde` | SP LINK writes `0x0000` after connect |
| `0xa251` | download mode, 1 while a log/config download runs |
| `0xca14` | `0xffff` normally, `0x0000` while configuration is written |
| `0x1d0000` | real-time clock (8 words, BCD); polled ~1/s by SP LINK |


# Cross-reference: Selectronic Modbus RTU map

The SP PRO also offers a Modbus RTU map (registers 8000+, values pre-scaled, e.g. 0.1 V; `ModbusRtuPort`
`0xc0f5`, `ModbusRtuAddress` `0xc0f6`; `ModbusRtuPort` is the line format: 11 = 115200 8N1). It shares
the firmware's enums with the logs, confirmed by matching state changes against the event log:

| Modbus register | Meaning | Same as |
|---|---|---|
| 8022 | Solar hybrid priority in force (0 = none, e.g. grid down) | the N in "Solar Hybrid Priority N - Begin/End" events |
| 8060 | Generator status enum (0 Not Running, 1 Running, 6 Starting, 8 Stopping, 10 AC Source Present; full list in sppro/splink_tables.json) | event record word 25, Generator Status (0 = Not Running, 10 = AC Source Present) |
| 8084 | DC-coupled (shunt 1) power, negative for solar | shunt 1 values in the logs are negative for solar too |

### Version block and compatibility (0xa007 x25)
SP LINK reads this on connect (request type 17) and branches all display/config code on it.
Words: [0] config settings version, [1] service settings version, [2] memory map version,
[3] event / [4] detailed / [5] daily-summary log formats, [14] string-inverter support,
[15] comms card attached, [18] comms card revision, [20] comms card software.
SP LINK 15.40's "latest" values are 48 / 14 / 31 / 3 / 3 / 5 (`ResetAllVersionToLatest`). It handles
any older value; if any is newer it says "This version of SP LINK is older than the firmware inside
the SP PRO" and disconnects. Its "firmware too old" path is never set in 15.40. The SP PRO firmware
number itself (15.47 here, `version_number`) is display-only; nothing is gated on it. Comms card
software (511 here) gates some comms features (e.g. multiphase scan below 302).
This unit (17 Nov 2026): 48 / 14 / 31 / 3 / 3 / 5, comms card rev 10, software 511.
selpi: `sppro.live.versions()` / `version_problems()`; `selpi live` warns and `tools/write_test.py`
refuses to write when the unit differs from these verified values.
