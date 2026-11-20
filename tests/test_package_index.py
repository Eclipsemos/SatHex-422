import hashlib
from pathlib import Path
import sys
import tempfile
import unittest
from gs422.package_index import PackageIndex,ArchivedInput


@unittest.skipUnless(sys.platform.startswith('linux'),'directory fsync')
class PackageIndexTests(unittest.TestCase):
    def fixture(self,root):
        folder=Path(root)/'packages';folder.mkdir()
        entries={}
        for value in (b'one',b'two'):
            digest=hashlib.sha256(value).hexdigest();entries[digest]={'version':value.decode()}
            (folder/(digest+'.tar')).write_bytes(value)
        return entries

    def test_history_stable_across_restart_and_duplicate(self):
        with tempfile.TemporaryDirectory() as root:
            cat=self.fixture(root);a,b=cat
            p=PackageIndex(root,cat)
            self.assertEqual(p.register_history(a),1);self.assertEqual(p.register_history(b),2)
            q=PackageIndex(root,cat)
            self.assertEqual(q.register_history(a),1);self.assertEqual(q.resolve(2).read_bytes(),b'two')

    def test_initial_requires_explicit_registration_and_no_overwrite(self):
        with tempfile.TemporaryDirectory() as root:
            cat=self.fixture(root);a,b=cat;p=PackageIndex(root,cat)
            with self.assertRaises(ValueError):p.resolve(0xf0)
            p.register_initial(0xf0,a);self.assertEqual(p.resolve(0xf0).read_bytes(),b'one')
            with self.assertRaises(ValueError):p.register_initial(0xf0,b)
            for index in (0,0x81,0xef,256,-1):
                with self.assertRaises(ValueError):p.resolve(index)

    def test_corruption_missing_file_and_symlink_refused(self):
        with tempfile.TemporaryDirectory() as root:
            cat=self.fixture(root);a=next(iter(cat));p=PackageIndex(root,cat);p.register_history(a)
            path=p.resolve(1);path.write_bytes(b'wrong')
            with self.assertRaises(ValueError):p.resolve(1)
            path.unlink()
            with self.assertRaises(ValueError):p.resolve(1)
            outside=Path(root)/'outside';outside.write_bytes(b'one');path.symlink_to(outside)
            with self.assertRaises(ValueError):p.resolve(1)

    def test_full_registry_never_reassigns_indices(self):
        with tempfile.TemporaryDirectory() as root:
            cat=self.fixture(root);a,b=cat;p=PackageIndex(root,cat)
            p.entries={str(i):a for i in range(1,129)};p._save()
            self.assertIsNone(p.register_history(b));self.assertEqual(p.resolve(128).read_bytes(),b'one')

    def test_archive_adapter_targets_inactive_slot_and_does_not_overwrite(self):
        with tempfile.TemporaryDirectory() as root:
            source=Path(root)/'source';source.write_bytes(b'file')
            p=ArchivedInput(source,2,None);self.assertEqual(p.state['request'],[4,2,1,0,0])
            target=Path(root)/'target';p.export_verified(target);self.assertEqual(target.read_bytes(),b'file')
            with self.assertRaises(FileExistsError):p.export_verified(target)
