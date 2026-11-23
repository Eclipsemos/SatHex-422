"""Strict validation of the supplied 感算 K/D sheets; no hardware effects."""
import struct
from .ota_wire import validate_control

K_EMPTY = {0x87: 'software_resume', 0x80: 'software_stop', 0x84: 'board_reboot',
           0x13: 'storage_clean', 0x42: 'playback_start', 0x43: 'playback_stop',
           0x51: 'storage_stop', 0x52: 'storage_status'}


class CommandError(ValueError):
    def __init__(self, error):
        self.error = error
        super().__init__(str(error))


def require(condition):
    if not condition:
        raise CommandError(4)


SHORT_CODES = frozenset((0x13, 0x40, 0x41, 0x42, 0x43, 0x51, 0x52))
REPLAY_TYPES = (0x11, 0x12, 0x13, 0x14, 0x20, 0x21, 0x22)


def validate_short(code, words):
    require(len(words) == 6)
    if code == 0x40:
        require(words[0] in (0, 1) and not any(words[1:]))
        return 'short_link', {'link': words[0]}
    if code == 0x41:
        require(not any(words[2:]))
        return 'short_list', {'file_type': words[0], 'count': words[1]}
    if code == 0x13:
        require(1 <= words[0] <= 6 and not any(words[1:]))
        return 'short_clean', {'file_type': words[0]}
    require(code in (0x42, 0x43, 0x51, 0x52) and not any(words))
    return {0x42: 'short_start', 0x43: 'playback_stop',
            0x51: 'storage_stop', 0x52: 'short_status'}[code], {}


def validate(message):
    kind, code, body = message.kind, message.code, message.body
    if kind == 0x21:
        if code in (0x53, 0x54, 0x55, 0x56):
            # K1119 is no-parameter in the workbook; OTA TYPE30 uses six zeros.
            if code == 0x56:
                require(not body)
                return 'firmware_version', {}
            try:
                action, parameters = validate_control(bytes((0x30, code)) + body)
            except ValueError:
                raise CommandError(4)
            return 'ota_' + action, parameters
        if code in K_EMPTY:
            require(not body)
            return K_EMPTY[code], {}
        if code not in (0x17, 0x83, 0x81, 0x85, 0x32):
            raise CommandError(3)
        require(len(body) == 6)
        if code == 0x17:
            require(body[0] in (0, 1, 2) and not any(body[1:]))
            return 'receive_gate', {'mode': body[0]}
        if code == 0x83:
            require(body[2] in (1, 2) and body[3] in (1, 2) and not any(body[4:]))
            return 'task_start', dict(task_id=body[0], priority=body[1], mode=body[2], npu=body[3] == 1)
        if code in (0x81, 0x85):
            require(not any(body[1:]))
            return ('task_stop' if code == 0x81 else 'task_pause'), {'task_id': body[0]}
        require(body[0] in (1, 2) and body[1] in (0, 1) and body[2] in (1, 2, 3) and not any(body[3:]))
        return 'capture_control', dict(enabled=body[0] == 1, storage=body[1], mode=body[2])
    if kind != 0x25:
        raise CommandError(3)
    if code == 0x40:
        require(len(body) == 16)
        link, file_type, start, end, size, reserved = struct.unpack('>BBIIIH', body)
        require(link in (1, 2, 3) and file_type in (1, 2, 3, 4) and start <= end and reserved == 0)
        return 'playback_link', dict(link=link, file_type=file_type, start_utc=start, end_utc=end, byte_count=size)
    if code == 0x41:
        require(len(body) == 15)
        file_id, start, end, rate, reserved = struct.unpack('>IIIBH', body)
        require(start <= end and rate in (1, 2, 4) and reserved == 0)
        return 'playback_file', dict(file_id=file_id, start_frame=start, end_frame=end, rate=rate)
    if code == 0x50:
        require(len(body) == 6 and body[5] == 0)
        return 'storage_start', dict(file_id=body[0], start_frame=int.from_bytes(body[1:3], 'little'),
                                     frame_count=int.from_bytes(body[3:5], 'little'))
    if code == 4:
        require(26 <= len(body) <= 537)
        length, reserved, file_size, frames, bitmap_size, index = struct.unpack_from('>IIIIIB', body)
        data = body[21:-4]
        require(reserved == 0 and 1 <= frames <= 256 and 1 <= file_size <= 131072)
        require(frames == (file_size + 511) // 512 and index < frames)
        require(len(data) == min(512, file_size - index * 512))
        packet_sum, file_sum = struct.unpack('>HH', body[-4:])
        # The spreadsheet does not specify checksum coverage or ByteMap semantics.
        # Preserve them for the configured OTA validator; never claim flashed here.
        return 'ota_upload', dict(data_length=length, file_size=file_size, total_frames=frames,
                                  bitmap_size=bitmap_size, packet_index=index, data_hex=data.hex(),
                                  packet_checksum=packet_sum, file_checksum=file_sum)
    raise CommandError(3)
