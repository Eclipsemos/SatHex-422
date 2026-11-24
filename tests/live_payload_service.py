"""Real subprocess lifecycle test for configured daemon, dual PTY and snapshot TTL.

No production service is stopped. Measurements explicitly marked fixture.
"""
import json
import os
from pathlib import Path
import pty
import select
import signal
import subprocess
import sys
import tempfile
import threading
import time

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from gs422.ota_wire import control,file_packets
from gs422.space_protocol import Message,Decoder
from gs422 import tmg


def main():
    with tempfile.TemporaryDirectory(prefix='sathex-daemon-') as folder:
        root=Path(folder);cm,cs=pty.openpty();um,us=pty.openpty()
        snapshot=root/'measurements.json';producer_stop=threading.Event();paused=threading.Event()
        def produce():
            while not producer_stop.is_set():
                if not paused.is_set():
                    d={'source':'fixture','monotonic':time.monotonic(),'values':{'TMG004':int(time.time()),'TMG005':1200,'TMG006':40,'TMG007':41,'TMG034':1200}}
                    temp=root/'measurements.tmp';temp.write_text(json.dumps(d));os.replace(temp,snapshot)
                producer_stop.wait(.1)
        thread=threading.Thread(target=produce);thread.start()
        deadline=time.monotonic()+2
        while not snapshot.exists() and time.monotonic()<deadline:time.sleep(.01)
        c={'mode':'lab','control_port':os.ttyname(cs),'upload_port':os.ttyname(us),'state_root':str(root/'state'),
           'records_root':str(root/'records'),'telemetry_file':str(snapshot),'telemetry_max_age':.6,
           'allow_fixture_telemetry':True,'catalog':{},'ota_capacity':10000,'enable_reboot':False}
        cfg=root/'config.json';cfg.write_text(json.dumps(c));process=log=None
        service_command=['/usr/bin/gs422-payload-lab'] if '--installed' in sys.argv else [sys.executable,'-m','gs422.payload_service']
        def start():
            nonlocal process,log
            log=(root/'service.log').open('a')
            process=subprocess.Popen(service_command+['--config',str(cfg)],cwd=ROOT,stdout=log,stderr=log)
            time.sleep(.5)
            assert process.poll() is None,(root/'service.log').read_text()
        def stop():
            nonlocal process,log
            if process and process.poll() is None:
                process.send_signal(signal.SIGTERM)
                try:process.wait(timeout=40)
                except subprocess.TimeoutExpired:process.kill();process.wait();raise
            if log:log.close();log=None
        def request(frame,expect=True):
            os.write(cm,frame);d=Decoder(ota101=True);deadline=time.monotonic()+(3 if expect else .3)
            while time.monotonic()<deadline:
                if select.select([cm],[],[],.05)[0]:
                    frames=d.feed(os.read(cm,4096))
                    if frames:
                        assert expect,'unexpected telemetry'
                        return frames[0]
            assert not expect,'no response'
        def tm():return tmg.unpack(Message.parse(request(Message(0x11,1,0).wire())).body)
        try:
            check=subprocess.run(service_command+['--config',str(cfg),'--check-config'],cwd=ROOT,capture_output=True)
            assert check.returncode==0,check.stderr
            start();assert tm()['TMG006']==40
            assert request(control(0x30,0x17,b'\x01'+bytes(5),ota101=True))==b'\x31\x17\x00'+bytes(5)
            assert request(control(0x30,0x53,bytes([3,1,2,0,0,0]),ota101=True))==b'\x31\x53\x00'+bytes(5)
            packets=list(file_packets(b'x'*700,'XMODEM',ota101=True))
            os.write(um,packets[0]);time.sleep(.1);assert tm()['TMG010']==1
            stop();assert process.returncode==0
            start();assert tm()['TMG010']==1
            os.write(um,packets[1]);time.sleep(.1)
            assert request(control(0x30,0x54,bytes(6),ota101=True))==b'\x31\x54\x00'+bytes(5)
            assert tm()['TMG014']==0x23
            assert not select.select([um],[],[],.05)[0]
            print('PASS configured daemon stop/start and persisted upload resume',flush=True)
            paused.set();time.sleep(.9)
            assert tm()['TMG006']==0xff
            assert request(control(0x30,0x56,bytes(6),ota101=True))==b'\x33\x56UNCONFIGURED\x00'
            print('PASS expired measurements report invalid markers while controls work',flush=True)
            paused.clear();time.sleep(.4);assert tm()['TMG006']==40
            print('PASS fresh measurements restore telemetry; no fake missing zeros',flush=True)
        finally:
            stop();producer_stop.set();thread.join()
            for fd in (cm,cs,um,us):os.close(fd)


if __name__=='__main__':main()
