import struct
import tempfile
import unittest
from pathlib import Path
from gs422.core import Cache
from gs422 import tmg
from gs422.ota_wire import control,file_packets,progress_reply,segment_ranges
from gs422.ota_staging import Staging
from gs422.space_protocol import Decoder
from gs422.revised_runtime import Revised


class SegmentedTests(unittest.TestCase):
    def test_ranges_cover_packets_once_and_boundaries(self):
        self.assertEqual(segment_ranges(7,3),[(0,2),(2,4),(4,7)])
        self.assertEqual(segment_ranges(255,255)[-1],(254,255))
        for n,s in [(0,1),(2,3),(3,0),(999,256)]:
            with self.assertRaises(ValueError):segment_ranges(n,s)

    def test_segmented_out_of_order_restart_gap_progress_final_crc(self):
        with tempfile.TemporaryDirectory() as root:
            s=Staging(root,crc_profile='CCITT-FALSE',capacity=10000)
            s.gate(True);self.assertEqual(s.begin(3,1,1,1,3),0)
            data=bytes(range(256))*12
            packets=[Decoder().feed(p)[0] for p in file_packets(data,'CCITT-FALSE')]
            self.assertEqual(len(packets),7)
            self.assertTrue(s.receive(packets[0]));self.assertTrue(s.receive(packets[1]))
            for i in (4,5,6):self.assertTrue(s.receive(packets[i]))
            self.assertEqual(s.snapshot()['TMG009'],2);self.assertEqual(s.end(),1)
            s.close();s=Staging(root,crc_profile='CCITT-FALSE',capacity=10000)
            self.assertEqual(s.state['request'][3:],[1,3])
            self.assertEqual(s.begin(3,1,1,1,3),0)
            self.assertEqual(s.begin(3,1,1,1,2),1)
            for i in (2,3):self.assertTrue(s.receive(packets[i]))
            self.assertEqual(s.end(),0)
            out=Path(root)/'result';s.export_verified(out);self.assertEqual(out.read_bytes(),data);s.close()

    def test_too_many_segments_for_actual_file_fails_at_first_metadata(self):
        with tempfile.TemporaryDirectory() as root:
            s=Staging(root,crc_profile='CCITT-FALSE',capacity=10000);s.gate(True)
            self.assertEqual(s.begin(3,1,1,1,255),0)
            self.assertFalse(s.receive(Decoder().feed(next(file_packets(b'x','CCITT-FALSE')))[0]))
            self.assertEqual(s.snapshot()['TMG014'],0x40);s.close()

    def test_progress_query_uses_cached_state_and_exact_layout(self):
        with tempfile.TemporaryDirectory() as root:
            s=Staging(root,crc_profile='CCITT-FALSE',capacity=1000)
            r=Revised(s,Cache(100),{f.id:0 for f in tmg.FIELDS},version='test')
            try:
                r.raw.update(TMG008=0x12345678,TMG009=2,TMG010=5,TMG011=1,TMG012=71,TMG013=2,TMG014=0x20)
                # A query must not depend on a database lookup or valid sensor snapshot.
                s.snapshot=lambda:(_ for _ in ()).throw(AssertionError('no database on RX'))
                r.set_telemetry_deadline(0)
                response=r.feed_control(control(0x32,0x53,bytes(6)))
                self.assertEqual(len(response),29)
                self.assertEqual(Decoder().feed(response),[b'\x33\x53'+struct.pack('>4IBBB',0x12345678,2,5,1,71,2,0x20)])
                self.assertEqual(Decoder().feed(r.feed_control(control(0x32,0x53,b'\x01'+bytes(5)))),[b'\x31\x53\xff'])
            finally:r.close();s.close()
