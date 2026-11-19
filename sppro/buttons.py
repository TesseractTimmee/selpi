"""
SP PRO front panel buttons, pressed over the link the way SP LINK's Quick View front panel does it
(SP LINK 15.40, fclsMain::picbBUTTON_*_MouseDown -> CommunicateButtonPress).

Each SP PRO comms port has its own three button registers; SP LINK uses the set for the port it is talking
through, which the unit reports in the low byte of 0xa000 (port 1 = the DB9, where Select.Live sits).
A press writes 1 (short) or 2 (long) to the button's register; SP LINK then reads it back, and a 3 means the
button is "already being pressed" (the press was not accepted).

What the buttons do (SP PRO manual OI0003 rev 04, Controls and Indicators):
  generator  short press: start or stop the auto start generator (a stop skips the cool down time);
             two long presses (>2 s): request an equalise charge after the next full charge
  on         long press: SP PRO ON <-> IDLE (IDLE = no AC output: an outage)
  silence    press: silence the alarm for this alert
A single long generator press and long silence presses do nothing documented, so `press` doesn't offer them;
the equalise request (two long generator presses) is `sppro.controls.equalise`.
"""
import struct
import time

PORT_REGISTER = 0xa000
BUTTON_BASE = {1: 0xa232, 2: 0xa235}
BUTTONS = {'generator': 0, 'on': 1, 'silence': 2}
SHORT, LONG, ALREADY_PRESSED = 1, 2, 3
ALLOWED = {('generator', SHORT), ('silence', SHORT), ('on', LONG)}


class ButtonError(Exception):
    pass


def port(reader) -> int:
    """The SP PRO comms port this link is on (low byte of 0xa000)."""
    return reader.words(PORT_REGISTER, 1)[0] & 0xff


def register(reader, button: str) -> int:
    p = port(reader)
    if p not in BUTTON_BASE:
        raise ButtonError('the SP PRO reports comms port %d; button registers are only known for ports 1 and 2' % p)
    return BUTTON_BASE[p] + BUTTONS[button]


def press(reader, button: str, long: bool = False, settle: float = 3.0, poll: float = 0.5) -> dict:
    """Press a front panel button once. Returns what was written and what the register read back over `settle` s."""
    value = LONG if long else SHORT
    if button not in BUTTONS:
        raise ButtonError('unknown button %r (one of %s)' % (button, ', '.join(BUTTONS)))
    if (button, value) not in ALLOWED:
        raise ButtonError('%s %s press is not supported (only: %s)' % (
            'long' if long else 'short', button,
            ', '.join('%s %s' % ('long' if v == LONG else 'short', b) for b, v in sorted(ALLOWED))))
    return send(reader, button, value, settle, poll)


def send(reader, button: str, value: int, settle: float = 3.0, poll: float = 0.5) -> dict:
    """Write one press (no check against ALLOWED: for sppro.controls) and watch the register settle."""
    address = register(reader, button)
    before = reader.words(address, 1)[0]
    if before == ALREADY_PRESSED:
        raise ButtonError('%s button is already being pressed (0x%x = 3); not sent' % (button, address))
    reader.write(address, struct.pack('<H', value))
    seen = []
    deadline = time.monotonic() + settle
    while True:
        v = reader.words(address, 1)[0]
        seen.append(v)
        if v == ALREADY_PRESSED:
            raise ButtonError('%s button press not accepted: already being pressed (0x%x = 3)' % (button, address))
        if time.monotonic() >= deadline or (v == 0 and len(seen) > 1):
            break
        time.sleep(poll)
    return {'button': button, 'press': 'long' if value == LONG else 'short', 'register': address, 'value': value,
            'before': before, 'read_back': seen}
