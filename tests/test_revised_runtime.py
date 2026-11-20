import tempfile
import unittest
from gs422.core import Cache
from gs422 import tmg
from gs422.ota_wire import control, file_packets
from gs422.ota_staging import Staging
from gs422.space_protocol import Decoder, Message
from gs422.revised_runtime import Revised


class RevisedRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.storage = Staging(self.temp.name, crc_profile='CCITT-FALSE', capacity=10000)
        self.values = {f.id: 0 for f in tmg.FIELDS}
        self.values.update(TMG004=123456789, TMG005=1200, TMG006=42, TMG007=41, TMG034=1200)
        self.now = [0]
        self.runtime = Revised(self.storage, Cache(1000), self.values, version='V1.0.4', clock=lambda: self.now[0])

    def tearDown(self):
        self.runtime.close()
        self.storage.close()
        self.temp.cleanup()

    def drain_work(self):
        while self.runtime.execute_next():
            pass
        return Decoder().feed(self.runtime.drain_replies())

    def telemetry(self):
        response = self.runtime.feed_control(Message(0x11, 7, 0).wire())
        message = Message.parse(Decoder().feed(response)[0])
        self.assertEqual((message.kind, message.sequence), (0x12, 7))
        return tmg.unpack(message.body)

    def test_complete_two_channel_upload_with_live_telemetry_and_no_data_tx(self):
        self.runtime.feed_control(control(0x30, 0x17, b'\x01' + bytes(5)))
        self.assertEqual(self.drain_work(), [b'\x31\x17\x00'])
        self.runtime.feed_control(control(0x30, 0x53, bytes([3, 1, 2, 0, 0, 0])))
        self.assertEqual(self.drain_work(), [b'\x31\x53\x00'])
        self.assertEqual(self.telemetry()['TMG014'], 0x20)
        frames = list(file_packets(b'x' * 900, 'CCITT-FALSE'))
        for frame in frames:
            self.assertEqual(self.runtime.feed_upload(frame), b'')
            self.assertEqual(self.drain_work(), [])
        self.assertEqual(self.telemetry()['TMG010'], 2)
        self.runtime.feed_control(control(0x30, 0x54, bytes(6)))
        self.assertEqual(self.drain_work(), [b'\x31\x54\x00'])
        values = self.telemetry()
        self.assertEqual((values['TMG012'], values['TMG013'], values['TMG014']), (100, 4, 0x23))
        self.now[0] = 257
        self.assertEqual(self.telemetry()['TMG040'], 1)

    def test_type21_gate_semantics_remain_separate_from_type30(self):
        self.assertEqual(self.runtime.feed_control(Message(0x21, 1, 0x17, b'\x02' + bytes(5)).wire()), b'')
        self.runtime.legacy.execute_next()
        self.assertEqual(self.runtime.legacy.receive_mode, 2)
        self.assertTrue(self.storage.state['gate'])
        self.runtime.feed_control(control(0x30, 0x17, b'\x02' + bytes(5)))
        self.assertEqual(self.drain_work(), [b'\x31\x17\x00'])
        self.assertFalse(self.storage.state['gate'])

    def test_k1116_k1117_share_upload_state_and_async_results(self):
        def send(seq,code,body):
            self.assertEqual(self.runtime.feed_control(Message(0x21,seq,code,body).wire()),b'')
            self.runtime.legacy.execute_next()
            answer=Message.parse(Decoder().feed(self.runtime.feed_control(Message(0x15,500,0).wire()))[0]).body
            self.assertEqual(answer[4:7],b'\x03\x00\x00')
        send(1,0x17,b'\x02'+bytes(5))
        send(2,0x53,bytes([3,1,2,0,0,0]))
        for frame in file_packets(b'hello','CCITT-FALSE'):
            self.runtime.feed_upload(frame);self.drain_work()
        send(3,0x54,bytes(6))
        self.assertEqual(self.telemetry()['TMG014'],0x23)

    def test_bad_frames_counters_no_reply_and_unsupported_flash(self):
        bad = bytearray(control(0x30, 0x56, bytes(6))); bad[-4] ^= 1
        self.assertEqual(self.runtime.feed_control(bad), b'')
        values = self.telemetry()
        self.assertEqual((values['TMG002'], values['TMG003']), (1, 1))
        self.runtime.feed_control(control(0x30, 0x55, b'\x01' + bytes(5)))
        self.assertEqual(self.drain_work(), [b'\x31\x55\xff'])
        self.assertNotIn(self.telemetry()['TMG014'], (0x11, 0x31))

    def test_missing_fields_and_wrong_crc_rejected(self):
        with self.assertRaises(ValueError):
            self.runtime.update_telemetry({})
        with self.assertRaises(ValueError):
            Revised(self.storage, Cache(1000), {}, version='V1')

    def test_reply_queue_backpressure_preserves_responses(self):
        for _ in range(128):
            self.runtime.replies.put_nowait(b'x')
        self.runtime.feed_control(control(0x30, 0x17, b'\x01' + bytes(5)))
        self.assertFalse(self.runtime.execute_next())
        self.assertFalse(self.storage.state['gate'])
        self.assertEqual(len(self.runtime.drain_replies()), 128)
        self.assertTrue(self.runtime.execute_next())
        self.assertTrue(self.storage.state['gate'])


if __name__ == '__main__':
    unittest.main()
