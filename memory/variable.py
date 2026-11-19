"""
Variables: named SP PRO memory values.

Two sources:
- MAP below: the original hand-written variables (names, units W / Wh, conversions in memory/converter.py),
  used by `selpi stat` / `http` and kept unchanged so their output stays the same.
- SP LINK's own field table (sppro/live_fields.json, 276 fields, matching SP LINK 15.40):
  any name not in MAP is looked up there, by SP LINK field name (e.g. 'BattOutKWHTotalAcc') or selpi key
  (e.g. 'batt_out_kwh_total_acc'), and evaluated with SP LINK's formula (units as SP LINK shows them, e.g. kW,
  kWh). Where both cover the same value they agree (tests/memory/test_variable.py checks it).
"""
import re
import struct
from memory import convert
from memory import Range

def create(arg):
    if type(arg) is str:
        if arg in MAP:
            return Variable(arg, MAP[arg][ADDRESS])
        field = live_field(arg)
        if field is not None:
            return LiveVariable(field)
        raise KeyError("unknown variable %r (not in MAP or SP LINK's field table)" % arg)
    if type(arg) is int:
        name = address_to_name(arg)
        if name == 'Unknown':
            field = _live_by_address().get(arg)
            if field is not None:
                return LiveVariable(field)
        return Variable(name, arg)
    raise NotImplementedError("Unable to create Variable from %s" % type(arg))

def address_to_name(address):
    for name in MAP.keys():
        if MAP[name][ADDRESS] == address:
            return name
    return 'Unknown'


_LIVE = {}


def _live_tables():
    """{lower-case SP LINK field name or selpi key: field}, loaded on first use."""
    if not _LIVE:
        from sppro import live
        for f in live.FIELDS:
            if f.get('field'):  # selpi's derived fields (e.g. ac_source_power_in) have only a key
                _LIVE.setdefault(f['field'].lower(), f)
            _LIVE.setdefault(f['key'].lower(), f)
    return _LIVE


def _live_by_address():
    out = {}
    for f in _live_tables().values():
        if f['words']:
            out.setdefault(int(f['words'][0], 16), f)
    return out


def live_field(name: str):
    """SP LINK field for a field name or selpi key (case-insensitive), or None."""
    return _live_tables().get(name.lower())


def live_names() -> list:
    """Every SP LINK field name available through create()."""
    return sorted({f['field'] for f in _live_tables().values() if f.get('field')})

ADDRESS = 'address'
TYPE = 'type'
DESCRIPTION = 'description'
UNITS = 'units'
CONVERSION = 'conversion'
FORMAT = 'format'
WORDS = 'words'

MAP = {
    "CommonScaleForAcVolts": {
        ADDRESS: 41000,
        TYPE: "ushort",
    },
    "CommonScaleForAcCurrent": {
        ADDRESS: 41001,
        TYPE: "ushort",
    },
    "CommonScaleForDcVolts": {
        ADDRESS: 41002,
        TYPE: "ushort",
    },
    "CommonScaleForDcCurrent": {
        ADDRESS: 41003,
        TYPE: "ushort",
    },
    "CommonScaleForTemperature": {
        ADDRESS: 41004,
        TYPE: "ushort",
    },
    "CommonScaleForInternalVoltages": {
        ADDRESS: 41005,
        TYPE: "ushort",
    },
    "TotalKacokWhTotalAcc": {
        DESCRIPTION: 'AC Lifetime Solar Energy',
        ADDRESS: 41519,
        TYPE: "uint",
        UNITS: "Wh",
        CONVERSION: "ac_wh",
    },
    "CombinedKacoAcPowerHiRes": {
        DESCRIPTION: 'AC Solar Power',
        ADDRESS: 0xa3a8,
        TYPE: "uint",
        UNITS: "W",
        CONVERSION: "ac_w",
    },
    "LoadAcPower": {
        DESCRIPTION: 'AC Load Power',
        ADDRESS: 41107,
        TYPE: "uint",
        UNITS: "W",
        CONVERSION: "ac_w",
    },
    "ACGeneratorPower": {
        DESCRIPTION: 'AC Generator Power',
        ADDRESS: 41098,
        TYPE: "uint",
        UNITS: "W",
        CONVERSION: "ac_w",
    },
    "ACLoadkWhTotalAcc": {
        DESCRIPTION: 'AC Lifetime Load Energy',
        ADDRESS: 41438,
        TYPE: "uint",
        UNITS: "Wh",
        CONVERSION: "ac_wh",
    },
    "BatteryVolts": {
        DESCRIPTION: 'Battery Volts',
        ADDRESS: 0xa05c,
        TYPE: "ushort",
        UNITS: "V",
        CONVERSION: "dc_v",
    },
    "DCBatteryPower": {
        DESCRIPTION: 'Battery Power',
        ADDRESS: 0xa02f,
        TYPE: "int",
        UNITS: "W",
        CONVERSION: "dc_w",
    },
    "Shunt1Power": {
        DESCRIPTION: 'Shunt 1 Power',
        ADDRESS: 0xa088,
        TYPE: "short",
        UNITS: "W",
        CONVERSION: "dc_w",
    },
    "Shunt2Power": {
        DESCRIPTION: 'Shunt 2 Power',
        ADDRESS: 0xa089,
        TYPE: "short",
        UNITS: "W",
        CONVERSION: "dc_w",
    },
    "Shunt1Name": {
        DESCRIPTION: 'Shunt 1 Name',
        ADDRESS: 0xc109,
        TYPE: "short",
        UNITS: "",
        CONVERSION: "shunt_name",
    },
    "Shunt2Name": {
        DESCRIPTION: 'Shunt 2 Name',
        ADDRESS: 0xc10a,
        TYPE: "short",
        UNITS: "",
        CONVERSION: "shunt_name",
    },
    "BatteryTemperature": {
        DESCRIPTION: "Battery Temperature",
        ADDRESS: 0xa03c,
        TYPE: "ushort",
        UNITS: "°C",
        CONVERSION: "temperature",
    },
    "DCkWhOut": {
        ADDRESS: 41257,
        TYPE: "uint",
        UNITS: "Wh",
        CONVERSION: "dc_wh",
    },
    "DCkWhOutToday": {
        ADDRESS: 41137,
        TYPE: "uint",
        UNITS: "Wh",
        CONVERSION: "dc_wh",
    },
    "DCkWhInToday": {
        ADDRESS: 41135,
        TYPE: "uint",
        UNITS: "Wh",
        CONVERSION: "dc_wh",
    },
    "BattOutkWhAcc": {
        ADDRESS: 41143,
        TYPE: "uint",
        UNITS: "Wh",
        CONVERSION: "dc_wh",
    },
    "QuickView_BattOutkWhAcc": {
        ADDRESS: 41178,
        TYPE: "uint",
        UNITS: "Wh",
        CONVERSION: "dc_wh",
    },
    "BattOutkWhPreviousAcc": {
        DESCRIPTION: "Battery Out Energy Today",
        ADDRESS: 41356,
        TYPE: "uint",
        UNITS: "Wh",
        CONVERSION: "dc_wh",
    },
    "BattOutkWh7DayAcc": {
        ADDRESS: 41358,
        TYPE: "uint",
        UNITS: "Wh",
        CONVERSION: "dc_wh",
    },
    "BattOutkWh7DayAccAvg": {
        ADDRESS: 41360,
        TYPE: "uint",
        UNITS: "Wh",
        CONVERSION: "dc_wh",
    },
    "BattOutkWh30DayAcc": {
        ADDRESS: 41362,
        TYPE: "uint",
        UNITS: "Wh",
        CONVERSION: "dc_wh",
    },
    "BattOutkWh30DayAccAvg": {
        ADDRESS: 41364,
        TYPE: "uint",
        UNITS: "Wh",
        CONVERSION: "dc_wh",
    },
    "BattOutkWh365DayAcc": {
        ADDRESS: 41366,
        TYPE: "uint",
        UNITS: "Wh",
        CONVERSION: "dc_wh",
    },
    "BattOutkWh365DayAccAvg": {
        ADDRESS: 41368,
        TYPE: "uint",
        UNITS: "Wh",
        CONVERSION: "dc_wh",
    },
    "BattOutkWhYearAcc": {
        ADDRESS: 41370,
        TYPE: "uint",
        UNITS: "Wh",
        CONVERSION: "dc_wh",
    },
    "BattOutkWhYearAccAvg": {
        ADDRESS: 41372,
        TYPE: "uint",
        UNITS: "Wh",
        CONVERSION: "dc_wh",
    },
    "BattOutkWhResetableAcc": {
        ADDRESS: 41374,
        TYPE: "uint",
        UNITS: "Wh",
        CONVERSION: "dc_wh",
    },
    "BattOutkWhResetableAccAvg": {
        ADDRESS: 41376,
        TYPE: "uint",
        UNITS: "Wh",
        CONVERSION: "dc_wh",
    },
    "BattOutkWhTotalAcc": {
        DESCRIPTION: "Battery Out Energy Total Accumulated",
        ADDRESS: 41381,
        TYPE: "uint",
        UNITS: "Wh",
        CONVERSION: "dc_wh",
    },
    "BattInkWhTotalAcc": {
        DESCRIPTION: "Battery In Energy Total Accumulated",
        ADDRESS: 41354,
        TYPE: "uint",
        UNITS: "Wh",
        CONVERSION: "dc_wh",
    },
    "BattSocPercent": {
        DESCRIPTION: "Battery State of Charge",
        ADDRESS: 41089,
        TYPE: "ushort",
        UNITS: "%",
        CONVERSION: "percent",
    },
    "ACInputWhTotalAcc": {
        DESCRIPTION: "Input energy accumulated total",
        ADDRESS: 41459,
        TYPE: "uint",
        CONVERSION: "ac_wh",
    },
    "ACInputWhTodayAcc": {
        DESCRIPTION: "Input energy accumulated today",
        ADDRESS: 41151,
        TYPE: "ushort",
        CONVERSION: "ac_wh",
    },
    "ACExportWhTotalAcc": {
        DESCRIPTION: "Export energy accumulated total",
        ADDRESS: 41499,
        TYPE: "uint",
        CONVERSION: "ac_wh",
    },
    "ACExportWhTodayAcc": {
        DESCRIPTION: "Export energy accumulated today",
        ADDRESS: 41154,
        TYPE: "ushort",
        CONVERSION: "ac_wh",
    },
    "ACLoadWhAcc": {
        DESCRIPTION: "Load energy accumulated today",
        ADDRESS: 41150,
        TYPE: "ushort",
        CONVERSION: "ac_wh",
    },
    "ACSolarWhTotalAcc": {
        DESCRIPTION: "AC coupled solar energy accumulated total",
        ADDRESS: 41519,
        TYPE: "uint",
        CONVERSION: "ac_wh",
    },
    "Shunt1WhTotalAcc": {
        DESCRIPTION: "Shunt 1 energy accumulated total",
        ADDRESS: 41305,
        TYPE: "int",
        CONVERSION: "dc_wh",
    },
    "ACSolarWhTodayAcc": {
        DESCRIPTION: "AC coupled solar energy accumulated today",
        ADDRESS: 41157,
        TYPE: "short",
        CONVERSION: "ac_wh",
    },
    "Shunt1WhTodayAcc": {
        DESCRIPTION: "Shunt 1 energy accumulated today",
        ADDRESS: 41146,
        TYPE: "short",
        CONVERSION: "dc_wh",
    },
    "ACGeneratorPower": {
        ADDRESS: 41098,
        TYPE: "short",
        CONVERSION: "ac_w_signed",
    },
    "LoginHash": {
        ADDRESS: 0x1f0000,
        TYPE: ""
    },
    "LoginStatus": {
        ADDRESS: 0x1f0010,
        TYPE: "ushort"
    },
}

TYPES = {
    "ushort": {
        FORMAT: "<H",
        WORDS: 1,
    },
    "short": {
        FORMAT: "<h",
        WORDS: 1,
    },
    "uint": {
        FORMAT: "<I",
        WORDS: 2,
    },
    "int": {
        FORMAT: "<i",
        WORDS: 2,
    },
}

class Variable:
    def __init__(self, name: str, address: int, bytes: bytes=b'\x00\x00'):
        self.__name = name
        self.__address = address
        self.__bytes = bytes

    def get_name(self):
        return self.__name

    """
    Get the memory range for this variable
    """
    @property
    def range(self):
        return Range(self.__address, TYPES[self.get_type()][WORDS])

    def get_type(self):
        if not self.__name in MAP:
            return 'ushort'
        return MAP[self.__name][TYPE]

    """
    Set the internal bytes
    """
    @property
    def bytes(self):
        return self.__bytes

    @bytes.setter
    def bytes(self, bytes):
        self.__bytes = bytes

    """
    Get the converted value
    """
    def get_value(self, scales: dict):
        if not self.is_known():
            raise Exception("Can not convert value for unknown variable type")
        mem_info = MAP[self.__name]
        type_info = TYPES[self.get_type()]
        format = type_info["format"]
        unscaled = struct.unpack(format, self.__bytes)[0]
        if not CONVERSION in mem_info:
            return unscaled
        return convert(mem_info[CONVERSION], unscaled, scales)

    def is_known(self):
        return self.__name in MAP


class LiveVariable(Variable):
    """A field from SP LINK's table (sppro/live_fields.json), evaluated with SP LINK's formula.

    Its range covers every word the formula reads; scale factors come from get_value(scales), as for MAP
    variables ('CommonScaleForDcVolts', ...). Enum fields return their SP LINK label."""

    def __init__(self, field: dict, bytes: bytes = None):
        self.field = field
        words = sorted({int(a, 16) for a in re.findall(r'\{(0x[0-9a-f]+)\}', field['formula'] or '')}
                       | {int(a, 16) for a in field['words']})
        self.__first = words[0]
        self.__words = words[-1] - words[0] + 1
        super().__init__(field.get('field') or field['key'], self.__first, bytes if bytes is not None else b'\x00\x00' * self.__words)

    @property
    def range(self):
        return Range(self.__first, self.__words)

    def get_type(self):
        return 'live'

    @property
    def units(self):
        return self.field['unit']

    @property
    def description(self):
        return self.field['name']

    def get_value(self, scales: dict):
        from sppro import live
        mem = {self.__first + i: w for i, w in enumerate(struct.unpack('<%dH' % self.__words, self.bytes))}
        scales = scales or {}
        short = {n: scales.get('CommonScaleFor' + n) for n in live.SCALES}
        missing = [n for n in re.findall(r'\{scale:(\w+)\}', self.field['formula'] or '') if short.get(n) is None]
        if missing:
            raise ValueError('%s needs scale factors %s' % (self.get_name(), ', '.join(sorted(set(missing)))))
        for i, n in enumerate(live.SCALES):   # every scale must be present; unused ones may be 0
            mem[live.SCALE_BASE + i] = short[n] if short[n] is not None else 0
        value, text = live.evaluate(self.field, mem)
        return text if self.field['kind'] == 'enum' else value

    def is_known(self):
        return True
