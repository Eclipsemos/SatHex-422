"""Bounded execution and observable command state for the experimental protocol."""
from collections import OrderedDict, deque
import json
import os
import queue
import selectors
import signal
import struct
import subprocess
import threading
import time

from .space_commands import CommandError, validate
from .space_protocol import Message, Reassembler


class Adapter:
    """Run only administrator-configured local argv, never shell text from the wire."""
    def __init__(self, actions=None, timeout=5):
        self.actions = actions or {}
        self.timeout = timeout
        if not 0 < timeout <= 30:
            raise ValueError('adapter timeout must be 0..30 seconds')
        for name, argv in self.actions.items():
            if not isinstance(name, str) or not isinstance(argv, list) or not argv or not all(isinstance(x, str) for x in argv) or not os.path.isabs(argv[0]):
                raise ValueError('adapter argv must begin with an absolute executable')

    def execute(self, action, parameters):
        if action not in self.actions:
            raise CommandError(7)
        p = subprocess.Popen(self.actions[action], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                             stderr=subprocess.DEVNULL, start_new_session=True)
        output = bytearray()
        deadline = time.monotonic() + self.timeout
        try:
            request = json.dumps({'action': action, 'parameters': parameters}, separators=(',', ':')).encode()
            if len(request) > 4096:
                raise CommandError(4)
            p.stdin.write(request)
            p.stdin.close()
            os.set_blocking(p.stdout.fileno(), False)
            with selectors.DefaultSelector() as selector:
                selector.register(p.stdout, selectors.EVENT_READ)
                while True:
                    if time.monotonic() >= deadline:
                        raise CommandError(9)
                    if not selector.select(min(.05, max(0, deadline - time.monotonic()))):
                        continue
                    chunk = os.read(p.stdout.fileno(), 4096)
                    if not chunk:
                        break
                    output.extend(chunk)
                    if len(output) > 4096:
                        raise CommandError(11)
            try:
                status = p.wait(timeout=max(.001, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                raise CommandError(9)
            result = json.loads(output)
            if status != 0 or not isinstance(result, dict) or result.get('ok') is not True:
                raise CommandError(8)
            data = json.dumps(result.get('data', {}), ensure_ascii=True, separators=(',', ':')).encode()
            if len(data) > 490:
                raise CommandError(11)
            return data
        except CommandError:
            raise
        except (OSError, ValueError, TypeError):
            raise CommandError(8)
        finally:
            # Adapters dispatch into managed services; grandchildren must not escape.
            try:
                os.killpg(p.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            try:
                p.wait(timeout=.2)
            except subprocess.TimeoutExpired:
                pass
            if not p.stdin.closed:
                p.stdin.close()
            p.stdout.close()


class Controller:
    def __init__(self, sensors, adapter=None, clock=time.monotonic):
        self.sensors, self.adapter, self.clock = sensors, adapter or Adapter(), clock
        self.started = clock()
        self.lock = threading.RLock()
        self.rx = self.executed = self.failures = self.resets = self.dropped = 0
        self.checksum_errors = self.format_errors = 0
        self.latest = (0, 0, 0, 0)
        self.receive_mode = 0
        self.results = deque(maxlen=64)
        self.seen = OrderedDict()
        self.work = queue.Queue(maxsize=16)
        self.fragments = Reassembler(clock=clock)
        self.stop = threading.Event()
        self.thread = None

    def start(self):
        self.thread = threading.Thread(target=self._worker, daemon=True)
        self.thread.start()

    def close(self):
        self.stop.set()
        if self.thread:
            self.thread.join(timeout=self.adapter.timeout + 1)

    def _result(self, message, status, error=0, data=b''):
        with self.lock:
            self.latest = (message.sequence, message.code, status, error)
            if status in (4, 5):
                self.failures += 1
            if status == 3:
                self.executed += 1
            if len(self.results) == self.results.maxlen:
                self.dropped += 1
            self.results.append((message.sequence, message.code, status, error, data))
            entry = self.seen.get(message.sequence)
            if entry and entry[0] == message.payload():
                self.seen[message.sequence] = (entry[0], True)

    def tick(self):
        with self.lock:
            expired = self.fragments.expire()
            if expired:
                self._result(expired, 4, 10)

    def telemetry(self, message):
        values = [self.sensors.value(i) for i in range(4)]
        valid = sum(1 << i for i, v in enumerate(values) if v is not None and -(1 << 31) <= v < (1 << 31))
        values = [v if valid & (1 << i) else 0 for i, v in enumerate(values)]
        counters = [max(0, int(self.clock() - self.started)), self.rx, self.executed,
                    self.checksum_errors, self.format_errors, self.failures, self.resets, self.dropped]
        body = struct.pack('>8IHBBHH4iBBH', *(v & 0xffffffff for v in counters),
                           *self.latest, valid, *values, self.receive_mode, self.work.qsize(), 0)
        return Message(0x12, message.sequence, 0, body).payload()

    def handle(self, payload):
        with self.lock:
            self.tick()
            try:
                message = Message.parse(payload)
            except ValueError:
                self.format_errors += 1
                return None
            if message.kind not in (0x11, 0x15):
                self.rx += 1
            if message.version != 1:
                self._result(message, 4, 2)
                return None
            if message.kind in (0x11, 0x15):
                if message.code != 0 or message.body:
                    self._result(message, 4, 1)
                    return None
                if message.kind == 0x11:
                    return self.telemetry(message)
                if not self.results:
                    body = b'\x00'
                else:
                    seq, code, status, error, data = self.results.popleft()
                    body = b'\x01' + struct.pack('>HBBHH', seq, code, status, error, len(data)) + data
                return Message(0x16, message.sequence, 0, body).payload()
            if message.kind == 0x29:
                try:
                    message = self.fragments.add(message)
                except ValueError:
                    self._result(message, 4, 10)
                    return None
                if message is None:
                    return None
            previous = self.seen.get(message.sequence)
            if previous:
                if previous[0] != message.payload():
                    self._result(message, 4, 5)
                return None
            try:
                action, parameters = validate(message)
            except CommandError as error:
                self._result(message, 4, error.error)
                return None
            # Never evict pending/running operations from the replay window.
            if len(self.seen) >= 128:
                completed = next((k for k, v in self.seen.items() if v[1]), None)
                if completed is None:
                    self._result(message, 4, 6)
                    return None
                del self.seen[completed]
            try:
                self.work.put_nowait((message, action, parameters))
            except queue.Full:
                self._result(message, 4, 6)
                return None
            self.seen[message.sequence] = (message.payload(), False)
            self.latest = (message.sequence, message.code, 1, 0)
            return None

    def execute_next(self, timeout=0):
        try:
            message, action, parameters = self.work.get(timeout=timeout)
        except queue.Empty:
            return False
        with self.lock:
            self.latest = (message.sequence, message.code, 2, 0)
        try:
            if action == 'receive_gate':
                with self.lock:
                    self.receive_mode = parameters['mode']
                data = b''
            else:
                if action == 'ota_upload' and self.receive_mode != 2:
                    raise CommandError(4)
                data = self.adapter.execute(action, parameters)
            self._result(message, 3, data=data)
        except CommandError as error:
            self._result(message, 5, error.error)
        except Exception:
            self._result(message, 5, 8)
        finally:
            self.work.task_done()
        return True

    def _worker(self):
        while not self.stop.is_set():
            self.execute_next(timeout=.05)
