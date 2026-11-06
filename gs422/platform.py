"""Linux interfaces and bounded, cancellable subprocess reads."""
import fcntl
import ipaddress
import os
from pathlib import Path
import selectors
import signal
import socket
import struct
import subprocess
import termios
import time


class Reader:
    """One per collector. A stuck kernel child cannot create an unbounded backlog."""
    def __init__(self, stop):
        self.stop = stop
        self.pending = None

    def read(self, argv, timeout, limit=262144):
        if self.pending is not None:
            if self.pending.poll() is None:
                return None
            self.pending = None
        p = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                             stderr=subprocess.DEVNULL, start_new_session=True,
                             env={**os.environ, "LC_ALL": "C"})
        result = bytearray()
        deadline = time.monotonic() + timeout
        success = False
        try:
            os.set_blocking(p.stdout.fileno(), False)
            with selectors.DefaultSelector() as selector:
                selector.register(p.stdout, selectors.EVENT_READ)
                while not self.stop.is_set() and time.monotonic() < deadline:
                    if not selector.select(min(.1, max(0, deadline - time.monotonic()))):
                        continue
                    chunk = os.read(p.stdout.fileno(), 16384)
                    if not chunk:
                        try:
                            success = p.wait(timeout=max(.01, deadline-time.monotonic())) == 0
                        except subprocess.TimeoutExpired:
                            pass
                        break
                    result.extend(chunk)
                    if len(result) > limit:
                        break
        finally:
            # Kill descendants as well, including a child retaining the pipe after parent exit.
            if not success:
                for sig, grace in ((signal.SIGTERM, .2), (signal.SIGKILL, .05)):
                    try:
                        os.killpg(p.pid, sig)
                    except ProcessLookupError:
                        pass
                    try:
                        p.wait(timeout=grace)
                    except subprocess.TimeoutExpired:
                        pass
                if p.poll() is None:
                    self.pending = p
            p.stdout.close()
        if not success:
            return None
        try:
            return result.decode("utf-8", errors="strict")
        except UnicodeDecodeError:
            return None


def interfaces(config, root=Path("/sys/class/net")):
    candidates = []
    for path in root.iterdir():
        try:
            if not (path / "device").exists() or (path / "type").read_text().strip() != "1":
                continue
            wireless = (path / "wireless").exists() or (path / "phy80211").exists()
            def value(name):
                try:
                    return (path / name).read_text().strip()
                except OSError:
                    return ""
            candidates.append((str((path / "device").resolve()), value("dev_port"),
                               value("phys_port_name"), path.name, wireless))
        except OSError:
            continue
    candidates.sort()
    explicit = {x for x in (config.ethernet1, config.ethernet2) if x != "auto"}
    wired = [x[3] for x in candidates if not x[4] and x[3] not in explicit]
    wireless = [x[3] for x in candidates if x[4]]
    result = {}
    for label, name in (("IP1", config.ethernet1), ("IP2", config.ethernet2)):
        result[label] = name if name != "auto" else (wired.pop(0) if wired else None)
    result["WIFI"] = config.wifi if config.wifi != "auto" else (wireless[0] if wireless else None)
    return result


def ipv4(name):
    if not name or len(name.encode()) > 15:
        raise ValueError("missing interface")
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        request = struct.pack("256s", name.encode())
        addr = socket.inet_ntoa(fcntl.ioctl(sock, 0x8915, request)[20:24])
        mask = socket.inet_ntoa(fcntl.ioctl(sock, 0x891b, request)[20:24])
    address = ipaddress.IPv4Address(addr)
    if address.is_unspecified or address.is_loopback or address.is_link_local or address.is_multicast:
        raise ValueError("no usable IPv4")
    return addr, mask


class Serial:
    def __init__(self, path):
        self.fd = os.open(path, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK | os.O_CLOEXEC)
        self.saved = None
        try:
            fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            # Kernel exclusion also protects against programs ignoring advisory flock.
            fcntl.ioctl(self.fd, termios.TIOCEXCL)
            self.saved = termios.tcgetattr(self.fd)
            attr = termios.tcgetattr(self.fd)
            attr[0] = attr[1] = attr[3] = 0
            attr[2] = termios.CLOCAL | termios.CREAD | termios.CS8
            attr[4] = attr[5] = termios.B9600
            attr[6][termios.VMIN] = 0
            attr[6][termios.VTIME] = 0
            termios.tcsetattr(self.fd, termios.TCSANOW, attr)
        except BaseException:
            self.close()
            raise

    def close(self):
        if self.fd is not None:
            try:
                if self.saved is not None:
                    termios.tcsetattr(self.fd, termios.TCSANOW, self.saved)
                    fcntl.ioctl(self.fd, termios.TIOCNXCL)
            except OSError:
                pass
            os.close(self.fd)
            self.fd = None
