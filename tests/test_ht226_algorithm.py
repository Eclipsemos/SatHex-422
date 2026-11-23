import json
import tempfile
import unittest
from unittest.mock import Mock

from gs422 import tmg
from gs422.core import Cache
from gs422.file_services import FileServices
from gs422.ota_staging import Staging
from gs422.payload_backend import PayloadBackend
from gs422.revised_runtime import Revised
from gs422.space_commands import CommandError, validate, validate_short
from gs422.space_protocol import Decoder, Message, encode
from gs422.task_scheduler import Scheduler


class AlgorithmTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.files = FileServices(tmp.name)
        self.addCleanup(self.files.close)
        self.sw = Mock(owned=['fixture-cid'])
        self.sw._inspect_owned.return_value = {'Config':{'Cmd':['service']}}
        self.sw._create.return_value = 'fixture-cid'
        self.sw._state.return_value = {'Running': True}
        self.jobs = Scheduler(self.sw, {'1':{'ht226_image':'pinned'}}, self.files)
        stage = Staging(tmp.name,crc_profile='XMODEM',capacity=2048)
        self.addCleanup(stage.close)
        self.rt = Revised(stage,Cache(10),dict.fromkeys(tmg.BY_ID,0),version='test',ota101=True,
                          algorithms_enabled=True,adapter=PayloadBackend(self.sw,self.files,scheduler=self.jobs))
        self.addCleanup(self.rt.close)

    def send(self, words):
        self.assertFalse(self.rt.feed_control(encode(bytes([0x21,0x83,*words]))))
        self.rt.legacy.execute_next()

    def poll(self):
        reply = self.rt.feed_control(Message(0x15,1,0).wire())
        return Message.parse(Decoder().feed(reply)[0]).body

    def test_parameters_run_completion_and_result_file(self):
        words = [1,0,17,254,128,255]
        self.send(words)
        self.sw._create.assert_called_once_with('pinned','algorithm',words=tuple(words[1:]))
        self.assertEqual(self.poll(),b'\0')
        self.assertEqual(self.jobs.telemetry()['TMG032'],1)
        self.send(words)
        self.assertEqual(self.poll()[4],5)  # busy, original job continues
        self.sw._state.return_value = {'Running':False,'ExitCode':0}
        self.sw.runner.return_value = '{"value":89,"words":[0,17,254,128,255]}'
        values = self.jobs.telemetry()
        self.assertEqual((values['TMG032'],values['TMG017']),(3,1))
        reply = self.poll()
        detail = json.loads(reply[9:])
        self.assertEqual((reply[4],detail['value'],detail['app_exit'],detail['words']),
                         (3,89,0,bytes(words).hex()))
        raw = self.files.db.execute('select body from frames where file=?',(detail['file_id'],)).fetchone()[0]
        self.assertEqual(json.loads(raw)['words'],words[1:])

    def test_registered_app_and_parameter_bounds(self):
        for i in range(5):
            for value in range(256):
                words = bytearray(6)
                words[i+1] = value
                self.assertEqual(validate_short(0x83,words)[1]['words'],tuple(words[1:]))
        for words in (bytes(5),bytes(7),bytes([16])+bytes(5)):
            with self.assertRaises(CommandError):
                validate_short(0x83,words)
        self.send([2,0,0,0,0,0])
        self.assertEqual(self.poll()[4],5)
        self.sw._create.assert_not_called()
        with self.assertRaises(CommandError):
            validate(Message(0x21,1,0x83,bytes.fromhex('010203040506')))
        self.assertEqual(validate(Message(0x21,1,0x83,bytes.fromhex('010002020000')))[0],'task_start')

    def test_process_failure_returns_real_exit(self):
        self.send([1]*6)
        self.sw._state.return_value = {'Running':False,'ExitCode':17}
        self.assertEqual(self.jobs.telemetry()['TMG032'],2)
        reply = self.poll()
        info = json.loads(reply[9:])
        self.assertEqual((reply[4],info['app_exit']),(5,17))
        self.assertNotIn('file_id',info)

    def test_bad_output_cannot_be_success(self):
        self.send([1]*6)
        self.sw._state.return_value = {'Running':False,'ExitCode':0}
        self.sw.runner.return_value = 'no result'
        self.assertEqual(self.jobs.telemetry()['TMG032'],2)
        self.assertEqual(self.poll()[4],5)
