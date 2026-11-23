import binascii
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from gs422 import tmg
from gs422.core import Cache
from gs422.file_services import FileServices
from gs422.ota_staging import Staging
from gs422.ota_wire import control, file_packets
from gs422.revised_runtime import Revised
from gs422.space_commands import CommandError
from gs422.space_protocol import Decoder, Message
from gs422.task_scheduler import Scheduler


class StatusTests(unittest.TestCase):
    def test_complete_then_verify_and_internal_fault_then_recover(self):
        with tempfile.TemporaryDirectory() as directory:
            make = lambda: Staging(directory,crc_profile='CCITT-FALSE',capacity=8192)
            s = make()
            r = Revised(s,Cache(10),dict.fromkeys(tmg.BY_ID,0),version='test')
            def status():
                p=Decoder().feed(r.feed_control(control(0x32,0x53,bytes(6))))[0]
                m=Message.parse(Decoder().feed(r.feed_control(Message(0x11,9,0).wire()))[0])
                value=tmg.unpack(m.body)['TMG014']
                self.assertEqual(value,p[-1])
                return value
            def op(code, body):
                r.feed_control(control(0x30,code,body));r.execute_next()
                return Decoder().feed(r.drain_replies())[0][-1]
            try:
                op(0x17,b'\1'+bytes(5)); begin=bytes([3,1,1,0,0,0]);op(0x53,begin)
                packets=list(file_packets(b'x'*1000,'CCITT-FALSE'))
                for i,expected in [(0,32),(2,32),(1,33),(2,33)]:
                    self.assertEqual(r.feed_upload(packets[i]),b'');r.execute_next()
                    self.assertEqual(status(),expected)
                s.close();s=make();r.staging=s;r._snapshot()
                self.assertEqual(status(),0x21)
                self.assertEqual(op(0x54,bytes(6)),0);self.assertEqual(status(),0x23)
                backend=Mock(); backend.ota.side_effect=RuntimeError('isolated fault injection')
                r.software_backend=backend
                with self.assertLogs('gs422.revised_runtime',level='ERROR'):
                    self.assertEqual(op(0x55,bytes([2,1,0,0,0,0])),255)
                r.update_telemetry(dict.fromkeys(tmg.BY_ID,0))
                self.assertEqual(status(),255)
                self.assertEqual(op(0x53,begin),0);self.assertEqual(status(),0x20)
            finally:r.close();s.close()


class CaptureTests(unittest.TestCase):
    @unittest.skipUnless(hasattr(os, 'statvfs'), 'Linux filesystem accounting')
    def test_bound_storage_mode_idempotence_and_failed_stop(self):
        with tempfile.TemporaryDirectory() as folder:
            driver=Mock()
            service=FileServices(folder,capture_driver=driver,ssd_root=folder,ssd_uuid='fixture')
            try:
                p=dict(enabled=True,storage=1,mode=2)
                with self.assertRaises(CommandError):service.execute('capture_control',p)
                # Only the mount identity is simulated. Writes use a separate real SQLite file.
                with patch('gs422.file_services.Path.is_mount',return_value=True),patch('gs422.file_services.Path.exists',return_value=True),patch('gs422.file_services.stat.S_ISBLK',return_value=True),patch('gs422.file_services.Path.stat',return_value=Mock(st_dev=8,st_rdev=8)):
                    fid=json.loads(service.execute('capture_control',p))['file_id']
                    self.assertEqual(json.loads(service.execute('capture_control',p))['file_id'],fid)
                    with self.assertRaises(CommandError):service.execute('capture_control',dict(p,mode=3))
                    service.capture_record(b'star frame',100)
                    service.execute('capture_control',dict(p,enabled=False))
                    self.assertEqual(service.ssd_store.execute('select body from records').fetchone(),(b'star frame',))
                    self.assertEqual(service.db.execute('select count(*) from frames').fetchone(),(0,))
                    with tempfile.TemporaryDirectory() as fresh_ram:
                        restored = FileServices(Path(fresh_ram)/"cache",ssd_root=folder,ssd_uuid='fixture')
                        try:
                            restored._ssd()
                            self.assertEqual(restored.db.execute('select id,complete from files').fetchall(),[(fid,1)])
                        finally:
                            restored.close()
                    service.execute('capture_control',p)
                    with patch('gs422.file_services.os.statvfs', return_value=Mock(f_bavail=1,f_frsize=4096)):
                        with self.assertRaises(CommandError):
                            service.capture_record(b'full',101)
                    driver.stop.side_effect=OSError('link lost')
                    with self.assertRaises(OSError):service.execute('capture_control',dict(p,enabled=False))
                    self.assertIsNone(service.capture)
                    self.assertEqual(service.ssd_store.execute('select count(*) from clips where complete=0').fetchone(),(1,))
            finally:service.close()


class ScheduleTests(unittest.TestCase):
    def test_capacity_priority_fifo_exclusive_and_npu_refusal(self):
        sw=Mock(owned=[], task=None, active=1, slots={1:{'image_id':'img'}},catalog={'digest':{'image_id':'img'}})
        sw._create.side_effect=['cid1','cid2','cid4','cid3']
        sw._state.return_value={'Running':True}
        q=Scheduler(sw,{str(i):{'cpu_image':'active'} for i in range(1,6)})
        def start(i,prio,mode,npu=False):
            return q.execute('task_start',dict(task_id=i,priority=prio,mode=mode,npu=npu))
        start(1,0,2);start(2,0,2);start(3,1,2);start(4,9,1)
        with self.assertRaises(CommandError):start(5,1,2,True)
        self.assertEqual(list(q.running),[1,2]);self.assertEqual([j.task_id for j in q.pending],[4,3])
        sw.owned=['cid1','cid2','cid4','cid3']
        q.execute('task_stop',{'task_id':1});self.assertEqual(list(q.running),[2])
        q.execute('task_stop',{'task_id':2});self.assertEqual(list(q.running),[4])
        q.execute('task_stop',{'task_id':4});self.assertEqual(list(q.running),[3])


    def test_queue_capacity_and_equal_priority(self):
        sw = Mock(owned=[],task=None,active=1,slots={1:{'image_id':'img'}},catalog={'sha':{'image_id':'img'}})
        sw._state.return_value = {'Running':True}
        sw._create.return_value = 'exclusive'
        q = Scheduler(sw,{str(i):{'cpu_image':'active'} for i in range(12)})
        def start(i):
            q.execute('task_start',dict(task_id=i,priority=9,mode=1,npu=False))
        for i in range(9):
            start(i)
        with self.assertRaises(CommandError):
            start(9)
        self.assertEqual([j.task_id for j in q.pending],list(range(1,9)))
        q.execute('task_stop',{'task_id':4})
        start(9)
        self.assertEqual([j.task_id for j in q.pending],[1,2,3,5,6,7,8,9])
