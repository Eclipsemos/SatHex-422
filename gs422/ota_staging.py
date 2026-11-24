"""Durable OTA *staging* receiver. Never erases flash or applies a firmware.

SQLite commits each accepted packet before its telemetry count becomes visible.
This is a software reference backend, not an implementation of a board partition
driver. A CRC profile and capacity must be supplied by the deployment owner.
"""
import json
import os
from pathlib import Path
import sqlite3
import struct
import threading
from .ota_wire import crc16, expected_packets, parse_packet


class Staging:
    def __init__(self, directory, *, crc_profile, capacity):
        crc16(b'', crc_profile)  # reject missing/unknown profile before writing
        if type(capacity) is not int or not 1 <= capacity <= 0xffffffff:
            raise ValueError('invalid configured staging capacity')
        root = Path(directory)
        root.mkdir(parents=True, exist_ok=True)
        self.root = root
        self.db = sqlite3.connect(root / 'ota.sqlite3', check_same_thread=False)
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('PRAGMA synchronous=FULL')
        self.db.execute('CREATE TABLE IF NOT EXISTS meta (id INTEGER PRIMARY KEY CHECK(id=1), body TEXT NOT NULL)')
        self.db.execute('CREATE TABLE IF NOT EXISTS packets (seq INTEGER PRIMARY KEY, data BLOB NOT NULL)')
        self.lock = threading.RLock()
        self.crc_config_valid = True
        self.profile, self.capacity = crc_profile, capacity
        row = self.db.execute('SELECT body FROM meta WHERE id=1').fetchone()
        self.state = json.loads(row[0]) if row else dict(gate=False, status=0, check=0, errors=0, bus_errors=0,
                                                      file_id=0, size=None, checksum=None, request=None,
                                                      crc_profile=crc_profile, capacity=capacity, next_missing=0)
        if self.state['crc_profile'] != crc_profile or self.state['capacity'] != capacity:
            self.db.close()
            raise ValueError('persisted upload profile/capacity mismatch')
        if crc_profile == 'XMODEM' and self.state['status'] == 0x31:
            self.state.update(status=0, check=0)
        if self.state['status'] == 0x22:
            self.state.update(status=0x20, check=0)  # interrupted CRC can be retried by Z54
        self._save()

    def _save(self):
        self.db.execute('INSERT OR REPLACE INTO meta VALUES(1,?)', (json.dumps(self.state),))
        self.db.commit()

    def close(self):
        self.db.close()

    @property
    def ordinary_gate(self):
        return self.state['gate'] and self.state.get('ordinary_gate', False)

    @property
    def ordinary_file(self):
        return self.state.get('ordinary_file', False)

    def gate(self, enabled, *, ordinary=False):
        with self.lock:
            self.state.update(gate=bool(enabled), ordinary_gate=bool(enabled and ordinary))
            self._save()

    def begin(self, component, region, mode, segmented=0, segments=0):
        with self.lock:
            if not self.state['gate']:
                return 0xff
            if component not in (1, 2, 3, 4) or region not in (1, 2, 3, 4) or mode not in (1, 2):
                return 2
            # Project segmented-stream profile: one global packet sequence and
            # one file CRC across batches; no reset of sequence or metadata.
            if segmented not in (0,1) or (segmented==0 and segments!=0) or (segmented==1 and not 1<=segments<=255):
                return 0xff
            if self.ordinary_gate and (mode != 2 or (component, region) not in ((3, 1), (4, 2))):
                return 2
            request = [component, region, mode, segmented, segments]
            if self.state['status'] in (0x20, 0x21):
                return 0 if request == self.state['request'] and self.ordinary_file == self.ordinary_gate else 1
            if self.state['file_id'] == 0xffffffff:
                return 0xff
            self.db.execute('DELETE FROM packets')
            self.state.update(status=0x20, check=0, errors=0, bus_errors=0,
                              ordinary_file=self.ordinary_gate, publish_failed=False,
                              file_id=self.state['file_id'] + 1, size=None, checksum=None, request=request, next_missing=0)
            self._save()
            return 0

    def _error(self, *, status=None):
        self.state['errors'] = (self.state['errors'] + 1) & 0xffffffff
        self.state['bus_errors'] = (self.state['bus_errors'] + 1) & 255
        self.state['check'] = 3
        if status is not None:
            self.state['status'] = status
        self._save()
        return False

    def receive(self, payload):
        """Return local acceptance only. Caller MUST NOT transmit on RX channel."""
        with self.lock:
            if (not self.state['gate'] or self.ordinary_file != self.ordinary_gate
                    or self.state['status'] not in (0x20, 0x21)):
                return False
            try:
                seq, data = parse_packet(payload)
            except ValueError:
                return self._error()
            if seq == 0:
                size, checksum = struct.unpack_from('>IH', data)
                if not 1 <= size <= self.capacity:
                    return self._error(status=0x40)
                if self.state['size'] is not None and (self.state['size'], self.state['checksum']) != (size, checksum):
                    return self._error()
            elif self.state['size'] is None:
                return self._error()
            else:
                size, checksum = self.state['size'], self.state['checksum']
            count = expected_packets(size)
            if self.state['request'][3] and self.state['request'][4]>count:
                return self._error(status=0x40)
            if seq >= count:
                return self._error()
            used = min(499, size + 6 - seq * 499)
            if any(data[used:]):
                return self._error()
            old = self.db.execute('SELECT data FROM packets WHERE seq=?', (seq,)).fetchone()
            if old:
                if old[0] != data:
                    return self._error()
                return True  # retransmission is not a newly received file packet
            first_missing = self._missing()
            gap = seq > first_missing
            try:
                self.db.execute('INSERT INTO packets VALUES(?,?)', (seq, data))
                self.state.update(size=size, checksum=checksum, check=2)
                while self.db.execute('SELECT 1 FROM packets WHERE seq=?', (self.state['next_missing'],)).fetchone():
                    self.state['next_missing'] += 1
                if self.profile != 'XMODEM' and self.state['next_missing'] == count:
                    self.state['status'] = 0x21
                if gap:
                    self.state['bus_errors'] = (self.state['bus_errors'] + 1) & 255
                self._save()
            except sqlite3.Error:
                self.db.rollback()
                # Reload last committed metadata, then expose a write failure.
                self.state = json.loads(self.db.execute('SELECT body FROM meta WHERE id=1').fetchone()[0])
                return self._error(status=0x40)
            return True

    def _missing(self):
        return self.state['next_missing']

    def snapshot(self):
        with self.lock:
            count = self.db.execute('SELECT count(*) FROM packets').fetchone()[0]
            size = self.state['size']
            total = expected_packets(size) if size else 0
            percent = 0
            if count and size:
                # The first accepted packet must be seq 0. It contains 6 header
                # bytes; only the final packet can contain zero padding.
                received = count * 499 - 6
                if self.db.execute('SELECT 1 FROM packets WHERE seq=?', (total - 1,)).fetchone():
                    received -= total * 499 - 6 - size
                percent = 100 * received // size
            missing = self._missing()
            return dict(TMG008=self.state['file_id'], TMG009=missing if not total or missing < total else 0xffffffff,
                        TMG010=count, TMG011=self.state['errors'], TMG012=percent,
                        TMG013=self.state['check'], TMG014=self.state['status'])

    def end(self, aborted=False, publish=None):
        with self.lock:
            if (self.ordinary_file or self.ordinary_gate) and not (
                    self.ordinary_file and self.ordinary_gate):
                return 0xff
            if aborted:
                self.state.update(status=0x40, gate=False, publish_failed=False)
                self._save()
                return 0xff
            if not self.crc_config_valid:
                self.state.update(status=0x24, check=5)
                self._save()
                return 0x11
            if self.state['status'] == 0x24:
                return 0x11
            if self.state['status'] == 0x23:
                return 0
            if (self.state['status'] not in (0x20, 0x21) and not self.state.get('publish_failed')
                    or self.state['size'] is None):
                return 0xff
            if self._missing() != expected_packets(self.state['size']):
                return 1
            self.state.update(status=0x21)
            self._save()
            if publish:
                publish(0x21)
            self.state.update(status=0x22, check=1)
            self._save()
            if publish:
                publish(0x22)
            # Stream rather than hold the advertised (up to 4 GiB) file in RAM.
            import binascii
            value = 0xffff if self.profile in ('CCITT-FALSE', 'MODBUS') else 0
            remaining = self.state['size']
            for seq, raw in self.db.execute('SELECT seq,data FROM packets ORDER BY seq'):
                chunk = raw[6:] if seq == 0 else raw
                chunk = chunk[:remaining]
                remaining -= len(chunk)
                if self.profile in ('CCITT-FALSE', 'XMODEM'):
                    value = binascii.crc_hqx(chunk, value)
                else:
                    for byte in chunk:
                        value ^= byte
                        for _ in range(8):
                            value = (value >> 1) ^ (0xa001 if value & 1 else 0)
            success = remaining == 0 and value == self.state['checksum']
            if success and self.ordinary_file:
                try:
                    self._publish_file()
                except OSError:
                    self.state.update(status=0x40, check=4, publish_failed=True)
                    self._save()
                    return 0xff
            self.state.update(publish_failed=False, status=0x23 if success else 0x24, check=4 if success else 5)
            self._save()
            return 0 if success else 0x11

    def _write_contents(self, stream):
        remaining = self.state['size']
        for seq, raw in self.db.execute('SELECT seq,data FROM packets ORDER BY seq'):
            chunk = (raw[6:] if seq == 0 else raw)[:remaining]
            remaining -= len(chunk)
            stream.write(chunk)
        stream.flush()
        os.fsync(stream.fileno())

    def _publish_file(self):
        region = self.state['request'][1]
        directory = self.root / 'received-files' / f'APP{region}'
        directory.mkdir(parents=True, exist_ok=True)
        name = f"{self.state['file_id']:08X}.bin"
        # A single staging owner holds the session lock. Reuse the same partial
        # name after a crash and expose the final name only after the file fsync.
        partial = directory / ('.' + name + '.partial')
        try:
            fd = os.open(partial, os.O_WRONLY | os.O_CREAT | os.O_TRUNC |
                         getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_BINARY', 0), 0o600)
            with os.fdopen(fd, 'wb') as stream:
                self._write_contents(stream)
            os.replace(partial, directory / name)
            if os.name == 'posix':
                for path in (directory, directory.parent, self.root):
                    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
                    try:
                        os.fsync(fd)
                    finally:
                        os.close(fd)
        finally:
            partial.unlink(missing_ok=True)

    def export_verified(self, destination):
        """Export verified bytes to an explicit caller-owned new file."""
        with self.lock:
            if self.state['status'] != 0x23 or not self.crc_config_valid:
                raise ValueError('upload is not verified')
            with open(destination, 'xb') as output:
                self._write_contents(output)
