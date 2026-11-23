"""Temporary control-port test fixture. Requires the operator to free the UART."""
import os
from pathlib import Path
import pty
import sys
import tempfile
import time
from contextlib import ExitStack
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from gs422 import tmg
from gs422.core import Cache
from gs422.dual_uart import DualUART
from gs422.ota_staging import Staging
from gs422.ota_wire import file_packets
from gs422.revised_runtime import Revised


def main(port):
    with ExitStack() as stack:
        root = stack.enter_context(tempfile.TemporaryDirectory())
        stage = Staging(root, crc_profile='XMODEM', capacity=8192)
        stack.callback(stage.close)
        backend = Mock()
        backend.ota.side_effect = RuntimeError('intentional isolated QA exception')
        backend.version.return_value = 'CONTROL-QA'
        r = Revised(stage, Cache(10), dict.fromkeys(tmg.BY_ID,0), version='QA',
                    software_backend=backend, ota101=True)
        stack.callback(r.close)
        publish = r._set_erase_status
        def observe(value):
            publish(value)
            if value == 0x21:
                time.sleep(1.5)  # Only the test fixture stretches this short observable phase.
        r._set_erase_status = observe
        master, slave = pty.openpty()
        stack.callback(os.close, master)
        stack.callback(os.close, slave)
        ports = DualUART(r,port,os.ttyname(slave))
        stack.callback(ports.close)
        stage.gate(True)
        assert stage.begin(4,2,1) == 0
        r.start()
        for frame in file_packets(b'\xff'*499,'XMODEM',ota101=True):
            os.write(master,frame)
        deadline = time.monotonic()+6
        while r.raw['TMG010'] != 2:
            assert time.monotonic()<deadline
            ports.pump()
        print('CONTROL_QA_READY',flush=True)
        deadline = time.monotonic()+40
        while time.monotonic()<deadline:
            ports.pump(.005)
            if backend.version.called:
                for _ in range(4):
                    ports.pump(.01)
                return
        raise TimeoutError('control verification did not finish')


if __name__ == '__main__':
    main(sys.argv[1])
