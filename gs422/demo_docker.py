"""Explicit LAB backend: deploy trusted sample image archives and run sample tasks.

Never installed by default. Trust catalog is local, not supplied by the wire.
Archive SHA256 and expected immutable image ID are checked before use. Only
containers created by this instance may be controlled. This is not a general
production OTA installer or a substitute for package-signature verification.
"""
import hashlib
import json
from pathlib import Path
import subprocess
import threading
import time
import uuid

from .space_commands import CommandError


class DemoDocker:
    timeout = 30

    def __init__(self, root, catalog, runner=None):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.catalog = catalog
        self.runner = runner or self._run
        self.prefix = 'sathex-demo-' + uuid.uuid4().hex[:12]
        self.owned = []
        self.lock = threading.RLock()
        self.slots = {1: None, 2: None}
        self.active = None
        self.release = 'UNCONFIGURED'
        self.software = None
        self.task = None
        self.task_id = 0
        self.latest_completed = 0
        self.last_task_state = 0
        self.retained = None

    @staticmethod
    def _run(*args):
        r = subprocess.run(['docker', *args], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                           timeout=25, text=True)
        if r.returncode:
            raise CommandError(8)
        return r.stdout.strip()

    def version(self):
        # Read the last committed immutable string; do not block the control
        # query behind docker load or candidate health checks.
        return self.release

    def permits(self, args):
        return (args['component'], args['region']) in ((3, 1), (4, 2)) and args['region'] != self.active

    def _create(self, image, mode, *, device=None, words=()):
        name = self.prefix + '-' + uuid.uuid4().hex[:8]
        cid = self.runner('create', '--name', name, '--label', 'sathex.demo=' + self.prefix,
                          '--network', 'none', '--read-only', '--cap-drop', 'ALL',
                          '--security-opt', 'no-new-privileges', '--memory', '64m', '--pids-limit', '16',
                          '--user', '65534:65534', *(['--device',device] if device else []),
                          '--entrypoint', '/sample', image, mode, *(str(word) for word in words))
        self.owned.append(cid)
        try:
            self.runner('start', cid)
        except Exception:
            self.runner('rm', '-f', cid)
            self.owned.remove(cid)
            raise
        return cid

    def _state(self, cid):
        if cid not in self.owned:
            raise CommandError(7)
        return json.loads(self.runner('inspect', cid))[0]['State']

    def _health(self, cid, release):
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            state = self._state(cid)
            logs = self.runner('logs', '--tail', '4', cid)
            if state['Running'] and 'READY:' + release in logs:
                return
            if not state['Running']:
                raise CommandError(8)
            time.sleep(.02)
        raise CommandError(9)

    def ota(self, action, args, staging):
        with self.lock:
            if action == 'erase':
                region = args['region']
                if region not in (1, 2) or region in (self.active, self.retained):
                    return 0xff
                if args['scope'] == 0:
                    self.slots[region] = None  # only lab-owned software registration, no block device
                return 0
            if args['index'] != 0 or staging.state['status'] != 0x23 or not staging.state['request']:
                return 0xff
            component, region, mode, segmented, segments = staging.state['request']
            if (component, region) not in ((3, 1), (4, 2)) or region == self.active:
                return 0xff
            archive = self.root / (uuid.uuid4().hex + '.tar')
            candidate = None
            old = self.software
            stopped = False
            try:
                staging.export_verified(archive)
                h = hashlib.sha256()
                with archive.open('rb') as stream:
                    for chunk in iter(lambda: stream.read(1024 * 1024), b''):
                        h.update(chunk)
                entry = self.catalog.get(h.hexdigest())
                if not entry:
                    staging.state['status'] = 0x40
                    staging._save()
                    return 0xff
                staging.state['status'] = 0x30
                staging._save()
                self.runner('load', '-i', str(archive))
                actual = self.runner('image', 'inspect', '--format', '{{.Id}}', entry['image_id'])
                if actual != entry['image_id']:
                    raise CommandError(8)
                if old:
                    self.runner('stop', '-t', '2', old)
                    stopped = True
                candidate = self._create(actual, 'service')
                self._health(candidate, entry['version'])
                previous = self.active
                self.slots[region] = dict(entry)
                self.active, self.release, self.software = region, entry['version'], candidate
                self.retained = previous
                staging.state['status'] = 0x31
                staging._save()
                return 0
            except (CommandError, OSError, ValueError, subprocess.TimeoutExpired):
                if candidate:
                    self.runner('rm', '-f', candidate)
                    self.owned.remove(candidate)
                if old and stopped:
                    self.runner('start', old)
                    self._health(old, self.release)
                staging.state['status'] = 0x40
                staging._save()
                return 0xff
            finally:
                archive.unlink(missing_ok=True)

    def execute(self, action, parameters):
        with self.lock:
            if action == 'firmware_version':
                return json.dumps({'version': self.release}).encode()
            if action == 'task_start':
                if parameters['task_id'] != 1 or parameters['mode'] != 1 or parameters['npu'] or parameters['priority'] != 0:
                    raise CommandError(4)
                if self.active is None:
                    raise CommandError(7)
                if self.task and self._state(self.task)['Running']:
                    raise CommandError(6)
                self.task = self._create(self.slots[self.active]['image_id'], 'algorithm')
                self.task_id, self.last_task_state = 1, 1
            elif action in ('task_stop', 'task_pause'):
                if parameters['task_id'] != 1 or not self.task:
                    raise CommandError(7)
                if action == 'task_pause':
                    self.runner('pause', self.task)
                else:
                    if self._state(self.task).get('Paused'):
                        self.runner('unpause', self.task)
                    self.runner('stop', '-t', '2', self.task)
                    self.latest_completed, self.last_task_state = 1, 0
            elif action == 'software_stop':
                if not self.software:
                    raise CommandError(7)
                if self.task and self._state(self.task)['Running']:
                    if self._state(self.task).get('Paused'):
                        self.runner('unpause', self.task)
                    self.runner('stop', '-t', '2', self.task)
                    self.last_task_state = 0
                self.runner('stop', '-t', '2', self.software)
            elif action == 'software_resume':
                if not self.software:
                    raise CommandError(7)
                self.runner('start', self.software)
                if self.task and self._state(self.task).get('Paused'):
                    self.runner('unpause', self.task)
            else:
                raise CommandError(7)
            return b'{}'

    def telemetry(self):
        with self.lock:
            status = self.last_task_state
            if self.task:
                state = self._state(self.task)
                if not state['Running'] and status == 1:
                    status = 3 if state['ExitCode'] == 0 else 2
                    self.latest_completed = self.task_id
                    self.last_task_state = status
            return {'TMG015': self.task_id, 'TMG016': self.task_id if status == 1 else 0,
                    'TMG017': self.latest_completed, 'TMG032': status}  # APP1

    def cleanup(self):
        for cid in list(self.owned):
            self.runner('rm', '-f', cid)
            self.owned.remove(cid)
