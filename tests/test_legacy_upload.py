import binascii
from pathlib import Path
import struct
import tempfile
import unittest
from gs422.legacy_upload import LegacyUpload
from gs422.space_commands import CommandError, validate
from gs422.space_protocol import Message, fragments
from gs422.ota_staging import Staging
from gs422.revised_runtime import Revised
from gs422.core import Cache
from gs422 import tmg


def messages(data,sequence=1):
    frames=(len(data)+511)//512
    crc=binascii.crc_hqx(data,0xffff)
    result=[]
    for i in range(frames):
        chunk=data[i*512:(i+1)*512]
        body=struct.pack('>IIIIIB',len(chunk),0,len(data),frames,(frames+7)//8,i)+chunk+struct.pack('>HH',sum(chunk)&65535,crc)
        result.append(Message(0x25,sequence+i,4,body))
    return result


class LegacyTests(unittest.TestCase):
    def test_out_of_order_duplicate_resume_exact_export(self):
        with tempfile.TemporaryDirectory() as folder:
            data=bytes(range(256))*5
            msg=messages(data)
            receive=LegacyUpload(folder)
            receive.receive(validate(msg[2])[1]);receive.receive(validate(msg[0])[1])
            self.assertEqual(receive.snapshot()['TMG009'],1)
            receive.close();receive=LegacyUpload(folder)
            receive.receive(validate(msg[2])[1])
            self.assertEqual(receive.snapshot()['TMG010'],2)
            receive.receive(validate(msg[1])[1])
            self.assertEqual(receive.snapshot()['TMG014'],0x23)
            target=Path(folder)/'output.bin';receive.export_verified(target)
            self.assertEqual(target.read_bytes(),data)
            receive.close()

    def test_crc_failure_and_bad_metadata_never_export(self):
        with tempfile.TemporaryDirectory() as folder:
            receive=LegacyUpload(folder)
            p=validate(messages(b'hello')[0])[1]
            p['bitmap_size']=2
            with self.assertRaises(CommandError):receive.receive(p)
            self.assertEqual(receive.snapshot()['TMG010'],0)
            p['bitmap_size']=1;p['file_checksum']^=1
            with self.assertRaises(CommandError):receive.receive(p)
            self.assertEqual(receive.snapshot()['TMG014'],0x24)
            with self.assertRaises(CommandError):receive.receive(p)  # a retry cannot turn failed CRC into success
            with self.assertRaises(CommandError):receive.export_verified(Path(folder)/'bad')
            receive.reset();self.assertEqual(receive.snapshot()['TMG010'],0)
            receive.close()

    def test_conflicting_duplicate_and_transfer_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            receive=LegacyUpload(folder)
            p=validate(messages(b'x'*600)[0])[1];receive.receive(p)
            changed=dict(p,data_hex=(b'y'*512).hex(),packet_checksum=sum(b'y'*512)&65535)
            with self.assertRaises(CommandError):receive.receive(changed)
            other=validate(messages(b'z'*700)[0])[1]
            with self.assertRaises(CommandError):receive.receive(other)
            self.assertEqual(receive.snapshot()['TMG010'],1)
            receive.close()

    def test_fragmented_d0351_via_runtime_and_modern_mutual_exclusion(self):
        with tempfile.TemporaryDirectory() as folder:
            old=LegacyUpload(Path(folder)/'old');new=Staging(Path(folder)/'new',crc_profile='CCITT-FALSE',capacity=4096)
            runtime=Revised(new,Cache(100),{f.id:0 for f in tmg.FIELDS},version='test',legacy_upload=old)
            try:
                runtime.legacy.handle(Message(0x21,900,0x17,b'\x02'+bytes(5)).payload())
                runtime.legacy.execute_next()
                for msg in messages(b'a'*600):
                    for fragment in fragments(msg):runtime.feed_control(fragment.wire())
                    runtime.legacy.execute_next()
                    if msg.sequence==1:
                        self.assertEqual(runtime.run_ota('begin',dict(component=3,region=1,mode=2,segmented=0,segments=0)),1)
                self.assertEqual(old.snapshot()['TMG014'],0x23)
                self.assertEqual(runtime.raw['TMG010'],2)
                runtime.set_legacy_gate(0)
                runtime.run_ota('gate',{'enabled':True})
                runtime.run_ota('begin',dict(component=3,region=1,mode=2,segmented=0,segments=0))
                runtime.legacy.receive_mode=2
                runtime.legacy.handle(messages(b'next',300)[0].payload());runtime.legacy.execute_next()
                self.assertEqual(runtime.legacy.latest[-1],6)
            finally:runtime.close();old.close();new.close()
