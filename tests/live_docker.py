"""Explicit real-Docker / PTY test using only a disposable locally created container.

Run as root. No image pull, network, host mounts, or changes to existing containers.
"""
import json
import os
from pathlib import Path
import pty
import select
import subprocess
import sys
import tempfile
import time
import uuid

ROOT=Path(__file__).resolve().parents[1]


def main():
    name='gs422-test-'+uuid.uuid4().hex[:12]
    image='spaceborne-4gapp:1.2.0-prep.2'
    cid=subprocess.check_output(['docker','create','--name',name,'--label','gs422.test=1',
          '--network','none','--read-only','--cap-drop','ALL','--security-opt','no-new-privileges',
          '--memory','64m','--pids-limit','32','--entrypoint','/bin/sh',image,
          '-c',"trap 'exit 0' TERM; while :; do sleep 1; done"],text=True).strip()
    try:
        with tempfile.TemporaryDirectory(prefix='gs422-docker-test-') as tmp:
            base=Path(tmp);master,slave=pty.openpty()
            config=base/'config'
            lines=Path('/etc/gs422.conf').read_text().splitlines()
            lines=[line for line in lines if not line.startswith(('docker_allow=','docker_socket='))]
            config.write_text('\n'.join(lines).replace('port=/dev/ttyS6','port='+os.ttyname(slave))+'\ndocker_allow='+name+'\n')
            command=['/usr/bin/gs422'] if '--installed' in sys.argv else [sys.executable,'-m','gs422.service']
            with (base/'service.log').open('w') as log:
                server=subprocess.Popen(command+['--config',str(config)],cwd=ROOT,stdout=log,stderr=log)
                pending=bytearray()
                def read(timeout=16):
                    deadline=time.monotonic()+timeout
                    while b'\n' not in pending and time.monotonic()<deadline:
                        if select.select([master],[],[],.1)[0]:pending.extend(os.read(master,4096))
                    assert b'\n' in pending,'no response'
                    line,_,rest=pending.partition(b'\n');pending[:]=rest
                    return line.rstrip(b'\r').decode()
                def ask(command):
                    os.write(master,command.encode()+b'\n');result=read();print(result,flush=True);return result
                def state():
                    return json.loads(subprocess.check_output(['docker','inspect',cid],text=True))[0]['State']['Status']
                try:
                    time.sleep(2)
                    assert 'STATE=READY' in ask('AT+DOCKER?')
                    assert 'TOTAL=' in ask('AT+DOCKER=LIST,0')
                    assert 'SIZE_BYTES=' in ask('AT+DOCKER=IMAGES,0')
                    assert 'STATE=created' in ask('AT+DOCKER=INFO,'+name)
                    assert ask('AT+DOCKER=STATS,'+name)=='+DOCKER:ERR,NOT_RUNNING'
                    assert ask('AT+DOCKER=START,'+name).endswith(',OK')
                    assert state()=='running'
                    assert 'MEM_USAGE_BYTES=' in ask('AT+DOCKER=STATS,'+name)
                    assert ask('AT+DOCKER=RESTART,'+name).endswith(',OK')
                    assert state()=='running'
                    # Stop has its own worker: telemetry can complete ahead of it.
                    started=time.monotonic()
                    os.write(master,('AT+DOCKER=STOP,'+name+'\nAT+STA\nAT+CPU\n').encode())
                    responses=[]; fast=[]
                    for _ in range(3):
                        result=read();responses.append(result)
                        if result.startswith(('+STA:','+CPU:')):fast.append(time.monotonic()-started)
                    assert len(fast)==2 and max(fast)<.5,(responses,fast)
                    assert any(x.startswith('+DOCKER:STOP,') and x.endswith(',OK') for x in responses)
                    assert state()=='exited'
                    print('Concurrent STA/CPU while stopping:',fast,flush=True)
                    other=subprocess.check_output(['docker','ps','-aq','--filter','ancestor=hello-world'],text=True).split()
                    if other:
                        full=subprocess.check_output(['docker','inspect','--format','{{.Id}}',other[0]],text=True).strip()
                        assert ask('AT+DOCKER=START,'+full)=='+DOCKER:ERR,NOT_ALLOWED'
                    assert ask('AT+DOCKER=EXEC,'+name)=='+DOCKER:ERR,BAD_COMMAND'
                    print('Docker live test PASS',flush=True)
                finally:
                    server.terminate()
                    try:server.wait(timeout=10)
                    except subprocess.TimeoutExpired:server.kill();server.wait()
                    os.close(master);os.close(slave)
    finally:
        subprocess.run(['docker','rm','-f',cid],check=True,stdout=subprocess.DEVNULL)


if __name__=='__main__':main()
