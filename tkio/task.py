from .acts import *
from .exceptions import *
from .holders import *



class Task:

    _next_id = 0

    def __init__(self, coro, /, *, eventless=False):

        # Task identification
        self.id = Task._next_id
        Task._next_id += 1

        # Task state
        self.coro = coro           # Wrapped coroutine
        self.state = "INITIAL"     # Debug state
        self.cancelled = False     # Flag for task cancellation
        self.terminated = False    # Flag for task termination
        self.waiting = SetHolder() # Tasks waiting for termination
        self.cancel_func = None    # Function invoked on cancellation
        self.report_crash = True   # Flag to print task exception
        self.daemon = False        # Flag to ignore result

        # -1 to signify that task doesn't want events
        self._next_event = -1 if eventless else 0

        # Task result / exception
        self._result_val = None
        self._result_exc = None

        # Value to send on resumation
        self._val = None

    def __repr__(self, /):
        coro = self._coro.__qualname__
        state = self.state
        id = self.id
        return f"<{type(self).__qualname__} {id=} {coro=!s} {state=!s}>"

    async def wait(self):
        await _wait_task(self)

    async def cancel(self, *, exc=TaskCancelled, blocking=True):
        if self.cancelled:
            return False
        await _cancel_task(self, exc=exc)
        if blocking:
            await self.wait()
        return True

    async def join(self):
        await self.wait()
        if exc := self._exc:
            raise TaskError("Task crashed") from exc
        return self._val

    @property
    def result(self):
        if not self.terminated:
            raise RuntimeError("Task still running")
        if exc := self._result_exc:
            raise exc
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

async def sleep(tm, /):
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
            await schedule()

async def get_events():
    return await _get_events()

async def clear_events():
    await _clear_events()

async def get_tk():
    return await _get_tk()

async def this_task():
    return await _this_task()