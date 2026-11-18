"""
Configuration backup: read the SP PRO's configuration and service-settings blocks and write them in
SP LINK's file formats (.SPLC configuration, ServiceSettings CSV) or as JSON.

The layout (section bases and slot names, from SP LINK 15.40 / firmware 15.47) is in layout.json.
Read-only: nothing is written to the SP PRO.
"""
import json
import os

from .reader import Reader

LAYOUT = json.load(open(os.path.join(os.path.dirname(__file__), 'layout.json')))

# Blocks SP LINK reads and writes, in its order.
BLOCKS = [(0xc000, 197), (0xc0e5, 18), (0xc100, 125), (0xc180, 60), (0xc800, 193)]


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
    memory = {}
    blocks = BLOCKS + [(int(LAYOUT['service']['base'], 16), LAYOUT['service']['read_words'])]
    for address, words in blocks:
        for i, value in enumerate(reader.words(address, words)):
            memory[address + i] = value
    return memory


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
