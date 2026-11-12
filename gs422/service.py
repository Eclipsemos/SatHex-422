"""Serial event loop, three independent collectors and bounded command workers."""
import argparse
import json
import logging
import os
import queue
import select
import signal
import threading
import time
import sys
from pathlib import Path
from . import __version__
from .core import Cache, Config, Framer, parse, parse_smi, scaled
from .network import NMClient
from .platform import Reader, Serial, interfaces, ipv4
from .cpu import CPUCache
from .ip_admin import IPAdmin
from .ssh_admin import SSHAdmin
from .sdr import request as sdr_request

LOG = logging.getLogger("gs422")


class Worker:
    def __init__(self, count, capacity, stop, output, function):
        self.stop, self.output, self.function = stop, output, function
        self.slots = threading.BoundedSemaphore(capacity)
        self.queue = queue.Queue(capacity)
        self.threads = [threading.Thread(target=self.run, daemon=True) for _ in range(count)]
        for thread in self.threads:
            thread.start()

    def submit(self, kind, args):
        if not self.slots.acquire(blocking=False):
            return False
        self.queue.put_nowait((time.monotonic(), kind, args))
        return True

    def run(self):
        while not self.stop.is_set():
            try:
                entered, kind, args = self.queue.get(timeout=.1)
            except queue.Empty:
                continue
            try:
                prefix = "WIFI" if kind == "SET_WIFI" else 'DOCKER' if kind.startswith('DOCKER') else kind.removesuffix('_ADMIN')
                if self.stop.is_set():
                    continue
                if time.monotonic() - entered >= 5:
                    result = "+" + prefix + ":ERR"
                else:
                    try:
                        result = self.function(kind, args)
                    except Exception:
                        result = "+" + prefix + ":ERR"
                        LOG.warning("REQUEST_FAILED kind=%s", kind)
                self.output(result)
            finally:
                args = None
                self.slots.release()
                self.queue.task_done()

    def join(self):
        for thread in self.threads:
            thread.join(timeout=15)
        while True:
            try:
                self.queue.get_nowait()
                self.queue.task_done()
                self.slots.release()
            except queue.Empty:
                break


class Service:
    def __init__(self, config):
        self.config = config
        self.stop = threading.Event()
        self.failed = False
        self.cache = Cache(config.cache_ttl_ms)
        self.cpu = CPUCache(config.cache_ttl_ms)
        self.tx = queue.Queue(512)
        self.serial = None
        self.threads = []
        self.workers = []
        self.ip_admin = IPAdmin(self.stop)
        self.ssh_admin = SSHAdmin()

    def emit(self, reply):
        if self.stop.is_set():
            return
        try:
            self.tx.put_nowait(reply.encode("utf-8") + b"\r\n")
        except queue.Full:
            LOG.error("TX_OVERFLOW")
            self.failed = True
            self.stop.set()

    def collect(self, kind, indices):
        c = self.config
        reader = Reader(self.stop)
        previous = None
        while not self.stop.is_set():
            values = (None,) * len(indices)
            try:
                if kind == "NPU":
                    text = reader.read([c.smi, "-a"], 3)
                    if text is not None:
                        values = parse_smi(text, c.card)
                else:
                    path, divisor = (c.soc_temp_path, c.soc_divisor) if kind == "SOC" else (c.current_path, c.current_divisor)
                    if path:
                        text = reader.read(["/bin/cat", "--", path], c.sensor_timeout_ms / 1000, 4096)
                        if text is not None:
                            values = (scaled(text, divisor),)
            except (OSError, ValueError):
                pass
            for index, value in zip(indices, values):
                self.cache.update(index, value)
            state = tuple(value is not None for value in values)
            if state != previous:
                LOG.info("SAMPLE source=%s valid=%s", kind, ",".join("1" if v else "0" for v in state))
                previous = state
            self.stop.wait(c.sample_interval_ms / 1000)

    def collect_cpu(self):
        reader = Reader(self.stop)
        previous = None
        while not self.stop.is_set():
            started = time.monotonic()
            ok = False
            try:
                text = reader.read([sys.executable, str(Path(__file__).with_name('cpu.py'))], 2)
                if text is not None:
                    self.cpu.update(json.loads(text))
                    ok = True
            except (OSError, ValueError, KeyError, TypeError):
                pass
            if not ok:
                self.cpu.failed()
            if ok != previous:
                LOG.info('SAMPLE source=CPU valid=%d', ok)
                previous = ok
            self.stop.wait(max(.1, 1 - (time.monotonic() - started)))

    def network(self, kind, args):
        mapping = interfaces(self.config)
        if kind in ("IP1", "IP2"):
            return "+" + kind + ":" + ",".join(ipv4(mapping[kind]))
        if kind.endswith('_ADMIN'):
            label = kind.removesuffix('_ADMIN')
            return self.ip_admin.execute(label, mapping[label], args)
        if kind == 'SET_WIFI' and self.ip_admin.busy():
            return '+WIFI:ERR,BUSY'
        client = NMClient(self.stop, self.config.wifi_timeout if kind == "SET_WIFI" else 5)
        if kind == "WIFI":
            return client.query(mapping["WIFI"])
        return client.connect(mapping["WIFI"], *args)

    def docker(self, kind, args):
        # Reader's overall deadline also protects against slow/trickling daemon replies.
        # Retain one Reader per worker to avoid multiplying a kernel-stuck helper.
        if not hasattr(self.docker_local, 'reader'):
            self.docker_local.reader = Reader(self.stop)
        argv = [sys.executable, str(Path(__file__).with_name('docker.py')),
                '--socket', self.config.docker_socket, '--allow', self.config.docker_allow,
                *args]
        result = self.docker_local.reader.read(argv, 22 if kind == 'DOCKER_CONTROL' else 10, 4096)
        if result is None:
            return '+DOCKER:ERR,' + ('OUTCOME_UNKNOWN' if kind == 'DOCKER_CONTROL' else 'TIMEOUT')
        result = result.rstrip('\n')
        if not result.startswith('+DOCKER:') or '\n' in result or '\r' in result:
            return '+DOCKER:ERR,INVALID_RESPONSE'
        return result

    def storage(self, kind, page):
        result = self.storage_reader.read(
            [sys.executable, str(Path(__file__).with_name('storage.py')), str(page)], 8, 8192)
        if result is None:
            return '+DISK:ERR,TIMEOUT'
        lines = result.rstrip('\n').split('\n')
        if not lines or any(not line.startswith('+DISK:') or '\r' in line for line in lines):
            return '+DISK:ERR,INVALID_RESPONSE'
        return '\r\n'.join(lines)

    def transmit(self):
        while not self.stop.is_set():
            try:
                data = self.tx.get(timeout=.1)
            except queue.Empty:
                continue
            try:
                deadline = time.monotonic() + 5
                view = memoryview(data)
                while view and not self.stop.is_set():
                    if time.monotonic() >= deadline:
                        raise TimeoutError("serial write deadline")
                    if select.select([], [self.serial.fd], [], .1)[1]:
                        try:
                            written = os.write(self.serial.fd, view)
                            if written == 0:
                                raise OSError("serial write zero")
                            view = view[written:]
                        except BlockingIOError:
                            continue
                LOG.info("TX_QUEUED bytes=%d", len(data))
            except (OSError, TimeoutError):
                LOG.error("TX_FAILED")
                self.failed = True
                self.stop.set()
            finally:
                self.tx.task_done()

    def start_thread(self, function, *args):
        thread = threading.Thread(target=function, args=args, daemon=True)
        thread.start()
        self.threads.append(thread)

    def run(self):
        self.serial = Serial(self.config.port)
        try:
            for kind, indices in (("NPU", (0, 1)), ("SOC", (2,)), ("CURRENT", (3,))):
                self.start_thread(self.collect, kind, indices)
            self.start_thread(self.collect_cpu)
            self.start_thread(self.transmit)
            ip = Worker(2, 128, self.stop, self.emit, self.network)
            wifi = Worker(1, 32, self.stop, self.emit, self.network)
            setting = Worker(1, 1, self.stop, self.emit, self.network)
            self.docker_local = threading.local()
            docker_query = Worker(1, 8, self.stop, self.emit, self.docker)
            docker_control = Worker(1, 1, self.stop, self.emit, self.docker)
            ssh = Worker(1, 1, self.stop, self.emit, lambda kind, args: self.ssh_admin.execute(args))
            self.storage_reader = Reader(self.stop)
            storage = Worker(1, 1, self.stop, self.emit, self.storage)
            sdr = Worker(1, 8, self.stop, self.emit, lambda kind, args: sdr_request(args))
            self.workers = [ip, wifi, setting, docker_query, docker_control, ssh, storage, sdr]
            framing = Framer()
            LOG.info("READY version=%s baud=9600 cache_ttl_ms=%d", __version__, self.config.cache_ttl_ms)
            while not self.stop.is_set():
                if not select.select([self.serial.fd], [], [], .1)[0]:
                    continue
                try:
                    data = os.read(self.serial.fd, 4096)
                except BlockingIOError:
                    continue
                if not data:
                    raise OSError("serial disconnected")
                for frame in framing.feed(data):
                    kind, args = parse(frame)
                    # Do not log raw frames, SSIDs, passwords, or external error text.
                    LOG.info("RX_COMMAND kind=%s", kind)
                    if kind == "STA":
                        self.emit(self.cache.reply())
                    elif kind == "CPU":
                        self.emit(self.cpu.reply(self.cache.value(2)))
                    elif kind == "ERROR":
                        self.emit("ERROR")
                    elif kind == "WIFI_ERR":
                        self.emit("+WIFI:ERR")
                    elif kind == 'DOCKER_ERR':
                        self.emit('+DOCKER:ERR,BAD_COMMAND')
                    elif kind in ('IP1_ERR', 'IP2_ERR', 'SSH_ERR'):
                        self.emit('+' + kind.removesuffix('_ERR') + ':ERR,BAD_COMMAND')
                    elif kind == 'DISK_ERR':
                        self.emit('+DISK:ERR,BAD_COMMAND')
                    elif kind == 'SDR_ERR':
                        self.emit('+SDR:ERR,BAD_COMMAND')
                    elif kind == 'SDR':
                        if not sdr.submit(kind, args):
                            self.emit('+SDR:ERR,BUSY')
                    elif kind == 'DISK':
                        if not storage.submit(kind, args):
                            self.emit('+DISK:ERR,BUSY')
                    elif kind == 'SSH':
                        if not ssh.submit(kind, args):
                            self.emit('+SSH:ERR,BUSY')
                    elif kind.endswith('_ADMIN'):
                        if not setting.submit(kind, args):
                            self.emit('+' + kind.removesuffix('_ADMIN') + ':ERR,BUSY')
                    elif kind in ('DOCKER', 'DOCKER_CONTROL'):
                        worker = docker_control if kind == 'DOCKER_CONTROL' else docker_query
                        if not worker.submit(kind, args):
                            self.emit('+DOCKER:ERR,BUSY')
                    else:
                        worker = ip if kind in ("IP1", "IP2") else setting if kind == "SET_WIFI" else wifi
                        if not worker.submit(kind, args):
                            self.emit("+" + ("WIFI" if kind == "SET_WIFI" else kind) + ":ERR")
                    frame = args = None
        finally:
            self.stop.set()
            for worker in self.workers:
                worker.join()
            try:
                self.ip_admin.rollback()
            except Exception:
                LOG.warning('NETWORK_ROLLBACK_FAILED')
            for thread in self.threads:
                thread.join(timeout=4)
            self.serial.close()
        return 1 if self.failed else 0


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument("--config", default="/etc/gs422.conf")
    parser.add_argument("--show-interfaces", metavar="CONFIG")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        c = Config.read(args.show_interfaces or args.config)
        if args.show_interfaces:
            for key, value in interfaces(c).items():
                print(key + "=" + (value or "-"))
            return 0
        service = Service(c)
        for sig in (signal.SIGTERM, signal.SIGINT):
            signal.signal(sig, lambda *_: service.stop.set())
        return service.run()
    except Exception as error:
        LOG.error("START_OR_RUNTIME_FAILED type=%s", type(error).__name__)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
