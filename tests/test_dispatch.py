"""Check desktop apartment ownership using thread-affine fake application state."""

import importlib.util
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
MODULE_SPEC = importlib.util.spec_from_file_location("desktop_dispatch", ROOT / "src" / "desktop_agent" / "dispatch.py")
DISPATCH = importlib.util.module_from_spec(MODULE_SPEC)
MODULE_SPEC.loader.exec_module(DISPATCH)


class DispatchTests(unittest.TestCase):
    def test_concurrent_requests_reuse_one_apartment_and_cleanup_on_owner(self):
        lifecycle = []
        objects = {}

        def initialize():
            lifecycle.append(("init", threading.get_ident()))

        def finalize():
            lifecycle.append(("close", threading.get_ident()))
            objects.clear()

        def create():
            objects["handle"] = {"owner": threading.get_ident(), "value": 0}

        def increment():
            obj = objects["handle"]
            self.assertEqual(obj["owner"], threading.get_ident(), "proxy crossed apartments")
            obj["value"] += 1
            return obj["value"]

        dispatcher = DISPATCH.SerialDispatcher("test-owner", initialize, finalize)
        self.addCleanup(dispatcher.close)
        dispatcher.call(create)
        with ThreadPoolExecutor(max_workers=8) as callers:
            results = list(callers.map(lambda _: dispatcher.call(increment), range(40)))
        self.assertEqual(list(range(1, 41)), sorted(results))
        dispatcher.close()
        self.assertEqual(["init", "close"], [event[0] for event in lifecycle])
        self.assertEqual(lifecycle[0][1], lifecycle[1][1])
        self.assertNotEqual(threading.get_ident(), lifecycle[0][1])
        self.assertEqual({}, objects)

    def test_reentrant_adapter_calls_do_not_deadlock(self):
        dispatcher = DISPATCH.SerialDispatcher("test-reentrant")
        self.addCleanup(dispatcher.close)
        result = dispatcher.call(lambda: dispatcher.call(lambda: threading.get_ident()))
        self.assertNotEqual(threading.get_ident(), result)

    def test_failure_is_returned_once_and_does_not_kill_worker_or_retry_write(self):
        dispatcher = DISPATCH.SerialDispatcher("test-failure")
        self.addCleanup(dispatcher.close)
        writes = []

        def uncertain_write():
            writes.append("written")
            raise ValueError("provider failed after writing")

        with self.assertRaisesRegex(ValueError, "after writing"):
            dispatcher.call(uncertain_write)
        self.assertEqual(["written"], writes)
        self.assertEqual(7, dispatcher.call(lambda: 7))

    def test_initialization_failure_rejects_future_work_without_hanging(self):
        def bad_init():
            raise ValueError("cannot initialize apartment")

        dispatcher = DISPATCH.SerialDispatcher("test-init-failure", initialize=bad_init)
        self.addCleanup(dispatcher.close)
        with self.assertRaises(ValueError):
            dispatcher.call(lambda: self.fail("must not run without initialization"))
        with self.assertRaisesRegex(RuntimeError, "initialization failed"):
            dispatcher.call(lambda: 3)

    def test_separate_workers_do_not_block_each_other(self):
        com = DISPATCH.SerialDispatcher("test-com")
        uia = DISPATCH.SerialDispatcher("test-uia")
        self.addCleanup(com.close)
        self.addCleanup(uia.close)
        started = threading.Event()
        release = threading.Event()

        def busy_app():
            started.set()
            self.assertTrue(release.wait(3))

        with ThreadPoolExecutor(max_workers=1) as caller:
            pending = caller.submit(com.call, busy_app)
            try:
                self.assertTrue(started.wait(2))
                self.assertEqual("available", uia.call(lambda: "available"))
            finally:
                release.set()
            pending.result(timeout=2)

    def test_closed_dispatcher_rejects_new_actions(self):
        dispatcher = DISPATCH.SerialDispatcher("test-closed")
        dispatcher.close()
        with self.assertRaisesRegex(RuntimeError, "closed"):
            dispatcher.call(lambda: self.fail("must not run after shutdown"))

    def test_expired_queued_write_never_executes_after_blocker_finishes(self):
        dispatcher = DISPATCH.SerialDispatcher("test-queue-expiry", max_queue_wait=0.05)
        self.addCleanup(dispatcher.close)
        started = threading.Event()
        release = threading.Event()
        writes = []

        def blocked_provider():
            started.set()
            self.assertTrue(release.wait(3))

        with ThreadPoolExecutor(max_workers=1) as caller:
            pending = caller.submit(dispatcher.call, blocked_provider)
            try:
                self.assertTrue(started.wait(2))
                with self.assertRaisesRegex(DISPATCH.QueueWaitTimeout, "action was not started"):
                    dispatcher.call(lambda: writes.append("stale write"))
            finally:
                release.set()
            pending.result(timeout=2)
        dispatcher.call(lambda: None)  # owner has now consumed the canceled queue item
        self.assertEqual([], writes)

    def test_owner_rejects_stale_write_even_before_waiting_caller_wakes(self):
        # Advance only the dispatcher's monotonic clock. Future.result keeps
        # waiting on real time, reproducing a caller that has not canceled yet.
        clock = [0.0]
        dispatcher = DISPATCH.SerialDispatcher("test-owner-deadline", max_queue_wait=10)
        self.addCleanup(dispatcher.close)
        started = threading.Event()
        release = threading.Event()
        writes = []

        def blocker():
            started.set()
            self.assertTrue(release.wait(3))

        with patch.object(DISPATCH, "time", SimpleNamespace(monotonic=lambda: clock[0])):
            with ThreadPoolExecutor(max_workers=2) as callers:
                first = callers.submit(dispatcher.call, blocker)
                self.assertTrue(started.wait(2))
                second = callers.submit(dispatcher.call, lambda: writes.append("stale"))
                try:
                    deadline = time.monotonic() + 2
                    while dispatcher._queue.qsize() == 0 and time.monotonic() < deadline:
                        time.sleep(0.001)
                    self.assertEqual(1, dispatcher._queue.qsize())
                    clock[0] = 11
                finally:
                    release.set()
                first.result(timeout=2)
                with self.assertRaises(DISPATCH.QueueWaitTimeout):
                    second.result(timeout=2)
        self.assertEqual([], writes)


if __name__ == "__main__":
    unittest.main()
