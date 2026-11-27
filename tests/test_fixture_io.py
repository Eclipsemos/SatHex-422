import tempfile
import threading
import time
import unittest
from pathlib import Path
from gs422.fixture_io import FixtureIO


class FixtureIOTests(unittest.TestCase):
    def test_explicit_sources_and_sink_file_contents(self):
        import zlib
        with tempfile.TemporaryDirectory() as root:
            f=FixtureIO(root);cancel=threading.Event()
            frames=list(f.solid_source(1,2,cancel))
            self.assertEqual([x[0] for x in frames],[b'fixture-solid-1',b'fixture-solid-2'])
            for body,crc in frames:self.assertEqual(zlib.crc32(body),crc);f.sinks[1](body,cancel)
            self.assertEqual((Path(root)/'downlink-1.bin').read_bytes(),b'fixture-solid-1fixture-solid-2')
            cancel.set();f.sinks[1](b'forbidden',cancel)
            self.assertNotIn(b'forbidden',(Path(root)/'downlink-1.bin').read_bytes())

    def test_capture_start_stop_and_error_are_observable(self):
        with tempfile.TemporaryDirectory() as root:
            f=FixtureIO(root);seen=[];ready=threading.Event()
            def receive(body,utc):seen.append(body);ready.set()
            f.start(3,receive);self.assertTrue(ready.wait(1));f.stop()
            n=len(seen);time.sleep(.15);self.assertEqual(len(seen),n)
            self.assertTrue(seen[0].startswith(b'fixture-star mode=3'))
            ready.clear()
            def fail(*_):ready.set();raise OSError('test failure')
            f.start(1,fail);self.assertTrue(ready.wait(1));f.thread.join(1)
            with self.assertRaises(RuntimeError):f.stop()
