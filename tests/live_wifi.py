"""Explicit opt-in Wi-Fi interruption test. Run via wired SSH as root.

Uses the active Wi-Fi profile's secret only in memory, never prints or stores it.
Restores the original profile and deletes only profiles created by this test.
"""
import argparse
import os
from pathlib import Path
import pty
import select
import subprocess
import sys
import tempfile
import threading
import time
import uuid

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from gs422.core import escape
from gs422.network import NMClient, NM, ROOT as NM_ROOT, DEVICE, CONNECTION, ACTIVE


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--allow-wifi-interruption',action='store_true',required=True)
    parser.parse_args()
    client=NMClient(threading.Event(),300)
    device=client.device('wlan0')
    ssid,_,_=client.snapshot(device)
    old_active=client.get(device,DEVICE,'ActiveConnection')
    old_connection=client.get(old_active,ACTIVE,'Connection')
    secrets=client.call(old_connection,CONNECTION,'GetSecrets','(s)',('802-11-wireless-security',))[0]
    password=secrets.get('802-11-wireless-security',{}).get('psk','')
    before=set(client.call(NM_ROOT+'/Settings',NM+'.Settings','ListConnections')[0])
    with tempfile.TemporaryDirectory(prefix='gs422-wifi-check-') as tmp:
        base=Path(tmp); master,slave=pty.openpty()
        text=Path('/etc/shanhe-serial.conf').read_text().replace('port=/dev/ttyS6','port='+os.ttyname(slave)).replace('wifi_timeout=45','wifi_timeout=25')
        config=base/'config'; config.write_text(text)
        log_path=base/'log'
        with log_path.open('w+') as log:
            proc=subprocess.Popen([sys.executable,'-m','gs422.service','--config',str(config)],cwd=ROOT,stdout=log,stderr=log)
            pending=bytearray()
            def receive(timeout):
                deadline=time.monotonic()+timeout
                while b'\n' not in pending and time.monotonic()<deadline:
                    if select.select([master],[],[],.1)[0]:pending.extend(os.read(master,8192))
                if b'\n' not in pending:raise TimeoutError('no serial reply')
                line,_,rest=pending.partition(b'\n');pending[:]=rest
                return line.rstrip(b'\r').decode()
            try:
                time.sleep(1.5)
                command=('AT+WIFI='+escape(ssid)+','+escape(password)+'\r\n').encode()
                os.write(master,command); command=None
                # Prove the setting operation does not block telemetry or wired queries.
                os.write(master,b'AT+STA\nAT+IP2\n')
                start=time.monotonic()
                responses=[receive(2),receive(2)]
                assert any(r.startswith('+STA:') for r in responses),responses
                assert any(r.startswith('+IP2:192.168.5.135,') for r in responses),responses
                print('Concurrent STA/IP2 latency:',round(time.monotonic()-start,3),flush=True)
                assert receive(35)=='+WIFI:OK','Wi-Fi connection failed'
                assert client.snapshot(device)[0]==ssid
                current_active=client.get(device,DEVICE,'ActiveConnection')
                current_connection=client.get(current_active,ACTIVE,'Connection')
                assert not client.get(current_connection,CONNECTION,'Unsaved')
                print('Wi-Fi off/on, connection, SSID/IPv4 verification and persistent Save: PASS',flush=True)
                # Deliberately absent SSID; second setting must fail immediately.
                absent='gs422-test-'+uuid.uuid4().hex[:12]
                os.write(master,('AT+WIFI='+absent+',\nAT+WIFI='+absent+',\n').encode())
                assert receive(2)=='+WIFI:ERR'
                os.write(master,b'AT+STA\nAT+IP2\n')
                replies=[receive(2),receive(2)]
                assert any(r.startswith('+STA:') for r in replies)
                assert any(r.startswith('+IP2:192.168.5.135,') for r in replies)
                assert receive(40)=='+WIFI:ERR'
                print('Busy rejection, timeout and ERR reply: PASS',flush=True)
                deadline=time.monotonic()+20
                while time.monotonic()<deadline:
                    try:
                        if client.snapshot(device)[0]==ssid:break
                    except Exception:pass
                    time.sleep(.5)
                else:raise RuntimeError('Wi-Fi rollback did not restore original SSID')
                print('Failed setting rollback restored Wi-Fi: PASS',flush=True)
                log.flush()
                if password:assert password not in log_path.read_text()
                print('No password in service log: PASS',flush=True)
            finally:
                proc.terminate()
                try:proc.wait(timeout=15)
                except subprocess.TimeoutExpired:proc.kill();proc.wait()
                os.close(master);os.close(slave)
                client.deadline=time.monotonic()+30
                client.radio(True)
                client.call(NM_ROOT,NM,'ActivateConnection','(ooo)',(old_connection,device,'/'))
                for path in set(client.call(NM_ROOT+'/Settings',NM+'.Settings','ListConnections')[0])-before:
                    settings=client.call(path,CONNECTION,'GetSettings')[0]
                    if settings.get('connection',{}).get('id','').startswith('gs422-'):
                        client.call(path,CONNECTION,'Delete')
                deadline=time.monotonic()+20
                while time.monotonic()<deadline:
                    try:
                        if client.snapshot(device)[0]==ssid:
                            print('Original profile restored; test profiles removed',flush=True)
                            break
                    except Exception:pass
                    time.sleep(.5)
                else:raise RuntimeError('Original profile restoration failed')


if __name__=='__main__':main()
