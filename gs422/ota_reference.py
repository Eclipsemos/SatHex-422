"""Offline two-channel reference harness, NOT a live UART or Flash service.

Caller must explicitly select the unresolved source-document interpretation.
Unknown state-query layouts, erase and load are refused, never simulated as
successful hardware operations. Frames from the RX channel never get replies.
"""
from .ota_wire import validate_control, reply, version_reply, progress_reply
from .space_protocol import Decoder


class Reference:
    def __init__(self, staging, *, version, interpretation):
        if interpretation != 'ota-doc-v1':
            raise ValueError('explicit source interpretation is required')
        version_reply(version)
        self.staging, self.version = staging, version
        self.control_decoder, self.data_decoder = Decoder(), Decoder()
        self.data_errors = 0

    def feed_upload(self, data):
        frames = self.data_decoder.feed(data)
        errors = self.data_decoder.checksum_errors + self.data_decoder.format_errors
        for _ in range(errors - self.data_errors):
            with self.staging.lock:
                if self.staging.state['gate'] and self.staging.state['status'] == 0x20:
                    self.staging._error()
        self.data_errors = errors
        for frame in frames:
            self.staging.receive(frame)
        return b''

    def feed_control(self, data):
        output = bytearray()
        for payload in self.control_decoder.feed(data):
            if len(payload) < 2 or payload[0] not in (0x30, 0x32):
                continue
            code = payload[1]
            try:
                action, args = validate_control(payload)
            except ValueError:
                output.extend(reply(code, 0xff))
                continue
            if action == 'version':
                output.extend(version_reply(self.version))
                continue
            if action == 'progress':
                output.extend(progress_reply(self.staging.snapshot()))
                continue
            if action == 'gate':
                self.staging.gate(args['enabled'])
                result = 0
            elif action == 'begin':
                result = self.staging.begin(**args)
            elif action == 'end':
                result = self.staging.end(**args)
            else:
                result = 0xff  # no Flash backend and no pretending to erase/load
            output.extend(reply(code, result))
        return bytes(output)
