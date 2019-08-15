import contextlib
import logging

from .acts import *
from .exceptions import *
from .holders import *


__all__ = [
    "Task",
    "get_time",
    "sleep",
    "schedule",
    "wait_event",
    "pop_event",
    "get_tk",
    "new_task",
    "this_task",
    "block_cancellation",
    "timeout",
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

        # Index of next event that will be returned next
        # Note: -1 to signify that task doesn't want events
        self._next_event = 0

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
    return await _sleep(tm)


async def schedule():
    await _sleep(0)


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
    task.eventless = -1 if eventless else 0
    task.allow_cancel = allow_cancel
    task.daemon = daemon
    task.report_crash = report_crash
    return task


async def this_task():
    return await _this_task()


@contextlib.asynccontextmanager
async def block_cancellation():
    task = await _this_task()
    previous = task.allow_cancel
    task.allow_cancel = False
    try:
        yield
    finally:
        task.allow_cancel = previous

@contextlib.asynccontextmanager
async def timeout(tm):
    previous = await _add_timeout(tm)
    tm += await _get_time()

    task = await _this_task()
    deadlines = task._deadlines
    deadlines.append(tm)

    try:
        yield

    except (TaskTimeout, TimeoutCancellation) as e:
 
        now = await _remove_timeout(previous)

        # Check who caused this timeout
        for n, deadline in enumerate(deadlines):
            if deadline <= now:
                break

        # No one raised this timeout? Raise a RuntimeError.
        else:
            raise RuntimeError("Unexpected timeout caught")

        # Some other outer timeout timed us out ._.
        if n != len(deadlines) - 1:
            if isinstance(e, TaskTimeout):
                raise TimeoutCancellation(*e.args) from e

        # We are the cause of this timeout
        else:
            if isinstance(e, TimeoutCancellation):
                raise TaskTimeout(*e.args) from e

        # Otherwise, just let the exception propagate
        raise

    else:
        now = await _remove_timeout(previous)
        if now > tm:
            logger.warning("%r: Operation took longer than an enclosing timeout.", task)

    finally:
        deadlines.pop()
