"""Explicit file-source/sink fixtures exercising K/D routes on Linux dual PTYs.

Real SQLite/tmpfs IO and protocol execution; external star sensor and solid-state
device are TEST FIXTURES, not real hardware. No production UART is opened.
"""
import json
import os
from pathlib import Path
import pty
import select
import struct
import sys
import tempfile
import threading
import time
import zlib

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from gs422 import tmg
from gs422.core import Cache
from gs422.file_services import FileServices
from gs422.ota_staging import Staging
from gs422.revised_runtime import Revised
from gs422.dual_uart import DualUART
from gs422.space_protocol import Message,Decoder
from gs422.ota_wire import file_packets
from gs422.legacy_upload import LegacyUpload
from gs422.space_protocol import fragments
import binascii


class CaptureFixture:
    def start(self,mode,receiver):self.mode=mode;self.receiver=receiver
    def stop(self):self.receiver=None


def main():
    results=[]
    def passed(name,**extra):
        row=dict(test=name,status='PASS',**extra);results.append(row);print(json.dumps(row),flush=True)
    with tempfile.TemporaryDirectory(prefix='sathex-files-',dir='/dev/shm') as folder:
        root=Path(folder);one=root/'downlink-1.bin';two=root/'downlink-2.bin'
        def sink(path):
            def send(data,cancel):
                if not cancel.is_set():
                    with path.open('ab') as stream:stream.write(data);stream.flush();os.fsync(stream.fileno())
            return send
        device_frames=[b'frame-0',b'frame-1',b'frame-2']
        def source(start,count,cancel):
            for frame in device_frames[start:None if count==0 else start+count]:
                if cancel.is_set():return
                yield frame,zlib.crc32(frame)
        capture=CaptureFixture()
        files=FileServices(root/'files',sources={7:source},sinks={1:sink(one),2:sink(two)},capture_driver=capture)
        staging=Staging(root/'ota',crc_profile='CCITT-FALSE',capacity=10000)
        old_upload=LegacyUpload(root/'legacy')
        values={f.id:0 for f in tmg.FIELDS}
        runtime=Revised(staging,Cache(5000),values,version='file-fixture',adapter=files,legacy_upload=old_upload)
        cm,cs=pty.openpty();um,us=pty.openpty()
        transport=DualUART(runtime,os.ttyname(cs),os.ttyname(us));runtime.start()
        decoder=Decoder();seq=0
        def until(predicate):
            deadline=time.monotonic()+5
            while time.monotonic()<deadline:
                transport.pump()
                if predicate():return
            raise AssertionError('timeout')
        def exchange(frame):
            os.write(cm,frame);answers=[]
            def ready():
                if select.select([cm],[],[],0)[0]:answers.extend(decoder.feed(os.read(cm,4096)))
                return bool(answers)
            until(ready);assert len(answers)==1
            return Message.parse(answers[0])
        def command(kind,code,body=b''):
            nonlocal seq
            seq+=1
            os.write(cm,Message(kind,seq,code,body).wire())
            until(lambda:runtime.legacy.executed+runtime.legacy.failures>=seq)
            assert not select.select([cm],[],[],0)[0], 'unexpected unsolicited reply'
            result=exchange(Message(0x15,600,0).wire()).body
            assert result[4:7]==b'\x03\x00\x00',(code,result)
            return json.loads(result[9:] or b'{}')
        def tm():
            values.update(files.telemetry());runtime.update_telemetry(values)
            return tmg.unpack(exchange(Message(0x11,601,0).wire()).body)
        try:
            fid=files.add_file([(10,b'alpha'),(10,b'beta')])
            command(0x25,0x40,struct.pack('>BBIIIH',3,2,0,100,9,0));passed('D0352_configure_dual_link')
            command(0x25,0x41,struct.pack('>IIIBH',fid,0,1,1,0));passed('D0353_configure_file')
            command(0x21,0x42);until(lambda:files.play_state==2)
            assert one.read_bytes()==two.read_bytes()==b'alphabeta'
            assert tm()['TMG035']==fid and tm()['TMG036']==2
            passed('K1111_playback_exact_bytes_and_telemetry')
            command(0x21,0x43);assert tm()['TMG036']==3;passed('K1112_stop')
            command(0x25,0x50,bytes([7,1,0,0,0,0]));until(lambda:files.store_state==2)
            assert tm()['TMG037']==2 and tm()['TMG038']==14
            passed('D0354_crc_source_and_counters')
            state=command(0x21,0x52);assert state['frames']==2 and state['bytes']==14;passed('K1114_status_result')
            command(0x21,0x51);assert files.store_state==3;passed('K1113_stop')
            state=command(0x21,0x32,bytes([1,0,3,0,0,0]))
            capture.receiver(b'star-record',20)
            command(0x21,0x32,bytes([2,0,3,0,0,0]))
            assert files.db.execute('SELECT body FROM frames WHERE file=?',(state['file_id'],)).fetchone()[0]==b'star-record'
            passed('K1115_tmpfs_capture_start_stop',source='TEST_FIXTURE')
            foreign=root/'keep.txt';foreign.write_text('preserve')
            clean=command(0x21,0x13);assert clean['deleted_files']==3 and foreign.read_text()=='preserve'
            passed('K1110_managed_cleanup',deleted=3)
            command(0x21,0x17,b'\x02'+bytes(5))
            command(0x21,0x53,bytes([3,1,2,0,0,0]));passed('K1116_shared_ota_begin')
            frame=list(file_packets(b'uploaded','CCITT-FALSE'))[0];os.write(um,frame)
            until(lambda:runtime.raw['TMG010']==1)
            assert not select.select([um],[],[],0)[0]
            command(0x21,0x54,bytes(6));assert tm()['TMG014']==0x23;passed('K1117_shared_ota_end')
            command(0x21,0x17,bytes(6))
            command(0x21,0x17,b'\x02'+bytes(5))
            data=bytes(range(256))*5
            total=(len(data)+511)//512;crc=binascii.crc_hqx(data,0xffff)
            for idx in (2,0,1):
                block=data[idx*512:(idx+1)*512]
                body=struct.pack('>IIIIIB',len(block),0,len(data),total,(total+7)//8,idx)+block+struct.pack('>HH',sum(block)&65535,crc)
                seq+=1
                for fragment in fragments(Message(0x25,seq,4,body)):
                    os.write(cm,fragment.wire());transport.pump()
                until(lambda:runtime.legacy.executed+runtime.legacy.failures>=seq)
                result=exchange(Message(0x15,600,0).wire()).body
                assert result[4:7]==b'\x03\x00\x00',result
            old_upload.export_verified(root/'legacy-result.bin')
            assert (root/'legacy-result.bin').read_bytes()==data
            assert tm()['TMG010']==3 and tm()['TMG014']==0x23
            passed('D0351_fragmented_persistent_legacy_upload',bytes=len(data),packets=3)
            passed('scope',external_hardware='TEST_FIXTURES',physical_uart=False)
        finally:
            runtime.close();transport.close();files.close();staging.close();old_upload.close()
            for fd in (cm,cs,um,us):os.close(fd)


if __name__=='__main__':main()
