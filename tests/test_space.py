import random
import struct
import unittest

from gs422.core import Cache
from gs422.space_protocol import Decoder, Message, Reassembler, encode, fragments
from gs422.space_commands import CommandError, K_EMPTY, validate
from gs422.space_runtime import Controller


class TransportTests(unittest.TestCase):
    def test_reference_vector(self):
        self.assertEqual(Message(0x11, 0x1234, 0).wire().hex(), 'eb9000051101123400005809d7')

    def test_all_split_points_and_embedded_markers(self):
        payload = b'\x21\x01\x12\x34\x17\xeb\x90\x09\xd7'
        frame = encode(payload)
        for cut in range(len(frame) + 1):
            parser = Decoder()
            self.assertEqual(parser.feed(frame[:cut]) + parser.feed(frame[cut:]), [payload])

    def test_random_chunking_and_noise(self):
        rng = random.Random(83)
        payloads = [bytes(rng.randrange(256) for _ in range(n)) for n in (0, 1, 20, 504, 7)]
        wire = b'noise' + b''.join(encode(x) for x in payloads)
        parser = Decoder()
        got = []
        while wire:
            n = rng.randint(1, 33)
            got += parser.feed(wire[:n])
            wire = wire[n:]
        self.assertEqual(got, payloads)
        self.assertEqual(len(encode(payloads[3])), 512)
        with self.assertRaises(ValueError):
            encode(b'x' * 505)

    def test_bad_checksum_tail_length_and_timeout(self):
        good = Message(0x11, 2, 0).wire()
        bad = bytearray(good)
        bad[-4] ^= 1
        parser = Decoder()
        self.assertEqual(parser.feed(bad + good), [Message(0x11, 2, 0).payload()])
        self.assertEqual(parser.checksum_errors, 1)
        parser = Decoder()
        self.assertEqual(parser.feed(good[:-1] + b'X' + b'\xeb\x90\xff\xff' + good), [Message(0x11, 2, 0).payload()])
        self.assertEqual(parser.format_errors, 2)
        now = [0]
        parser = Decoder(clock=lambda: now[0])
        parser.feed(b'\xeb\x90\x01\xf8abc')
        now[0] = 2
        self.assertEqual(parser.feed(good), [Message(0x11, 2, 0).payload()])
        self.assertEqual(parser.format_errors, 1)

    def test_single_header_byte_also_expires(self):
        now = [0]
        parser = Decoder(clock=lambda: now[0])
        parser.feed(b'\xeb')
        now[0] = .5
        parser.feed()
        now[0] = 1.1
        parser.feed()
        self.assertEqual(parser.format_errors, 1)
        self.assertFalse(parser.buffer)

    def test_reassembly_out_of_order_duplicate_and_conflict(self):
        msg = Message(0x25, 4, 4, bytes(range(256)) * 2 + bytes(25))
        pieces = fragments(msg)
        self.assertEqual([len(x.wire()) for x in pieces], [512, 61])
        r = Reassembler()
        self.assertIsNone(r.add(pieces[1]))
        self.assertIsNone(r.add(pieces[1]))
        self.assertEqual(r.add(pieces[0]), msg)
        r.add(pieces[0])
        with self.assertRaises(ValueError):
            r.add(Message(0x29, 4, 4, pieces[0].body[:-1] + b'X'))
        self.assertIsNone(r.pending)

    def test_reassembly_expiry_bounds_and_session_isolation(self):
        now = [0]
        r = Reassembler(clock=lambda: now[0])
        p = fragments(Message(0x25, 1, 4, b'x' * 537))[0]
        r.add(p)
        with self.assertRaises(ValueError):
            r.add(Message(0x29, 2, 4, p.body))
        now[0] = 6
        self.assertEqual(r.expire(), p)
        for body in (b'', struct.pack('>BHH', 0x25, 4097, 0) + b'x', struct.pack('>BHH', 0x25, 1, 1) + b'x'):
            with self.assertRaises(ValueError):
                r.add(Message(0x29, 1, 4, body))


def ota_body(data=b'x' * 512):
    return struct.pack('>IIIIIB', len(data), 0, len(data), 1, 1, 0) + data + bytes(4)


class CommandTests(unittest.TestCase):
    def test_all_k_entries(self):
        for mode in (0, 1, 2):
            self.assertEqual(validate(Message(0x21, 1, 0x17, bytes([mode]) + bytes(5))), ('receive_gate', {'mode': mode}))
        self.assertEqual(validate(Message(0x21, 1, 0x83, bytes([2, 3, 1, 2, 0, 0]))),
                         ('task_start', dict(task_id=2, priority=3, mode=1, npu=False)))
        for code in (0x81, 0x85):
            self.assertEqual(validate(Message(0x21, 1, code, b'\x09' + bytes(5)))[1], {'task_id': 9})
        for code, action in K_EMPTY.items():
            self.assertEqual(validate(Message(0x21, 1, code)), (action, {}))
        self.assertEqual(validate(Message(0x21, 1, 0x32, bytes([1, 0, 3, 0, 0, 0])))[1], dict(enabled=True, storage=0, mode=3))

    def test_all_d_entries(self):
        self.assertEqual(validate(Message(0x25, 1, 0x40, struct.pack('>BBIIIH', 3, 4, 1, 9, 100, 0)))[1]['byte_count'], 100)
        self.assertEqual(validate(Message(0x25, 1, 0x41, struct.pack('>IIIBH', 19, 1, 9, 4, 0)))[1]['rate'], 4)
        self.assertEqual(validate(Message(0x25, 1, 0x50, bytes([1, 0x34, 0x12, 0x78, 0x56, 0])))[1],
                         dict(file_id=1, start_frame=0x1234, frame_count=0x5678))
        action, args = validate(Message(0x25, 1, 4, ota_body()))
        self.assertEqual(action, 'ota_upload')
        self.assertEqual(args['data_hex'], (b'x' * 512).hex())

    def test_reserved_fields_enums_lengths_ranges_and_type(self):
        invalid = [(0x21, 0x17, bytes([3]) + bytes(5)), (0x21, 0x17, bytes(5) + b'\x01'),
                   (0x21, 0x83, bytes(6)), (0x21, 0x81, bytes(5)), (0x21, 0x85, b'\x00' * 5 + b'\x01'),
                   (0x21, 0x32, bytes(6)), (0x21, 0x84, b'\x00'), (0x21, 0x99, b''),
                   (0x25, 0x40, struct.pack('>BBIIIH', 1, 1, 9, 1, 0, 0)),
                   (0x25, 0x41, struct.pack('>IIIBH', 1, 0, 9, 3, 0)),
                   (0x25, 0x50, bytes(5) + b'\x01'), (0x25, 4, bytes(25)), (0xcc, 0x84, b'')]
        for kind, code, body in invalid:
            with self.subTest(kind=kind, code=code), self.assertRaises(CommandError):
                validate(Message(kind, 1, code, body))
        bad = bytearray(ota_body())
        bad[4] = 1
        with self.assertRaises(CommandError):
            validate(Message(0x25, 1, 4, bad))


class FakeAdapter:
    timeout = .1
    def __init__(self):
        self.calls = []
    def execute(self, action, parameters):
        self.calls.append((action, parameters))
        return b'{"value":42}'


class ControllerTests(unittest.TestCase):
    def setUp(self):
        self.cache = Cache(5000)
        self.cache.update(2, 4800)
        self.cache.update(3, 825)
        self.adapter = FakeAdapter()
        self.c = Controller(self.cache, self.adapter)

    def test_no_ack_gate_and_health_layout(self):
        self.assertIsNone(self.c.handle(Message(0x21, 9, 0x17, b'\x02' + bytes(5)).payload()))
        self.assertEqual(self.c.executed, 0)
        self.c.execute_next()
        response = Message.parse(self.c.handle(Message(0x11, 8, 0).payload()))
        self.assertEqual((response.kind, response.sequence), (0x12, 8))
        self.assertEqual(len(response.body), 60)
        self.assertEqual(struct.unpack_from('>II', response.body, 4), (1, 1))
        self.assertEqual(struct.unpack_from('>H', response.body, 38)[0], 12)
        self.assertEqual(struct.unpack_from('>ii', response.body, 48), (4800, 825))
        self.assertEqual(response.body[56], 2)

    def test_replay_conflict_and_no_effect_from_invalid_command(self):
        msg = Message(0x21, 1, 0x84)
        self.c.handle(msg.payload())
        self.c.handle(msg.payload())
        self.c.execute_next()
        self.c.handle(msg.payload())
        self.assertEqual(len(self.adapter.calls), 1)
        self.c.handle(Message(0x21, 1, 0x13).payload())
        self.assertEqual(self.c.latest[-1], 5)
        self.c.handle(Message(0x21, 2, 0x17, bytes([4]) + bytes(5)).payload())
        self.assertFalse(self.c.execute_next())
        self.assertEqual(self.c.receive_mode, 0)

    def test_missing_backend_and_result_poll(self):
        c = Controller(self.cache)
        c.handle(Message(0x21, 3, 0x52).payload())
        c.execute_next()
        self.assertEqual(c.latest, (3, 0x52, 5, 7))
        answer = Message.parse(c.handle(Message(0x15, 4, 0).payload()))
        self.assertEqual(answer.kind, 0x16)
        self.assertEqual(answer.body, b'\x01' + struct.pack('>HBBHH', 3, 0x52, 5, 7, 0))
        self.assertEqual(Message.parse(c.handle(Message(0x15, 5, 0).payload())).body, b'\x00')

    def test_ota_gate_and_fragment_execution(self):
        msg = Message(0x25, 10, 4, ota_body())
        for p in fragments(msg):
            self.assertIsNone(self.c.handle(p.payload()))
        self.c.execute_next()
        self.assertEqual(self.c.latest[-1], 4)
        self.assertFalse(self.adapter.calls)
        self.c.handle(Message(0x21, 11, 0x17, b'\x02' + bytes(5)).payload())
        self.c.execute_next()
        for p in fragments(Message(0x25, 12, 4, ota_body())):
            self.c.handle(p.payload())
        self.c.execute_next()
        self.assertEqual(self.adapter.calls[0][0], 'ota_upload')

    def test_busy_and_version_and_results_bound(self):
        self.c.handle(Message(0x21, 0, 0x84, version=2).payload())
        self.assertEqual(self.c.latest[-1], 2)
        for seq in range(1, 18):
            self.c.handle(Message(0x21, seq, 0x52).payload())
        self.assertEqual(self.c.work.qsize(), 16)
        self.assertEqual(self.c.latest[-1], 6)
        for seq in range(50, 150):
            self.c.handle(Message(0xcc, seq, 0).payload())
        self.assertEqual(len(self.c.results), 64)
        self.assertGreater(self.c.dropped, 0)


if __name__ == '__main__':
    unittest.main()
