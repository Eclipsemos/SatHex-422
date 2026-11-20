"""Explicit opt-in K1109 backend, once per boot, with durable request evidence.

Success means systemd accepted a delayed reboot, not that reboot already finished.
The caller confirms a changed Linux boot_id and recovered services afterward.
"""
import argparse
import json
import os
from pathlib import Path
import subprocess
import tempfile
import uuid

from .space_commands import CommandError


class BoardReboot:
    timeout=5

    def __init__(self, state_path, *, enabled=False, boot_id=None, schedule=None):
        self.path=Path(state_path)
        self.enabled=enabled
        self.boot_id=boot_id or Path('/proc/sys/kernel/random/boot_id').read_text().strip()
        uuid.UUID(self.boot_id)
        self.schedule=schedule or self._schedule

    @staticmethod
    def _schedule(unit):
        subprocess.run(['/usr/bin/systemd-run','--unit='+unit,'--on-active=10s',
                        '--timer-property=AccuracySec=1s','/bin/systemctl','reboot'],
                       stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.PIPE,
                       timeout=4,check=True)

    def _save(self, record):
        self.path.parent.mkdir(parents=True,exist_ok=True)
        fd,name=tempfile.mkstemp(prefix='.reboot-',dir=self.path.parent)
        try:
            with os.fdopen(fd,'w') as stream:
                json.dump(record,stream);stream.flush();os.fsync(stream.fileno())
            os.replace(name,self.path)
            directory=os.open(self.path.parent,os.O_RDONLY|os.O_DIRECTORY)
            try:os.fsync(directory)
            finally:os.close(directory)
        finally:
            if os.path.exists(name):os.unlink(name)

    def execute(self,action,parameters):
        if action!='board_reboot' or not self.enabled:raise CommandError(7)
        if parameters:raise CommandError(4)
        import fcntl
        self.path.parent.mkdir(parents=True,exist_ok=True)
        with self.path.with_suffix('.lock').open('a') as lock:
            fcntl.flock(lock,fcntl.LOCK_EX)
            if self.path.exists():
                previous=json.loads(self.path.read_text())
                if previous.get('boot_id')==self.boot_id:
                    if previous.get('state')=='scheduled':
                        return json.dumps(previous).encode()
                    # A failed or interrupted scheduler may have already accepted
                    # the request. Require explicit local investigation, not retry.
                    raise CommandError(8)
            record={'boot_id':self.boot_id,'unit':'sathex-reboot-'+self.boot_id,
                    'state':'intent','delay_seconds':10}
            self._save(record)
            try:
                self.schedule(record['unit'])
            except (OSError,subprocess.SubprocessError):
                record['state']='outcome_unknown';self._save(record)
                raise CommandError(8)
            record['state']='scheduled';self._save(record)
            return json.dumps(record).encode()


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--state',required=True)
    parser.add_argument('--enable',action='store_true')
    args=parser.parse_args()
    import sys
    try:
        request=json.loads(sys.stdin.buffer.read(4097))
        value=BoardReboot(args.state,enabled=args.enable).execute(request['action'],request['parameters'])
        print(json.dumps({'ok':True,'data':json.loads(value)}))
    except (CommandError,OSError,ValueError,KeyError,TypeError):
        print(json.dumps({'ok':False}))
        return 1
    return 0


if __name__=='__main__':raise SystemExit(main())
