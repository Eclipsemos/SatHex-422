"""Independent literal wire vectors and externally observable OTA transitions."""
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from gs422 import tmg
from gs422.core import Cache
from gs422.ota_staging import Staging
from gs422.ota_wire import control, crc16, file_packets, packet, reply, version_reply
from gs422.revised_runtime import Revised
from gs422.space_protocol import Decoder, Message, encode


class Appendix(unittest.TestCase):
    def test_complete_examples(self):
        zeros = bytes(6)
        for code, params, checksum, ack in [
            (0x55, bytes.fromhex('010000000000'), '0086', '0086'),
            (0x53, bytes.fromhex('010201000000'), '0087', '0084'),
            (0x54, zeros, '0084', '0085'), (4, zeros, '0034', '0035')
        ]:
            expected = bytes.fromhex(f'eb90000830{code:02x}{params.hex()}{checksum}09d7')
            self.assertEqual(control(0x30, code, params, ota101=True), expected)
            self.assertEqual(reply(code, 0, ota101=True),
                             bytes.fromhex(f'eb90000831{code:02x}000000000000{ack}09d7'))
        self.assertEqual(control(0x30, 0x56, zeros, ota101=True),
                         bytes.fromhex('eb9000083056000000000000008609d7'))
        for version, expected in [('V1.2.3', 'eb900009335656312e322e330001d109d7'),
                                  ('V2.0.0', 'eb900009335656322e302e300001cd09d7')]:
            self.assertEqual(version_reply(version, ota101=True), bytes.fromhex(expected))
        self.assertEqual(Decoder(ota101=True).feed(bytes.fromhex('eb900009335656322e302e300001e809d7')), [])

    def test_crc_vectors_and_data_examples(self):
        for data, expected in [(b'', 0), (b'\0', 0), (b'\xff', 0x1ef0),
                               (b'123456789', 0x31c3), (b'\xff'*128, 0xeda9), (b'\xff'*499, 0x80e3)]:
            self.assertEqual(crc16(data, 'XMODEM'), expected)
        for seq, total in [(0, 'f147'), (1, 'f148')]:
            raw = packet(seq, b'\xff'*499, ota101=True)
            self.assertEqual(len(raw), 512)
            self.assertEqual(raw[-4:], bytes.fromhex(total + '09d7'))
        first, last = file_packets(b'\xff'*499, 'XMODEM', ota101=True)
        self.assertEqual(first[9:-4], bytes.fromhex('000001f380e3') + b'\xff'*493)
        self.assertEqual(last[9:-4], b'\xff'*6 + bytes(493))


class Status(unittest.TestCase):
    query = bytes.fromhex('eb9000023214004609d7')

    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.staging = Staging(folder.name, crc_profile='XMODEM', capacity=8192)
        self.addCleanup(self.staging.close)
        self.r = Revised(self.staging, Cache(10), dict.fromkeys(tmg.BY_ID, 0), version='V1.2.3', ota101=True)
        self.addCleanup(self.r.close)
        self.op(0x17, b'\1'+bytes(5))
        self.begin()

    def op(self, code, params, result=0):
        self.assertEqual(self.r.feed_control(control(0x30, code, params, ota101=True)), b'')
        self.assertTrue(self.r.execute_next())
        self.assertEqual(self.r.drain_replies(), reply(code, result, ota101=True))

    def begin(self):
        self.op(0x53, bytes.fromhex('040201000000'))

    def status(self, expected):
        raw = self.r.feed_control(self.query)
        self.assertEqual(raw, bytes.fromhex('eb90000233') + bytes([expected]) +
                         (0x33+expected).to_bytes(2,'big') + bytes.fromhex('09d7'))
        body = Message.parse(Decoder().feed(self.r.feed_control(Message(0x11,9,0).wire()))[0]).body
        values = tmg.unpack(body)
        self.assertEqual(values['TMG014'], expected)
        return values

    def send(self, frame):
        self.assertEqual(self.r.feed_upload(frame), b'')
        self.assertTrue(self.r.execute_next())
        self.assertEqual(self.r.drain_replies(), b'')

    def test_z54_complete_phase_visible_and_unknown_fault_recovers(self):
        frames = list(file_packets(b'\xff'*1400, 'XMODEM', ota101=True))
        for idx in (0,2,2):
            self.send(frames[idx])
        self.op(0x54, bytes(6), 1)
        values = self.status(0x20)
        self.assertEqual((values['TMG009'], values['TMG010']), (1,2))
        self.send(frames[1])
        self.status(0x20)
        arrived, release = threading.Event(), threading.Event()
        original = self.r._set_erase_status
        def observe(state):
            original(state)
            if state == 0x21:
                arrived.set()
                release.wait(3)
        with patch.object(self.r, '_set_erase_status', side_effect=observe):
            self.r.feed_control(control(0x30,0x54,bytes(6),ota101=True))
            worker = threading.Thread(target=self.r.execute_next)
            worker.start()
            try:
                self.assertTrue(arrived.wait(2))
                self.status(0x21)
            finally:
                release.set()
                worker.join(3)
        self.assertEqual(self.r.drain_replies(), reply(0x54,0,ota101=True))
        self.assertEqual(self.status(0x23)['TMG013'], 4)
        self.op(0x54,bytes(6))
        backend = self.r.software_backend = Mock()
        backend.ota.return_value = 0xff
        self.op(0x55,bytes.fromhex('020100000000'),0xff)
        self.status(0x40)
        backend.ota.side_effect = RuntimeError('isolated failure')
        with self.assertLogs('gs422.revised_runtime', level='ERROR'):
            self.op(0x55,bytes.fromhex('020100000000'),0xff)
        self.r.update_telemetry(dict.fromkeys(tmg.BY_ID,0))
        self.status(0xff)
        self.begin()
        self.status(0x20)

    def test_obc_wire_requests(self):
        r = self.r
        r.version = 'sample-v1'
        version = bytes.fromhex('eb90000c335673616d706c652d76310003df09d7')
        self.assertEqual(r.feed_control(bytes.fromhex('eb9000083056000000000000008609d7')),version)
        self.assertFalse(r.feed_control(bytes.fromhex('eb9000083056000000000000008e09d7')))
        values = dict.fromkeys(tmg.BY_ID,0)
        values['TMG034'] = 1256
        r.update_telemetry(values)
        for wire, header in [('eb90000111001109d7','12'),('eb9000021100001109d7','1200'),
                             ('eb9000081100000000000000001109d7','1200')]:
            out, = Decoder().feed(r.feed_control(bytes.fromhex(wire)))
            self.assertTrue(out.startswith(bytes.fromhex(header)))
            self.assertEqual(tmg.unpack(out[len(bytes.fromhex(header)):])['TMG034'],1256)
        r.set_telemetry_deadline(0)
        out, = Decoder().feed(r.feed_control(bytes.fromhex('eb90000111001109d7')))
        self.assertEqual(tmg.unpack(out[1:])['TMG034'],65535)

    def test_obc_gate_separation(self):
        r = self.r
        def ordinary(v, wrapped=False):
            w = bytes([v])+bytes(5)
            frame = Message(0x21,17,0x17,w).wire() if wrapped else encode(bytes([0x21,0x17])+w)
            self.assertFalse(r.feed_control(frame))
            self.assertTrue(r.legacy.execute_next())
        ordinary(1)
        self.assertTrue(self.staging.ordinary_gate)
        self.op(0x17,bytes([2])+bytes(5))
        self.assertTrue(self.staging.ordinary_gate)
        ordinary(2,True)
        self.assertFalse(self.staging.state['gate'])
        self.op(0x17,bytes([1])+bytes(5))
        ordinary(2)
        self.assertTrue(self.staging.state['gate'])
        self.assertFalse(self.staging.ordinary_gate)
        self.op(0x17,bytes([2])+bytes(5))
        self.assertFalse(self.staging.state['gate'])

    def test_wrong_reference_crc_and_invalid_profile(self):
        first, last = file_packets(b'\xff'*499, 'XMODEM', ota101=True)
        body = bytearray(Decoder(ota101=True).feed(first)[0])
        body[9:11] = bytes.fromhex('7c6d')
        self.send(encode(body, ota101=True))
        self.send(last)
        self.op(0x54,bytes(6),0x11)
        self.assertEqual(self.status(0x24)['TMG013'],5)
        self.begin()
        self.send(first)
        self.send(last)
        self.staging.crc_config_valid = False
        self.op(0x54,bytes(6),0x11)
        self.assertEqual(self.status(0x24)['TMG013'],5)



class Restart(unittest.TestCase):
    def test_successful_activation_returns_to_idle_after_service_restart(self):
        with tempfile.TemporaryDirectory() as root:
            stage = Staging(root, crc_profile='XMODEM', capacity=8192)
            stage.state.update(status=0x31, check=4)
            stage._save()
            stage.close()
            stage = Staging(root, crc_profile='XMODEM', capacity=8192)
            try:
                self.assertEqual(stage.snapshot()['TMG014'], 0)
            finally:
                stage.close()
