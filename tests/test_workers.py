import queue
import threading
import time
import unittest
from gs422.service import Worker


class WorkerTests(unittest.TestCase):
    def test_single_setting_does_not_block_other_worker(self):
        stop=threading.Event(); entered=threading.Event(); release=threading.Event()
        replies=queue.Queue()
        def slow(kind,args):
            entered.set(); release.wait(2); return '+WIFI:OK'
        setter=Worker(1,1,stop,replies.put,slow)
        fast=Worker(2,2,stop,replies.put,lambda *_:'+IP2:192.0.2.1,255.255.255.0')
        try:
            self.assertTrue(setter.submit('SET_WIFI',('ssid','secret')))
            self.assertTrue(entered.wait(1))
            self.assertFalse(setter.submit('SET_WIFI',('ssid','secret')))
            self.assertTrue(fast.submit('IP2',None))
            self.assertEqual(replies.get(timeout=1),'+IP2:192.0.2.1,255.255.255.0')
            release.set()
            self.assertEqual(replies.get(timeout=1),'+WIFI:OK')
        finally:
            stop.set(); release.set(); setter.join(); fast.join()

    def test_expired_queue_does_not_execute_setting(self):
        stop=threading.Event(); replies=queue.Queue(); called=[]
        worker=Worker(1,1,stop,replies.put,lambda *_:called.append(True))
        try:
            worker.slots.acquire()
            worker.queue.put((time.monotonic()-6,'SET_WIFI',('ssid','secret')))
            self.assertEqual(replies.get(timeout=1),'+WIFI:ERR')
            self.assertEqual(called,[])
        finally:
            stop.set(); worker.join()


if __name__=='__main__':unittest.main()
