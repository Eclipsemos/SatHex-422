"""Explicit action composition: one runtime, distinct business ownership.

Drivers and software catalogs are supplied by the caller, not synthesized here.
An optional reboot executor may be installed separately. No service starts on
import and missing dependencies remain errors rather than simulated success.
"""
import threading
from .space_commands import CommandError


class PayloadBackend:
    timeout=30
    FILE_ACTIONS=frozenset(('storage_clean','playback_link','playback_file','playback_start',
                           'playback_stop','storage_start','storage_stop','storage_status','capture_control'))
    SOFTWARE_ACTIONS=frozenset(('task_start','task_stop','task_pause','software_resume','software_stop','firmware_version'))

    def __init__(self,software,files,*,reboot=None):
        self.software,self.files,self.reboot=software,files,reboot
        self.lock=threading.RLock()

    def _files_busy(self):
        return self.files.capture is not None or any(t and t.is_alive() for t in (self.files.play_thread,self.files.store_thread))

    def version(self):return self.software.version()

    def permits(self,args):return self.software.permits(args)

    def ota(self,action,parameters,staging):
        with self.lock:
            if self._files_busy():return 1
            return self.software.ota(action,parameters,staging)

    def execute(self,action,parameters):
        with self.lock:
            if action in self.FILE_ACTIONS:
                return self.files.execute(action,parameters)
            if action in self.SOFTWARE_ACTIONS:
                return self.software.execute(action,parameters)
            if action=='board_reboot':
                if self.reboot is None:raise CommandError(7)
                # Reject ongoing work rather than losing partially written data.
                # Deployment owner can stop it with the defined commands first.
                if self._files_busy():raise CommandError(6)
                if self.software.task and self.software._state(self.software.task)['Running']:
                    raise CommandError(6)
                return self.reboot.execute(action,parameters)
            raise CommandError(7)

    def telemetry(self):
        # No composition lock: version/telemetry collection must not block behind
        # OTA load; callers should collect these snapshots on a background worker.
        return {**self.software.telemetry(),**self.files.telemetry()}
