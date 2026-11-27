"""20261119 感算遥测: 44 unsigned fields, contiguous MSB-first wire layout.

The workbook's fractional byte offsets for two-bit APP states are interpreted
as bit offsets, not rounded byte offsets. Enum conflicts are NOT resolved here.
"""
from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP


@dataclass(frozen=True)
class Field:
    id: str
    bits: int
    offset: int

    @property
    def byte(self):
        return self.offset // 8

    @property
    def high_bit(self):
        return 7 - self.offset % 8


WIDTHS = (8, 8, 8, 32, 16, 8, 8, 32, 32, 32, 32, 8, 8, 8,
          8, 8, 8) + (2,) * 16 + (16, 32, 8, 32, 32, 32, 8, 8, 8, 8, 8)
offset = 0
fields = []
for number, bits in enumerate(WIDTHS, 1):
    fields.append(Field(f'TMG{number:03}', bits, offset))
    offset += bits
FIELDS = tuple(fields)
BY_ID = {f.id: f for f in FIELDS}
SIZE = offset // 8
assert len(FIELDS) == 44 and offset % 8 == 0
RESERVED = frozenset(('TMG042', 'TMG043', 'TMG044'))
ALGORITHMS = frozenset(f'TMG{i:03}' for i in range(15, 34))
INVALID = {'TMG005': 0xffff, 'TMG006': 0xff, 'TMG007': 0xff, 'TMG034': 0xffff}


def pack(values, *, algorithms_enabled=False):
    """Encode RAW integers. Missing/unknown values must not silently become zero."""
    if set(values) != set(BY_ID):
        raise ValueError('supply exactly all 44 raw telemetry fields')
    number = 0
    for field in FIELDS:
        value = values[field.id]
        if type(value) is not int or not 0 <= value < (1 << field.bits):
            raise ValueError(f'{field.id}: raw unsigned value out of range')
        if field.id in RESERVED and value:
            raise ValueError('reserved telemetry must be zero')
        if not algorithms_enabled and field.id in ALGORITHMS and value:
            raise ValueError('algorithm fields are reserved empty by user instruction')
        if field.id == 'TMG012' and value > 100:
            raise ValueError('percentage exceeds 100')
        number = (number << field.bits) | value
    return number.to_bytes(SIZE, 'big')


def unpack(data):
    if len(data) != SIZE:
        raise ValueError('incorrect telemetry length')
    number = int.from_bytes(data, 'big')
    return {f.id: (number >> (SIZE * 8 - f.offset - f.bits)) & ((1 << f.bits) - 1)
            for f in FIELDS}


def raw_value(identifier, physical):
    """Explicit engineering conversion; no invented nominal voltage or power."""
    if identifier not in ('TMG005', 'TMG006', 'TMG007', 'TMG034'):
        raise ValueError('not an engineering field')
    value = Decimal(str(physical))
    if not value.is_finite() or value < 0:
        raise ValueError('invalid physical value')
    scale = Decimal('0.01') if identifier in ('TMG005', 'TMG034') else Decimal(1)
    raw = int((value / scale).quantize(Decimal(1), rounding=ROUND_HALF_UP))
    if raw >= INVALID[identifier]:
        raise ValueError('physical value out of wire range')
    return raw


def physical_value(identifier, raw):
    """Decode approved sensor invalid markers as None, never as 255C/655.35W."""
    if identifier not in INVALID or type(raw) is not int or not 0 <= raw <= INVALID[identifier]:
        raise ValueError('invalid raw sensor value')
    if raw == INVALID[identifier]:return None
    return raw / 100 if identifier in ('TMG005','TMG034') else raw


def split_storage_bytes(value):
    if type(value) is not int or not 0 <= value < (1 << 64):
        raise ValueError('invalid u64 byte count')
    # The workbook explicitly puts low word before high word; each word is BE.
    return {'TMG038': value & 0xffffffff, 'TMG039': value >> 32}


def increment(values, identifier, amount=1):
    if identifier not in ('TMG001', 'TMG002', 'TMG003', 'TMG010', 'TMG011', 'TMG037', 'TMG040', 'TMG041'):
        raise ValueError('not a counter')
    if type(amount) is not int or amount < 0:
        raise ValueError('invalid counter increment')
    values[identifier] = (values[identifier] + amount) % (1 << BY_ID[identifier].bits)
