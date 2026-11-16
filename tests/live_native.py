"""Explicit board acceptance: real 20 MS/s IQ and broker crash during reception.

Run as root on an idle board. Uses the installed broker socket, not the UART;
restarts gs422-sdr during the crash case and restores the saved preset.
"""
import json
import re
import socket
import subprocess
import time
from pathlib import Path


def ask(*args):
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.settimeout(3)
        sock.connect('/run/gs422-sdr/control.sock')
        sock.sendall(json.dumps(list(args)).encode()+b'\n')
        reply=sock.recv(4096).decode().strip()
        if ':ERR,' in reply:
            raise AssertionError(reply)
        return reply


def workers():
    return [p.name for p in Path('/proc').glob('[0-9]*')
            if (p/'comm').exists() and (p/'comm').read_text().strip()=='sc16_capture']


def main():
    assert not workers(), 'another native capture is running'
    root=Path(json.loads(Path('/etc/gs422-sdr.json').read_text())['output_root'])
    saved=ask('CONFIG').split(',')[1:]
    saved=[field.split('=',1)[1] for field in saved]
    try:
        print(ask('CONFIG','LTE','2130150000','20000000','20000000','2000','30','1','DCI','100'),flush=True)
        for mode in ('complete','cancel','crash'):
            reply=ask('START'); identity=re.search(r'ID=([0-9a-f]{12})',reply)[1]
            folder=root/identity
            if mode!='complete':
                log=folder/'capture/transport/acquisition.log'
                deadline=time.monotonic()+30
                while time.monotonic()<deadline:
                    if log.exists() and 'READY rate=' in log.read_text():
                        break
                    time.sleep(.02)
                else:
                    raise AssertionError('native READY not reached: '+identity)
                assert workers(), 'capture already exited before interruption'
                if mode=='cancel':
                    print(ask('STOP',identity),flush=True)
                else:
                    subprocess.run(['systemctl','kill','--kill-who=all','--signal=SIGKILL','gs422-sdr'],check=True)
                    time.sleep(7)
            deadline=time.monotonic()+330
            while time.monotonic()<deadline:
                task=json.loads((folder/'task.json').read_text())
                if task['state'] in ('DONE','FAILED','CANCELLED','INTERRUPTED'):
                    break
                time.sleep(.1)
            expected={'complete':'DONE','cancel':'CANCELLED','crash':'INTERRUPTED'}[mode]
            assert task['state']==expected,task
            deadline=time.monotonic()+10
            while workers() and time.monotonic()<deadline:
                time.sleep(.1)
            assert not workers(),'orphaned native worker'
            if mode=='complete':
                capture=json.loads((folder/'capture/transport/capture.json').read_text())
                assert capture['accepted'] and capture['samples']==40000000 and capture['bytes_written']==160000000,capture
            print('NATIVE_LIVE_PASS',mode,identity,task['state'],flush=True)
    finally:
        ask('STOP')
        deadline=time.monotonic()+15
        while workers() and time.monotonic()<deadline:
            time.sleep(.1)
        print(ask('CONFIG',*saved),flush=True)


if __name__=='__main__':
    main()
