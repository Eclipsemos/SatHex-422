"""CPU telemetry. This module also runs as a bounded, read-only helper process."""
import json
import math
from pathlib import Path
import re
import threading
import time


def cpu_list(text):
    result = set()
    for part in text.strip().replace(' ', ',').split(','):
        if not part:
            continue
        if not re.fullmatch(r'\d+(?:-\d+)?', part):
            raise ValueError('invalid CPU list')
        pair = [int(x) for x in part.split('-')]
        low, high = pair[0], pair[-1]
        if not 0 <= low <= high < 4096:
            raise ValueError('CPU list out of range')
        result.update(range(low, high + 1))
    if not result:
        raise ValueError('empty CPU list')
    return sorted(result)


def parse_stat(text):
    result = {}
    for line in text.splitlines():
        parts = line.split()
        if not parts or not re.fullmatch(r'cpu\d*', parts[0]):
            continue
        # guest/guest_nice are already included in user/nice: do not count twice.
        counters = [int(x) for x in parts[1:9]]
        if len(counters) < 4 or min(counters) < 0 or parts[0] in result:
            raise ValueError('invalid CPU counters')
        result[parts[0]] = counters + [0] * (8 - len(counters))
    if 'cpu' not in result:
        raise ValueError('aggregate missing')
    return result


def percentages(previous, current):
    if previous is None or current is None:
        return None, None
    delta = [b - a for a, b in zip(previous, current)]
    # iowait can decrease on Linux. Do not turn a reset into a false spike.
    if len(delta) != 8 or min(delta) < 0 or sum(delta) == 0:
        return None, None
    total = sum(delta)
    busy = sum(delta[i] for i in (0, 1, 2, 5, 6))
    return 100 * busy / total, 100 * delta[4] / total


def read_snapshot(proc=Path('/proc'), syscpu=Path('/sys/devices/system/cpu')):
    stat = parse_stat((proc / 'stat').read_text())
    stamp = time.monotonic()
    online = cpu_list((syscpu / 'online').read_text())
    if any('cpu' + str(i) not in stat for i in online):
        raise ValueError('CPU topology changing')
    loads = None
    try:
        loads = [float(x) for x in (proc / 'loadavg').read_text().split()[:3]]
        if len(loads) != 3 or any(not math.isfinite(x) or x < 0 for x in loads):
            loads = None
    except (OSError, ValueError):
        pass
    frequencies = []
    for policy in sorted((syscpu / 'cpufreq').glob('policy[0-9]*'),
                         key=lambda p: int(p.name[6:])):
        try:
            cores = sorted(set(cpu_list((policy / 'affected_cpus').read_text())) & set(online))
            if not cores:
                continue
            khz = None
            try:
                khz = int((policy / 'scaling_cur_freq').read_text())
                if khz <= 0:
                    khz = None
            except (OSError, ValueError):
                pass
            frequencies.append([cores, khz])
        except (OSError, ValueError):
            continue
    return {'time': stamp, 'stat': stat, 'online': online, 'loads': loads, 'frequencies': frequencies}


def group_label(cores):
    if len(cores) > 1 and cores == list(range(cores[0], cores[-1] + 1)):
        return f'{cores[0]}-{cores[-1]}'
    return '+'.join(str(i) for i in cores)


class CPUCache:
    def __init__(self, ttl_ms=5000, clock=time.monotonic):
        self.ttl = ttl_ms / 1000
        self.clock = clock
        self.lock = threading.Lock()
        self.previous = None
        self.snapshot = None

    def failed(self):
        # Keep the last snapshot only until TTL; next success must re-warm usage.
        with self.lock:
            self.previous = None

    def update(self, data):
        with self.lock:
            previous = self.previous
            elapsed = data['time'] - previous['time'] if previous else 0
            valid = previous is not None and 0 < elapsed <= 3 and previous['online'] == data['online']
            old_stat = previous['stat'] if valid else {}
            usage, wait = percentages(old_stat.get('cpu'), data['stat']['cpu'])
            cores = [(i, percentages(old_stat.get('cpu' + str(i)), data['stat']['cpu' + str(i)])[0])
                     for i in data['online']]
            self.snapshot = (data, usage, wait, cores, round(elapsed * 1000) if valid else None)
            self.previous = data

    def reply(self, temperature):
        with self.lock:
            snap = self.snapshot
            if snap is not None and not 0 <= self.clock() - snap[0]['time'] < self.ttl:
                snap = None
        pct = lambda x: '-' if x is None else f'{x:.1f}%'
        usage = wait = None
        core_text = freq_text = count = window = '-'
        loads = ['-'] * 3
        if snap is not None:
            data, usage, wait, cores, duration = snap
            core_text = '|'.join(f'{i}:{pct(value)}' for i, value in cores)
            freq_text = '|'.join(group_label(ids) + ':' + ('-' if khz is None else f'{khz / 1000:.1f}MHz')
                                 for ids, khz in data['frequencies']) or '-'
            count = str(len(data['online']))
            window = str(duration) if duration is not None else '-'
            if data['loads'] is not None:
                loads = [f'{x:.2f}' for x in data['loads']]
        temp = '-' if temperature is None else f'{temperature / 100:.2f}C'
        return (f'+CPU:USAGE={pct(usage)},IOWAIT={pct(wait)},LOAD1={loads[0]},LOAD5={loads[1]},'
                f'LOAD15={loads[2]},CORE_COUNT={count},CORE_USAGE={core_text},FREQ={freq_text},'
                f'TEMP={temp},WINDOW_MS={window}')


if __name__ == '__main__':
    print(json.dumps(read_snapshot(), separators=(',', ':')))
