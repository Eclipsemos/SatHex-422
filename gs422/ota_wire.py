"""OTA V1.0.0 wire structures; separate from the experimental v1 envelope.

CRC profile must be chosen explicitly by the integrator. No CRC default is
implied by the source document, which only says 'CRC-16'.
"""
import binascii
import struct
from .space_protocol import encode


def crc16(data, profile):
    if profile == 'CCITT-FALSE':
        return binascii.crc_hqx(data, 0xffff)
    if profile == 'XMODEM':
        return binascii.crc_hqx(data, 0)
    if profile != 'MODBUS':
        raise ValueError('CRC-16 profile must be explicitly configured')
    value = 0xffff
    for byte in data:
        value ^= byte
        for _ in range(8):
            value = (value >> 1) ^ (0xa001 if value & 1 else 0)
    return value


def control(kind, code, parameters):
    if kind not in (0x30, 0x32):
        raise ValueError('invalid OTA request type')
    return encode(bytes((kind, code)) + parameters)


def validate_control(payload):
    if len(payload) != 8 or payload[0] not in (0x30, 0x32):
        raise ValueError('OTA control requires TYPE CODE and six parameter bytes')
    kind, code = payload[:2]
    w = payload[2:]
    if kind == 0x32:
        if any(w) or code not in (0x53,0x56):
            raise ValueError('query requires known code and six zero reserved bytes')
        return ('progress' if code==0x53 else 'version'), {}
    if code == 0x17 and w[0] in (1, 2) and not any(w[1:]):
        return 'gate', {'enabled': w[0] == 1}
    if code == 4 and w[0] == 0 and (w[1] <= 0x80 or w[1] >= 0xf0) and not any(w[2:]):
        return 'load', {'index': w[1]}
    if code == 0x53 and w[0] in range(1, 5) and w[1] in range(1, 5) and w[2] in (1, 2) and w[3] in (0, 1) and w[5] == 0:
        if w[3] == 1 and w[4] == 0:
            raise ValueError('segmented transfer needs a nonzero segment count')
        if w[3] == 0 and w[4] != 0:
            raise ValueError('unused segment count must be zero in selected profile')
        return 'begin', dict(component=w[0], region=w[1], mode=w[2], segmented=w[3], segments=w[4])
    if code == 0x54 and w[0] in (0, 255) and not any(w[1:]):
        return 'end', {'aborted': w[0] == 255}
    if code == 0x55 and w[0] in range(1, 5) and w[1] in (0, 1) and not any(w[2:]):
        return 'erase', {'region': w[0], 'scope': w[1]}
    if code == 0x56 and not any(w):
        return 'version', {}
    raise ValueError('invalid OTA code, enum or reserved bytes')


def reply(code, result):
    if not 0 <= result <= 255:
        raise ValueError('invalid response code')
    return encode(bytes((0x31, code, result)))


def version_reply(version):
    data = version.encode('ascii')
    if not data or len(data) > 501 or any(b < 32 or b > 126 for b in data):
        raise ValueError('invalid firmware version')
    return encode(b'\x33\x56' + data + b'\x00')


def progress_reply(snapshot):
    """Project extension: 33 53 + exact raw TMG008..014, all big endian."""
    values=[snapshot[f'TMG{i:03}'] for i in range(8,15)]
    if any(type(v) is not int or not 0<=v<(1<<(32 if i<4 else 8)) for i,v in enumerate(values)):
        raise ValueError('invalid progress fields')
    if values[4]>100:raise ValueError('invalid percentage')
    return encode(b'\x33\x53'+struct.pack('>4IBBB',*values))


def segment_ranges(total_packets,segments):
    """Balanced contiguous sequence ranges; end exclusive, sequence never resets."""
    if type(total_packets) is not int or type(segments) is not int or not 1<=segments<=255 or total_packets<segments:
        raise ValueError('segments must be nonempty and <=255')
    return [(i*total_packets//segments,(i+1)*total_packets//segments) for i in range(segments)]


def parse_packet(payload):
    if len(payload) != 504 or payload[0] != 0x3a:
        raise ValueError('upload payload must be 3A + u32 sequence + 499 bytes')
    return int.from_bytes(payload[1:5], 'big'), payload[5:]


def packet(sequence, data):
    if not 0 <= sequence <= 0xffffffff or len(data) != 499:
        raise ValueError('invalid sequence or fixed data size')
    return encode(b'\x3a' + struct.pack('>I', sequence) + data)


def expected_packets(size):
    if not 1 <= size <= 0xffffffff:
        raise ValueError('file size must fit nonzero u32')
    return (size + 6 + 498) // 499


def file_packets(data, profile):
    expected_packets(len(data))
    stream = struct.pack('>IH', len(data), crc16(data, profile)) + data
    for seq, offset in enumerate(range(0, len(stream), 499)):
        yield packet(seq, stream[offset:offset + 499].ljust(499, b'\x00'))
