"""Explicit 20261119 profile: legacy K/D envelope plus separate OTA channels.

The telemetry producer supplies all 44 raw fields. Missing hardware data must
be resolved by that producer, not invented here. OTA operations run outside the
receive path; telemetry reads a cached snapshot, never the database.
"""
import queue
import threading
import time
from . import tmg
from .space_protocol import Decoder, Message, encode
from .space_runtime import Controller, Adapter
from .space_commands import CommandError
from .ota_wire import validate_control, reply, version_reply


class LegacyActions:
    """Keep K/D wire semantics, but share actual OTA executor with TYPE30."""
    def __init__(self, runtime, delegate):
        self.runtime, self.delegate = runtime, delegate
        self.timeout = delegate.timeout

    def execute(self, action, parameters):
        if action == 'ota_upload' and self.runtime.legacy_upload is not None:
            import json
            with self.runtime.ota_lock:
                if self.runtime.upload_profile == 'modern' and self.runtime.staging.state['status'] == 0x20:
                    raise CommandError(6)
                self.runtime.upload_profile = 'legacy'
                try:
                    result = self.runtime.legacy_upload.receive(parameters)
                finally:
                    self.runtime._snapshot()
                return json.dumps(result).encode()
        if action == 'firmware_version':
            import json
            version = self.runtime.software_backend.version() if self.runtime.software_backend else self.runtime.version
            return json.dumps({'version': version}).encode()
        if action in ('ota_begin', 'ota_end', 'ota_erase'):
            result = self.runtime.run_ota(action[4:], parameters)
            if result:
                raise CommandError({1: 6, 2: 4, 3: 4}.get(result, 8))
            return b'{}'
        return self.delegate.execute(action, parameters)


class Revised:
    def __init__(self, staging, sensors, raw_telemetry, *, version, adapter=None, clock=time.monotonic,
                 software_backend=None, algorithms_enabled=False, legacy_upload=None):
        if staging.profile != 'CCITT-FALSE':
            raise ValueError('agreed profile requires CRC-16/CCITT-FALSE')
        tmg.pack(raw_telemetry, algorithms_enabled=algorithms_enabled)
        version_reply(version)
        self.staging, self.version, self.clock = staging, version, clock
        self.software_backend = software_backend
        self.legacy_upload = legacy_upload
        self.upload_profile = 'modern'
        self.algorithms_enabled = algorithms_enabled
        self.raw = dict(raw_telemetry)
        self.started = clock()
        self.lock = threading.RLock()
        self.ota_lock = threading.RLock()
        self.control_decoder, self.upload_decoder = Decoder(), Decoder()
        self.operations = queue.Queue(128)
        self.replies = queue.Queue(128)
        self.stop = threading.Event()
        self.thread = None
        self.control_errors = self.upload_errors = 0
        self.correct = self.incorrect = self.bus_errors = 0
        self.upload_drops = 0
        self.failure = None
        self.telemetry_deadline = None
        self.legacy = Controller(sensors, LegacyActions(self, adapter or Adapter()), clock=clock,
                                 gate_handler=self.set_legacy_gate)
        self._snapshot()

    def _snapshot(self):
        snapshot = (self.legacy_upload.snapshot() if self.upload_profile == 'legacy' and self.legacy_upload is not None
                    else self.staging.snapshot())
        with self.lock:
            self.raw.update(snapshot)
            self.raw['TMG011'] = (snapshot['TMG011'] + self.upload_drops) & 0xffffffff

    def set_legacy_gate(self, mode):
        # K1103/02 opens OTA; K1101/01 is ordinary-file mode, not OTA mode.
        # TYPE30/17 retains its independent 01=open,02=close parameter syntax.
        with self.ota_lock:
            self.staging.gate(mode == 2)
            if mode == 0 and self.legacy_upload is not None:
                self.legacy_upload.reset()
            self._snapshot()

    def run_ota(self, action, parameters):
        with self.ota_lock:
            if action == 'gate':
                self.staging.gate(parameters['enabled'])
                result = 0
            elif action == 'begin':
                if self.legacy_upload and self.legacy_upload.state['status'] == 0x20:
                    result = 1
                elif self.software_backend and not self.software_backend.permits(parameters):
                    result = 2
                else:
                    self.upload_profile = 'modern'
                    result = self.staging.begin(**parameters)
            elif action == 'end':
                result = self.staging.end(**parameters)
            elif action in ('load', 'erase') and self.software_backend:
                result = self.software_backend.ota(action, parameters, self.staging)
            else:
                result = 0xff
            self._snapshot()
            return result

    def update_telemetry(self, values):
        # Caller owns measurement freshness, UTC synchronization and missing-data policy.
        tmg.pack(values, algorithms_enabled=self.algorithms_enabled)
        with self.lock:
            self.raw.update(values)
        self._snapshot()

    def _telemetry(self, seq):
        with self.lock:
            if self.telemetry_deadline is not None and self.clock() >= self.telemetry_deadline:
                return b''  # No defined missing-value encoding: do not publish stale numbers.
            values = dict(self.raw)
            values.update(TMG001=self.correct & 255, TMG002=self.incorrect & 255,
                          TMG003=self.bus_errors & 255,
                          TMG040=int(max(0, self.clock() - self.started)) & 255)
        return Message(0x12, seq, 0, tmg.pack(values, algorithms_enabled=self.algorithms_enabled)).wire()

    def set_telemetry_deadline(self, deadline):
        with self.lock:self.telemetry_deadline=deadline

    def feed_control(self, data=b''):
        responses = bytearray()
        for payload in self.control_decoder.feed(data):
            if payload[:1] in (b'\x30', b'\x32'):
                try:
                    action, parameters = validate_control(payload)
                except ValueError:
                    self.incorrect += 1
                    if len(payload) >= 2:
                        responses.extend(reply(payload[1], 0xff))
                    continue
                self.correct += 1
                if action == 'version':
                    version = self.software_backend.version() if self.software_backend else self.version
                    responses.extend(version_reply(version))
                else:
                    try:
                        self.operations.put_nowait(('control', payload, action, parameters))
                    except queue.Full:
                        responses.extend(reply(payload[1], 1))
                continue
            try:
                message = Message.parse(payload)
            except ValueError:
                self.incorrect += 1
                continue
            if message.kind == 0x11 and message.version == 1 and message.code == 0 and not message.body:
                responses.extend(self._telemetry(message.sequence))
                continue
            if message.kind not in (0x11, 0x15):
                from .space_commands import validate
                try:
                    validate(message)
                    self.correct += 1
                except CommandError:
                    self.incorrect += 1
            answer = self.legacy.handle(payload)
            if answer is not None:
                responses.extend(encode(answer))
        errors = self.control_decoder.checksum_errors + self.control_decoder.format_errors
        self.incorrect += errors - self.control_errors
        self.bus_errors += errors - self.control_errors
        self.control_errors = errors
        return bytes(responses)

    def feed_upload(self, data=b''):
        for payload in self.upload_decoder.feed(data):
            try:
                self.operations.put_nowait(('data', payload, None, None))
            except queue.Full:
                # No data-channel acknowledgement, even on overload.
                with self.lock:
                    self.bus_errors += 1
                    self.upload_drops += 1
                    self.raw['TMG011'] = (self.raw['TMG011'] + 1) & 0xffffffff
        errors = self.upload_decoder.checksum_errors + self.upload_decoder.format_errors
        for _ in range(errors - self.upload_errors):
            try:
                self.operations.put_nowait(('error', None, None, None))
            except queue.Full:
                self.bus_errors += 1
                with self.lock:
                    self.upload_drops += 1
                    self.raw['TMG011'] = (self.raw['TMG011'] + 1) & 0xffffffff
        self.upload_errors = errors
        return b''

    def execute_next(self):
        if self.replies.full():
            return False  # preserve completed acknowledgements until control TX drains
        try:
            kind, payload, action, parameters = self.operations.get_nowait()
        except queue.Empty:
            return False
        try:
            if kind == 'data':
                with self.ota_lock:
                    before = self.staging.state['bus_errors']
                    self.staging.receive(payload)
                    self.bus_errors += (self.staging.state['bus_errors'] - before) & 255
            elif kind == 'error':
                self.bus_errors += 1
                with self.staging.lock:
                    self.staging._error()
            else:
                result = self.run_ota(action, parameters)
                self.replies.put_nowait(reply(payload[1], result))
            self._snapshot()
        finally:
            self.operations.task_done()
        return True

    def drain_replies(self):
        output = bytearray()
        while True:
            try:
                output.extend(self.replies.get_nowait())
            except queue.Empty:
                return bytes(output)

    def start(self):
        self.legacy.start()
        def worker():
            try:
                while not self.stop.is_set():
                    if not self.execute_next():
                        self.stop.wait(.001)
            except Exception as error:
                self.failure = type(error).__name__
                self.stop.set()  # owning service must fail closed, not silently hang
        self.thread = threading.Thread(target=worker, daemon=True)
        self.thread.start()

    def close(self):
        self.stop.set()
        self.legacy.close()
        if self.thread:
            self.thread.join(timeout=30)
            if self.thread.is_alive():
                raise TimeoutError('OTA operation still active; do not close its database')
