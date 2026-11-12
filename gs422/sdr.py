"""Bounded AT protocol and local client for the separate SDR task service."""
import json
import re
import socket

SOCKET = '/run/gs422-sdr/control.sock'
TERMINAL = ('DONE', 'FAILED', 'CANCELLED', 'INTERRUPTED')
PRESETS = {
    'LTE': ('LTE', 1815000000, 2000000, 2000000, 300, 45, 0, 'BROADCAST', 100),
    'LTE_DCI': ('LTE', 1815000000, 20000000, 20000000, 120, 45, 0, 'DCI', 100),
    'NR': ('NR', 2130150000, 4000000, 4000000, 500, 45, 0, 'BROADCAST', 100),
}
KEYS = ('TECH', 'FREQ_HZ', 'RATE_SPS', 'BW_HZ', 'DURATION_MS', 'GAIN_DB', 'CHANNEL', 'STAGE', 'PRB')


def settings(values):
    if len(values) != 9:
        raise ValueError('arguments')
    tech, freq, rate, bw, duration, gain, channel, stage, prb = values
    numbers = (freq, rate, bw, duration, gain, channel, prb)
    if any(not re.fullmatch(r'[0-9]{1,10}', str(v)) for v in numbers):
        raise ValueError('integer required')
    freq, rate, bw, duration, gain, channel, prb = map(int, numbers)
    if (tech not in ('LTE', 'NR') or stage not in ('BROADCAST', 'DCI') or
            not 70000000 <= freq <= 6000000000 or not 1000000 <= rate <= 20000000 or
            not 1000000 <= bw <= rate or not 50 <= duration <= 2000 or
            not 0 <= gain <= 76 or channel not in (0, 1) or prb not in (6, 15, 25, 50, 75, 100)):
        raise ValueError('range')
    if rate * duration * 8 // 1000 > 512 * 1024**2:
        raise ValueError('memory')
    if tech == 'NR' and (stage != 'BROADCAST' or bw < 4000000):
        raise ValueError('NR requires 15 kHz SSB broadcast bandwidth')
    nominal = {6: 1400000, 15: 3000000, 25: 5000000, 50: 10000000, 75: 15000000, 100: 20000000}
    if tech == 'LTE' and stage == 'DCI' and bw < nominal[prb]:
        raise ValueError('LTE DCI bandwidth')
    return dict(zip(KEYS, (tech, freq, rate, bw, duration, gain, channel, stage, prb)))


def parse_command(line):
    if line in ('AT+SDR', 'AT+SDR?'):
        return ('STATUS',)
    if not line.startswith('AT+SDR='):
        raise ValueError('command')
    parts = line[7:].split(',')
    if parts[0] in ('STATUS', 'RESULT', 'STOP') and len(parts) in (1, 2):
        if len(parts) == 2 and not re.fullmatch(r'[0-9a-f]{12}', parts[1]):
            raise ValueError('task id')
    elif parts == ['CONFIG'] or parts == ['START']:
        pass
    elif len(parts) == 2 and parts[0] == 'PRESET' and parts[1] in PRESETS:
        pass
    elif parts[0] == 'CONFIG' and len(parts) == 10:
        settings(parts[1:])
    else:
        raise ValueError('command')
    return tuple(parts)


def request(args, path=SOCKET):
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(3)
            connection.connect(path)
            connection.sendall(json.dumps(list(args)).encode() + b'\n')
            data = bytearray()
            while b'\n' not in data and len(data) <= 2048:
                block = connection.recv(2048)
                if not block:
                    break
                data.extend(block)
        reply = bytes(data).decode().rstrip('\n')
        if not reply.startswith('+SDR:') or '\r' in reply or '\n' in reply or len(reply) > 2048:
            return '+SDR:ERR,INVALID_RESPONSE'
        return reply
    except (OSError, ValueError):
        return '+SDR:ERR,UNAVAILABLE'
