"""Serial event loop, three independent collectors and bounded command workers."""
import argparse
import logging
import os
import queue
import select
import signal
import threading
import time
from . import __version__
from .core import Cache, Config, Framer, parse, parse_smi, scaled
from .network import NMClient
from .platform import Reader, Serial, interfaces, ipv4

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
                prefix = "WIFI" if kind == "SET_WIFI" else kind
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
        self.tx = queue.Queue(512)
        self.serial = None
        self.threads = []
        self.workers = []

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

    def network(self, kind, args):
        mapping = interfaces(self.config)
        if kind in ("IP1", "IP2"):
            return "+" + kind + ":" + ",".join(ipv4(mapping[kind]))
        client = NMClient(self.stop, self.config.wifi_timeout if kind == "SET_WIFI" else 5)
        if kind == "WIFI":
            return client.query(mapping["WIFI"])
        return client.connect(mapping["WIFI"], *args)

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
            self.start_thread(self.transmit)
            ip = Worker(2, 128, self.stop, self.emit, self.network)
            wifi = Worker(1, 32, self.stop, self.emit, self.network)
            setting = Worker(1, 1, self.stop, self.emit, self.network)
            self.workers = [ip, wifi, setting]
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
                    elif kind == "ERROR":
                        self.emit("ERROR")
                    elif kind == "WIFI_ERR":
                        self.emit("+WIFI:ERR")
                    else:
                        worker = ip if kind in ("IP1", "IP2") else setting if kind == "SET_WIFI" else wifi
                        if not worker.submit(kind, args):
                            self.emit("+" + ("WIFI" if kind == "SET_WIFI" else kind) + ":ERR")
                    frame = args = None
        finally:
            self.stop.set()
            for worker in self.workers:
                worker.join()
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
