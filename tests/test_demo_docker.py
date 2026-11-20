import hashlib
from pathlib import Path
import tempfile
import unittest
import threading
from gs422.demo_docker import DemoDocker


class Upload:
    def __init__(self,data):
        self.data=data
        self.state={'status':0x23,'request':[3,1,1,0,0]}
    def export_verified(self,path):
        Path(path).write_bytes(self.data)
    def _save(self):pass


class DemoTrustTests(unittest.TestCase):
    def test_version_query_does_not_wait_for_install_lock(self):
        with tempfile.TemporaryDirectory() as root:
            backend=DemoDocker(root,{})
            results=[]
            with backend.lock:
                query=threading.Thread(target=lambda:results.append(backend.version()))
                query.start()
                query.join(timeout=1)
                nonblocking=not query.is_alive()
            query.join(timeout=1)
            self.assertTrue(nonblocking)
            self.assertEqual(results,['UNCONFIGURED'])

    def test_unknown_archive_never_reaches_docker_load(self):
        calls=[]
        with tempfile.TemporaryDirectory() as root:
            backend=DemoDocker(root,{},runner=lambda *args:calls.append(args))
            upload=Upload(b'untrusted image')
            self.assertEqual(backend.ota('load',{'index':0},upload),255)
            self.assertEqual(upload.state['status'],0x40)
            self.assertFalse(calls)
            self.assertFalse(list(Path(root).iterdir()))

    def test_active_region_and_boot_components_rejected(self):
        with tempfile.TemporaryDirectory() as root:
            backend=DemoDocker(root,{})
            for component,region in ((1,1),(2,2),(3,2),(4,1),(3,3),(4,4)):
                self.assertFalse(backend.permits({'component':component,'region':region}))
            backend.active=1
            self.assertFalse(backend.permits({'component':3,'region':1}))
            self.assertTrue(backend.permits({'component':4,'region':2}))
            self.assertEqual(backend.ota('erase',{'region':1,'scope':0},None),255)
            backend.retained=2
            self.assertEqual(backend.ota('erase',{'region':2,'scope':0},None),255)

    def test_no_container_can_be_controlled_without_ownership(self):
        with tempfile.TemporaryDirectory() as root:
            backend=DemoDocker(root,{})
            from gs422.space_commands import CommandError
            with self.assertRaises(CommandError):backend._state('unrelated-container')
