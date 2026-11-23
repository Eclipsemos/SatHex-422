"""Raw HT226 frames through the normal control entry, result FIFO and file backend."""
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest

from gs422 import tmg
from gs422.core import Cache
from gs422.file_services import FileServices
from gs422.fixture_io import FixtureIO
from gs422.ht226_files import PublishedFiles
from gs422.ota_staging import Staging
from gs422.payload_backend import PayloadBackend
from gs422.revised_runtime import Revised
from gs422.space_commands import REPLAY_TYPES, validate
from gs422.space_protocol import Decoder, Message, encode


@unittest.skipUnless(sys.platform.startswith('linux'), 'uses Linux dirfd file operations')
class ShortTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.pub = self.root/'published'
        for group, types in [('clean',range(1,7)),('replay',REPLAY_TYPES)]:
            for n in types:
                (self.pub/group/f'{n:02X}').mkdir(parents=True)
        self.fixture = FixtureIO(self.root/'output')
        self.files = FileServices(self.root/'records', published=PublishedFiles(self.pub),
                                  replay_sinks={n:self.fixture.file_sink(n) for n in (0,1)})
        self.addCleanup(self.files.close)
        stage = Staging(self.root/'ota',crc_profile='XMODEM',capacity=2048)
        self.addCleanup(stage.close)
        self.r = Revised(stage,Cache(10),dict.fromkeys(tmg.BY_ID,0),version='test',ota101=True,
                         adapter=PayloadBackend(None,self.files))
        self.addCleanup(self.r.close)

    def short(self, code, words=bytes(6), status=3):
        frame = encode(bytes((0x21,code))+words)
        self.assertEqual(self.r.feed_control(frame),b'')
        self.r.legacy.execute_next()
        out = self.r.feed_control(Message(0x15,9,0).wire())
        body = Message.parse(Decoder().feed(out)[0]).body
        self.assertEqual((body[0],body[3],body[4]),(1,code,status))
        info = json.loads(body[9:])
        self.assertEqual(info['words'],words[:6].hex())
        self.assertEqual(info['device'],1 if code==0x13 else 2)
        self.assertEqual(info['exit'],int.from_bytes(body[5:7],'big'))
        return info

    def test_reserved_bytes_enum_length_and_original_routes(self):
        for code, words, start in [(0x40,bytes(6),1),(0x41,bytes.fromhex('110100000000'),2),
                                   (0x13,bytes.fromhex('010000000000'),1),
                                   *((n,bytes(6),0) for n in (0x42,0x43,0x51,0x52))]:
            for i in range(start,6):
                invalid=bytearray(words)
                invalid[i]=1
                self.short(code,bytes(invalid),4)
            self.short(code,words[:-1],4)
            self.short(code,bytes(480),4)
        for code, words in [(0x40,[2,0]),(0x41,[0x10,1]),(0x41,[0x11,0]),
                            *((0x41,[n,1]) for n in range(0x40,0x50)),(0x13,[0,0]),(0x13,[7,0])]:
            self.short(code,bytes(words)+bytes(4),5 if code==0x41 else 4)
        self.assertEqual(validate(Message(0x21,1,0x13,b''))[0],'storage_clean')
        self.assertEqual(validate(Message(0x25,1,0x40,bytes.fromhex('0101')+bytes(14)))[0],'playback_link')
        self.assertEqual(validate(Message(0x25,1,0x41,bytes(12)+bytes((1,0,0))))[0],'playback_file')

    def test_latest_files_on_both_links(self):
        for n in REPLAY_TYPES:
            directory=self.pub/'replay'/f'{n:02X}'
            for i in range(3):
                f=directory/f'item{i}.bin'
                f.write_bytes(bytes([n,i])*3000)
                os.utime(f,(100+i,100+i))
            self.assertEqual(self.short(0x41,bytes([n,2])+bytes(4))['count'],2)
        for link,name in [(0,'main-downlink'),(1,'backup-downlink')]:
            self.assertEqual(self.short(0x40,bytes([link])+bytes(5))['link'],link)
            self.short(0x42)
            self.files.play_thread.join(2)
            info=self.short(0x52)
            self.assertEqual((info['link'],info['play_state'],info['play_frames']),(link,2,4))
            out=self.fixture.root/name
            self.assertEqual(sorted(f.name for f in out.iterdir()),['item1.bin','item2.bin'])
            for i in (1,2):
                self.assertEqual((out/f'item{i}.bin').read_bytes(),bytes([0x22,i])*3000)
        self.short(0x41,bytes([0x4f,1])+bytes(4),5)
        self.short(0x42,status=5)
        self.short(0x41,bytes([0x11,4])+bytes(4),5)
        self.short(0x42,status=5)

    def test_clean_only_published_target(self):
        for n in range(1,7):
            d=self.pub/'clean'/f'{n:02X}'
            (d/'old').write_bytes(bytes([n]))
            (d/'.writing').touch()
            (d/'symlink').symlink_to(self.root/'records'/'records.sqlite3')
        for n in range(1,7):
            self.assertEqual(self.short(0x13,bytes([n])+bytes(5))['deleted_files'],1)
            for j in range(1,7):
                d=self.pub/'clean'/f'{j:02X}'
                self.assertEqual((d/'old').exists(), j>n)
                self.assertTrue((d/'.writing').exists() and (d/'symlink').is_symlink())
            self.assertEqual(self.short(0x13,bytes([n])+bytes(5))['deleted_files'],0)

    def test_stop_then_restart_and_storage_counters(self):
        d=self.pub/'replay'/'11'
        (d/'old').write_bytes(b'payload')
        (d/'new').write_bytes(b'second')
        os.utime(d/'old',(1,1))
        os.utime(d/'new',(2,2))
        sent=threading.Event()
        names=[]
        def send(name,source,cancel,progress):
            names.append(name)
            progress()
            sent.set()
            cancel.wait(2)
        self.files.replay_sinks[0]=send
        self.short(0x40)
        self.short(0x41,bytes.fromhex('110200000000'))
        self.short(0x42)
        self.assertTrue(sent.wait(1))
        self.short(0x43)
        self.assertEqual(names,['new'])
        self.assertEqual(self.short(0x52)['play_frames'],1)
        self.assertEqual(self.short(0x52)['play_state'],3)
        self.files.replay_sinks[0]=lambda n,s,c,p:(names.append(n),p())
        self.short(0x42)
        self.files.play_thread.join(2)
        self.assertEqual(names,['new','new','old'])
        received=threading.Event()
        def produce(start,count,cancel):
            import zlib
            yield b'abc',zlib.crc32(b'abc')
            received.set()
            cancel.wait(2)
        self.files.sources[7]=produce
        self.files.execute('storage_start',dict(file_id=7,start_frame=0,frame_count=0))
        self.assertTrue(received.wait(1))
        self.assertEqual(self.short(0x52)['state'],1)
        self.short(0x51)
        for _ in range(2):
            info=self.short(0x52)
            self.assertEqual((info['state'],info['frames'],info['bytes']),(3,1,3))
        self.assertEqual(self.r.correct,self.r.legacy.executed)

    def test_replaced_source_does_not_send_wrong_file(self):
        f=self.pub/'replay'/'11'/'nfs.bin'
        f.write_bytes(b'old')
        self.short(0x40)
        self.short(0x41,bytes.fromhex('110100000000'))
        tmp=f.with_name('.next')
        tmp.write_bytes(b'changed')
        tmp.replace(f)
        self.short(0x42)
        self.files.play_thread.join(2)
        self.assertEqual(self.short(0x52)['play_state'],3)
        self.assertFalse((self.fixture.root/'main-downlink'/'nfs.bin').exists())
