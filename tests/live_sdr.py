"""Explicit installed PTY acceptance: captures real IQ, never opens production UART."""
import json
import argparse
import os
from pathlib import Path
import pty
import re
import select
import subprocess
import tempfile
import time


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--resume-results', nargs='+', help='verify existing successful task IDs, then cancellation/restart')
    parser.add_argument('--crash-recovery', action='store_true', help='kill the task service during decode to verify restart cleanup')
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix='gs422-sdr-test-', dir='/run') as tmp:
        master, slave = pty.openpty()
        config = Path(tmp) / 'config'
        config.write_text(Path('/etc/gs422.conf').read_text().replace(
            'port=/dev/ttyS6', 'port=' + os.ttyname(slave)))
        unit = 'gs422-sdr-pty-' + str(os.getpid())
        subprocess.run(['systemd-run', '--quiet', '--collect', '--unit=' + unit,
                        '--property=ProtectHome=true', '--property=ProtectSystem=strict',
                        '--property=PrivateTmp=true', '--property=NoNewPrivileges=true',
                        '/usr/bin/gs422', '--config', str(config)], check=True)
        pending = bytearray()
        def ask(command):
            started = time.monotonic()
            os.write(master, command.encode() + b'\r\n')
            while time.monotonic() - started < 6:
                if b'\n' in pending:
                    line, _, rest = pending.partition(b'\n'); pending[:] = rest
                    reply = line.rstrip(b'\r').decode()
                    print(command, '=>', reply, flush=True)
                    return reply
                if select.select([master], [], [], .1)[0]:
                    pending.extend(os.read(master, 4096))
            raise AssertionError('timeout: ' + command)
        def start():
            reply = ask('AT+SDR=START')
            match = re.search(r'ID=([0-9a-f]{12}),STATE=CHECKING', reply)
            assert match, reply
            return match[1]
        def wait(identity, expected='DONE'):
            for _ in range(660):
                status = ask('AT+SDR=STATUS,' + identity)
                if any('STATE=' + state in status for state in ('DONE', 'FAILED', 'CANCELLED', 'INTERRUPTED')):
                    assert 'STATE=' + expected in status, status
                    return ask('AT+SDR=RESULT,' + identity)
                time.sleep(.5)
            raise AssertionError('task timeout')
        try:
            time.sleep(2)
            assert ask('AT+SDR=CONFIG,NR,2130150000,2000000,2000000,500,45,0,BROADCAST,100') == '+SDR:ERR,BAD_COMMAND'
            assert 'TECH=NR' in ask('AT+SDR=PRESET,NR')
            cancelled = start()
            assert ask('AT+SDR=START') == '+SDR:ERR,BUSY'
            assert ask('AT+SDR=PRESET,LTE') == '+SDR:ERR,BUSY'
            assert '+CPU:' in ask('AT+CPU')
            assert 'STATE=STOPPING' in ask('AT+SDR=STOP,' + cancelled)
            wait(cancelled, 'CANCELLED')
            records = []
            for identity in args.resume_results or []:
                assert re.fullmatch(r'[0-9a-f]{12}', identity)
                reply = ask('AT+SDR=RESULT,' + identity)
                assert 'STATE=DONE' in reply, reply
                records.append({'preset': 'existing', 'id': identity, 'reply': reply})
            for preset in (() if args.resume_results else ('NR', 'LTE', 'LTE_DCI')):
                assert 'CONFIG,' in ask('AT+SDR=PRESET,' + preset)
                if preset == 'NR':
                    assert 'CHANNEL=1' in ask('AT+SDR=CONFIG,NR,2130150000,4000000,4000000,500,45,1,BROADCAST,100')
                identity = start()
                assert '+STA:' in ask('AT+STA')
                result = wait(identity)
                # Signal detection is measured, not guaranteed by an AT task.
                assert 'STATE=DONE' in result and 'DETECTED=' in result, result
                task = json.loads((Path('/media/hzx/ssd/gs422-tasks') / identity / 'task.json').read_text())
                assert task['state'] == 'DONE'
                for key, value in task['result'].items():
                    assert f'{key}={value}' in result
                records.append({'preset': preset, 'id': identity, 'reply': result})
            # Cancel while Docker owns an active decoder, not just during startup.
            ask('AT+SDR=PRESET,LTE')
            identity = start()
            for _ in range(60):
                status = ask('AT+SDR=STATUS,' + identity)
                if 'STATE=DECODING' in status:
                    break
                assert 'STATE=FAILED' not in status
                time.sleep(.25)
            else:
                raise AssertionError('decode phase not reached')
            time.sleep(.5)
            assert 'STATE=STOPPING' in ask('AT+SDR=STOP,' + identity)
            wait(identity, 'CANCELLED')
            run_path = Path('/media/hzx/ssd/gs422-tasks') / identity / 'decode/run.json'
            if run_path.exists():
                cmd = json.loads(run_path.read_text())['command']
                container = cmd[cmd.index('--name') + 1]
                assert not subprocess.check_output(['docker', 'ps', '-aq', '--filter', 'name=^/' + container + '$']).strip()
            if args.crash_recovery:
                identity = start()
                for _ in range(60):
                    if 'STATE=DECODING' in ask('AT+SDR=STATUS,' + identity):
                        break
                    time.sleep(.25)
                else:
                    raise AssertionError('decode phase not reached')
                time.sleep(1)
                run_path = Path('/media/hzx/ssd/gs422-tasks') / identity / 'decode/run.json'
                cmd = json.loads(run_path.read_text())['command']
                container = cmd[cmd.index('--name') + 1]
                subprocess.run(['systemctl', 'kill', '--kill-who=all', '--signal=SIGKILL', 'gs422-sdr'], check=True)
                time.sleep(7)
                wait(identity, 'INTERRUPTED')
                assert not subprocess.check_output(['docker', 'ps', '-aq', '--filter', 'name=^/' + container + '$']).strip()
            # Verify completed results survive a broker restart.
            subprocess.run(['systemctl', 'restart', 'gs422-sdr'], check=True)
            time.sleep(1)
            for row in records:
                assert ask('AT+SDR=RESULT,' + row['id']) == row['reply']
            assert 'TECH=NR' in ask('AT+SDR=PRESET,NR')
            print('SDR_CONTROL_PTY_PASS' if args.resume_results else 'SDR_INSTALLED_PTY_PASS', json.dumps(records), flush=True)
        finally:
            # Leave no task running if an acceptance assertion fails.
            try:
                ask('AT+SDR=STOP')
            except Exception:
                pass
            subprocess.run(['systemctl', 'stop', unit], check=True)
            os.close(master); os.close(slave)


if __name__ == '__main__':
    main()
