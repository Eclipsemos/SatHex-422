import json
import tempfile
import threading
import sys
import unittest
from pathlib import Path
from unittest.mock import patch
from gs422.core import parse
from gs422.sdr import PRESETS, settings
from gs422.sdr_tasks import Manager, TaskError, result_summary


class SDRTests(unittest.TestCase):
    def test_protocol_and_parameter_limits(self):
        self.assertEqual(parse(b'AT+SDR?'), ('SDR', ('STATUS',)))
        self.assertEqual(parse(b'AT+SDR=STOP,abcdef123456'), ('SDR', ('STOP', 'abcdef123456')))
        for name in PRESETS:
            self.assertEqual(parse(('AT+SDR=PRESET,' + name).encode())[0], 'SDR')
            settings(PRESETS[name])
        for command in ['AT+SDR=START;reboot', 'AT+SDR=STOP,../../etc', 'AT+SDR=RESULT,-1',
                        'AT+SDR=CONFIG,NR,2130150000,2000000,2000000,500,45,0,BROADCAST,100',
                        'AT+SDR=CONFIG,LTE,1815000000,2000000,2000000,500,45,0,DCI,100',
                        'AT+SDR=CONFIG,NR,2130150000,4000000,4000000,500,45,0,DCI,100',
                        'AT+SDR=CONFIG,LTE,1815000000,2000000,2000000,300,45,2,BROADCAST,100']:
            self.assertEqual(parse(command.encode())[0], 'SDR_ERR', command)

    def test_single_job_snapshot_cancel_and_persistence(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = {'output_root': tmp}
            m = Manager(config)
            def wait(task):
                m.cancel.wait(3)
                with m.lock:
                    task.update(state='CANCELLED', error='CANCELLED')
                    m.store(task)
            with patch.object(m, 'run', side_effect=wait):
                started = m.execute(('START',))
                self.assertIn('STATE=CHECKING', started)
                identity = m.latest
                self.assertEqual(m.execute(('START',)), '+SDR:ERR,BUSY')
                self.assertEqual(m.execute(('PRESET', 'LTE')), '+SDR:ERR,BUSY')
                self.assertIn('STATE=CHECKING', m.execute(('RESULT', identity)))
                self.assertIn('STATE=STOPPING', m.execute(('STOP', identity)))
                m.close()
            self.assertIn('STATE=CANCELLED', m.execute(('STATUS', identity)))
            self.assertIn('TECH=LTE', m.execute(('PRESET', 'LTE')))
            restored = Manager(config)
            self.assertEqual(restored.params['TECH'], 'LTE')
            self.assertEqual(restored.latest, identity)

    def test_restart_marks_interrupted_and_cleanup_failure_blocks_start(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); identity = 'abcdef123456'
            (root / identity).mkdir()
            (root / identity / 'task.json').write_text(json.dumps(
                {'id': identity, 'state': 'DECODING', 'params': settings(PRESETS['NR']),
                 'created': 0, 'error': '-'}))
            (root / 'latest.json').write_text(json.dumps({'id': identity}))
            with patch.object(Manager, 'cleanup', return_value=False):
                m = Manager({'output_root': tmp})
            self.assertEqual(m.load(identity)['state'], 'INTERRUPTED')
            self.assertEqual(m.execute(('START',)), '+SDR:ERR,CLEANUP_FAILED')

    def test_failed_partial_decoder_result_not_reported_as_success(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); (root / 'analysis').mkdir()
            (root / 'run.json').write_text('{"status":"failed"}')
            (root / 'analysis/summary.json').write_text('{"events":{"pbch":8}}')
            with self.assertRaises(TaskError):
                result_summary(root, 'NR')
            (root / 'run.json').write_text('{"status":"completed"}')
            (root / 'analysis/summary.json').write_text(json.dumps(
                {'decoder_returncodes': [0], 'events': {'pbch': 2, 'mib': 2, 'dci': 0}}))
            (root / 'analysis/results.jsonl').write_text('\n'.join(json.dumps(
                {'event': 'pbch', 'nr': {'cell_id': pci, 'crc_ok': True}}) for pci in (690, 690)))
            self.assertEqual(result_summary(root, 'NR')['PCI'], '690')
            self.assertEqual(result_summary(root, 'NR')['CELLS'], 1)

    def test_cancel_is_not_decode_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            m = Manager({'output_root': tmp})
            m.cancel.set()
            with self.assertRaisesRegex(TaskError, 'CANCELLED'):
                m.transition({'id': 'abcdef123456'}, 'DECODING')

    def test_process_cancel_timeout_and_nonzero_exit(self):
        with tempfile.TemporaryDirectory() as tmp:
            m = Manager({'output_root': tmp})
            task = {'id': 'abcdef123456', 'state': 'CAPTURING'}
            log = Path(tmp) / 'process.log'
            import os
            argv = [sys.executable, '-c', 'import time; time.sleep(30)']
            m.cancel.set()
            with self.assertRaisesRegex(TaskError, 'CANCELLED'):
                m.process(task, argv, os.environ.copy(), 60, log)
            m.cancel.clear()
            with self.assertRaisesRegex(TaskError, 'TIMEOUT'):
                m.process(task, argv, os.environ.copy(), .01, log)
            with self.assertRaisesRegex(TaskError, 'CAPTURE_FAILED'):
                m.process(task, [sys.executable, '-c', 'raise SystemExit(7)'], os.environ.copy(), 5, log)

    def test_no_signal_is_completed_summary_not_detected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); (root / 'analysis').mkdir()
            (root / 'run.json').write_text('{"status":"completed"}')
            (root / 'analysis/summary.json').write_text(json.dumps(
                {'decoder_returncodes': [0], 'events': {'pbch': 0, 'mib': 0, 'dci': 0}}))
            (root / 'analysis/results.jsonl').write_text('')
            self.assertEqual(result_summary(root, 'NR')['DETECTED'], 0)


if __name__ == '__main__':
    unittest.main()
