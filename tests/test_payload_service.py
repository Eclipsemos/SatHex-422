import json
from pathlib import Path
import sys
import tempfile
import unittest


@unittest.skipUnless(sys.platform.startswith('linux'),'Linux deployment imports')
class PayloadServiceTests(unittest.TestCase):
    def test_fixture_requires_opt_in_and_fresh_measurements(self):
        from gs422.payload_service import measurement_snapshot
        with tempfile.TemporaryDirectory() as root:
            p=Path(root)/'snapshot.json'
            d={'source':'fixture','monotonic':10,'values':{'TMG004':1700000000,'TMG005':1200,'TMG006':45,'TMG007':46,'TMG034':1200}}
            p.write_text(json.dumps(d))
            with self.assertRaises(ValueError):measurement_snapshot(p,2,False,clock=lambda:11)
            raw,deadline=measurement_snapshot(p,2,True,clock=lambda:11)
            self.assertEqual(deadline,12);self.assertEqual(raw['TMG005'],1200)
            with self.assertRaises(ValueError):measurement_snapshot(p,2,True,clock=lambda:13)
            with self.assertRaises(ValueError):measurement_snapshot(p,2,True,clock=lambda:9)
            del d['values']['TMG005'];p.write_text(json.dumps(d))
            with self.assertRaises(ValueError):measurement_snapshot(p,2,True,clock=lambda:11)

    def test_config_path_overlap_booleans_and_catalog(self):
        from gs422.payload_service import load_config
        root=Path(__file__).resolve().parents[1]
        c=json.loads((root/'config/gs422-payload-lab.example.json').read_text())
        with tempfile.TemporaryDirectory() as temp:
            p=Path(temp)/'config.json';p.write_text(json.dumps(c))
            self.assertEqual(load_config(p)['mode'],'lab')
            for field,value in [('mode','production'),('enable_reboot','yes'),('telemetry_max_age',float('nan')),
                                ('records_root',c['state_root']+'/records'),('upload_port',c['control_port'])]:
                bad=dict(c);bad[field]=value;p.write_text(json.dumps(bad))
                with self.subTest(field=field),self.assertRaises(ValueError):load_config(p)

    def test_expired_snapshot_suppresses_only_telemetry(self):
        from gs422.revised_runtime import Revised
        from gs422.ota_staging import Staging
        from gs422.core import Cache
        from gs422 import tmg
        from gs422.space_protocol import Message,Decoder
        from gs422.ota_wire import control
        with tempfile.TemporaryDirectory() as root:
            s=Staging(root,crc_profile='CCITT-FALSE',capacity=1000)
            r=Revised(s,Cache(100),{f.id:0 for f in tmg.FIELDS},version='vtest',clock=lambda:10)
            try:
                r.set_telemetry_deadline(9)
                self.assertEqual(r.feed_control(Message(0x11,1,0).wire()),b'')
                self.assertEqual(Decoder().feed(r.feed_control(control(0x30,0x56,bytes(6)))),[b'\x33\x56vtest\x00'])
                r.set_telemetry_deadline(11)
                self.assertTrue(r.feed_control(Message(0x11,2,0).wire()))
            finally:r.close();s.close()
