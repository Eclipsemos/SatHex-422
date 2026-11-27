from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

from gs422 import tmg
from gs422.core import Cache
from gs422.ota_staging import Staging
from gs422.ota_wire import file_packets
from gs422.revised_runtime import Revised
from gs422.space_protocol import Decoder, encode


class OrdinaryUploadTests(unittest.TestCase):
    def setUp(self):
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.open()
        self.addCleanup(lambda: self.stage.close())
        self.data = bytes(range(256))*4
        self.packets = list(file_packets(self.data,'XMODEM',ota101=True))
        self.request = dict(component=3,region=1,mode=2,segmented=0,segments=0)

    def open(self):
        self.stage = Staging(self.root,crc_profile='XMODEM',capacity=4096)
        self.backend = Mock()
        self.rt = Revised(self.stage,Cache(20),dict.fromkeys(tmg.BY_ID,0),version='test',
                          ota101=True,software_backend=self.backend)

    def feed(self,packet):
        self.rt.feed_upload(packet)
        self.assertTrue(self.rt.execute_next())

    def begin(self):
        self.rt.set_legacy_gate(1)
        self.assertEqual(self.rt.run_ota('begin',self.request),0)

    def finish(self):
        return self.rt.run_ota('end',{'aborted':False})

    def test_pause_restart_retry_and_isolation(self):
        self.backend.permits.return_value = False  # active app can receive a data file
        self.begin()
        self.feed(self.packets[0])
        self.rt.feed_upload(self.packets[1])  # pending at close must not leak through reopen
        self.rt.set_legacy_gate(0)
        self.rt.set_legacy_gate(1)
        self.rt.execute_next()
        self.assertEqual(self.stage.snapshot()['TMG010'],1)
        self.rt.set_legacy_gate(0)
        self.feed(self.packets[1])
        self.assertNotEqual(self.finish(),0)
        self.stage.close()
        self.open()
        self.assertFalse(self.stage.ordinary_gate)
        self.begin()
        self.feed(self.packets[2])
        self.assertNotEqual(self.finish(),0)
        self.assertFalse((self.root/'received-files').exists())
        self.feed(self.packets[1])
        with patch.object(self.stage,'_write_contents',side_effect=OSError('full disk')):
            self.assertNotEqual(self.finish(),0)
        self.assertEqual(self.stage.snapshot()['TMG014'],0x40)
        self.assertEqual(list((self.root/'received-files'/'APP1').iterdir()),[])
        self.assertEqual(self.finish(),0)
        self.assertEqual(self.finish(),0)
        f=self.root/'received-files'/'APP1'/'00000001.bin'
        self.assertEqual(f.read_bytes(),self.data)
        self.stage.close()
        self.open()
        self.assertEqual(self.rt.legacy.receive_mode,1)
        self.rt.set_legacy_gate(2)
        self.assertNotEqual(self.rt.run_ota('load',{'index':0}),0)
        self.backend.ota.assert_not_called()

    def test_bad_crc_and_targets(self):
        self.begin()
        for args in (dict(mode=1),dict(component=1),dict(region=3)):
            self.assertNotEqual(self.rt.run_ota('begin',dict(self.request,**args)),0)
        payload = bytearray(Decoder(ota101=True).feed(self.packets[0])[0])
        payload[10]^=1
        self.feed(encode(payload,ota101=True))
        for frame in self.packets[1:]:
            self.feed(frame)
        self.assertEqual(self.finish(),0x11)
        self.assertFalse((self.root/'received-files').exists())
        self.assertEqual(self.stage.snapshot()['TMG013'],5)
        self.begin()
        for frame in self.packets:
            self.feed(frame)
        self.assertEqual(self.finish(),0)
        fid=self.stage.state['file_id']
        self.assertEqual((self.root/'received-files'/'APP1'/f'{fid:08X}.bin').read_bytes(),self.data)

    def test_mode_switch_cannot_mix_staged_bytes(self):
        self.begin()
        self.feed(self.packets[0])
        self.rt.set_legacy_gate(2)
        self.feed(self.packets[1])
        self.assertEqual(self.stage.snapshot()['TMG010'],1)
        self.assertNotEqual(self.rt.run_ota('begin',self.request),0)
        self.assertNotEqual(self.finish(),0)
        self.rt.set_legacy_gate(1)
        self.assertNotEqual(self.rt.run_ota('end',{'aborted':True}),0)
        self.rt.set_legacy_gate(2)
        self.assertEqual(self.rt.run_ota('begin',self.request),0)
        self.assertFalse(self.stage.ordinary_file)
        self.assertEqual(self.stage.snapshot()['TMG010'],0)
