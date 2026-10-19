"""A persistent owner thread for apartment-bound desktop automation.

Initialization, calls, idle pumping and cleanup all run on that one thread.
Windows APIs are injected by the agent so ordering and error behaviour can be
tested on any platform without opening a desktop or using an app session.
"""

import queue
import threading
import time
from concurrent.futures import Future, TimeoutError as FutureTimeoutError


class QueueWaitTimeout(TimeoutError):
    """The requested call was canceled before its owner thread started it."""


class SerialDispatcher:
    def __init__(self, name, initialize=None, finalize=None, idle=None, max_pending=256, max_queue_wait=10):
        self._name = name
        self._initialize = initialize
        self._finalize = finalize
        self._idle = idle
        self._max_queue_wait = max_queue_wait
        self._queue = queue.Queue(maxsize=max_pending)
        self._lock = threading.Lock()
        self._thread = None
        self._owner = None
        self._closed = False
        self._failure = None

    def call(self, function, *args, **kwargs):
        # Adapter/helper code may call back into its own apartment. It must not
        # queue behind itself and deadlock.
        if threading.get_ident() == self._owner:
            return function(*args, **kwargs)
        future = Future()
        deadline = time.monotonic() + self._max_queue_wait
        with self._lock:
            if self._closed:
                raise RuntimeError(f"{self._name} dispatcher is closed")
            if self._failure is not None:
                raise RuntimeError(f"{self._name} initialization failed") from self._failure
            if self._thread is None:
                self._thread = threading.Thread(target=self._run, name=self._name, daemon=True)
                self._thread.start()
            try:
                self._queue.put_nowait((future, function, args, kwargs, deadline))
            except queue.Full:
                raise QueueWaitTimeout(f"{self._name} queue is full; action was not started") from None
        try:
            return future.result(timeout=max(0, deadline - time.monotonic()))
        except FutureTimeoutError:
            # cancel() atomically succeeds only before set_running... in the
            # owner. Expired queued writes therefore cannot execute later.
            if future.cancel():
                raise QueueWaitTimeout(f"{self._name} queue wait expired; action was not started") from None
            # Running native COM/UIA cannot safely be canceled. Keep its owner
            # occupied and never retry an action with an uncertain outcome.
            return future.result()

    def _run(self):
        self._owner = threading.get_ident()
        initialized = False
        try:
            if self._initialize:
                self._initialize()
            initialized = True
            while True:
                try:
                    item = self._queue.get(timeout=0.05 if self._idle else None)
                except queue.Empty:
                    self._idle()
                    continue
                if item is None:
                    break
                future, function, args, kwargs, deadline = item
                if future.set_running_or_notify_cancel():
                    if time.monotonic() >= deadline:
                        # The HTTP waiter may be descheduled at its deadline.
                        # The owner independently rejects stale work before any
                        # application function is invoked.
                        future.set_exception(QueueWaitTimeout(
                            f"{self._name} queue wait expired; action was not started"
                        ))
                        continue
                    try:
                        future.set_result(function(*args, **kwargs))
                    except BaseException as error:
                        future.set_exception(error)
                if self._idle:
                    self._idle()
        except BaseException as error:
            with self._lock:
                self._failure = error
                while True:
                    try:
                        item = self._queue.get_nowait()
                    except queue.Empty:
                        break
                    if item is not None and item[0].set_running_or_notify_cancel():
                        item[0].set_exception(error)
        finally:
            if initialized and self._finalize:
                self._finalize()
            self._owner = None

    def close(self, timeout=5):
        """Cancel queued work and wait briefly for the active native call.

        An in-flight native call cannot be killed safely. Threads are daemonized
        so a hung app cannot prevent agent process shutdown.
        """
        with self._lock:
            if not self._closed:
                self._closed = True
                while True:
                    try:
                        item = self._queue.get_nowait()
                    except queue.Empty:
                        break
                    if item is not None:
                        item[0].cancel()
                self._queue.put_nowait(None)
            thread = self._thread
        if thread and threading.get_ident() != self._owner:
            thread.join(timeout=timeout)
