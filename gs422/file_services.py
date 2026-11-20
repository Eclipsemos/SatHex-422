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
from .space_commands import CommandError


class FileServices:
    timeout = 5

    def __init__(self, root, *, sources=None, sinks=None, capture_driver=None, ssd_root=None,
                 delegate=None, max_bytes=64*1024*1024):
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

    def _new(self, file_type):
        cursor = self.db.execute('INSERT INTO files(type,complete) VALUES(?,0)',(file_type,))
        self.db.commit()
        return cursor.lastrowid

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
            self._append(self.capture,payload,utc)

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
            self.capture_driver.stop()
            with self.lock:
                if self.capture is not None:self._complete(self.capture)
                self.capture=None
            return b'{}'
        with self.lock:
            if action in ('playback_link','playback_file'):
                if self.play_thread and self.play_thread.is_alive():raise CommandError(6)
                if action=='playback_link':self.link=dict(p)
                else:self.selection=dict(p)
            elif action=='playback_start':
                if self.play_thread and self.play_thread.is_alive():raise CommandError(6)
                if not self.link or not self.selection:raise CommandError(4)
                s,l=self.selection,self.link
                row=self.db.execute('SELECT type,complete FROM files WHERE id=?',(s['file_id'],)).fetchone()
                if not row or row!=(l['file_type'],1):raise CommandError(4)
                count=self.db.execute('SELECT count(*) FROM frames WHERE file=?',(s['file_id'],)).fetchone()[0]
                if not 0<=s['start_frame']<=s['end_frame']<count:raise CommandError(4)
                if s['rate'] not in (1,2,4):raise CommandError(4)
                records=self.db.execute('SELECT utc,body FROM frames WHERE file=? AND idx BETWEEN ? AND ? AND utc BETWEEN ? AND ? ORDER BY idx',
                    (s['file_id'],s['start_frame'],s['end_frame'],l['start_utc'],l['end_utc'])).fetchall()
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
            elif action=='storage_status':
                return json.dumps({'state':self.store_state,'frames':self.store_frames,'bytes':self.store_bytes,'crc_errors':self.crc_errors,'file_id':self.store_file,'error':self.last_error}).encode()
            elif action=='storage_clean':
                if self.capture is not None or any(t and t.is_alive() for t in (self.play_thread,self.store_thread)):raise CommandError(6)
                # Remove only locally owned completed records, never arbitrary paths.
                count=self.db.execute('SELECT count(*) FROM files WHERE complete=1').fetchone()[0]
                with self.db:
                    self.db.execute('DELETE FROM frames WHERE file IN (SELECT id FROM files WHERE complete=1)')
                    self.db.execute('DELETE FROM files WHERE complete=1')
                self.db.execute('VACUUM')
                return json.dumps({'deleted_files':count}).encode()
            elif action=='capture_control':
                if self.capture_driver is None:raise CommandError(7)
                if p['storage']!=0 or not self._is_memory_store():raise CommandError(7)
                if self.capture is not None:raise CommandError(6)
                fid=self._new(1);self.capture=fid
                try:self.capture_driver.start(p['mode'],self.capture_record)
                except Exception:
                    self.capture=None;raise
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
        self.db.close()
