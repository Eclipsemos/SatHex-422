import os
from pathlib import Path
import select
import sys
import tempfile
import time
import unittest

from gs422 import tmg
from gs422.core import Cache
from gs422.ota_staging import Staging
from gs422.ota_wire import control, file_packets
from gs422.revised_runtime import Revised
from gs422.space_protocol import Decoder, Message


@unittest.skipUnless(sys.platform.startswith('linux'), 'requires Linux UART/PTYS')
class DualUARTTests(unittest.TestCase):
    def setUp(self):
        import pty
        self.control_master, self.control_slave = pty.openpty()
        self.upload_master, self.upload_slave = pty.openpty()
        self.ports = os.ttyname(self.control_slave), os.ttyname(self.upload_slave)
        self.tmp = tempfile.TemporaryDirectory()
        self.staging = Staging(self.tmp.name, crc_profile='CCITT-FALSE', capacity=2000)
        # Explicit simulated measurement source; never installed on a real board.
        values = {f.id: 0 for f in tmg.FIELDS}
        self.runtime = Revised(self.staging, Cache(1000), values, version='test-only')
        self.transport = None

    def tearDown(self):
        self.runtime.close()
        if self.transport:
            self.transport.close()
        self.staging.close()
        self.tmp.cleanup()
        for fd in (self.control_master, self.control_slave, self.upload_master, self.upload_slave):
            os.close(fd)

    def read_response(self):
        decoder = Decoder()
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            self.transport.pump()
            if select.select([self.control_master], [], [], 0)[0]:
                frames = decoder.feed(os.read(self.control_master, 4096))
                if frames:
                    return frames[0]
        self.fail('no control reply')

    def ask(self, code, body):
        os.write(self.control_master, control(0x30, code, body))
        return self.read_response()

    def test_real_descriptors_two_channels_upload_cannot_write(self):
        from gs422.dual_uart import DualUART
        self.runtime.start()
        self.transport = DualUART(self.runtime, *self.ports)
        with self.assertRaises(OSError):
            os.write(self.transport.upload.fd, b'must not transmit')
        self.assertEqual(self.ask(0x17, b'\x01' + bytes(5)), b'\x31\x17\x00')
        self.assertEqual(self.ask(0x53, bytes([3, 1, 2, 0, 0, 0])), b'\x31\x53\x00')
        os.write(self.upload_master, b''.join(file_packets(b'x' * 900, 'CCITT-FALSE')))
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline and self.staging.snapshot()['TMG010'] < 2:
            self.transport.pump()
        self.assertEqual(self.staging.snapshot()['TMG010'], 2)
        self.assertFalse(select.select([self.upload_master], [], [], .05)[0])
        self.assertEqual(self.ask(0x54, bytes(6)), b'\x31\x54\x00')
        os.write(self.control_master, Message(0x11, 1, 0).wire())
        values = tmg.unpack(Message.parse(self.read_response()).body)
        self.assertEqual((values['TMG010'], values['TMG012'], values['TMG014']), (2, 100, 0x23))

    def test_reject_same_device_including_symlink(self):
        from gs422.dual_uart import DualUART
        with self.assertRaises(ValueError):
            DualUART(self.runtime, self.ports[0], self.ports[0])
        alias = Path(self.tmp.name) / 'alias'
        alias.symlink_to(self.ports[0])
        with self.assertRaises(ValueError):
            DualUART(self.runtime, self.ports[0], str(alias))

    def test_close_releases_exclusive_lock_and_output_is_bounded(self):
        from gs422.dual_uart import DualUART
        from gs422.platform import Serial
        self.transport = DualUART(self.runtime, *self.ports, max_pending=512)
        with self.assertRaises(OSError):
            Serial(self.ports[0])
        with self.assertRaises(BufferError):
            self.transport._queue(b'x' * 513)
        self.transport.close()
        reopened = Serial(self.ports[0])
        reopened.close()

    def test_upload_open_failure_releases_control(self):
        from gs422.dual_uart import DualUART
        from gs422.platform import Serial
        unavailable = Path(self.tmp.name) / 'not-a-tty'
        unavailable.write_bytes(b'')
        with self.assertRaises(OSError):
            DualUART(self.runtime, self.ports[0], str(unavailable))
        reopened = Serial(self.ports[0])
        reopened.close()
