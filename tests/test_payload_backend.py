import unittest
from gs422.payload_backend import PayloadBackend
from gs422.space_commands import CommandError


class Leaf:
    capture=None
    play_thread=store_thread=task=None
    def __init__(self):self.calls=[]
    def execute(self,action,args):self.calls.append(action);return b'{}'
    def ota(self,action,args,staging):self.calls.append(action);return 0
    def telemetry(self):return {}
    def permits(self,args):return True
    def version(self):return 'v1'


class CompositionTests(unittest.TestCase):
    def setUp(self):
        self.software=Leaf();self.files=Leaf();self.reboot=Leaf()
        self.b=PayloadBackend(self.software,self.files,reboot=self.reboot)

    def test_action_ownership_and_unknown_rejection(self):
        self.b.execute('storage_clean',{});self.b.execute('task_start',{})
        self.assertEqual(self.files.calls,['storage_clean'])
        self.assertEqual(self.software.calls,['task_start'])
        with self.assertRaises(CommandError):self.b.execute('arbitrary',{})

    def test_active_capture_blocks_upgrade_and_reboot(self):
        self.files.capture=1
        self.assertEqual(self.b.ota('load',{},None),1)
        with self.assertRaises(CommandError) as e:self.b.execute('board_reboot',{})
        self.assertEqual(e.exception.error,6)
        self.assertEqual(self.software.calls,[]);self.assertEqual(self.reboot.calls,[])

    def test_running_algorithm_blocks_reboot(self):
        self.software.task='task'
        self.software._state=lambda _: {'Running':True}
        with self.assertRaises(CommandError) as e:self.b.execute('board_reboot',{})
        self.assertEqual(e.exception.error,6)
        self.assertFalse(self.reboot.calls)

    def test_reboot_requires_binding_and_idle_state(self):
        self.b.execute('board_reboot',{})
        self.assertEqual(self.reboot.calls,['board_reboot'])
        with self.assertRaises(CommandError):PayloadBackend(self.software,self.files).execute('board_reboot',{})
