import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from gs422.board_reboot import BoardReboot
from gs422.space_commands import CommandError


@unittest.skipUnless(sys.platform.startswith('linux'),'Linux locking and durable directory fsync')
class RebootTests(unittest.TestCase):
    BOOT='11111111-1111-4111-8111-111111111111'
    def test_default_disabled_never_schedules(self):
        with tempfile.TemporaryDirectory() as root:
            calls=[];p=Path(root)/'request.json'
            backend=BoardReboot(p,boot_id=self.BOOT,schedule=calls.append)
            with self.assertRaises(CommandError):backend.execute('board_reboot',{})
            self.assertFalse(calls);self.assertFalse(p.exists())

    def test_retry_and_new_process_schedule_only_once_per_boot(self):
        with tempfile.TemporaryDirectory() as root:
            calls=[];p=Path(root)/'request.json'
            first=BoardReboot(p,enabled=True,boot_id=self.BOOT,schedule=calls.append)
            a=first.execute('board_reboot',{})
            second=BoardReboot(p,enabled=True,boot_id=self.BOOT,schedule=calls.append)
            self.assertEqual(second.execute('board_reboot',{}),a)
            self.assertEqual(len(calls),1)
            self.assertEqual(json.loads(p.read_text())['state'],'scheduled')

    def test_uncertain_scheduler_never_blindly_retries(self):
        with tempfile.TemporaryDirectory() as root:
            calls=[];p=Path(root)/'request.json'
            def fail(unit):calls.append(unit);raise subprocess.TimeoutExpired('systemd-run',4)
            backend=BoardReboot(p,enabled=True,boot_id=self.BOOT,schedule=fail)
            for _ in range(2):
                with self.assertRaises(CommandError):backend.execute('board_reboot',{})
            self.assertEqual(len(calls),1)
            self.assertEqual(json.loads(p.read_text())['state'],'outcome_unknown')

    def test_new_boot_and_invalid_parameters(self):
        with tempfile.TemporaryDirectory() as root:
            calls=[];p=Path(root)/'request.json'
            b=BoardReboot(p,enabled=True,boot_id=self.BOOT,schedule=calls.append)
            with self.assertRaises(CommandError):b.execute('board_reboot',{'command':'arbitrary'})
            b.execute('board_reboot',{})
            BoardReboot(p,enabled=True,boot_id='22222222-2222-4222-8222-222222222222',schedule=calls.append).execute('board_reboot',{})
            self.assertEqual(len(calls),2)
