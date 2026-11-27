"""File-backed record/replay services with explicitly bound external drivers.

No device emulation is implicit. A source must supply (payload, expected_crc32)
and a sink must accept one complete record. Tests bind file fixtures; deployments
must bind real hardware adapters separately.
"""
import json
import os
from pathlib import Path
import sqlite3
import threading
import time
import zlib
import uuid
import stat
from .space_commands import CommandError, REPLAY_TYPES


class FileServices:
    timeout = 5

    def __init__(self, root, *, sources=None, sinks=None, capture_driver=None, ssd_root=None, ssd_uuid=None,
                 published=None, replay_sinks=None, delegate=None, max_bytes=64*1024*1024):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.root/'records.sqlite3', check_same_thread=False)
        self.db.execute('PRAGMA synchronous=FULL')
        self.db.execute('CREATE TABLE IF NOT EXISTS files(id INTEGER PRIMARY KEY AUTOINCREMENT, type INTEGER, complete INTEGER)')
        self.db.execute('CREATE TABLE IF NOT EXISTS frames(file INTEGER, idx INTEGER, utc INTEGER, body BLOB, PRIMARY KEY(file,idx))')
        self.db.commit()
        self.lock = threading.RLock()
        self.sources, self.sinks = sources or {}, sinks or {}
        self.capture_driver, self.ssd_root, self.delegate = capture_driver, ssd_root, delegate
        self.ssd_uuid = ssd_uuid
        self.capture_params = self.ssd_store = None
        self.db.execute("CREATE TABLE IF NOT EXISTS ssd_files(file INTEGER PRIMARY KEY, token TEXT UNIQUE NOT NULL)")
        self.published = published
        self.replay_sinks = replay_sinks or {}
        self.short_link = self.short_selection = None
        self.play_frames = 0
        self.max_bytes = max_bytes
        self.link = self.selection = None
        self.capture = None
        self.play_thread = self.store_thread = None
        self.play_cancel, self.store_cancel = threading.Event(), threading.Event()
        self.play_file = self.store_file = None
        self.play_state = self.store_state = 0
        self.store_frames = self.store_bytes = self.crc_errors = 0
        self.last_error = None

    def _is_memory_store(self):
        try:
            mounts=[]
            for line in Path('/proc/self/mountinfo').read_text().splitlines():
                before,after=line.split(' - ',1)
                mount=before.split()[4].replace('\\040',' ')
                path=Path(mount)
                if self.root==path or path in self.root.parents:
                    mounts.append((len(path.parts),after.split()[0]))
            return bool(mounts) and max(mounts)[1]=='tmpfs'
        except (OSError,ValueError,IndexError):
            return False

    def _ssd(self):
        if not self.ssd_root or not self.ssd_uuid:
            raise CommandError(7)
        mount = Path(self.ssd_root)
        device = Path('/dev/disk/by-uuid') / self.ssd_uuid
        if not mount.is_mount() or not device.exists():
            raise CommandError(7)
        disk = device.stat()
        if not stat.S_ISBLK(disk.st_mode) or disk.st_rdev != mount.stat().st_dev:
            raise CommandError(7)
        if self.ssd_store is None:
            self.ssd_store = sqlite3.connect(mount/'sathex-records.sqlite3', check_same_thread=False)
            self.ssd_store.execute('PRAGMA synchronous=FULL')
            self.ssd_store.execute('CREATE TABLE IF NOT EXISTS clips(token TEXT PRIMARY KEY, mode INTEGER, complete INTEGER)')
            self.ssd_store.execute('CREATE TABLE IF NOT EXISTS clip_ids(id INTEGER PRIMARY KEY AUTOINCREMENT, token TEXT UNIQUE)')
            self.ssd_store.execute('CREATE TABLE IF NOT EXISTS records(token TEXT, idx INTEGER, utc INTEGER, body BLOB, PRIMARY KEY(token,idx))')
            # Restore the lookup index after the RAM disk is recreated.
            rows = self.ssd_store.execute('SELECT id,token,complete FROM clip_ids JOIN clips USING(token)')
            with self.db:
                for number, token, complete in rows:
                    fid = 0x80000000 + number
                    self.db.execute('INSERT OR REPLACE INTO files VALUES(?,1,?)', (fid,complete))
                    self.db.execute('INSERT OR REPLACE INTO ssd_files VALUES(?,?)', (fid,token))
        return self.ssd_store

    def _new_ssd(self, db, mode):
        token = uuid.uuid4().hex
        with db:
            number = db.execute('INSERT INTO clip_ids(token) VALUES(?)', (token,)).lastrowid
            if number > 0x7fffffff:
                raise CommandError(8)
            db.execute('INSERT INTO clips VALUES(?,?,0)', (token,mode))
        fid = 0x80000000 + number
        with self.db:
            self.db.execute('INSERT INTO files VALUES(?,1,0)', (fid,))
            self.db.execute('INSERT INTO ssd_files VALUES(?,?)', (fid,token))
        return fid

    def _new(self, file_type):
        # Keep RAM IDs monotonically increasing across cleanup and reserve bit 31 for SSD IDs.
        self.db.execute('CREATE TABLE IF NOT EXISTS ram_ids(id INTEGER PRIMARY KEY CHECK(id=1), last INTEGER)')
        with self.db:
            row = self.db.execute('SELECT last FROM ram_ids WHERE id=1').fetchone()
            previous = row[0] if row else self.db.execute(
                'SELECT coalesce(max(id),0) FROM files WHERE id<2147483648').fetchone()[0]
            number = previous + 1
            if number >= 0x80000000:
                raise CommandError(8)
            self.db.execute('INSERT OR REPLACE INTO ram_ids VALUES(1,?)', (number,))
            self.db.execute('INSERT INTO files VALUES(?,?,0)', (number,file_type))
        return number

    def _append(self, file_id, payload, utc):
        if not isinstance(payload, bytes) or len(payload)>1024*1024:
            raise CommandError(4)
        used=self.db.execute('SELECT coalesce(sum(length(body)),0) FROM frames').fetchone()[0]
        if used+len(payload)>self.max_bytes:
            raise CommandError(8)
        index=self.db.execute('SELECT count(*) FROM frames WHERE file=?',(file_id,)).fetchone()[0]
        self.db.execute('INSERT INTO frames VALUES(?,?,?,?)',(file_id,index,int(utc),payload))
        self.db.commit()

    def _complete(self, file_id):
        self.db.execute('UPDATE files SET complete=1 WHERE id=?',(file_id,))
        self.db.commit()

    def add_file(self, frames, file_type=2):
        """Trusted local producer input. No serial-supplied filesystem path."""
        with self.lock:
            fid=self._new(file_type)
            for utc,data in frames:self._append(fid,data,utc)
            self._complete(fid)
            return fid

    def _stop_worker(self, name):
        event=getattr(self,name+'_cancel'); event.set()
        thread=getattr(self,name+'_thread')
        if thread:
            thread.join(timeout=3)
            if thread.is_alive():raise CommandError(9)

    def capture_record(self, payload, utc):
        with self.lock:
            if self.capture is None:raise CommandError(7)
            if self.capture_params[0] == 0:
                self._append(self.capture,payload,utc)
                return
            db = self._ssd()
            if not isinstance(payload, bytes) or len(payload) > 1024 * 1024:
                raise CommandError(4)
            space = os.statvfs(self.ssd_root)
            if space.f_bavail * space.f_frsize < len(payload) + 16 * 1024 * 1024:
                raise CommandError(8)
            token = self.db.execute('SELECT token FROM ssd_files WHERE file=?',(self.capture,)).fetchone()[0]
            index, used = db.execute('SELECT count(*),coalesce(sum(length(body)),0) FROM records WHERE token=?',(token,)).fetchone()
            if used + len(payload) > self.max_bytes:
                raise CommandError(8)
            with db:
                db.execute('INSERT INTO records VALUES(?,?,?,?)',(token,index,int(utc),payload))

    def _play(self, records, sinks, rate):
        try:
            previous=None
            for utc,body in records:
                if previous is not None:
                    if utc<previous:raise CommandError(4)
                    if self.play_cancel.wait((utc-previous)/rate):return
                if self.play_cancel.is_set():return
                for sink in sinks:
                    if self.play_cancel.is_set():return
                    sink(body,self.play_cancel)  # driver must bound blocking and honor cancel
                previous=utc
            with self.lock:self.play_state=2
        except Exception as error:
            with self.lock:
                self.last_error=type(error).__name__
                self.play_state=3
        finally:
            if self.play_cancel.is_set():
                with self.lock:self.play_state=3

    def _sent_frame(self):
        with self.lock:
            self.play_frames += 1

    def _play_list(self, selection, sink):
        try:
            self.published.send(*selection, sink, self.play_cancel, self._sent_frame)
            with self.lock:
                self.play_state = 3 if self.play_cancel.is_set() else 2
        except Exception as error:
            with self.lock:
                self.play_state = 3
                self.last_error = type(error).__name__

    def _short_playback(self, action, p):
        if self.play_thread and self.play_thread.is_alive():
            raise CommandError(6)
        if action == 'short_link':
            self.short_link = p['link']
            return json.dumps({'link': self.short_link}).encode()
        if self.published is None:
            raise CommandError(7)
        if action == 'short_list':
            self.short_selection = None
            if p['file_type'] not in REPLAY_TYPES or not 1 <= p['count'] <= 255:
                raise CommandError(4)
            files = self.published.newest(p['file_type'], p['count'])
            self.short_selection = (p['file_type'], files)
            return json.dumps({'count': len(files), 'file_type': p['file_type']}).encode()
        if self.short_selection is None:
            raise CommandError(4)
        if self.short_link not in self.replay_sinks:
            raise CommandError(7)
        self.play_cancel.clear()
        self.play_state, self.play_file, self.play_frames, self.last_error = 1, None, 0, None
        self.play_thread = threading.Thread(target=self._play_list, daemon=True,
                                           args=(self.short_selection, self.replay_sinks[self.short_link]))
        self.play_thread.start()
        return b'{"state":1}'

    def _store(self, source, start, count, fid):
        try:
            received=0
            for payload,checksum in source(start,count,self.store_cancel):
                if self.store_cancel.is_set():return
                if count and received>=count:raise CommandError(4)
                with self.lock:
                    if zlib.crc32(payload)&0xffffffff != checksum:
                        self.crc_errors=(self.crc_errors+1)&255
                        raise CommandError(8)
                    self._append(fid,payload,time.time())
                    self.store_frames+=1;self.store_bytes+=len(payload)
                    received+=1
            if self.store_cancel.is_set():return
            if count and received!=count:raise CommandError(8)
            with self.lock:
                self._complete(fid);self.store_state=2
        except Exception as error:
            with self.lock:self.last_error=type(error).__name__;self.store_state=3
        finally:
            if self.store_cancel.is_set():
                with self.lock:self.store_state=3

    def execute(self, action, p):
        if action=='playback_stop':
            self._stop_worker('play');self.play_state=3
            return b'{}'
        if action=='storage_stop':
            self._stop_worker('store');self.store_state=3
            return b'{}'
        if action=='capture_control' and not p['enabled']:
            if self.capture_driver is None:raise CommandError(7)
            try:
                self.capture_driver.stop()
            except Exception:
                with self.lock:
                    self.capture = self.capture_params = None
                raise
            with self.lock:
                try:
                    if self.capture is not None:
                        if self.capture_params[0]:
                            token=self.db.execute('SELECT token FROM ssd_files WHERE file=?',(self.capture,)).fetchone()[0]
                            db=self._ssd()
                            with db:db.execute('UPDATE clips SET complete=1 WHERE token=?',(token,))
                        self._complete(self.capture)
                finally:
                    self.capture_params = self.capture = None
            return b'{}'
        with self.lock:
            if action in ('short_link','short_list','short_start'):
                return self._short_playback(action, p)
            if action == 'short_clean':
                if self.capture is not None or any(t and t.is_alive() for t in (self.play_thread,self.store_thread)):
                    raise CommandError(6)
                if self.published is None:
                    raise CommandError(7)
                return json.dumps({'deleted_files':self.published.clean(p['file_type'])}).encode()
            if action in ('playback_link','playback_file'):
                if self.play_thread and self.play_thread.is_alive():raise CommandError(6)
                if action=='playback_link':self.link=dict(p)
                else:self.selection=dict(p)
            elif action=='playback_start':
                if self.play_thread and self.play_thread.is_alive():raise CommandError(6)
                if not self.link or not self.selection:raise CommandError(4)
                s,l=self.selection,self.link
                if s['file_id'] >= 0x80000000:
                    self._ssd()
                row=self.db.execute('SELECT type,complete FROM files WHERE id=?',(s['file_id'],)).fetchone()
                if not row or row!=(l['file_type'],1):raise CommandError(4)
                token=self.db.execute('SELECT token FROM ssd_files WHERE file=?',(s['file_id'],)).fetchone()
                source,table,column,key=(self._ssd(),'records','token',token[0]) if token else (self.db,'frames','file',s['file_id'])
                count=source.execute(f'SELECT count(*) FROM {table} WHERE {column}=?',(key,)).fetchone()[0]
                if not 0<=s['start_frame']<=s['end_frame']<count:raise CommandError(4)
                if s['rate'] not in (1,2,4):raise CommandError(4)
                records=source.execute(f'SELECT utc,body FROM {table} WHERE {column}=? AND idx BETWEEN ? AND ? AND utc BETWEEN ? AND ? ORDER BY idx',
                    (key,s['start_frame'],s['end_frame'],l['start_utc'],l['end_utc'])).fetchall()
                # Preserve record boundaries: byte limit must select complete records.
                budget=l['byte_count'];chosen=[]
                for utc,body in records:
                    if budget<len(body):break
                    chosen.append((utc,body));budget-=len(body)
                    if not budget:break
                if not chosen or budget:raise CommandError(4)
                ids=(1,2) if l['link']==3 else (l['link'],)
                if any(i not in self.sinks for i in ids):raise CommandError(7)
                sinks=[self.sinks[i] for i in ids]
                self.play_cancel.clear();self.play_file=s['file_id'];self.play_state=1
                self.play_thread=threading.Thread(target=self._play,args=(chosen,sinks,s['rate']),daemon=True)
                self.play_thread.start()
            elif action=='storage_start':
                if self.store_thread and self.store_thread.is_alive():raise CommandError(6)
                if p['file_id'] not in self.sources:raise CommandError(7)
                fid=self._new(2);self.store_file=fid
                self.store_frames=self.store_bytes=0;self.store_state=1
                self.store_cancel.clear()
                self.store_thread=threading.Thread(target=self._store,args=(self.sources[p['file_id']],p['start_frame'],p['frame_count'],fid),daemon=True)
                self.store_thread.start()
            elif action == 'short_status':
                return json.dumps(dict(state=self.store_state,frames=self.store_frames,bytes=self.store_bytes,
                                       crc_errors=self.crc_errors,link=self.short_link,
                                       play_state=self.play_state,play_frames=self.play_frames)).encode()
            elif action=='storage_status':
                return json.dumps({'state':self.store_state,'frames':self.store_frames,'bytes':self.store_bytes,'crc_errors':self.crc_errors,'file_id':self.store_file,'error':self.last_error}).encode()
            elif action=='storage_clean':
                if self.capture is not None or any(t and t.is_alive() for t in (self.play_thread,self.store_thread)):raise CommandError(6)
                # Remove only locally owned completed records, never arbitrary paths.
                if self.ssd_root:
                    self._ssd()
                count=self.db.execute('SELECT count(*) FROM files WHERE complete=1').fetchone()[0]
                tokens=self.db.execute('SELECT token FROM ssd_files WHERE file IN (SELECT id FROM files WHERE complete=1)').fetchall()
                if tokens:
                    db=self._ssd()
                    with db:
                        db.executemany('DELETE FROM records WHERE token=?',tokens)
                        db.executemany('DELETE FROM clips WHERE token=?',tokens)
                        db.executemany('DELETE FROM clip_ids WHERE token=?',tokens)
                with self.db:
                    self.db.execute('DELETE FROM ssd_files WHERE file IN (SELECT id FROM files WHERE complete=1)')
                    self.db.execute('DELETE FROM frames WHERE file IN (SELECT id FROM files WHERE complete=1)')
                    self.db.execute('DELETE FROM files WHERE complete=1')
                self.db.execute('VACUUM')
                return json.dumps({'deleted_files':count}).encode()
            elif action=='capture_control':
                if self.capture_driver is None:raise CommandError(7)
                params=(p['storage'],p['mode'])
                if self.capture is not None:
                    if self.capture_params==params:return json.dumps({'file_id':self.capture}).encode()
                    raise CommandError(6)
                if p['storage']==0 and not self._is_memory_store():raise CommandError(7)
                db=self._ssd() if p['storage']==1 else None
                fid=self._new_ssd(db, p['mode']) if db is not None else self._new(1)
                self.capture=fid
                self.capture_params=params
                try:
                    self.capture_driver.start(p['mode'],self.capture_record)
                except Exception:
                    self.capture=None;self.capture_params=None;raise
                return json.dumps({'file_id':fid}).encode()
            else:
                if self.delegate is None:raise CommandError(7)
                return self.delegate.execute(action,p)
        return b'{}'

    def telemetry(self):
        with self.lock:
            return {'TMG035':self.play_file or 0,'TMG036':self.play_state,
                    'TMG037':self.store_frames&0xffffffff,'TMG038':self.store_bytes&0xffffffff,
                    'TMG039':self.store_bytes>>32,'TMG041':self.crc_errors}

    def close(self):
        self._stop_worker('play');self._stop_worker('store')
        if self.capture_driver and self.capture is not None:self.capture_driver.stop()
        if self.ssd_store:self.ssd_store.close()
        self.db.close()
