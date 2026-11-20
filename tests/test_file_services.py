import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
import zlib
from gs422.file_services import FileServices
from gs422.space_commands import CommandError


class FileServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.service=FileServices(self.temp.name)

    def tearDown(self):
        self.service.close();self.temp.cleanup()

    def wait(self, name):
        thread=getattr(self.service,name+'_thread')
        thread.join(timeout=2)
        self.assertFalse(thread.is_alive())

    def configure_play(self,fid,size,end=1,link=1):
        self.service.execute('playback_link',dict(link=link,file_type=2,start_utc=0,end_utc=100,byte_count=size))
        self.service.execute('playback_file',dict(file_id=fid,start_frame=0,end_frame=end,rate=4))

    def test_dual_sink_payload_exact_and_complete_telemetry(self):
        first=[];second=[]
        self.service.sinks={1:lambda b,c:first.append(b),2:lambda b,c:second.append(b)}
        fid=self.service.add_file([(1,b'alpha'),(1,b'beta')])
        self.configure_play(fid,9,link=3)
        self.service.execute('playback_start',{})
        self.wait('play')
        self.assertEqual(first,[b'alpha',b'beta']);self.assertEqual(second,first)
        self.assertEqual(self.service.telemetry()['TMG036'],2)
        self.assertEqual(self.service.telemetry()['TMG035'],fid)

    def test_stop_waits_for_output_and_restart_clears_cancel(self):
        started=threading.Event();received=[]
        def sink(body,cancel):
            started.set();cancel.wait(1)
            if not cancel.is_set():received.append(body)
        self.service.sinks={1:sink}
        fid=self.service.add_file([(1,b'x')]);self.configure_play(fid,1,end=0)
        self.service.execute('playback_start',{});self.assertTrue(started.wait(1))
        self.service.execute('playback_stop',{})
        self.assertEqual(received,[]);self.assertEqual(self.service.play_state,3)
        self.service.sinks={1:lambda b,c:received.append(b)}
        self.service.execute('playback_start',{});self.wait('play')
        self.assertEqual(received,[b'x'])

    def test_no_sink_or_incomplete_frame_budget_rejected(self):
        fid=self.service.add_file([(1,b'123')]);self.configure_play(fid,2,end=0)
        with self.assertRaises(CommandError):self.service.execute('playback_start',{})
        self.configure_play(fid,3,end=0)
        with self.assertRaises(CommandError):self.service.execute('playback_start',{})
        self.assertEqual(self.service.play_state,0)

    def test_crc_checked_source_persists_exact_records(self):
        frames=[b'a',b'bc',b'def']
        def source(start,count,cancel):
            for b in frames[start:None if count==0 else start+count]:yield b,zlib.crc32(b)
        self.service.sources={7:source}
        self.service.execute('storage_start',dict(file_id=7,start_frame=1,frame_count=0))
        self.wait('store')
        status=json.loads(self.service.execute('storage_status',{}))
        self.assertEqual((status['frames'],status['bytes'],status['state']),(2,5,2))
        raw=self.service.db.execute('SELECT body FROM frames WHERE file=? ORDER BY idx',(status['file_id'],)).fetchall()
        self.assertEqual(raw,[(b'bc',),(b'def',)])

    def test_bad_crc_not_counted_and_partial_not_playable(self):
        self.service.sources={1:lambda start,count,cancel:iter([(b'bad',0)])}
        self.service.execute('storage_start',dict(file_id=1,start_frame=0,frame_count=1));self.wait('store')
        self.assertEqual(self.service.store_frames,0);self.assertEqual(self.service.crc_errors,1)
        self.assertEqual(self.service.db.execute('SELECT complete FROM files').fetchone(),(0,))

    def test_short_source_does_not_mark_requested_range_complete(self):
        self.service.sources={1:lambda start,count,cancel:iter([(b'x',zlib.crc32(b'x'))])}
        self.service.execute('storage_start',dict(file_id=1,start_frame=0,frame_count=2));self.wait('store')
        self.assertEqual(self.service.store_frames,1)
        self.assertEqual(self.service.store_state,3)
        self.assertEqual(self.service.db.execute('SELECT complete FROM files').fetchone(),(0,))

    def test_cleanup_only_managed_complete_data_and_no_id_reuse(self):
        foreign=Path(self.temp.name)/'unrelated.txt';foreign.write_text('keep')
        fid=self.service.add_file([(1,b'old')])
        result=json.loads(self.service.execute('storage_clean',{}))
        self.assertEqual(result,{'deleted_files':1});self.assertEqual(foreign.read_text(),'keep')
        new=self.service.add_file([(2,b'new')]);self.assertGreater(new,fid)

    def test_capture_absent_or_wrong_storage_never_succeeds(self):
        with self.assertRaises(CommandError):self.service.execute('capture_control',dict(enabled=True,storage=0,mode=1))
        class Driver:
            def start(self,*args):raise AssertionError('must not start')
            def stop(self):pass
        self.service.capture_driver=Driver()
        with self.assertRaises(CommandError):self.service.execute('capture_control',dict(enabled=True,storage=1,mode=1))

    def test_storage_stop_honors_source_cancellation(self):
        started=threading.Event()
        def source(start,count,cancel):
            started.set();cancel.wait(2)
            if not cancel.is_set():yield b'x',zlib.crc32(b'x')
        self.service.sources={1:source}
        self.service.execute('storage_start',dict(file_id=1,start_frame=0,frame_count=0));self.assertTrue(started.wait(1))
        self.service.execute('storage_stop',{})
        self.assertEqual(self.service.store_state,3);self.assertEqual(self.service.store_frames,0)

    @unittest.skipUnless(Path('/dev/shm').is_dir(),'requires Linux tmpfs')
    def test_capture_writes_real_tmpfs_records_and_stops(self):
        class Driver:
            def start(self,mode,receiver):self.mode=mode;self.receiver=receiver
            def stop(self):self.receiver=None
        with tempfile.TemporaryDirectory(dir='/dev/shm') as folder:
            driver=Driver();service=FileServices(folder,capture_driver=driver)
            try:
                result=json.loads(service.execute('capture_control',dict(enabled=True,storage=0,mode=3)))
                driver.receiver(b'star-sample',100)
                service.execute('capture_control',dict(enabled=False,storage=0,mode=3))
                self.assertEqual(service.db.execute('SELECT body FROM frames').fetchone()[0],b'star-sample')
                self.assertEqual(service.db.execute('SELECT complete FROM files WHERE id=?',(result['file_id'],)).fetchone(),(1,))
                self.assertIsNone(driver.receiver)
            finally:service.close()
