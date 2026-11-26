"""Non-preemptive scheduling for registered payload algorithms.

State is owned by the service worker. A management restart never replays a job.
"""
from concurrent.futures import Future
from dataclasses import dataclass
import json
import time
import sqlite3
from pathlib import Path
import threading

from .space_commands import CommandError


@dataclass
class Job:
    task_id: int
    priority: int
    exclusive: bool
    image: str
    device: str | None
    cid: str | None = None
    completion: Future | None = None
    capture_profile: dict | None = None


class Scheduler:
    def __init__(self, software, registry, result_files=None):
        self.software = software
        self.registry = registry
        self.result_files = result_files
        self.lock = threading.RLock()
        self.pending = []
        self.running = {}
        self.states = {}
        self.configured = self.completed = 0
        # Interrupted jobs are never adopted without a task ID, or replayed.
        for cid in list(software.owned):
            info = software._inspect_owned(cid)
            if ((info['Config'].get('Cmd') or [])[:1] == ['algorithm'] or
                    (info['Config'].get('Labels') or {}).get('sathex.capture_job') == '1'):
                self._retire(cid)

    def _retire(self, cid):
        self.software.runner('rm','-f',cid)
        self.software.owned.remove(cid)
        if self.software.task == cid:
            self.software.task = None
        self.software._save('idle')

    def _active_capture(self, entry):
        profile = entry.get('capture_job')
        if profile and self.software.active:
            active_image = self.software.slots[self.software.active]['image_id']
            if active_image == profile['release_image']:
                return profile
            if active_image in profile.get('release_copies', ()):
                return dict(profile, release_image=active_image)
        return None

    def start_short(self, task_id, words):
        with self.lock:
            self._refresh()
            entry = self.registry.get(str(task_id), {})
            profile = self._active_capture(entry)
            image = profile['decoder_image'] if profile else entry.get('ht226_image')
            if image is None or self.result_files is None:
                raise CommandError(7)
            if self.pending or self.running:
                raise CommandError(6)
            if profile:
                if any(words):
                    raise CommandError(4)  # Packaged captures have fixed decoder settings.
                cid = self.software.create_capture_job(profile,task_id,words)
            else:
                cid = self.software._create(image, 'algorithm', words=words)
            completion = Future()
            self.running[task_id] = Job(task_id, 0, True, image, None, cid, completion, profile)
            self.states[task_id] = 1
            self.configured = task_id
            return completion

    def _short_result(self, job, exit_code):
        detail = {'task_id': job.task_id, 'app_exit': exit_code}
        if exit_code != 0:
            return 8, detail
        # Opt-in image contract: the last log line is a bounded JSON object.
        try:
            line = self.software.runner('logs', '--tail', '1', job.cid)
            raw = line.encode('utf-8')
            if len(raw) > 8192:
                raise ValueError('result exceeds 8 KiB')
            output = json.loads(raw)
            if not isinstance(output, dict):
                raise ValueError('expected result object')
            detail['file_id'] = self.result_files.add_file([(int(time.time()), raw)], file_type=3)
            if type(output.get('value')) is int and 0 <= output['value'] <= 0xffffffff:
                detail['value'] = output['value']
            return 0, detail
        except (ValueError, OSError, CommandError, sqlite3.Error):
            return 8, detail

    def _refresh(self):
        for ident, job in list(self.running.items()):
            state = self.software._state(job.cid)
            if not state['Running']:
                if job.completion is not None or job.capture_profile:
                    error, detail = self._short_result(job, state['ExitCode'])
                    if job.completion is not None:
                        job.completion.set_result((error, detail))
                else:
                    error = state['ExitCode']
                self.states[ident] = 3 if error == 0 else 2
                self.completed = ident
                del self.running[ident]
                self._retire(job.cid)

    def _dispatch(self):
        self.pending.sort(key=lambda job: -job.priority)  # stable FIFO for ties
        while self.pending and len(self.running) < 2:
            job = self.pending[0]
            if self.running and (job.exclusive or any(j.exclusive for j in self.running.values())):
                break
            self.pending.pop(0)
            try:
                if job.capture_profile:
                    job.cid = self.software.create_capture_job(job.capture_profile,job.task_id,(0,)*5)
                else:
                    job.cid = self.software._create(job.image, 'algorithm', device=job.device)
            except Exception:
                self.states[job.task_id] = 2
                self.completed = job.task_id
                raise
            self.running[job.task_id] = job
            self.states[job.task_id] = 1

    def busy(self):
        with self.lock:
            self._refresh()
            return bool(self.pending or self.running)

    def execute(self, action, params):
        with self.lock:
            self._refresh()
            if action == 'task_start':
                ident = params['task_id']
                entry = self.registry.get(str(ident))
                if entry is None or self.software.active is None:
                    raise CommandError(7)
                if ident in self.running or any(j.task_id == ident for j in self.pending) or len(self.pending) >= 8:
                    raise CommandError(6)
                device = entry.get('device') if params['npu'] else None
                if params['npu'] and (not device or not Path(device).is_char_device()):
                    raise CommandError(7)
                image = entry.get('npu_image') if params['npu'] else entry['cpu_image']
                if image == 'active':
                    image = self.software.slots[self.software.active]['image_id']
                profile = self._active_capture(entry)
                if profile:
                    if params['npu'] or self.result_files is None:
                        raise CommandError(7)
                    image = profile['decoder_image']
                elif not image or image not in {v['image_id'] for v in self.software.catalog.values()}:
                    raise CommandError(7)
                self.pending.append(Job(ident, params['priority'], params['mode']==1, image, device,capture_profile=profile))
                self._dispatch()
                self.configured = ident
            elif action in ('task_stop','task_pause'):
                ident = params['task_id']
                job = self.running.get(ident)
                if not job:
                    queued = next((j for j in self.pending if j.task_id == ident), None)
                    if action == 'task_stop' and queued:
                        self.pending.remove(queued)
                        self.states[ident] = 0
                        return b'{}'
                    raise CommandError(7)
                if action == 'task_pause':
                    self.software.runner('pause',job.cid)
                else:
                    if self.software._state(job.cid).get('Paused'):
                        self.software.runner('unpause',job.cid)
                    self.software.runner('stop','-t','2',job.cid)
                    if job.completion is not None:
                        actual_exit = self.software._state(job.cid)['ExitCode']
                        job.completion.set_result((8, {'task_id':ident,'app_exit':actual_exit}))
                    del self.running[ident]
                    self._retire(job.cid)
                    self.completed, self.states[ident] = ident, 0
                    self._dispatch()
            elif action == 'software_stop':
                self.pending.clear()
                for ident in list(self.running):
                    self.execute('task_stop', {'task_id':ident})
                return self.software.execute(action,params)
            elif action == 'software_resume':
                self.software.execute(action,params)
                for job in self.running.values():
                    if self.software._state(job.cid).get('Paused'):
                        self.software.runner('unpause',job.cid)
            return b'{}'

    def telemetry(self):
        with self.lock:
            self._refresh()
            self._dispatch()
            values = {'TMG015':self.configured,'TMG016':next(reversed(self.running),0),'TMG017':self.completed}
            values.update({f'TMG{33-i:03}':self.states.get(i,0) for i in range(16)})
            return values
