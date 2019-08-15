import contextlib
import logging

from .acts import *
from .exceptions import *
from .holders import *


__all__ = [
    "Task",
    "get_time",
    "sleep",
    "wake_at",
    "schedule",
    "wait_event",
    "pop_event",
    "get_tk",
    "new_task",
    "this_task",
    "block_cancellation",
    "timeout_after",
    "timeout_at",
    "ignore_after",
    "ignore_at",
]


logger = logging.getLogger(__name__)


class Task:

    _next_id = 0


    def __init__(self, coro):

        # Task identification
        self.id = Task._next_id
        Task._next_id += 1

        # Task info
        self.coro = coro            # Wrapped coroutine
        self.daemon = False         # Result won't be ignored
        self.report_crash = True    # Task exception will be printed

        # Task state (read only)
        self.state = "INITIAL"      # Debug state
        self.cancelled = False      # Flag for task cancellation
        self.terminated = False     # Flag for task termination
        self.sleep = None           # Time of next wake from sleep
        self.timeout = None         # Time of next timeout
        self.waiting = SetHolder()  # Tasks waiting for termination
        self.cancel_func = None     # Function invoked on cancellation
        self.cancel_pending = None  # Exception raised on next blocking call
        self.allow_cancel = True    # Task can be cancelled
        self.joined = False         # Task termination wasn't ignored
        self.next_event = 0         # Index of next event that will be returned next

        # Task result / exception
        self._result_val = None
        self._result_exc = None

        # Value to send on resumation
        self._val = None

        # Bound methods for coroutine
        self._send = coro.send

        # Stack of timeout times
        self._deadlines = []


    def __repr__(self):
        return (
            f"<{type(self).__qualname__} "
            f"id={self.id} coro={self.coro.__qualname__} state={self.state}>"
        )


    def __del__(self):
        self.coro.close()
        if not any((self.joined, self.cancelled, self.daemon, self.exception)):
            raise RuntimeError(f"{self} wasn't joined")


    async def wait(self):
        if not self.terminated:
            await _wait_holder(self.waiting, "TASK_WAIT")


    async def cancel(self, *, exc=TaskCancelled, blocking=True):
        if self.terminated:
            self.joined = True
            return False

        await _cancel_task(self, exc=exc)
        if blocking:
            await self.wait()
        return True


    async def join(self):
        await self.wait()
        self.joined = True
        if self.exception:
            raise TaskError("Task crashed") from self.exception
        return self.result


    @property
    def result(self):
        if not self.terminated:
            raise RuntimeError("Task still running")
        self.joined = True
        if self._result_exc:
            raise self._result_exc
        else:
            return self._result_val


    @result.setter
    def result(self, val):
        self._result_val = val
        self._result_exc = None


    @property
    def exception(self):
        if not self.terminated:
            raise RuntimeError("Task still running")
        return self._result_exc


    @exception.setter
    def exception(self, exc):
        self._result_val = None
        self._result_exc = exc


async def get_time():
    return await _get_time()


async def sleep(tm):
    return await _sleep(tm, False)


async def wake_at(tm):
    return await _sleep(tm, True)


async def schedule():
    await sleep(0)


async def wait_event():
    await _wait_event()


async def pop_event(*, blocking=True):
    if not blocking:
        return await _pop_event()
    while True:
        try:
            return await _pop_event()
        except NoEvent:
            await _wait_event()


async def get_tk():
    return await _get_tk()


async def new_task(
    coro,
    *,
    eventless=False,
    daemon=False,
    allow_cancel=True,
    report_crash=True,
):
    task = await _new_task(coro)
    task.next_event = -1 if eventless else 0
    task.allow_cancel = allow_cancel
    task.daemon = daemon
    task.report_crash = report_crash
    return task


async def this_task():
    return await _this_task()


class _CancellationHelper:

    async def __aenter__(self):
        self.task = await this_task()
        self._previous = self.task.allow_cancel
        self.task.allow_cancel = False
        return self


    async def __aexit__(self, ty, val, tb):
        self.task.allow_cancel = self._previous
        if isinstance(val, CancelledError) and not self.task.allow_cancel:
            self.task.cancel_pending = val
            return True
        else:
            return False


def block_cancellation(coro=None):
    if coro is None:
        return _CancellationHelper()
    else:
        async def _run():
            async with _CancellationHelper():
                return await coro
        return _run()


class _TimeoutHelper:


    def __init__(self, tm, *, absolute, ignore=False, result=None):
        self._tm = tm
        self._absolute = absolute
        self._ignore = ignore
        self._result = result
        self.expired = False
        self.result = None


    async def __aenter__(self):
        task = await this_task()

        if not self._absolute:
            self._tm += await get_time()
            self._absolute = True

        self._deadlines = task._deadlines
        self._deadlines.append(self._tm)
        self._previous = await _add_timeout(self._tm)
        return self


    async def __aexit__(self, ty, val, tb):
        now = await _remove_timeout(self._previous)

        try:
            if ty in (TaskTimeout, TimeoutCancellation):
                # Check who caused this timeout
                for n, deadline in enumerate(self._deadlines):
                    if deadline <= now:
                        break

                # No one raised this timeout? Raise a RuntimeError.
                else:
                    raise RuntimeError("Unexpected timeout")

                # Some other outer timeout timed us out ._.
                if n != len(self._deadlines) - 1:
                    if ty is TaskTimeout:
                        raise TimeoutCancellation(*val.args).with_traceback(tb) from None
                    else:
                        return False

                # We are the cause of this timeout
                else:
                    self.result = self._result
                    self.expired = True
                    if self._ignore:
                        return True
                    elif ty is TimeoutCancellation:
                        raise TaskTimeout(*val.args).with_traceback(tb) from None
                    else:
                        return False

            else:
                if now > self._deadlines[-1]:
                    logger.warning(
                        "%r: Operation took longer than an enclosing timeout.",
                        await this_task(),
                    )

        finally:
            self._deadlines.pop()


async def _timeout_func(tm, coro, **kwargs):
    async with _TimeoutHelper(tm, **kwargs):
        return await coro

    
def timeout_after(tm, coro=None):
    if coro is None:
        return _TimeoutHelper(tm, absolute=False)
    else:
        return _timeout_func(tm, coro, absolute=False)


def timeout_at(tm, coro=None):
    if coro is None:
        return _TimeoutHelper(tm, absolute=True)
    else:
        return _timeout_func(tm, coro, absolute=True)


def ignore_after(tm, coro=None, *, result=None):
    if coro is None:
        return _TimeoutHelper(tm, absolute=False, ignore=True, result=result)
    else:
        return _timeout_func(tm, coro, absolute=False, ignore=True, result=result)


def ignore_at(tm, coro=None, *, result=None):
    if coro is None:
        return _TimeoutHelper(tm, absolute=True, ignore=True, result=result)
    else:
        return _timeout_func(tm, coro, absolute=True, ignore=True, result=result)
