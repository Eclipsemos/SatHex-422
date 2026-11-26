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
from .space_commands import CommandError, SHORT_CODES
from .ota_wire import InvalidStorageRegion, validate_control, reply, version_reply, progress_reply


class LegacyActions:
    """Keep K/D wire semantics, but share actual OTA executor with TYPE30."""
    def __init__(self, runtime, delegate):
        self.runtime, self.delegate = runtime, delegate
        self.timeout = delegate.timeout

    def execute(self, action, parameters):
        if action == 'ordinary_gate':
            self.runtime.set_channel_gate(1, parameters['enabled'])
            return b'{}'
        if action == 'ota_upload' and self.runtime.legacy_upload is not None:
            import json
            with self.runtime.ota_lock:
                if self.runtime.upload_profile == 'modern' and self.runtime.staging.state['status'] in (0x20, 0x21):
                    raise CommandError(6)
                self.runtime._set_erase_status(None)
                try:
                    result = self.runtime.legacy_upload.receive(parameters)
                    self.runtime._select_upload('legacy')
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
    staging_fields = frozenset(f'TMG{i:03}' for i in range(8, 15))

    def __init__(self, staging, sensors, raw_telemetry, *, version, adapter=None, clock=time.monotonic,
                 software_backend=None, algorithms_enabled=False, legacy_upload=None, ota101=False, elapsed_clock=None):
        self.ota101 = ota101
        if staging.profile != ('XMODEM' if ota101 else 'CCITT-FALSE'):
            raise ValueError('file CRC profile does not match selected wire revision')
        tmg.pack(raw_telemetry, algorithms_enabled=algorithms_enabled)
        version_reply(version)
        self.staging, self.version, self.clock = staging, version, clock
        self.software_backend = software_backend
        self.legacy_upload = legacy_upload
        self.upload_profile = staging.state.get('upload_profile', 'modern')
        self.algorithms_enabled = algorithms_enabled
        self.raw = dict(raw_telemetry)
        self.elapsed_clock = clock if elapsed_clock is None else elapsed_clock
        self.utc_anchor = None  # (broadcast Unix seconds, local reception time)
        self.erase_status = (0 if self.upload_profile == 'legacy' and legacy_upload is not None
                            and staging.state.get('loaded_legacy') == legacy_upload.state['file_id'] else None)
        self.started = clock()
        self.lock = threading.RLock()
        self.ota_lock = threading.RLock()
        self.control_decoder, self.upload_decoder = Decoder(ota101=ota101), Decoder(ota101=ota101)
        self.gate_epoch = self.decoder_epoch = 0
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
                                 gate_handler=self.set_legacy_gate, split_gate=ota101)
        if staging.state['gate']:
            self.legacy.receive_mode = 1 if staging.ordinary_gate else 2
        self._snapshot()

    def _select_upload(self, profile):
        if self.upload_profile != profile:
            with self.staging.lock:
                self.staging.state['upload_profile'] = profile
                self.staging._save()
                self.upload_profile = profile

    def _snapshot(self):
        snapshot = (self.legacy_upload.snapshot() if self.upload_profile == 'legacy' and self.legacy_upload is not None
                    else self.staging.snapshot())
        with self.lock:
            self.raw.update(snapshot)
            if self.erase_status is not None:
                self.raw['TMG014'] = self.erase_status
            self.raw['TMG011'] = (snapshot['TMG011'] + self.upload_drops) & 0xffffffff

    def set_legacy_gate(self, mode):
        # K1103/02 opens OTA; K1101/01 is ordinary-file mode, not OTA mode.
        # TYPE30/17 retains its independent 01=open,02=close parameter syntax.
        with self.ota_lock:
            previous = (self.staging.state['gate'], self.staging.ordinary_gate)
            self.staging.gate(mode != 0, ordinary=mode == 1)
            if previous != (self.staging.state['gate'], self.staging.ordinary_gate):
                self.gate_epoch += 1
            if mode == 0 and self.legacy_upload is not None:
                self.legacy_upload.reset()
                self._select_upload('modern')
            self._snapshot()

    def set_channel_gate(self, channel, enabled):
        with self.ota_lock:
            current = 1 if self.staging.ordinary_gate else 2
            if not enabled and self.staging.state['gate'] and current != channel:
                return
            mode = channel if enabled else 0
            self.set_legacy_gate(mode)
            self.legacy.receive_mode = mode

    def run_ota(self, action, parameters):
        try:
            return self._run_ota(action, parameters)
        except CommandError:
            self._set_erase_status(0x40)
        except Exception:
            import logging
            logging.getLogger(__name__).exception('Unexpected OTA execution error')
            self._set_erase_status(0xff)
        return 0xff

    def _run_ota(self, action, parameters):
        with self.ota_lock:
            if action == 'gate':
                self.set_channel_gate(2, parameters['enabled'])
                result = 0
            elif action == 'begin':
                if self.legacy_upload and self.legacy_upload.state['status'] == 0x20:
                    result = 1
                elif not self.staging.ordinary_gate and self.software_backend and not self.software_backend.permits(parameters):
                    result = 2
                else:
                    result = self.staging.begin(**parameters)
                    if result == 0:
                        self._select_upload('modern')
            elif action == 'end':
                result = self.staging.end(**parameters, publish=self._set_erase_status)
            elif action == 'erase' and self.software_backend:
                self._set_erase_status(0x10)
                result = self.software_backend.ota(action, parameters, self.staging)
                self._set_erase_status(0x11 if result == 0 else 0x40)
            elif action == 'load' and self.software_backend:
                if parameters['index'] == 0 and self.upload_profile != 'legacy' and self.staging.ordinary_file:
                    return 0xff
                previous = self.raw['TMG014']
                self._set_erase_status(0x30)
                if self.upload_profile == 'legacy' and parameters['index'] == 0:
                    activate = getattr(self.software_backend, 'activate_legacy', None)
                    result = activate(self.legacy_upload) if activate else 0xff
                    if result == 0:
                        with self.staging.lock:
                            self.staging.state['loaded_legacy'] = self.legacy_upload.state['file_id']
                            self.staging._save()
                    self._set_erase_status(0x31 if result == 0 else previous if result == 1 else 0x40)
                    self._snapshot()
                    return result
                result = self.software_backend.ota(action, parameters, self.staging)
                if result:
                    self._set_erase_status(previous if result == 1 else 0x40)
            else:
                result = 0xff
            if (action in ('begin','end','load') and result == 0) or (action == 'end' and self.staging.state['status'] in (0x24, 0x40)):
                self._set_erase_status(None)
            self._snapshot()
            return result

    def _set_erase_status(self, value):
        with self.lock:
            self.erase_status = value
            if value is not None:
                self.raw['TMG014'] = value
                if value == 0x22:
                    self.raw['TMG013'] = 1

    def update_telemetry(self, values):
        # Caller owns measurement freshness, UTC synchronization and missing-data policy.
        tmg.pack(values, algorithms_enabled=self.algorithms_enabled)
        with self.lock:
            self.raw.update({key: value for key, value in values.items()
                             if key != 'TMG004' and key not in self.staging_fields})
        self._snapshot()

    def _telemetry(self, seq=None, category=b''):
        with self.lock:
            values = dict(self.raw)
            if self.telemetry_deadline is not None and self.clock() >= self.telemetry_deadline:
                values.update(tmg.INVALID)
            utc = 0
            if self.utc_anchor is not None:
                seconds, received_at = self.utc_anchor
                utc = (seconds + int(self.elapsed_clock() - received_at)) & 0xffffffff
            values.update(TMG004=utc,
                          TMG001=self.correct & 255, TMG002=self.incorrect & 255,
                          TMG003=self.bus_errors & 255,
                          TMG040=int(max(0, self.clock() - self.started)) & 255)
        body = tmg.pack(values, algorithms_enabled=self.algorithms_enabled)
        if seq is None:
            return encode(b'\x12' + category + body)
        return Message(0x12, seq, 0, body).wire()

    def set_telemetry_deadline(self, deadline):
        with self.lock:self.telemetry_deadline=deadline

    def feed_control(self, data=b''):
        responses = bytearray()
        for payload in self.control_decoder.feed(data):
            if payload == b'\x11':
                responses.extend(self._telemetry())
                continue
            if (payload[:1] == b'\x11' and len(payload) in (2, 8)
                    and payload[1] == 0 and not any(payload[2:])):
                responses.extend(self._telemetry(category=payload[1:2]))
                continue
            if len(payload) >= 2 and payload[0] == 0x21 and payload[1] in SHORT_CODES:
                if self.legacy.handle_short(payload):
                    self.correct += 1
                else:
                    self.incorrect += 1
                continue
            if payload[:1] == b'\xcc':
                # Proposed broadcast subtype 01. No sequence or acknowledgement.
                if len(payload) == 6 and payload[1] == 1:
                    with self.lock:
                        self.utc_anchor = (int.from_bytes(payload[2:], 'big'), self.elapsed_clock())
                else:
                    self.incorrect += 1
                continue
            if payload[:1] in (b'\x30', b'\x32'):
                if self.ota101 and payload == bytes((0x32, 0x14)):
                    self.correct += 1
                    with self.lock:
                        responses.extend(encode(bytes((0x33,self.raw['TMG014'])), ota101=True))
                    continue
                try:
                    action, parameters = validate_control(payload)
                except ValueError as error:
                    self.incorrect += 1
                    if len(payload) >= 2:
                        result = 2 if isinstance(error, InvalidStorageRegion) else 0xff
                        responses.extend(reply(payload[1], result, ota101=self.ota101))
                    continue
                self.correct += 1
                if action == 'version':
                    version = self.software_backend.version() if self.software_backend else self.version
                    responses.extend(version_reply(version, ota101=self.ota101))
                elif action == 'progress':
                    with self.lock:responses.extend(progress_reply(self.raw, ota101=self.ota101))
                else:
                    try:
                        self.operations.put_nowait(('control', payload, action, parameters))
                    except queue.Full:
                        responses.extend(reply(payload[1], 1, ota101=self.ota101))
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
                    validate(message, split_gate=self.ota101)
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
        epoch = self.gate_epoch
        if epoch != self.decoder_epoch:
            self.upload_decoder.clear()
            self.decoder_epoch = epoch
        for payload in self.upload_decoder.feed(data):
            try:
                self.operations.put_nowait(('data', payload, epoch, None))
            except queue.Full:
                # No data-channel acknowledgement, even on overload.
                with self.lock:
                    self.bus_errors += 1
                    self.upload_drops += 1
                    self.raw['TMG011'] = (self.raw['TMG011'] + 1) & 0xffffffff
        errors = self.upload_decoder.checksum_errors + self.upload_decoder.format_errors
        for _ in range(errors - self.upload_errors):
            try:
                self.operations.put_nowait(('error', None, epoch, None))
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
                    if action != self.gate_epoch:
                        return True
                    before = self.staging.state['bus_errors']
                    self.staging.receive(payload)
                    self.bus_errors += (self.staging.state['bus_errors'] - before) & 255
            elif kind == 'error':
                with self.ota_lock, self.staging.lock:
                    if action == self.gate_epoch:
                        self.bus_errors += 1
                        self.staging._error()
            else:
                result = self.run_ota(action, parameters)
                self.replies.put_nowait(reply(payload[1], result, ota101=self.ota101))
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
