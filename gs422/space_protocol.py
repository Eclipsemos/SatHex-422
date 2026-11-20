"""Experimental v1 application envelope over the supplied EB90 transport."""
from dataclasses import dataclass
import struct
import time

HEAD = b'\xeb\x90'
TAIL = b'\x09\xd7'
MAX_PAYLOAD = 504
HEADER = struct.Struct('>BBHB')


def encode(payload):
    if len(payload) > MAX_PAYLOAD:
        raise ValueError('payload exceeds 504 bytes')
    return HEAD + struct.pack('>H', len(payload)) + payload + struct.pack('>H', sum(payload) & 0xffff) + TAIL


@dataclass(frozen=True)
class Message:
    kind: int
    sequence: int
    code: int
    body: bytes = b''
    version: int = 1

    def payload(self):
        return HEADER.pack(self.kind, self.version, self.sequence, self.code) + self.body

    def wire(self):
        return encode(self.payload())

    @classmethod
    def parse(cls, payload):
        if len(payload) < HEADER.size:
            raise ValueError('short application header')
        kind, version, sequence, code = HEADER.unpack_from(payload)
        return cls(kind, sequence, code, payload[HEADER.size:], version)


class Decoder:
    """Bounded stream parser. Length owns framing; marker bytes may be data."""
    def __init__(self, timeout=1.0, clock=time.monotonic):
        self.buffer = bytearray()
        self.started = None
        self.timeout, self.clock = timeout, clock
        self.checksum_errors = 0
        self.format_errors = 0

    def clear(self):
        self.buffer.clear()
        self.started = None

    def feed(self, data=b''):
        frames = []
        # Process in bounded slices even when caller supplies a very large chunk.
        chunks = [memoryview(data)[i:i + 512] for i in range(0, len(data), 512)] or [b'']
        for chunk in chunks:
            now = self.clock()
            if self.buffer and self.started is not None and now - self.started >= self.timeout:
                self.format_errors += 1
                del self.buffer[:1]
                self.started = None
            self.buffer.extend(chunk)
            while self.buffer:
                pos = self.buffer.find(HEAD)
                if pos < 0:
                    # Retain a possible first byte of the next header.
                    suffix = self.buffer[-1:] if self.buffer[-1:] == HEAD[:1] else b''
                    self.buffer[:] = suffix
                    if not suffix:
                        self.started = None
                    elif self.started is None:
                        self.started = now
                    break
                if pos:
                    del self.buffer[:pos]
                    self.started = None
                if self.started is None:
                    self.started = now
                if len(self.buffer) < 4:
                    break
                length = struct.unpack_from('>H', self.buffer, 2)[0]
                if length > MAX_PAYLOAD:
                    self.format_errors += 1
                    del self.buffer[:1]
                    self.started = None
                    continue
                size = length + 8
                if len(self.buffer) < size:
                    break
                body = bytes(self.buffer[4:4 + length])
                checksum = struct.unpack_from('>H', self.buffer, 4 + length)[0]
                if self.buffer[size - 2:size] != TAIL:
                    self.format_errors += 1
                    del self.buffer[:1]
                elif sum(body) & 0xffff != checksum:
                    self.checksum_errors += 1
                    del self.buffer[:1]
                else:
                    frames.append(body)
                    del self.buffer[:size]
                self.started = None
        return frames


class Reassembler:
    """One bounded logical message; expire incomplete uploads, never execute them."""
    def __init__(self, timeout=5, clock=time.monotonic):
        self.timeout, self.clock = timeout, clock
        self.pending = None

    def expire(self):
        if self.pending and self.clock() - self.pending['started'] >= self.timeout:
            message = self.pending['message']
            self.pending = None
            return message

    def add(self, message):
        if message.code != 4 or len(message.body) < 6:
            raise ValueError('invalid fragment')
        kind, total, offset = struct.unpack_from('>BHH', message.body)
        data = message.body[5:]
        if kind != 0x25 or not 1 <= total <= 4096 or not data or offset + len(data) > total:
            raise ValueError('fragment bounds')
        key = (message.version, message.sequence, message.code, kind, total)
        if self.pending is None:
            self.pending = {'key': key, 'data': bytearray(total), 'seen': bytearray(total),
                            'started': self.clock(), 'message': message}
        p = self.pending
        if p['key'] != key:
            raise ValueError('another transfer in progress')
        for i, value in enumerate(data, offset):
            if p['seen'][i] and p['data'][i] != value:
                self.pending = None
                raise ValueError('conflicting fragment')
        p['data'][offset:offset + len(data)] = data
        p['seen'][offset:offset + len(data)] = b'\x01' * len(data)
        if all(p['seen']):
            self.pending = None
            return Message(kind, message.sequence, message.code, bytes(p['data']), message.version)


def fragments(message):
    if message.kind != 0x25 or message.code != 4 or not 1 <= len(message.body) <= 4096:
        raise ValueError('only bounded OTA parameters can be fragmented')
    return [Message(0x29, message.sequence, message.code,
                    struct.pack('>BHH', message.kind, len(message.body), i) + message.body[i:i + 494])
            for i in range(0, len(message.body), 494)]
