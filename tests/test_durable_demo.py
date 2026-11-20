import json
from pathlib import Path
import sys
import tempfile
import unittest
from gs422.durable_demo import DurableDemo
from gs422.space_commands import CommandError


@unittest.skipUnless(sys.platform.startswith('linux'),'flock and directory fsync')
class DurableTests(unittest.TestCase):
    def make(self,root,runner=None):
        return DurableDemo(root,{},runner=runner or (lambda *args:''))

    def test_single_owner_and_persisted_identity(self):
        with tempfile.TemporaryDirectory() as root:
            one=self.make(root);prefix=one.prefix
            with self.assertRaises(BlockingIOError):self.make(root)
            one.close();two=self.make(root)
            self.assertEqual(two.prefix,prefix)
            self.assertEqual(two.version(),'UNCONFIGURED');two.close()

    def test_corrupt_identity_refuses_before_docker_operations(self):
        with tempfile.TemporaryDirectory() as root:
            one=self.make(root);one.close()
            p=Path(root)/'state.json';s=json.loads(p.read_text());s['committed']['owned']=['arbitrary;command']
            p.write_text(json.dumps(s));calls=[]
            with self.assertRaises(ValueError):self.make(root,lambda *args:calls.append(args))
            self.assertEqual(calls,[])

    def test_recovery_never_trusts_foreign_container_label(self):
        with tempfile.TemporaryDirectory() as root:
            one=self.make(root);one.close()
            cid='a'*64
            def run(*args):
                if args[0]=='ps':return cid
                if args[0]=='inspect':return json.dumps([{'Id':cid,'Config':{'Labels':{'sathex.demo':'someone-else'}}}])
                self.fail('must not mutate foreign container')
            with self.assertRaises(CommandError):self.make(root,run)

    def test_interrupted_creation_cleans_only_matching_orphan(self):
        with tempfile.TemporaryDirectory() as root:
            one=self.make(root);prefix=one.prefix;one._save('activation');one.close()
            cid='a'*64;calls=[]
            def run(*args):
                calls.append(args)
                if args[0]=='ps':return cid
                if args[0]=='inspect':return json.dumps([{'Id':cid,'Config':{'Labels':{'sathex.demo':prefix}}}])
                return ''
            two=self.make(root,run)
            self.assertIn(('rm','-f',cid),calls)
            self.assertEqual(two.owned,[]);two.close()

    def test_candidate_cleanup_preserves_retained_slot_and_foreign_paths(self):
        with tempfile.TemporaryDirectory() as root:
            b=self.make(root);b.active=2;b.retained=1;b.slots[1]={'version':'old'}
            directory=Path(root)/'candidate-1';directory.mkdir()
            (directory/'package.tar').write_bytes(b'candidate')
            foreign=Path(root)/'keep';foreign.write_text('keep')
            self.assertEqual(b.ota('erase',{'region':1,'scope':1},None),0)
            self.assertFalse((directory/'package.tar').exists())
            self.assertEqual(b.slots[1],{'version':'old'});self.assertEqual(foreign.read_text(),'keep')
            (directory/'unexpected').write_text('keep')
            self.assertEqual(b.ota('erase',{'region':1,'scope':1},None),255)
            self.assertEqual(b.ota('erase',{'region':2,'scope':1},None),255)
            b.close()
