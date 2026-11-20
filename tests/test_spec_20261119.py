"""Acceptance against the 23 source rows and 44 telemetry fields.

These tests establish software encoding/validation and staging behavior only.
They do not establish real algorithm/Flash/solid-state/payload integration.
"""
import json
from pathlib import Path
import struct
import tempfile
import unittest

from gs422 import tmg
from gs422.space_commands import validate, CommandError
from gs422.space_protocol import Message, Decoder
from gs422.ota_wire import crc16, control, validate_control, file_packets, parse_packet, expected_packets, reply, version_reply
from gs422.ota_staging import Staging
from gs422.ota_reference import Reference

ROOT = Path(__file__).resolve().parents[1]
SPEC = json.loads((ROOT / 'docs/spec-20261119.json').read_text(encoding='utf-8'))


def ota_old():
    return struct.pack('>IIIIIB', 1, 0, 1, 1, 1, 0) + b'x' + bytes(4)


CASES = {
    'K1101': (0x21, 0x17, b'\x01' + bytes(5), 'receive_gate'),
    'K1102': (0x21, 0x17, bytes(6), 'receive_gate'),
    'K1103': (0x21, 0x17, b'\x02' + bytes(5), 'receive_gate'),
    'K1104': (0x21, 0x83, bytes([1, 0, 1, 2, 0, 0]), 'task_start'),
    'K1105': (0x21, 0x81, b'\x01' + bytes(5), 'task_stop'),
    'K1106': (0x21, 0x85, b'\x01' + bytes(5), 'task_pause'),
    'K1107': (0x21, 0x87, b'', 'software_resume'),
    'K1108': (0x21, 0x80, b'', 'software_stop'),
    'K1109': (0x21, 0x84, b'', 'board_reboot'),
    'K1110': (0x21, 0x13, b'', 'storage_clean'),
    'K1111': (0x21, 0x42, b'', 'playback_start'),
    'K1112': (0x21, 0x43, b'', 'playback_stop'),
    'K1113': (0x21, 0x51, b'', 'storage_stop'),
    'K1114': (0x21, 0x52, b'', 'storage_status'),
    'K1115': (0x21, 0x32, bytes([1, 0, 1, 0, 0, 0]), 'capture_control'),
    'K1116': (0x21, 0x53, bytes([3, 1, 2, 0, 0, 0]), 'ota_begin'),
    'K1117': (0x21, 0x54, bytes(6), 'ota_end'),
    'K1118': (0x21, 0x55, bytes([1, 1, 0, 0, 0, 0]), 'ota_erase'),
    'K1119': (0x21, 0x56, b'', 'firmware_version'),
    'D0351': (0x25, 4, ota_old(), 'ota_upload'),
    'D0352': (0x25, 0x40, struct.pack('>BBIIIH', 1, 1, 0, 10, 100, 0), 'playback_link'),
    'D0353': (0x25, 0x41, struct.pack('>IIIBH', 1, 0, 10, 1, 0), 'playback_file'),
    'D0354': (0x25, 0x50, bytes([1, 0x34, 0x12, 0, 1, 0]), 'storage_start'),
}


class SourceCoverageTests(unittest.TestCase):
    def test_all_23_source_rows_have_individual_cases(self):
        self.assertEqual(set(CASES), {r['id'] for r in SPEC['commands']})
        self.assertEqual(len(CASES), 23)

    def test_exact_codes_and_field_lengths_match_extracted_sources(self):
        for row in SPEC['commands']:
            self.assertEqual(int(row['code'].removesuffix('H'), 16), CASES[row['id']][1])
        self.assertEqual([(r['id'], r['bits']) for r in SPEC['telemetry']], [(f.id, f.bits) for f in tmg.FIELDS])

    def test_all_offsets_match_independent_ht226_sheet(self):
        rows = [r['cells'] for r in SPEC['ht226_telemetry_reference'] if str(r['cells'].get('A', '')).startswith('r0')]
        self.assertEqual(len(rows), 44)
        for field, row in zip(tmg.FIELDS, rows):
            self.assertEqual((field.bits, field.byte, field.high_bit), (row['D'], row['G'], row['H']))


def command_test(identifier):
    def check(self):
        kind, code, data, action = CASES[identifier]
        self.assertEqual(validate(Message(kind, 1, code, data))[0], action)
        with self.assertRaises(CommandError):
            validate(Message(kind, 1, code, data + b'\x01'))
    return check


for identifier in CASES:
    setattr(SourceCoverageTests, 'test_command_' + identifier, command_test(identifier))


class TelemetryTests(unittest.TestCase):
    def zeros(self):
        return {f.id: 0 for f in tmg.FIELDS}

    def test_independent_offsets_and_app_order(self):
        self.assertEqual(tmg.SIZE, 61)
        self.assertEqual((tmg.BY_ID['TMG004'].byte, tmg.BY_ID['TMG004'].bits), (3, 32))
        self.assertEqual(tmg.BY_ID['TMG018'].byte, 33)
        self.assertEqual(tmg.BY_ID['TMG034'].byte, 37)
        self.assertEqual(tmg.BY_ID['TMG044'].byte, 60)
        values = self.zeros()
        values.update(TMG004=0x12345678, TMG018=1, TMG019=2, TMG020=3)
        data = tmg.pack(values, algorithms_enabled=True)
        self.assertEqual(data[3:7], b'\x12\x34\x56\x78')
        self.assertEqual(data[33:37], b'\x6c\x00\x00\x00')  # APP15,14,13,12 from MSB

    def test_all_fields_round_trip_and_overflow(self):
        for field in tmg.FIELDS:
            with self.subTest(field=field.id):
                values = self.zeros()
                maximum = (1 << field.bits) - 1
                values[field.id] = 0 if field.id in tmg.RESERVED else min(maximum, 100) if field.id == 'TMG012' else maximum
                self.assertEqual(tmg.unpack(tmg.pack(values, algorithms_enabled=True)), values)
                values[field.id] = maximum + 1
                with self.assertRaises(ValueError):
                    tmg.pack(values, algorithms_enabled=True)

    def test_absent_unknown_negative_reserved_and_algorithm_values_rejected(self):
        values = self.zeros()
        for invalid in (None, -1, 1.5, True):
            values['TMG005'] = invalid
            with self.assertRaises(ValueError):
                tmg.pack(values)
        values = self.zeros()
        values['TMG018'] = 1
        with self.assertRaises(ValueError):
            tmg.pack(values)
        values = self.zeros()
        values['TMG042'] = 1
        with self.assertRaises(ValueError):
            tmg.pack(values)
        with self.assertRaises(ValueError):
            tmg.pack({})

    def test_scales_word_order_and_counter_wrap(self):
        self.assertEqual(tmg.raw_value('TMG005', '12.345'), 1235)
        self.assertEqual(tmg.raw_value('TMG034', '12.00'), 1200)
        self.assertEqual(tmg.raw_value('TMG006', '47.6'), 48)
        with self.assertRaises(ValueError):
            tmg.raw_value('TMG007', -5)
        values = self.zeros()
        values.update(tmg.split_storage_bytes(0x123456789abcdef0))
        data = tmg.pack(values)
        self.assertEqual(data[48:56], bytes.fromhex('9abcdef012345678'))
        values['TMG040'] = 255
        tmg.increment(values, 'TMG040')
        self.assertEqual(values['TMG040'], 0)


class OtaWireTests(unittest.TestCase):
    def test_crc_known_answers(self):
        for profile, expected in [('CCITT-FALSE', 0x29b1), ('XMODEM', 0x31c3), ('MODBUS', 0x4b37)]:
            self.assertEqual(crc16(b'123456789', profile), expected)
        with self.assertRaises(ValueError):
            crc16(b'', None)

    def test_control_short_parameters_and_distinct_17_semantics(self):
        self.assertEqual(validate_control(b'\x30\x17\x02' + bytes(5)), ('gate', {'enabled': False}))
        self.assertEqual(validate(Message(0x21, 1, 0x17, b'\x02' + bytes(5)))[1], {'mode': 2})
        for code, body in [(4, bytes(6)), (0x53, bytes([3, 1, 1, 0, 0, 0])),
                           (0x54, bytes(6)), (0x55, bytes([1, 1, 0, 0, 0, 0])), (0x56, bytes(6))]:
            payload = Decoder().feed(control(0x30, code, body))[0]
            validate_control(payload)
            with self.assertRaises(ValueError):
                validate_control(payload[:-1] + b'\x01')
        with self.assertRaises(ValueError):
            validate_control(b'\x30\x04\x00\x81' + bytes(4))

    def test_max_frame_metadata_padding_and_endianness(self):
        frames = list(file_packets(b'A' * 494, 'XMODEM'))
        self.assertEqual(len(frames), 2)
        self.assertTrue(all(len(f) == 512 for f in frames))
        seq, body = parse_packet(Decoder().feed(frames[0])[0])
        self.assertEqual(seq, 0)
        self.assertEqual(body[:4], bytes.fromhex('000001ee'))
        seq, last = parse_packet(Decoder().feed(frames[1])[0])
        self.assertEqual(seq, 1)
        self.assertEqual(last, b'A' + bytes(498))
        self.assertEqual(expected_packets(493), 1)
        self.assertEqual(expected_packets(494), 2)
        self.assertEqual(expected_packets(0xffffffff), (0xffffffff + 504) // 499)
        self.assertEqual(Decoder().feed(reply(0x54, 0x11)), [b'\x31\x54\x11'])
        self.assertEqual(Decoder().feed(version_reply('V1.2.3')), [b'\x33\x56V1.2.3\x00'])


class StagingTests(unittest.TestCase):
    def receiver(self, directory, profile='CCITT-FALSE', capacity=10000):
        return Staging(directory, crc_profile=profile, capacity=capacity)

    def open_transfer(self, r):
        r.gate(True)
        self.assertEqual(r.begin(3, 1, 2), 0)

    def payloads(self, data, profile='CCITT-FALSE'):
        return [Decoder().feed(x)[0] for x in file_packets(data, profile)]

    def test_resume_gap_duplicate_and_export_actual_bytes(self):
        with tempfile.TemporaryDirectory() as folder:
            data = bytes(range(256)) * 7
            packets = self.payloads(data)
            r = self.receiver(folder)
            self.open_transfer(r)
            self.assertTrue(r.receive(packets[0]))
            self.assertTrue(r.receive(packets[2]))
            self.assertEqual(r.snapshot()['TMG009'], 1)
            self.assertTrue(r.receive(packets[2]))
            self.assertEqual(r.snapshot()['TMG010'], 2)
            self.assertEqual(r.end(), 1)
            r.close()
            r = self.receiver(folder)
            self.assertEqual(r.snapshot()['TMG009'], 1)
            self.assertEqual(r.begin(3, 1, 2), 0)  # start retry preserves accepted packets
            self.assertEqual(r.snapshot()['TMG010'], 2)
            for packet in packets[1:]:
                self.assertTrue(r.receive(packet))
            self.assertEqual(r.end(), 0)
            self.assertEqual(r.end(), 0)
            self.assertEqual(r.snapshot()['TMG012'], 100)
            self.assertEqual(r.snapshot()['TMG013'], 4)
            self.assertEqual(r.snapshot()['TMG014'], 0x23)
            dest = Path(folder) / 'verified.bin'
            r.export_verified(dest)
            self.assertEqual(dest.read_bytes(), data)
            with self.assertRaises(FileExistsError):
                r.export_verified(dest)
            r.close()

    def test_all_crc_profiles_and_first_packet_boundary(self):
        for profile in ('CCITT-FALSE', 'XMODEM', 'MODBUS'):
            for size in (1, 493, 494, 992, 993):
                with self.subTest(profile=profile, size=size), tempfile.TemporaryDirectory() as folder:
                    r = self.receiver(folder, profile)
                    self.open_transfer(r)
                    for packet in self.payloads(b'x' * size, profile):
                        self.assertTrue(r.receive(packet))
                    self.assertEqual(r.end(), 0)
                    r.close()

    def test_gate_capacity_conflict_padding_crc_and_abort(self):
        with tempfile.TemporaryDirectory() as folder:
            r = self.receiver(folder)
            self.assertEqual(r.begin(3, 1, 2), 0xff)
            self.open_transfer(r)
            packets = self.payloads(b'hello')
            self.assertFalse(r.receive(packets[0][:-1] + b'\x01'))
            corrupt = bytearray(packets[0]); corrupt[11] ^= 1
            self.assertTrue(r.receive(corrupt))
            self.assertFalse(r.receive(packets[0]))  # conflicting duplicate must not replace data
            self.assertEqual(r.end(), 0x11)
            self.assertEqual(r.snapshot()['TMG013'], 5)
            with self.assertRaises(ValueError):
                r.export_verified(Path(folder) / 'bad.bin')
            r.close()
        with tempfile.TemporaryDirectory() as folder:
            r = self.receiver(folder, capacity=2)
            self.open_transfer(r)
            self.assertFalse(r.receive(self.payloads(b'abc')[0]))
            self.assertEqual(r.snapshot()['TMG014'], 0x40)
            self.assertEqual(r.end(True), 0xff)
            r.close()

    def test_no_unsafe_segment_guess_and_profile_mismatch(self):
        with tempfile.TemporaryDirectory() as folder:
            r = self.receiver(folder)
            r.gate(True)
            self.assertEqual(r.begin(3, 1, 2, 1, 2), 0xff)
            r.close()
            with self.assertRaises(ValueError):
                self.receiver(folder, 'XMODEM')

    def test_crc_interrupted_restart_can_retry_and_gate_stops_writes(self):
        with tempfile.TemporaryDirectory() as folder:
            r = self.receiver(folder)
            self.open_transfer(r)
            p = self.payloads(b'hello')[0]
            r.gate(False)
            self.assertFalse(r.receive(p))
            self.assertEqual(r.snapshot()['TMG010'], 0)
            r.gate(True)
            self.assertTrue(r.receive(p))
            r.state.update(status=0x22, check=1)
            r._save()
            r.close()
            r = self.receiver(folder)
            self.assertEqual(r.end(), 0)
            r.close()

    def test_two_channels_end_to_end_never_ack_upload_or_fake_flash_success(self):
        with tempfile.TemporaryDirectory() as folder:
            r = self.receiver(folder)
            h = Reference(r, version='Vtest', interpretation='ota-doc-v1')
            def ask(code, body):
                request = control(0x30, code, body)
                response = b''.join(h.feed_control(bytes((b,))) for b in request)
                return Decoder().feed(response)[0]
            self.assertEqual(ask(0x56, bytes(6)), b'\x33\x56Vtest\x00')
            self.assertEqual(ask(0x17, b'\x01' + bytes(5)), b'\x31\x17\x00')
            self.assertEqual(ask(0x53, bytes([3, 1, 2, 0, 0, 0])), b'\x31\x53\x00')
            data = b'firmware sample' * 70
            frames = list(file_packets(data, 'CCITT-FALSE'))
            bad = bytearray(frames[0]); bad[-4] ^= 1
            self.assertEqual(h.feed_upload(bad), b'')
            self.assertEqual(r.snapshot()['TMG011'], 1)
            for frame in frames:
                for b in frame:
                    self.assertEqual(h.feed_upload(bytes((b,))), b'')
            self.assertEqual(ask(0x54, bytes(6)), b'\x31\x54\x00')
            self.assertEqual(ask(0x04, bytes(6)), b'\x31\x04\xff')
            self.assertEqual(ask(0x55, b'\x01' + bytes(5)), b'\x31\x55\xff')
            self.assertEqual(r.snapshot()['TMG014'], 0x23)
            r.close()


if __name__ == '__main__':
    unittest.main()
