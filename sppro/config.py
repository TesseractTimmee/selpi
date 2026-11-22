"""
Configuration backup: read the SP PRO's configuration and service-settings blocks and write them in
SP LINK's file formats (.SPLC configuration, ServiceSettings CSV) or as JSON.

The layout (section bases and slot names, from SP LINK 15.40 / firmware 15.47) is in layout.json.
Read-only: nothing is written to the SP PRO.
"""
import json
import logging
import os
import time

from .reader import Reader

LAYOUT = json.load(open(os.path.join(os.path.dirname(__file__), 'layout.json')))

# Blocks SP LINK reads and writes, in its order.
BLOCKS = [(0xc000, 197), (0xc0e5, 18), (0xc100, 125), (0xc180, 60), (0xc800, 193)]

_MEM_CACHE = None   # (timestamp, memory): the last good full read, served when a fresh read stalls


def slots():
    """Yield (section title, slot index, name, address or None) for every configuration slot."""
    for section in LAYOUT['config']['sections']:
        base = int(section['base'], 16)
        split = section.get('split')
        for i, name in enumerate(section['slots']):
            if split and i >= split['slot']:
                address = int(split['address'], 16) + i - split['slot']
            else:
                address = base + i
            yield section['title'], i, name, address


def service_slots():
    base = int(LAYOUT['service']['base'], 16)
    for i, name in enumerate(LAYOUT['service']['slots']):
        yield i, name, base + i


def read_memory(reader: Reader) -> dict:
    """Read all configuration and service blocks; returns {address: uint16}."""
    global _MEM_CACHE
    # Start from the last good read so a block we can't read this time keeps its previous value (over a flaky
    # Select.Live link a single block can time out); a block that does come back overwrites it. A block that
    # fails is skipped rather than aborting the whole read, so a partial stall still returns (mostly) fresh data.
    memory = dict(_MEM_CACHE[1]) if _MEM_CACHE else {}
    blocks = BLOCKS + [(int(LAYOUT['service']['base'], 16), LAYOUT['service']['read_words'])]
    fresh = 0
    for address, words in blocks:
        try:
            for i, value in enumerate(reader.words(address, words)):
                memory[address + i] = value
            fresh += 1
        except Exception as e:
            logging.debug('config read 0x%x+%d skipped: %s', address, words, e)
    if fresh == 0 and not _MEM_CACHE:
        raise TimeoutError('the SP PRO link is busy or stalled and there is no earlier read to show')
    if fresh:
        _MEM_CACHE = (time.time(), memory)
    return memory


def cached_memory(max_age: int = 1800):
    """The last good read_memory result if within max_age seconds, else None. Lets the settings/config pages be
    served from the last read instead of re-reading all the config blocks over a slow/stalled Select.Live link."""
    if _MEM_CACHE and time.time() - _MEM_CACHE[0] < max_age:
        return _MEM_CACHE[1]
    return None


def settings(memory: dict) -> list:
    """Named settings with address and raw value (None when the address wasn't read)."""
    out = []
    for section, _, name, address in slots():
        if name:
            out.append({'name': name, 'section': section.rstrip(':').title(), 'address': '0x%x' % address,
                        'value': memory.get(address)})
    for _, name, address in service_slots():
        if name:
            out.append({'name': name, 'section': 'Service', 'address': '0x%x' % address, 'value': memory.get(address)})
    return out


def _row(*cells) -> str:
    return ','.join('"%s"' % c for c in cells) + '\r\n'


def to_splc(memory: dict, creator: str = 'selpi') -> str:
    """Configuration file in SP LINK's .SPLC layout (value in the first column, zeros in the other three)."""
    text = _row('File Created By:', creator, '', '', '')
    text += _row('Configuration Settings Version Number:', LAYOUT['config']['version'], '', '', '')
    text += _row('Software Variable Name of Setting:', 'Value:', '', '', '')
    current = None
    for section, _, name, address in slots():
        if section != current:
            current = section
            text += _row(section, '', '', '', '')
        text += _row(name, ' %d' % memory.get(address, 0), ' 0', ' 0', ' 0')
    return text.rstrip('\r\n')


def to_service_csv(memory: dict, creator: str = 'selpi') -> str:
    text = _row('File Created By:', creator, '', '', '')
    text += _row('Service Settings Version Number:', LAYOUT['service']['version'], '', '', '')
    text += _row('Software Variable Name of Setting:', 'Value:', '', '', '')
    for _, name, address in service_slots():
        if name and address in memory:
            text += _row(name, ' %d' % memory[address], '', '', '')
        else:
            text += _row('', '', '', '', '')
    return text.rstrip('\r\n')
