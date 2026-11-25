"""SP PRO multiphase (Powerchain): discover the units on the bus and address each one.

Decoded from SP LINK 16.11 and validated against it (details/selpi.md "Multi-inverter ... STEP 1/2"). A Powerchain
is several physical SP PROs on one link; SP LINK reads each by re-pointing the gateway at it, same registers. All
units share the one link, so callers serialise through the link lock and re-address (sticky) + verify before each
per-unit batch.

A single inverter is just a 1-unit system: `detect()` returns one unit (index 0) read in place, no re-address.
"""
import logging
import struct

# Registers (from the IL + live capture).
REG_STRUCTURE = 0xc0ed      # site structure (1 word): 0 = disabled/single; 3 = three-phase (L1/L2/L3[/Split])
REG_SERIALLIST = 0xdf50     # 64 x uint32 serial slots back-to-back (128 words), no count word; 0 = empty slot
REG_GATEWAY = 0xdfde        # write a unit index here to re-point the link at that unit (sticky); 0x2ff = broadcast
REG_SERIAL = 0xa05e         # unit serial number (uint32)
REG_COMMISSIONED = 0xca08   # enCommissionedState: 0 False, 1 True, 2 Undefined, 3 Degraded
REG_PHASEROLE = 0xc0ee      # phase + role: 1-4 Manager of L1/L2/L3/Split, 5-8 Worker of same, 0 unassigned
REG_MODEL = 0xa05d          # model code

SERIAL_SLOTS = 64
BROADCAST = 0x2ff
NOT_FOUND = 0xfffe
PHASES = {1: 'L1', 2: 'L2', 3: 'L3', 4: 'Split'}


def decode_phaserole(code):
    """A 0xc0ee code -> (phase 1-4 or None, role 'manager'/'worker' or None, human label)."""
    if not code or code > 8:
        return None, None, 'Unassigned'
    phase = code if code <= 4 else code - 4
    role = 'manager' if code <= 4 else 'worker'
    return phase, role, '%s %s' % (PHASES.get(phase, '?'), role.capitalize())


def structure(reader):
    """The site structure word (0xc0ed)."""
    return reader.words(REG_STRUCTURE, 1)[0]


def serial_list(reader):
    """{index: serial} from 0xdf50 - the units the gateway knows, skipping empty (0) slots."""
    words = reader.words(REG_SERIALLIST, SERIAL_SLOTS * 2)
    out = {}
    for i in range(SERIAL_SLOTS):
        s = words[2 * i] | (words[2 * i + 1] << 16)
        if s:
            out[i] = s
    return out


def change_destination(reader, index, expected_serial=None):
    """Re-point the link at a unit (sticky write to 0xdfde) and confirm by reading its serial back.

    Returns the unit's serial. Raises if it doesn't match `expected_serial` - the verify that MUST precede any
    per-unit write so a write can never land on the wrong inverter.
    """
    reader.write(REG_GATEWAY, struct.pack('<H', index))
    got = reader.u32(REG_SERIAL)
    if expected_serial is not None and got != expected_serial:
        raise RuntimeError('re-address to unit %d read serial %s, expected %s' % (index, got, expected_serial))
    return got


def read_identity(reader):
    """Identity of whoever the link is currently pointed at (no re-address)."""
    serial = reader.u32(REG_SERIAL)
    commissioned = reader.words(REG_COMMISSIONED, 1)[0]
    code = reader.words(REG_PHASEROLE, 1)[0]
    model = reader.words(REG_MODEL, 1)[0]
    phase, role, label = decode_phaserole(code)
    return {'serial': serial, 'commissioned': commissioned, 'phase_role_code': code,
            'phase': phase, 'role': role, 'label': label, 'model': model}


def read_unit(reader, index, serial=None):
    """Re-address to a unit and read its identity (index + the identity fields)."""
    change_destination(reader, index, serial)
    return dict(read_identity(reader), index=index)


def _multiphase_structure(value):
    """A 0xc0ed structure word that means a Powerchain (not single/disabled, not a read sentinel)."""
    return value not in (0, NOT_FOUND, 0xffff)


def detect(reader):
    """The unit list (always >= 1 unit).

    Gated on the structure register (0xc0ed): a single inverter reports structure 0 (disabled), so we treat it as
    one unit read in place - NO gateway write (0xdf50 can hold leftover data on a standalone unit, so the serial
    list alone isn't a reliable multi signal). Only a real Powerchain (structure set + >1 serial) re-addresses.
    """
    serials = serial_list(reader) if _multiphase_structure(structure(reader)) else {}
    if len(serials) <= 1:
        return [dict(read_identity(reader), index=0)]
    return disambiguate_labels([read_unit(reader, i, serials[i]) for i in sorted(serials)])


def disambiguate_labels(units):
    """Keep unit labels unique: when two units share a label (e.g. two workers on the same phase, both
    'L1 Worker'), append the serial so the web picker and HA sub-devices stay distinct. Mutates and returns."""
    counts = {}
    for u in units:
        counts[u['label']] = counts.get(u['label'], 0) + 1
    for u in units:
        if counts[u['label']] > 1 and u.get('serial'):
            u['label'] = '%s (%s)' % (u['label'], u['serial'])
    return units


def is_multi(reader):
    """True if a Powerchain with more than one unit is present."""
    return _multiphase_structure(structure(reader)) and len(serial_list(reader)) > 1


# System totals: how each field combines across the phases (SP LINK's subDisplayDataMultiPhase math).
# Powers/energies/currents add; voltages/SoC/frequency/temperature average; everything else (status, enums,
# counts, durations) has no meaningful whole-system value, so it is left off the System device.
SUM_UNITS = frozenset({'kWh', 'Wh', 'kW', 'W', 'A', 'Ah', 'kvar', 'var', 'kVA', 'VA'})
AVG_UNITS = frozenset({'V', '%', 'Hz', '°C', 'C'})

# Fields whose unit would otherwise sum/average but that have no meaningful whole-system value: they stay off
# the System device entirely (checked before the unit). Audited from the whole catalog, grouped by reason:
PER_UNIT_ONLY = frozenset({
    # per-inverter temperatures
    'battery_temperature', 'control_board_temperature', 'heatsink_temperature',
    'inlet_air_temperature', 'transformer_temperature',
    # per-inverter component health / wear
    'fan_speed', 'fan_life', 'capacitor_life_consumed_filter',
    'capacitor_life_consumed_pm1', 'capacitor_life_consumed_pm2',
    # arbitrary installer-wired aux inputs / per-unit diagnostics (battery_mid_point is an imbalance
    # diagnostic read per inverter to spot the drifting bank - averaging it hides exactly that)
    'general_purpose1', 'general_purpose2', 'load_volts_high_frequency_ripple', 'battery_mid_point',
    # pinned to one phase - averaging across units on other phases is meaningless
    'multi_phase_ac_source_volts_l1', 'multi_phase_load_ac_volts_l1',
    # peak powers: the per-unit peaks happen at different times, so a sum overstates the system peak
    'dc_shunt1_peak_power_previous', 'dc_shunt2_peak_power_previous',
    'shunt1_peak_power_today', 'shunt2_peak_power_today',
    # system-wide limits replicated on every unit: summing would multiply by the unit count
    'sys_reg_charge_power_limit', 'sys_reg_export_power_limit',
    # Service-readings screen: per-inverter diagnostic duplicates of fields already on other screens
    # (the catalog dedups these by register, but the web UI's raw per-screen snapshot does not)
    'fan_life_in_service_tab', 'batt_soc_percent_in_service_tab',
    'i_shunt1_in_service_tab', 'i_shunt2_in_service_tab',
})


def aggregation(unit, key=None):
    """'sum' / 'avg' / None (not aggregatable for the system) for a field. `key` lets a field opt out of the
    roll-up even when its unit would average (per-inverter temps / fan have no whole-system value)."""
    if key in PER_UNIT_ONLY:
        return None
    if unit in SUM_UNITS:
        return 'sum'
    if unit in AVG_UNITS:
        return 'avg'
    return None


def aggregate(unit, values, key=None):
    """Combine the per-unit values of one field into a system total; None if not aggregatable or no numbers."""
    agg = aggregation(unit, key)
    nums = [v for v in values if isinstance(v, (int, float)) and not isinstance(v, bool)]
    if agg is None or not nums:
        return None
    return sum(nums) if agg == 'sum' else sum(nums) / len(nums)


def for_unit(unit, multi, fn):
    """Wrap a read fn(reader) so it re-addresses to `unit` first. No-op for a single-inverter system (never writes
    the gateway register on a lone unit). `unit` is a dict from detect() (needs 'index' and 'serial').

    Compose this into the function handed to the link lock, so the re-address and the reads run as one locked,
    non-interleaved operation against the same addressed unit.
    """
    if not multi or unit is None:
        return fn
    idx, serial = unit['index'], unit['serial']

    def wrapped(reader):
        change_destination(reader, idx, serial)
        return fn(reader)
    return wrapped


def verified_write(reader, unit, multi, address, data):
    """A per-unit write guarded by a serial read-back: re-address to `unit`, confirm its serial, THEN write - so a
    write can never land on the wrong inverter. No-op re-address for a single unit. Call inside the link lock."""
    if multi and unit is not None:
        change_destination(reader, unit['index'], unit['serial'])   # raises on serial mismatch -> no write
    reader.write(address, data)


# --- Identify / flash front-panel LEDs --------------------------------------------------------------------------
# SP LINK's "Double-click a serial to identify it by flashing front panel LEDs" (btnIdentify_Click ->
# IdentifyFrontPanelLeds in the 16.11 IL; confirmed on the wire 25 Nov: captures/mp-identify-20261125.jsonl +
# identify-il-20261125.txt). A benign momentary write - no installer passcode, changes no configuration - so it
# can ship ahead of settings write-enablement.
REG_IDENTIFY = 0xa389              # flash-duration register. SP LINK tags the write enFrom 0x20 "FlashBattLeds";
                                   #   that tag is SP LINK-internal and does NOT appear on the wire (the capture
                                   #   shows a plain write to 0xa389), so selpi just writes the register.
IDENTIFY_MIN_MAP_VERSION = 20      # 0x14: IdentifyFrontPanelLeds returns without writing below this map version.
IDENTIFY_DEFAULT_MS = 3000         # SP LINK's btnIdentify_Click hardcodes 0xbb8 -> value 150.


def identify_value(duration_ms=IDENTIFY_DEFAULT_MS):
    """The 0xa389 value SP LINK writes to flash for `duration_ms`: (u16) round(min(ms / 20, 999)). 3000 -> 150."""
    return min(int(round(duration_ms / 20.0)), 999)


def identify(reader, unit=None, multi=False, duration_ms=IDENTIFY_DEFAULT_MS):
    """Flash a unit's front-panel LEDs to locate it physically (SP LINK's double-click identify).

    Re-addresses to `unit` (verified by a serial read-back, like any per-unit write), writes the flash-duration
    register, then restores the link to the primary - mirrors IdentifyFrontPanelLeds. `unit` is a detect() dict;
    None / multi=False = the lone inverter (no re-address). Call inside the link lock. Returns what was written.
    """
    value = identify_value(duration_ms)
    index = unit['index'] if unit else 0
    if multi and unit is not None:
        change_destination(reader, index, unit.get('serial'))    # verified re-address (raises on serial mismatch)
    reader.write(REG_IDENTIFY, struct.pack('<H', value))
    if multi and unit is not None and index != 0:
        change_destination(reader, 0)                            # restore the link to the primary, as SP LINK does
    return {'index': index, 'register': REG_IDENTIFY, 'value': value, 'duration_ms': duration_ms}
