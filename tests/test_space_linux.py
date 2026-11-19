"""PTY/runtime tests without occupying the board's production UART."""
import json
import os
from pathlib import Path
import select
import signal
import subprocess
import sys
import tempfile
import time
import unittest

from gs422.space_commands import CommandError
from gs422.space_protocol import Decoder, Message
from gs422.space_runtime import Adapter

LINUX = sys.platform.startswith('linux')


@unittest.skipUnless(LINUX, 'Linux adapters and PTY')
class LinuxSpaceTests(unittest.TestCase):
    def test_adapter_success_failure_output_limit_and_timeout(self):
        success = Adapter({'task_start': [sys.executable, '-c',
            'import json,sys; x=json.load(sys.stdin); print(json.dumps({"ok":True,"data":{"task":x["parameters"]["task_id"]}}))']})
        self.assertEqual(json.loads(success.execute('task_start', {'task_id': 9})), {'task': 9})
        for program, error in [('print("not-json")', 8), ('print("x"*8000)', 11),
                               ('import time; time.sleep(3)', 9), ('print(\'{"ok":false}\')', 8),
                               ('import json; print(json.dumps({"ok":True,"data":"x"*600}))', 11)]:
            with self.subTest(program=program), self.assertRaises(CommandError) as caught:
                Adapter({'a': [sys.executable, '-c', program]}, timeout=.3).execute('a', {})
            self.assertEqual(caught.exception.error, error)

    def test_pty_no_unsolicited_or_command_ack_then_telemetry_and_result(self):
        import pty
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root/'soc').write_text('41000')
            (root/'current').write_text('900')
            config = root/'config'
            master, slave = pty.openpty()
            config.write_text(f'port={os.ttyname(slave)}\nsoc_temp_path={root}/soc\ncurrent_path={root}/current\ncurrent_divisor=1\nsmi=/bin/false\n')
            process = subprocess.Popen([sys.executable, '-m', 'gs422.space_service', '--config', str(config),
                                        '--baud', '9600', '--parity', 'N', '--idle-reset', '1'],
                                       stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
            try:
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline:
                    if select.select([process.stderr], [], [], .1)[0]:
                        if b'READY' in os.read(process.stderr.fileno(), 4096):
                            break
                else:
                    self.fail('service did not start')
                decoder = Decoder()
                def receive():
                    limit = time.monotonic() + 2
                    while time.monotonic() < limit:
                        if select.select([master], [], [], .1)[0]:
                            messages = decoder.feed(os.read(master, 4096))
                            if messages:
                                return Message.parse(messages[0])
                    self.fail('no complete reply')
                self.assertFalse(select.select([master], [], [], .05)[0])
                os.write(master, Message(0x21, 1, 0x17, b'\x01' + bytes(5)).wire())
                self.assertFalse(select.select([master], [], [], .1)[0])
                os.write(master, Message(0x11, 2, 0).wire())
                reply = receive()
                self.assertEqual(reply.kind, 0x12)
                self.assertEqual(reply.body[56], 1)
                os.write(master, Message(0x15, 3, 0).wire())
                self.assertEqual(receive().body[4], 3)
                # Inactivity resets the UART without unsolicited output.
                self.assertFalse(select.select([master], [], [], 1.2)[0])
                os.write(master, Message(0x11, 4, 0).wire())
                self.assertGreaterEqual(int.from_bytes(receive().body[24:28], 'big'), 1)
            finally:
                process.send_signal(signal.SIGTERM)
                try:
                    process.wait(timeout=8)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
                process.stderr.close()
                os.close(master)
                os.close(slave)
            self.assertEqual(process.returncode, 0)
