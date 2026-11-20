"""Temporary physical AT fixture: restores config/containers after 180s or finish file.

Explicit root invocation only. Saves original gs422 config, adds only its own
container to the allowlist, restarts gs422, and restores everything in finally.
"""
import io
import json
import os
from pathlib import Path
import subprocess
import tarfile
import tempfile
import time
import uuid


def main():
    if os.geteuid():
        raise SystemExit('sudo required')
    config = Path('/etc/gs422.conf')
    original = config.read_bytes()
    name = 'sathex-serial-demo-' + uuid.uuid4().hex[:10]
    finish = Path('/run/' + name + '.finish')
    tag = name + ':test'
    cid = None
    image_created = changed = False
    def run(*args):
        return subprocess.check_output(args, text=True, stderr=subprocess.STDOUT, timeout=30).strip()
    try:
        with tempfile.TemporaryDirectory(prefix='sathex-physical-') as folder:
            root=Path(folder)
            source=root/'main.c'
            source.write_text('#include <signal.h>\n#include <unistd.h>\nvolatile sig_atomic_t r=1;void s(int x){r=0;}int main(){signal(SIGTERM,s);while(r)usleep(100000);return 0;}\n')
            run('gcc','-static','-Os','-s',str(source),'-o',str(root/'sample'))
            with tarfile.open(root/'image.tar','w') as tar:
                data=(root/'sample').read_bytes();info=tarfile.TarInfo('sample');info.size=len(data);info.mode=0o755
                tar.addfile(info,io.BytesIO(data))
            image=run('docker','import',str(root/'image.tar'),tag);image_created=True
            cid=run('docker','create','--name',name,'--label','sathex.physical-test=1',
                    '--network','none','--read-only','--cap-drop','ALL','--security-opt','no-new-privileges',
                    '--memory','64m','--pids-limit','16','--entrypoint','/sample',image)
            lines=original.decode().splitlines()
            allow=next((line.split('=',1)[1] for line in lines if line.startswith('docker_allow=')),'')
            lines=[line for line in lines if not line.startswith('docker_allow=')]
            lines.append('docker_allow='+';'.join(x for x in (allow,name) if x))
            changed=True
            config.write_text('\n'.join(lines)+'\n')
            run('systemctl','restart','gs422.service')
            run('systemctl','is-active','gs422.service')
            print(json.dumps({'ready':True,'name':name,'id':cid,'finish':str(finish)}),flush=True)
            deadline=time.monotonic()+180
            while not finish.exists() and time.monotonic()<deadline:
                time.sleep(.5)
    finally:
        if changed:
            config.write_bytes(original)
            run('systemctl','restart','gs422.service')
        if cid:run('docker','rm','-f',cid)
        if image_created:run('docker','image','rm',tag)
        finish.unlink(missing_ok=True)
        print(json.dumps({'restored':config.read_bytes()==original,'container_removed':cid is not None}),flush=True)


if __name__=='__main__':main()
