"""Read-only storage snapshot, executed in a time-bounded helper process."""
import json
import os
import re
import subprocess
import sys
from urllib.parse import quote

PAGE_ROWS = 8
COLUMNS = 'NAME,KNAME,PATH,TYPE,SIZE,FSTYPE,MOUNTPOINTS,MODEL,MAJ:MIN'


def text(value):
    value = str(value or '')
    if not value:
        return '-'
    if value == '-':
        return '%2D'
    return quote(value.encode('utf-8', errors='replace')[:120].decode('utf-8', errors='ignore'), safe='/._-:')


def gib(value):
    return '-' if value is None else f'{value / (1024 ** 3):.2f}GiB'


def filesystem_usage(path, device, stat=os.stat, statvfs=None):
    statvfs = statvfs or os.statvfs
    # Don't report the underlying root filesystem after a removable disk unmount.
    before = stat(path).st_dev
    if f'{os.major(before)}:{os.minor(before)}' != device:
        raise OSError('mount changed')
    s = statvfs(path)
    if stat(path).st_dev != before:
        raise OSError('mount changed')
    total = s.f_blocks * s.f_frsize
    used = (s.f_blocks - s.f_bfree) * s.f_frsize
    available = max(0, s.f_bavail) * s.f_frsize
    if total < 0 or used < 0 or used > total:
        raise ValueError('invalid filesystem counters')
    # Like df: reserved blocks are excluded from ordinary-user availability.
    percent = f'{100 * used / (used + available):.1f}%' if used + available else '-'
    return total, used, available, percent


def inventory(tree, usage=filesystem_usage):
    disks, volumes = {}, {}

    def walk(node, disk=None):
        name = node['kname']
        if node['type'] == 'disk':
            if re.match(r'^(loop|zram|ram)\d', name) or re.fullmatch(r'mmcblk\d+(?:boot\d+|rpmb)', name):
                return
            disk = name
            disks[disk] = node
        if not disk:
            return
        mounts = sorted({m for m in node.get('mountpoints', []) if m and m.startswith('/')})
        # Count raw boot partitions, but don't report nonexistent usage for them.
        if mounts or node.get('fstype'):
            key = node['maj:min']
            if key not in volumes:
                volumes[key] = {'node': node, 'disks': set(), 'mounts': set()}
            volumes[key]['disks'].add(disk)
            volumes[key]['mounts'].update(mounts)
        for child in node.get('children', []):
            walk(child, disk)

    for node in tree['blockdevices']:
        walk(node)
    rows = []
    for name, node in sorted(disks.items()):
        count = sum(child.get('type') == 'part' for child in node.get('children', []))
        rows.append(f'+DISK:DEVICE={text(node["path"])},MODEL={text(node.get("model"))},'
                    f'SIZE={gib(node["size"])},SIZE_BYTES={node["size"]},PARTITIONS={count}')
    for key, entry in sorted(volumes.items(), key=lambda item: item[1]['node']['path']):
        node = entry['node']
        mount, state = '-', 'UNMOUNTED'
        total = used = available = None
        percent = '-'
        mounts = sorted(entry['mounts'], key=lambda p: (len(p), p))
        for candidate in mounts:
            try:
                total, used, available, percent = usage(candidate, key)
                mount, state = candidate, 'MOUNTED'
                break
            except (OSError, ValueError):
                mount, state = candidate, 'UNAVAILABLE'
        rows.append(f'+DISK:VOLUME={text(node["path"])},DISKS={";".join(text(d) for d in sorted(entry["disks"]))},'
                    f'FS={text(node.get("fstype"))},MOUNT={text(mount) if mounts else "-"},STATE={state},'
                    f'TOTAL={gib(total)},USED={gib(used)},AVAILABLE={gib(available)},USE={percent}')
    return len(disks), rows


def render(tree, page, usage=filesystem_usage):
    count, rows = inventory(tree, usage)
    batches = [[]]
    size = 0
    for row in rows:
        if batches[-1] and (len(batches[-1]) >= PAGE_ROWS or size + len(row) > 2800):
            batches.append([])
            size = 0
        batches[-1].append(row)
        size += len(row) + 2
    pages = len(batches)
    if page < 0 or page >= pages:
        return '+DISK:ERR,NO_PAGE'
    selected = batches[page]
    next_page = page + 1 if page + 1 < pages else '-'
    header = f'+DISK:COUNT={count},PAGE={page},PAGES={pages},ROWS={len(selected)}'
    return '\n'.join([header, *selected, f'+DISK:END,NEXT={next_page}'])


def main():
    try:
        page = int(sys.argv[1]) if len(sys.argv) == 2 else 0
        result = subprocess.run(['/usr/bin/lsblk', '--json', '--bytes', '--output', COLUMNS],
                                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL, timeout=4, check=True)
        if len(result.stdout) > 1048576:
            raise ValueError('too many devices')
        print(render(json.loads(result.stdout), page))
    except Exception:
        print('+DISK:ERR,UNAVAILABLE')


if __name__ == '__main__':
    main()
