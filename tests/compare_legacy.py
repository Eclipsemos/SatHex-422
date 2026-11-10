"""PTY comparison: STA v2 intentional format change, other replies unchanged."""
import os
from pathlib import Path
import pty
import select
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]


def exercise(command, config_text, commands):
    with tempfile.TemporaryDirectory(prefix='gs422-compare-') as tmp:
        base=Path(tmp)
        master,slave=pty.openpty()
        soc=base/'soc'; soc.write_text('35150')
        current=base/'current'; current.write_text('1262')
        smi=base/'smi'
        smi.write_text('#!/bin/sh\ncat << EOF\n  device0 detail infos\n  Temperature :\n    DDR0 : 29.2 C\n    Core0 : 30.1 C\n  Board_Power : 4.92 W\nEOF\n'); smi.chmod(0o755)
        replace={'port':os.ttyname(slave),'soc_temp_path':str(soc),'current_path':str(current),
                 'current_divisor':'1','smi':str(smi)}
        text='\n'.join(k+'='+replace.get(k,v) if sep else line
                       for line in config_text.splitlines() for k,sep,v in [line.partition('=')])+'\n'
        cfg=base/'config'; cfg.write_text(text)
        with (base/'log').open('w') as log:
            proc=subprocess.Popen(command+['--config',str(cfg)],cwd=ROOT,stdout=log,stderr=log)
            result=[]
            pending=bytearray()
            try:
                time.sleep(1.5)
                for cmd in commands:
                    os.write(master,cmd+b'\r\n')
                    deadline=time.monotonic()+6
                    while b'\n' not in pending and time.monotonic()<deadline:
                        if select.select([master],[],[],.1)[0]: pending.extend(os.read(master,8192))
                    first,_,rest=pending.partition(b'\n'); pending[:]=rest
                    result.append(first.rstrip(b'\r').decode(errors='replace'))
            finally:
                proc.terminate()
                try:proc.wait(timeout=8)
                except subprocess.TimeoutExpired:proc.kill();proc.wait()
                os.close(master);os.close(slave)
            return result


if __name__=='__main__':
    config=Path('/etc/shanhe-serial.conf').read_text()
    commands=[b'AT+STA',b'AT+STA?',b'AT+IP1',b'AT+IP2?',b'AT+WIFI',b'AT+WIFI?',
              b'AT+BOGUS',b'AT+STA\x00',b'x'*513,b'AT+WIFI=x,short',b'at+sta',b' AT+STA ',b'AT+STA??']
    old=exercise(['/usr/sbin/shanhe-serial'],config,commands)
    command=['/usr/bin/gs422'] if '--installed' in sys.argv else [sys.executable,'-m','gs422.service']
    new=exercise(command,config,commands)
    failures=0
    for cmd,a,b in zip(commands,old,new):
        if cmd in (b'AT+STA', b'AT+STA?'):
            passed = (a == '+STA:3010,4920,3515,1262' and
                      b == '+STA:NPU_TEMP=30.10C,NPU_POWER=4.920W,RK_TEMP=35.15C,VIN12_CURRENT=1262mA')
        else:
            passed = a == b
        print(repr(cmd[:50]),'OLD:',a,'NEW:',b,'PASS:',passed)
        failures+=not passed
    raise SystemExit(bool(failures))
