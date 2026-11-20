"""D0351 software-file receiver with an explicit, bounded checksum profile.

Project convention for previously unspecified fields: data_length=N,
bitmap_size=ceil(total_frames/8), packet_checksum=sum(file bytes) mod 65536,
file_checksum=CRC-16/CCITT-FALSE. Not the TYPE30 control/3A upload format.
No software activation and no raw block-device writes.
"""
import json
from pathlib import Path
import sqlite3
import threading
import binascii
from .space_commands import CommandError


class LegacyUpload:
    def __init__(self, root):
        self.root=Path(root);self.root.mkdir(parents=True,exist_ok=True)
        self.db=sqlite3.connect(self.root/'legacy-upload.sqlite3',check_same_thread=False)
        self.db.execute('PRAGMA synchronous=FULL')
        self.db.execute('CREATE TABLE IF NOT EXISTS state(id INTEGER PRIMARY KEY CHECK(id=1), json TEXT)')
        self.db.execute('CREATE TABLE IF NOT EXISTS packets(idx INTEGER PRIMARY KEY, data BLOB)')
        row=self.db.execute('SELECT json FROM state WHERE id=1').fetchone()
        self.state=json.loads(row[0]) if row else dict(file_id=0,size=0,frames=0,checksum=0,errors=0,status=0,check=0)
        self.lock=threading.RLock()
        self._save()

    def _save(self):
        self.db.execute('INSERT OR REPLACE INTO state VALUES(1,?)',(json.dumps(self.state),))
        self.db.commit()

    def reset(self):
        """Explicit gate close cancels this legacy session, preserving no old packets."""
        with self.lock:
            self.db.execute('DELETE FROM packets')
            self.state.update(size=0,frames=0,status=0,check=0)
            self._save()

    def receive(self,p):
        with self.lock:
            try:
                data=bytes.fromhex(p['data_hex'])
                size,frames,index=p['file_size'],p['total_frames'],p['packet_index']
                if not 1<=size<=131072 or frames!=(size+511)//512 or not 0<=index<frames:
                    raise ValueError('bounds')
                if len(data)!=min(512,size-index*512) or p['data_length']!=len(data):
                    raise ValueError('length')
                if p['bitmap_size']!=(frames+7)//8 or p['packet_checksum']!=(sum(data)&65535):
                    raise ValueError('checksum/bitmap')
                if not 0<=p['file_checksum']<=65535:raise ValueError('file checksum')
                identity=(size,frames,p['file_checksum'])
                if self.state['size']:
                    if identity!=(self.state['size'],self.state['frames'],self.state['checksum']):
                        raise ValueError('different transfer requires gate close')
                else:
                    if self.state['file_id']==0xffffffff:raise ValueError('file id exhausted')
                    self.state.update(size=size,frames=frames,checksum=p['file_checksum'],
                                      file_id=self.state['file_id']+1,status=0x20,check=0)
                old=self.db.execute('SELECT data FROM packets WHERE idx=?',(index,)).fetchone()
                if old:
                    if old[0]!=data:raise ValueError('conflicting duplicate')
                    if self.state['status']==0x24:raise CommandError(8)
                    return self.snapshot()
                self.db.execute('INSERT INTO packets VALUES(?,?)',(index,data))
                self.state.update(check=2,status=0x20)
                self._save()
                count=self.db.execute('SELECT count(*) FROM packets').fetchone()[0]
                if count==frames:
                    crc=0xffff
                    for (block,) in self.db.execute('SELECT data FROM packets ORDER BY idx'):
                        crc=binascii.crc_hqx(block,crc)
                    ok=crc==self.state['checksum']
                    self.state.update(check=4 if ok else 5,status=0x23 if ok else 0x24)
                    self._save()
                    if not ok:raise CommandError(8)
                return self.snapshot()
            except (ValueError,KeyError,TypeError):
                self.db.rollback()
                row=self.db.execute('SELECT json FROM state WHERE id=1').fetchone()
                self.state=json.loads(row[0])
                self.state['errors']=(self.state['errors']+1)&0xffffffff
                self.state['check']=3
                self._save()
                raise CommandError(4)

    def snapshot(self):
        with self.lock:
            indexes=[r[0] for r in self.db.execute('SELECT idx FROM packets ORDER BY idx')]
            missing=0
            for index in indexes:
                if index!=missing:break
                missing+=1
            frames=self.state['frames']
            return dict(TMG008=self.state['file_id'],TMG009=missing if not frames or missing<frames else 0xffffffff,
                        TMG010=len(indexes),TMG011=self.state['errors'],TMG012=100*len(indexes)//frames if frames else 0,
                        TMG013=self.state['check'],TMG014=self.state['status'])

    def export_verified(self,path):
        with self.lock:
            if self.state['status']!=0x23:raise CommandError(8)
            import os
            with open(path,'xb') as stream:
                for (data,) in self.db.execute('SELECT data FROM packets ORDER BY idx'):stream.write(data)
                stream.flush();os.fsync(stream.fileno())

    def close(self):self.db.close()
