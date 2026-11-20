"""Opt-in root test on ARM/Linux: build local demo images, OTA deploy, algorithm.

No pulls, host mounts, production containers or production UART configuration.
All image archives and test containers created here are removed in finally.
Hardware-only sensors in this test are explicit simulated fixtures.
"""
import hashlib
import io
import json
import os
from pathlib import Path
import pty
import select
import subprocess
import sys
import tarfile
import tempfile
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from gs422 import tmg
from gs422.core import Cache
from gs422.demo_docker import DemoDocker
from gs422.dual_uart import DualUART
from gs422.ota_staging import Staging
from gs422.ota_wire import control, file_packets
from gs422.revised_runtime import Revised
from gs422.space_protocol import Decoder, Message

CODE = r'''
#include <stdio.h>
#include <string.h>
#include <signal.h>
#include <unistd.h>
volatile sig_atomic_t running=1;
void stop(int sig){running=0;}
int main(int argc,char **argv){
 setbuf(stdout,NULL);signal(SIGTERM,stop);signal(SIGINT,stop);
 if(BAD)return 17;
 if(argc>1 && !strcmp(argv[1],"algorithm")){
  for(int tick=0;running && tick<300;tick++){
   long result=0;for(int i=1;i<=100;i++)result+=i;
   printf("SUM=%ld TICK=%d\n",result,tick);usleep(100000);
  }
  return 0;
 }
 printf("READY:%s\n",RELEASE);
 while(running)usleep(100000);
 return 0;
}
'''


def main():
    if os.geteuid():
        raise SystemExit('Run explicitly with sudo')
    records, images = [], []
    backend = runtime = transport = staging = None
    descriptors = []
    token = uuid.uuid4().hex[:10]
    def docker(*args):
        return subprocess.check_output(['docker', *args], text=True, stderr=subprocess.STDOUT, timeout=45).strip()
    def passed(name, **detail):
        entry = dict(test=name, status='PASS', **detail)
        records.append(entry)
        print(json.dumps(entry), flush=True)
    try:
        with tempfile.TemporaryDirectory(prefix='sathex-demo-') as folder:
            base = Path(folder)
            source = base/'sample.c'; source.write_text(CODE)
            catalog, packages = {}, {}
            for version, bad in (('v1', 0), ('v2', 0), ('bad', 1)):
                executable = base/('sample-' + version)
                subprocess.run(['gcc', '-static', '-Os', '-s', f'-DRELEASE="{version}"', f'-DBAD={bad}',
                                str(source), '-o', str(executable)], check=True, timeout=60)
                layer = base/(version + '-root.tar')
                with tarfile.open(layer, 'w') as tar:
                    info = tarfile.TarInfo('sample'); info.mode=0o755; data=executable.read_bytes(); info.size=len(data)
                    tar.addfile(info, io.BytesIO(data))
                tag = 'sathex-demo-' + token + ':' + version
                image = docker('import', str(layer), tag)
                images.append(tag)
                archive = base/(version + '.tar')
                docker('save', '-o', str(archive), tag)
                data = archive.read_bytes()
                catalog[hashlib.sha256(data).hexdigest()] = dict(image_id=image, version=version)
                packages[version] = data
            passed('local_software_images_built', architecture=os.uname().machine, versions=list(packages))
            staging = Staging(base/'staging', crc_profile='CCITT-FALSE', capacity=8*1024*1024)
            backend = DemoDocker(base/'backend', catalog)
            raw = {f.id:0 for f in tmg.FIELDS}
            runtime = Revised(staging, Cache(5000), raw, version='UNCONFIGURED', adapter=backend,
                              software_backend=backend, algorithms_enabled=True)
            cm, cs = pty.openpty(); um, us = pty.openpty()
            descriptors = [cm, cs, um, us]
            runtime.start()
            transport = DualUART(runtime, os.ttyname(cs), os.ttyname(us))
            decoder = Decoder()
            def pump_until(predicate, timeout=35):
                deadline=time.monotonic()+timeout
                while time.monotonic()<deadline:
                    transport.pump(.001)
                    if predicate():return
                raise AssertionError('timeout')
            def exchange(frame):
                os.write(cm, frame)
                answers=[]
                def ready():
                    if select.select([cm],[],[],0)[0]:answers.extend(decoder.feed(os.read(cm,4096)))
                    return bool(answers)
                pump_until(ready)
                assert len(answers)==1,answers
                return answers[0]
            def ota(code, body):
                return exchange(control(0x30,code,body))
            def telemetry():
                raw.update(backend.telemetry());runtime.update_telemetry(raw)
                return tmg.unpack(Message.parse(exchange(Message(0x11,500,0).wire())).body)
            seq=0
            def command(code, body=b'', kind=0x21):
                nonlocal seq
                seq+=1
                os.write(cm,Message(kind,seq,code,body).wire())
                pump_until(lambda: runtime.legacy.executed+runtime.legacy.failures>=seq)
                assert not select.select([cm],[],[],.02)[0], 'unsolicited command ACK'
                answer=Message.parse(exchange(Message(0x15,501,0).wire())).body
                assert answer[0]==1
                assert int.from_bytes(answer[1:3],'big')==seq
                return answer[4],int.from_bytes(answer[5:7],'big')
            assert ota(0x17,b'\x01'+bytes(5))==b'\x31\x17\x00'
            def upload(version, region):
                assert ota(0x53,bytes([region+2,region,1,0,0,0]))==b'\x31\x53\x00'
                total=0
                # Bounded paced PTY upload, real filesystem persistence.
                for frame in file_packets(packages[version],'CCITT-FALSE'):
                    os.write(um,frame);total+=1
                    pump_until(lambda: runtime.raw['TMG010']>=total)
                    assert not select.select([um],[],[],0)[0], 'upload channel transmitted'
                assert ota(0x54,bytes(6))==b'\x31\x54\x00'
                assert telemetry()['TMG012']==100
                passed('ota_upload_'+version, packets=total, bytes=len(packages[version]))
            upload('v1',1)
            assert ota(4,bytes(6))==b'\x31\x04\x00'
            assert ota(0x56,bytes(6))==b'\x33\x56v1\x00'
            assert telemetry()['TMG014']==0x31
            passed('software_v1_activated', container=backend.software)
            assert command(0x83,bytes([1,0,1,2,0,0]))==(3,0)
            pump_until(lambda:'SUM=5050' in docker('logs','--tail','3',backend.task))
            assert telemetry()['TMG032']==1
            passed('algorithm_start_actual_sum', result=5050)
            assert command(0x85,b'\x01'+bytes(5))==(3,0)
            assert json.loads(docker('inspect',backend.task))[0]['State']['Paused']
            passed('algorithm_pause')
            assert command(0x87)==(3,0)
            assert not json.loads(docker('inspect',backend.task))[0]['State']['Paused']
            passed('software_resume_unpauses_demo_task')
            assert command(0x81,b'\x01'+bytes(5))==(3,0)
            assert not json.loads(docker('inspect',backend.task))[0]['State']['Running']
            passed('algorithm_stop')
            assert command(0x80)==(3,0)
            assert not json.loads(docker('inspect',backend.software))[0]['State']['Running']
            assert command(0x87)==(3,0)
            passed('software_stop_resume')
            assert command(0x83,bytes([1,0,1,2,0,0]))==(3,0)
            pump_until(lambda: not json.loads(docker('inspect',backend.task))[0]['State']['Running'],timeout=40)
            values=telemetry();assert values['TMG032']==3 and values['TMG017']==1
            passed('algorithm_natural_completion_telemetry')
            upload('v2',2)
            assert ota(4,bytes(6))==b'\x31\x04\x00'
            assert ota(0x56,bytes(6))==b'\x33\x56v2\x00'
            passed('software_v2_upgrade')
            assert ota(0x53,bytes([4,2,1,0,0,0]))==b'\x31\x53\x02'
            assert ota(0x55,bytes([2,0,0,0,0,0]))==b'\x31\x55\xff'
            passed('active_slot_overwrite_and_erase_rejected')
            upload('bad',1)
            assert ota(4,bytes(6))==b'\x31\x04\xff'
            assert ota(0x56,bytes(6))==b'\x33\x56v2\x00'
            assert json.loads(docker('inspect',backend.software))[0]['State']['Running']
            assert telemetry()['TMG014']==0x40
            passed('failed_upgrade_rolls_back_v2')
            assert command(0x83,bytes([2,0,1,2,0,0]))==(5,4)
            passed('unmapped_algorithm_rejected')
            # Cover every workbook row without substituting mocks for missing hardware.
            from test_spec_20261119 import CASES
            # Shared K1116/17 execution is tested in live_file_commands.py.
            assert command(0x55,bytes([2,0,0,0,0,0]))==(5,8)
            passed('K1118_active_slot_rejected_via_legacy_route')
            covered = {'K1101','K1102','K1103','K1104','K1105','K1106','K1107','K1108',
                       'K1116','K1117','K1118','K1119'}
            for key in ('K1101','K1102','K1103','K1119'):
                kind,code,body,_=CASES[key]
                assert command(code,body,kind)==(3,0)
                passed('workbook_'+key)
            for key,(kind,code,body,_) in CASES.items():
                if key in covered:continue
                status,error=command(code,body,kind)
                assert (status,error)==(5,7),(key,status,error)
                print(json.dumps({'test':'workbook_'+key,'status':'NOT_CONFIGURED',
                                  'verified':'explicit failure, no business effect','error':error}),flush=True)
            passed('scope', physical_uart=False, sensors='SIMULATED', software_backend='LAB_ONLY')
    finally:
        if runtime:runtime.close()
        if transport:transport.close()
        if backend:backend.cleanup()
        if staging:staging.close()
        for fd in descriptors:os.close(fd)
        for image in images:docker('image','rm',image)
        print(json.dumps(dict(test='cleanup',containers='removed',images='removed')),flush=True)


if __name__=='__main__':main()
