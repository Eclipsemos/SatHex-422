"""Published HT226 files. Local producers atomically rename complete files into a type directory."""
from contextlib import contextmanager
import os
from pathlib import Path
import stat

from .space_commands import CommandError


class PublishedFiles:
    def __init__(self, root):
        self.root = Path(root)

    @contextmanager
    def directory(self, group, file_type):
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        root = os.open(self.root, flags)
        try:
            parent = os.open(group, flags, dir_fd=root)
            try:
                directory = os.open(f'{file_type:02X}', flags, dir_fd=parent)
                try:
                    yield directory
                finally:
                    os.close(directory)
            finally:
                os.close(parent)
        finally:
            os.close(root)

    @staticmethod
    def entries(directory):
        for name in os.listdir(directory):
            if name.startswith('.'):
                continue
            info = os.stat(name, dir_fd=directory, follow_symlinks=False)
            if stat.S_ISREG(info.st_mode):
                yield name, (info.st_mtime_ns, info.st_dev, info.st_ino, info.st_size)

    def newest(self, file_type, count):
        with self.directory('replay', file_type) as directory:
            items = sorted(self.entries(directory), key=lambda item: (item[1][0], item[0]), reverse=True)
        if len(items) < count:
            raise CommandError(4)
        return items[:count]

    def send(self, file_type, items, sink, cancel, progress):
        with self.directory('replay', file_type) as directory:
            for name, identity in items:
                if cancel.is_set():
                    return
                fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
                with os.fdopen(fd, 'rb') as source:
                    info = os.fstat(fd)
                    if not stat.S_ISREG(info.st_mode) or identity != (
                            info.st_mtime_ns, info.st_dev, info.st_ino, info.st_size):
                        raise CommandError(8)
                    sink(name, source, cancel, progress)

    def clean(self, file_type):
        removed = 0
        with self.directory('clean', file_type) as directory:
            for name, _ in self.entries(directory):
                os.unlink(name, dir_fd=directory)
                removed += 1
            os.fsync(directory)
        return removed
