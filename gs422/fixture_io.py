"""Explicit file-only external-device fixtures. Never identifies as real hardware."""
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import zlib


class FixtureIO:
    def __init__(self,root):
        self.root=Path(root);self.root.mkdir(parents=True,exist_ok=True)
        self.cancel=threading.Event();self.thread=None;self.error=None
        self.sources={7:self.solid_source}
        self.sinks={1:self.sink('downlink-1.bin'),2:self.sink('downlink-2.bin')}

    def file_sink(self, link):
        directory = self.root / ('main-downlink' if link == 0 else 'backup-downlink')
        directory.mkdir(exist_ok=True)
        def send(name, source, cancel, progress):
            with tempfile.NamedTemporaryFile(dir=directory, prefix='.partial-', delete=False) as output:
                partial = Path(output.name)
                try:
                    while not cancel.is_set():
                        block = source.read(4096)
                        if not block:
                            output.flush()
                            os.fsync(output.fileno())
                            output.close()
                            os.replace(partial, directory / name)
                            return
                        output.write(block)
                        progress()
                finally:
                    partial.unlink(missing_ok=True)
        return send

    def solid_source(self,start,count,cancel):
        frames=[b'fixture-solid-0',b'fixture-solid-1',b'fixture-solid-2']
        if not 0<=start<len(frames) or count and start+count>len(frames):raise ValueError('fixture range')
        for body in frames[start:None if count==0 else start+count]:
            if cancel.is_set():return
            yield body,zlib.crc32(body)

    def sink(self,name):
        path=self.root/name
        def send(body,cancel):
            if cancel.is_set():return
            if path.is_symlink():raise ValueError('fixture sink is a symlink')
            if path.exists() and path.stat().st_size+len(body)>16*1024*1024:raise ValueError('fixture output limit')
            with path.open('ab') as output:output.write(body);output.flush();os.fsync(output.fileno())
        return send

    def start(self,mode,receiver):
        if mode not in (1,2,3):raise ValueError('fixture mode')
        if self.thread and self.thread.is_alive():raise RuntimeError('fixture already recording')
        self.cancel.clear();self.error=None
        def work():
            try:
                # Finite fixture stream; explicit stop completes the record file.
                for index in range(20):
                    if self.cancel.wait(.1):return
                    receiver(f'fixture-star mode={mode} frame={index}'.encode(),time.time())
            except Exception as error:self.error=type(error).__name__
        self.thread=threading.Thread(target=work,daemon=True);self.thread.start()

    def stop(self):
        self.cancel.set()
        if self.thread:
            self.thread.join(timeout=2)
            if self.thread.is_alive():raise TimeoutError('fixture did not stop')
        if self.error:raise RuntimeError('fixture receive failed: '+self.error)
