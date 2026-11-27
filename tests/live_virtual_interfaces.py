"""Opt-in X26 bench using two PTYs, isolated state and the installed lab sample image."""
from contextlib import ExitStack
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

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from gs422 import tmg
from gs422.core import Cache
from gs422.dual_uart import DualUART
from gs422.durable_demo import DurableDemo
from gs422.file_services import FileServices
from gs422.fixture_io import FixtureIO
from gs422.measurements import Measurements
from gs422.ota_staging import Staging
from gs422.ota_wire import file_packets, control, reply
from gs422.payload_backend import PayloadBackend
from gs422.revised_runtime import Revised
from gs422.space_protocol import Decoder, Message, encode
from gs422.task_scheduler import Scheduler


def record(items, **evidence):
    print(json.dumps(dict(items=items, **evidence)), flush=True)


class Link:
    def __init__(self, master, transport, runtime, backend, sensors):
        self.master, self.transport = master, transport
        self.runtime, self.backend, self.sensors = runtime, backend, sensors
        self.seq = 0

    def read(self, kind):
        os.write(self.master, Message(kind, 0, 0).wire())
        decoder = Decoder()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            self.transport.pump()
            if select.select([self.master], [], [], 0)[0]:
                for p in decoder.feed(os.read(self.master, 4096)):
                    m = Message.parse(p)
                    assert m.kind == kind+1 and m.sequence == 0
                    return m.body
        raise TimeoutError('no PTY response')

    def tm(self):
        values = dict.fromkeys(tmg.BY_ID, 0)
        values.update(self.sensors.sample()['values'])
        values.update(self.backend.telemetry())
        self.runtime.update_telemetry(values)
        return tmg.unpack(self.read(0x11))

    def result(self, code, success=True):
        deadline = time.monotonic()+5
        while time.monotonic() < deadline:
            body = self.read(0x15)
            if body == b'\0':
                time.sleep(.005)
                continue
            assert body[0] == 1 and body[3] == code, body.hex()
            if success is not None:
                assert (body[4] == 3) == success, body.hex()
            return body, json.loads(body[9:] or b'{}')
        raise TimeoutError('no command result')

    def command(self, code, body=b'', kind=0x21, success=True):
        self.seq += 1
        os.write(self.master, Message(kind, self.seq, code, body).wire())
        result, data = self.result(code, success)
        assert int.from_bytes(result[1:3],'big') == self.seq
        return data

    def ota_gate(self, enabled):
        words = bytes((1 if enabled else 2,)) + bytes(5)
        os.write(self.master, control(0x30,0x17,words,ota101=True))
        expected = reply(0x17,0,ota101=True)
        received = bytearray()
        deadline = time.monotonic()+3
        while len(received)<len(expected) and time.monotonic()<deadline:
            self.transport.pump()
            if select.select([self.master],[],[],0)[0]:
                received.extend(os.read(self.master,4096))
        assert received == expected

    def until(self, predicate, timeout=5):
        deadline = time.monotonic()+timeout
        while time.monotonic()<deadline:
            values = self.tm()
            if predicate(values):
                return values
            time.sleep(.02)
        raise TimeoutError('telemetry predicate not reached')


def main():
    config = json.loads(Path('/etc/gs422-payload-lab.json').read_text())
    image = config['algorithms']['1']['ht226_image']
    with ExitStack() as stack:
        root = Path(stack.enter_context(tempfile.TemporaryDirectory(dir='/dev/shm')))
        fixture = FixtureIO(root/'simulator')
        files = FileServices(root/'records',capture_driver=fixture,sinks=fixture.sinks,sources=fixture.sources)
        stack.callback(files.close)
        software = DurableDemo(root/'software', {})
        stack.callback(software.close)
        stack.callback(software.cleanup)
        scheduler = Scheduler(software,{str(n):{'ht226_image':image} for n in range(6)},files)
        backend = PayloadBackend(software,files,scheduler=scheduler)
        stage = Staging(root/'ota',crc_profile='XMODEM',capacity=16384)
        stack.callback(stage.close)
        smi = Path(stack.enter_context(tempfile.TemporaryDirectory()))/'hm-smi'
        smi.write_text("#!/bin/sh\nprintf 'device0 detail infos\\nTemperature:\\n core: 52.25 C\\n'\n")
        smi.chmod(0o700)
        def sensor(key, text):
            path = root/key
            path.write_text(text)
            return str(path)
        sensors = Measurements(dict(npu_temp_path=sensor('npu','48000'),smi=str(smi),card=0,
                                    power=dict(path=sensor('power','10000000'),divisor=1000000,verified=True),
                                    voltage=dict(path=sensor('voltage','12531'),divisor=1000,verified=True)))
        rt = Revised(stage,Cache(10),dict.fromkeys(tmg.BY_ID,0),version='bench',ota101=True,
                     algorithms_enabled=True,adapter=backend,software_backend=backend)
        stack.callback(rt.close)
        cm, cs = pty.openpty()
        um, us = pty.openpty()
        for fd in (cm,cs,um,us):
            stack.callback(os.close,fd)
        transport = DualUART(rt,os.ttyname(cs),os.ttyname(us))
        stack.callback(transport.close)
        rt.start()
        link = Link(cm,transport,rt,backend,sensors)
        fields = ('TMG005','TMG006','TMG007','TMG034')
        present = [link.tm()[k] for k in fields]
        assert present == [1000,52,48,1253]
        smi.unlink()
        for name in ('power','voltage','npu'):
            (root/name).unlink()
        absent = [link.tm()[k] for k in fields]
        assert absent == [65535,255,255,65535]
        record(fields, injected_raw=present, missing_raw=absent)

        for n in range(6):
            field = f'TMG{33-n:03d}'
            assert link.tm()[field] == 0
            os.write(cm,encode(bytes([0x21,0x83,n,1,2,3,4,5])))
            link.until(lambda v:v[field]==1)
            vals=link.until(lambda v:v[field]==3)
            _, result = link.result(0x83)
            assert result['value']==55 and result['app_exit']==0 and vals['TMG017']==n
            os.write(cm,encode(bytes([0x21,0x83,n,1,2,3,4,5])))
            link.until(lambda v:v[field]==1)
            # This Docker owner and its entire registry belong to this temporary bench.
            cid=scheduler.running[n].cid
            assert software._inspect_owned(cid)['Id']==cid
            software.runner('kill',cid)
            link.until(lambda v:v[field]==2)
            _, failed=link.result(0x83,False)
            assert failed['app_exit']!=0 and 'file_id' not in failed
            record(field, app=n, states=[0,1,3,1,2], result=55, file_id=result['file_id'],
                   fault_exit=failed['app_exit'])

        payload = b'bench-file'*100
        frames = list(file_packets(payload,'XMODEM',ota101=True))
        request = bytes((3,1,2,0,0,0))
        link.command(0x17,b'\x01'+bytes(5))
        link.command(0x53,request)
        os.write(um,frames[0])
        link.until(lambda v:v['TMG010']==1)
        link.command(0x17,b'\x02'+bytes(5))
        os.write(um,frames[1])
        for _ in range(4):
            assert link.tm()['TMG010']==1
        link.command(0x17,bytes([1])+bytes(5))
        link.command(0x53,request)
        for idx in (2,2,1):
            os.write(um,frames[idx])
        link.until(lambda v:v['TMG010']==len(frames))
        link.command(0x54,bytes(6))
        ordinary=link.tm()
        assert ordinary['TMG014']==0x23 and ordinary['TMG013']==4
        output=root/'ota'/'received-files'/'APP1'/f"{ordinary['TMG008']:08X}.bin"
        assert output.read_bytes()==payload
        record(['K1101','K1102'], result='PASS', verified=len(payload), closed_count=1,
               received=ordinary['TMG010'], file=output.name)
        link.ota_gate(True)
        link.command(0x53,request)
        broken = bytearray(frames[0])
        broken[-3] ^= 1
        os.write(um,broken)
        e=link.until(lambda v:v['TMG011']==1)
        assert e['TMG010']==0
        os.write(um,frames[0])
        link.until(lambda v:v['TMG010']==1)
        link.ota_gate(False)
        os.write(um,frames[1])
        for _ in range(4):
            assert link.tm()['TMG010']==1
        link.ota_gate(True)
        for frame in frames[1:]:
            os.write(um,frame)
        link.until(lambda v:v['TMG010']==len(frames))
        link.command(0x54,bytes(6))
        final=link.tm()
        assert final['TMG014']==0x23 and final['TMG013']==4
        stage.export_verified(root/'received.bin')
        assert (root/'received.bin').read_bytes()==payload
        assert not select.select([um],[],[],0)[0]
        record(['K1102','K1103','K1116','K1117','TMG011'], closed_count=1, verified=len(payload),
               unique_packets=len(frames), frame_errors=final['TMG011'], data_port='PTY')

        gate = threading.Event()
        go = threading.Event()
        def carry_source(start,count,cancel):
            gate.set()
            if not go.wait(3):
                return
            yield b'abcd',zlib.crc32(b'abcd')
            yield b'bad',0
        files.sources[7] = carry_source
        link.command(0x50,bytes([7,0,0,0,0,0]),0x25)
        assert gate.wait(1)
        with files.lock:
            files.store_bytes=(1<<32)-2  # Set up only the boundary; the worker adds the real next frame.
        go.set()
        v=link.until(lambda v:v['TMG041']==1)
        assert [v[k] for k in ('TMG037','TMG038','TMG039')]==[1,2,1]
        st=link.command(0x52)
        assert st['frames']==1 and st['bytes']==(1<<32)+2 and st['crc_errors']==1
        record(['D0354','K1114','TMG037','TMG038','TMG039','TMG041'], seed=(1<<32)-2,
               added=4, bad_frames=1, accepted=1, high=1, low=2)
        gate.clear()
        def slow(start,count,cancel):
            yield b'abc',zlib.crc32(b'abc')
            gate.set()
            cancel.wait(3)
        files.sources[7]=slow
        link.command(0x50,bytes([7,0,0,0,0,0]),0x25)
        assert gate.wait(1)
        link.command(0x51)
        for _ in range(3):
            st=link.command(0x52)
            assert (st['state'],st['frames'],st['bytes'])==(3,1,3)
        record(['K1113','K1114'], stopped=3, frames=1, bytes=3)

        for mode in (1,2,3):
            fid=link.command(0x32,bytes([1,0,mode,0,0,0]))['file_id']
            time.sleep(.24)
            link.command(0x32,bytes([2,0,mode,0,0,0]))
            got=files.db.execute('select body from frames where file=?',(fid,)).fetchall()
            assert got and all(f'mode={mode}'.encode() in body for body, in got)
        record('K1115', modes=[1,2,3], source='fixture', target='tmpfs')

        for route in (1,2,3):
            for name in ('downlink-1.bin','downlink-2.bin'):
                (fixture.root/name).unlink(missing_ok=True)
            fid=files.add_file([(10,b'alpha'),(10,b'beta')])
            link.command(0x40,struct.pack('>BBIIIH',route,2,0,100,9,0),0x25)
            link.command(0x41,struct.pack('>IIIBH',fid,0,1,1,0),0x25)
            link.command(0x42)
            v=link.until(lambda v:v['TMG036']==2)
            assert v['TMG035']==fid
            for ch in (1,2):
                out=fixture.root/f'downlink-{ch}.bin'
                if route in (ch,3):
                    assert out.read_bytes()==b'alphabeta'
                else:
                    assert not out.exists()
        fid=files.add_file([(10,b'one'),(20,b'two')])
        link.command(0x40,struct.pack('>BBIIIH',1,2,0,100,6,0),0x25)
        link.command(0x41,struct.pack('>IIIBH',fid,0,1,1,0),0x25)
        link.command(0x42)
        out=fixture.root/'downlink-1.bin'
        link.until(lambda v:v['TMG036']==1 and out.read_bytes().endswith(b'one'))
        link.command(0x43)
        assert link.tm()['TMG036']==3 and not files.play_thread.is_alive()
        assert out.read_bytes()==b'alphabetaone'
        record(['D0352','K1111','K1112','TMG035','TMG036'], routes=[1,2,3],
               complete='alphabeta', stop_after='one', last_file_id=fid)



if __name__=='__main__':
    main()
