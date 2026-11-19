"""
State-aware SP PRO controls built on the front panel buttons (sppro/buttons.py).

The raw buttons toggle: a Generator short press starts *or* stops the generator, an ON long press switches
ON <-> IDLE. These controls read the unit's own state first and press only when that changes something,
so they can be used as switches (the state comes from the unit, whatever started or stopped it):

  generator   on/off from Generator Status (0xa096). Presses Generator (short) only to get from off to on
              or back; refuses when the unit can't run it (grid present, disabled, fault, no fuel, ...).
  output      on/idle from Output Mode (0xa07d: Idle / Econo / On / Sync). Presses ON (long) only to get
              between Idle and the rest. Idle = no AC output (an outage).
  equalise    two long Generator presses: the SP PRO requests an equalise charge after the next full charge
              (SP PRO manual, front panel). Nothing to read back except Days to Equalise (0xa048).
"""
import time

from sppro import buttons

GENERATOR_STATUS = 0xa096
OUTPUT_MODE = 0xa07d
DAYS_TO_EQUALISE = 0xa048

# SP LINK's generator_status table
GEN_LABELS = {0: 'Not Running', 1: 'Running', 2: 'Low Fuel', 3: 'No Fuel', 4: 'Fault', 5: 'Not Available',
              6: 'Starting', 7: 'Retry Pause', 8: 'Stopping', 9: 'Disabled', 10: 'AC Source Present'}
GEN_ON = {1, 6}             # running or starting
GEN_OFF = {0, 8}            # not running or stopping
# anything else: the unit can't start / stop it from the button (grid present, disabled, fault, no fuel, ...)

OUT_LABELS = {0: 'Idle', 1: 'Econo', 2: 'On', 3: 'Sync'}
OUT_ON = {1, 2, 3}
OUT_OFF = {0}


class ControlError(Exception):
    pass


def generator_state(reader) -> dict:
    code = reader.words(GENERATOR_STATUS, 1)[0]
    on = True if code in GEN_ON else False if code in GEN_OFF else None
    return {'code': code, 'status': GEN_LABELS.get(code, 'code %d' % code), 'on': on}


def output_state(reader) -> dict:
    code = reader.words(OUTPUT_MODE, 1)[0]
    on = True if code in OUT_ON else False if code in OUT_OFF else None
    return {'code': code, 'mode': OUT_LABELS.get(code, 'code %d' % code), 'on': on}


def set_generator(reader, on: bool, **press_kw) -> dict:
    """Start (on=True) or stop the generator, pressing Generator only if that's needed."""
    s = generator_state(reader)
    want = 'on' if on else 'off'
    if s['on'] is None:
        raise ControlError('generator is %s: the SP PRO will not %s it from the button'
                           % (s['status'], 'start' if on else 'stop'))
    if s['on'] == on:
        return dict(s, pressed=False, wanted=want, note='already %s (%s)' % (want, s['status']))
    if on and s['code'] == 8:
        raise ControlError('generator is still stopping: try again once it has stopped')
    out = buttons.press(reader, 'generator', **press_kw)
    return dict(s, pressed=True, wanted=want, press=out)


def set_output(reader, on: bool, **press_kw) -> dict:
    """Inverter output ON (on=True) or IDLE (no AC output), pressing ON (long) only if that's needed."""
    s = output_state(reader)
    want = 'on' if on else 'idle'
    if s['on'] is None:
        raise ControlError('output mode is %s: not switching' % s['mode'])
    if s['on'] == on:
        return dict(s, pressed=False, wanted=want, note='already %s (%s)' % (want, s['mode']))
    out = buttons.press(reader, 'on', long=True, **press_kw)
    return dict(s, pressed=True, wanted=want, press=out)


def _wait_released(reader, button, timeout, poll):
    address = buttons.register(reader, button)
    deadline = time.monotonic() + timeout
    while reader.words(address, 1)[0] != 0:
        if time.monotonic() >= deadline:
            raise ControlError('%s button still held after %.0f s (0x%x)' % (button, timeout, address))
        time.sleep(poll)


def equalise(reader, gap: float = 1.0, timeout: float = 10.0, poll: float = 0.5) -> dict:
    """Request an equalise charge: two long Generator presses, the second once the first is released."""
    days_before = reader.words(DAYS_TO_EQUALISE, 1)[0]
    _wait_released(reader, 'generator', timeout, poll)
    first = buttons.send(reader, 'generator', buttons.LONG, settle=timeout, poll=poll)
    _wait_released(reader, 'generator', timeout, poll)
    time.sleep(gap)
    second = buttons.send(reader, 'generator', buttons.LONG, settle=timeout, poll=poll)
    return {'presses': [first, second], 'days_to_equalise_before': days_before,
            'days_to_equalise_after': reader.words(DAYS_TO_EQUALISE, 1)[0]}
