"""Opt-in experimental binary UART service; never starts alongside the AT service."""
import argparse
import json
import logging
import os
from pathlib import Path
import select
import signal
import time

from .core import Config
from .platform import Serial
from .service import Service
from .space_protocol import Decoder, Message, encode
from .space_runtime import Adapter, Controller

LOG = logging.getLogger('gs422.space')


def transmit(fd, data):
    deadline = time.monotonic() + 2
    view = memoryview(data)
    while view:
        if time.monotonic() >= deadline:
            raise TimeoutError('UART transmit deadline')
        if select.select([], [fd], [], .01)[1]:
            try:
                written = os.write(fd, view)
                if not written:
                    raise OSError('UART write returned zero')
                view = view[written:]
            except BlockingIOError:
                pass


def run(config, baud, parity, stop_bits, idle_reset, adapter):
    collectors = Service(config)
    control = Controller(collectors.cache, adapter)
    decoder = Decoder()
    serial = None
    last_tm = time.monotonic()
    last_reset = last_tm
    errors = (0, 0)
    def stop(*_):
        collectors.stop.set()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, stop)
    try:
        serial = Serial(config.port, baud, parity, stop_bits)
        for kind, indices in (('NPU', (0, 1)), ('SOC', (2,)), ('CURRENT', (3,))):
            collectors.start_thread(collectors.collect, kind, indices)
        control.start()
        LOG.info('READY experimental-v1 baud=%d parity=%s stop_bits=%d', baud, parity, stop_bits)
        while not collectors.stop.is_set():
            data = b''
            if select.select([serial.fd], [], [], .001)[0]:
                try:
                    data = os.read(serial.fd, 4096)
                except BlockingIOError:
                    continue
                if not data:
                    raise OSError('UART disconnected')
            frames = decoder.feed(data)
            with control.lock:
                control.checksum_errors += decoder.checksum_errors - errors[0]
                control.format_errors += decoder.format_errors - errors[1]
                errors = (decoder.checksum_errors, decoder.format_errors)
            control.tick()
            for payload in frames:
                reply = control.handle(payload)
                if reply is not None:
                    if Message.parse(reply).kind == 0x12:
                        last_tm = time.monotonic()
                    transmit(serial.fd, encode(reply))
            now = time.monotonic()
            if idle_reset and now - max(last_tm, last_reset) >= idle_reset:
                serial.close()
                serial = Serial(config.port, baud, parity, stop_bits)
                decoder.clear()
                with control.lock:
                    control.resets += 1
                last_reset = now
                LOG.info('UART_RESET telemetry_timeout')
        return 0
    finally:
        collectors.stop.set()
        control.close()
        for thread in collectors.threads:
            thread.join(timeout=4)
        if serial is not None:
            serial.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', default='/etc/gs422.conf')
    parser.add_argument('--baud', type=int, choices=(9600, 19200, 38400, 57600, 115200), default=115200)
    parser.add_argument('--parity', choices=('N', 'E', 'O'), default='E')
    parser.add_argument('--stop-bits', type=int, choices=(1, 2), default=1)
    parser.add_argument('--idle-reset', type=float, default=10, help='Seconds without telemetry request; 0 disables in lab')
    parser.add_argument('--adapters', type=Path, help='Administrator-owned JSON with actions argv and timeout')
    args = parser.parse_args()
    if args.idle_reset < 0 or (args.idle_reset != 0 and args.idle_reset < 1):
        parser.error('idle reset must be 0 or at least 1 second')
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    settings = json.loads(args.adapters.read_text()) if args.adapters else {}
    adapter = Adapter(settings.get('actions'), settings.get('timeout', 5))
    try:
        return run(Config.read(args.config), args.baud, args.parity, args.stop_bits, args.idle_reset, adapter)
    except (OSError, ValueError, TimeoutError):
        LOG.exception('EXPERIMENTAL_SERVICE_FAILED')
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
