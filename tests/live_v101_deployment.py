"""X26 opt-in isolated acceptance. Uses real Docker, staged files and tmpfs."""
import binascii
import gzip
import hashlib
import os
from pathlib import Path
import struct
import subprocess
import sys
import tarfile
import tempfile
import time
import uuid
from contextlib import ExitStack

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from gs422 import tmg
from gs422.core import Cache
from gs422.durable_demo import DurableDemo
from gs422.file_services import FileServices
from gs422.fixture_io import FixtureIO
from gs422.legacy_upload import LegacyUpload
from gs422.ota_staging import Staging
from gs422.ota_wire import control, file_packets, reply
from gs422.payload_backend import PayloadBackend
from gs422.revised_runtime import Revised
from gs422.space_protocol import Decoder, Message, fragments
from gs422.task_scheduler import Scheduler


def docker(*args):
    return subprocess.check_output(['docker', *args], text=True, timeout=30).strip()


def archive(root, stack, version):
    """Small AArch64 userspace fixture so the complete archive fits D0351."""
    tag = 'sathex-qa-' + uuid.uuid4().hex[:12]
    message = 'READY:' + version if version != 'BAD' else 'unhealthy'
    source = root/'sample.S'
    source.write_text(f'''
.global _start
.text
_start:
 mov x0, #1
 adr x1, msg
 mov x2, #{len(message)+1}
 mov x8, #64
 svc #0
wait:
 adr x0, delay
 mov x1, #0
 mov x8, #101
 svc #0
 b wait
.balign 8
delay: .quad 60, 0
msg: .ascii "{message}\\n"
''')
    exe = root/'sample'
    subprocess.run(['gcc','-nostdlib','-static','-s',str(source),'-o',str(exe)],check=True)
    layer = root/'root.tar'
    with tarfile.open(layer, 'w') as tar:
        tar.add(exe, arcname='sample')
    image = docker('import', str(layer), tag)
    stack.callback(docker, 'rmi', tag)
    raw = subprocess.check_output(['docker','save',tag],timeout=30)
    data = gzip.compress(raw, mtime=0)
    assert len(data) <= 131072
    return data, {'image_id':image, 'version':version}


def main():
    with ExitStack() as stack:
        root = Path(stack.enter_context(tempfile.TemporaryDirectory()))
        memory = stack.enter_context(tempfile.TemporaryDirectory(dir='/dev/shm'))
        packages, catalog = {}, {}
        for version in ('V1.2.3','V2.0.0','BAD'):
            data, entry = archive(root,stack,version)
            packages[version] = data
            catalog[hashlib.sha256(data).hexdigest()] = entry
        software = DurableDemo(root/'software',catalog)
        stack.callback(software.close)
        stack.callback(software.cleanup)
        scheduler = Scheduler(software,{str(i):{'cpu_image':'active'} for i in (1,2)})
        fixture = FixtureIO(root/'fixture')
        files = FileServices(memory,capture_driver=fixture)
        stack.callback(files.close)
        backend = PayloadBackend(software,files,scheduler=scheduler)
        stage = Staging(root/'stage',crc_profile='XMODEM',capacity=131072)
        stack.callback(stage.close)
        legacy = LegacyUpload(root/'legacy')
        stack.callback(legacy.close)
        r = Revised(stage,Cache(10),dict.fromkeys(tmg.BY_ID,0),version='UNCONFIGURED',
                    adapter=backend,software_backend=backend,algorithms_enabled=True,legacy_upload=legacy,ota101=True)
        stack.callback(r.close)
        seq = 0
        def command(code, data, kind=0x21):
            nonlocal seq
            seq += 1
            msg = Message(kind,seq,code,data)
            parts = fragments(msg) if len(msg.payload())>504 else [msg]
            for part in parts:
                assert r.feed_control(part.wire()) == b''
            r.legacy.execute_next()
            assert r.legacy.latest == (seq,code,3,0),r.legacy.latest

        def ota(code, params=bytes(6), result=0):
            assert not r.feed_control(control(0x30,code,params,ota101=True))
            assert r.execute_next()
            assert r.drain_replies() == reply(code,result,ota101=True)

        def version():
            frame = r.feed_control(bytes.fromhex('eb9000083056000000000000008e09d7'))
            return Decoder(ota101=True).feed(frame)[0][2:-1].decode()

        def state(expected):
            assert Decoder(ota101=True).feed(r.feed_control(bytes.fromhex('eb9000023214004809d7')))[0] == bytes((0x33,expected))

        def old(data):
            command(0x17,bytes(6))
            command(0x17,bytes.fromhex('020000000000'))
            n = (len(data)+511)//512
            for idx in reversed(range(n)):
                block = data[512*idx:512*(idx+1)]
                body = struct.pack('>IIIIIB',len(block),0,len(data),n,(n+7)//8,idx)
                body += block + struct.pack('>HH',sum(block)&65535,binascii.crc_hqx(data,0xffff))
                command(4,body,0x25)
            state(0x23)

        assert version() == 'UNCONFIGURED'
        ota(0x17,bytes.fromhex('010000000000'))
        ota(0x55,bytes.fromhex('010100000000'))
        state(0x11)
        ota(0x53,bytes.fromhex('030101000000'))
        for frame in file_packets(packages['V1.2.3'],'XMODEM',ota101=True):
            assert not r.feed_upload(frame)
            assert r.execute_next()
        state(0x20)
        ota(0x54)
        state(0x23)
        ota(4)
        state(0x31)
        assert version() == 'V1.2.3'
        cid = software.software
        ota(4)
        assert software.software == cid
        print('PASS V1.0.1 application OTA, real Docker health check and Z56',flush=True)

        for ident in (1,2):
            command(0x83,bytes((ident,0,2,2,0,0)))
        assert len(scheduler.running) == 2
        assert all(software._state(j.cid)['Running'] for j in scheduler.running.values())
        for ident in (1,2):
            command(0x81,bytes((ident,0,0,0,0,0)))
        print('PASS K1104 two concurrent CPU containers, individual stop',flush=True)

        for mode in (1,2,3):
            command(0x32,bytes((1,0,mode,0,0,0)))
            time.sleep(.25)
            fid = files.capture
            command(0x32,bytes((2,0,mode,0,0,0)))
            rows = files.db.execute('select body from frames where file=?',(fid,)).fetchall()
            assert rows and all(f'mode={mode}'.encode() in b for b, in rows)
        print('PASS K1115 three modes, tmpfs and simulated star source',flush=True)

        old(packages['V2.0.0'])
        assert version() == 'V1.2.3'
        ota(4)
        state(0x31)
        assert version() == 'V2.0.0'
        ota(4)
        old(b'unregistered')
        ota(4,result=0xff)
        state(0x40)
        assert version() == 'V2.0.0'
        old(packages['BAD'])
        ota(4,result=0xff)
        state(0x40)
        assert version() == 'V2.0.0' and software._state(software.software)['Running']
        print('PASS D0351 reverse order, trusted package activation, repeat, untrusted rejection and unhealthy rollback',flush=True)


if __name__ == '__main__':
    main()
