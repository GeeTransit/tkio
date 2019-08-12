from types import coroutine

from .exceptions import TaskCancelled


__all__ = [
    "_get_time",
    "_sleep",
    "_pop_event",
    "_wait_event",
    "_get_tk",
    "_new_task",
    "_this_task",
    "_cancel_task",
    "_wait_task",
    "_get_tasks",
]


# This basic generator is the only way into the loop
@coroutine
def _act(*data):
    result = (yield data)
    if isinstance(result, BaseException):
        raise result
    return result


# --- Specialized acts ---
# (These are the coroutine side of acts)

# Synchronous means that it will return immediately.

# Blocking means that it may suspend the coroutine
# and a pending cancellation raised can be here.

# Synchronous
async def _get_time():
    return await _act("get_time")


# Blocking
async def _sleep(tm):
    return await _act("sleep", tm)


# Synchronous
async def _pop_event():
    return await _act("pop_event")


# Blocking
async def _wait_event():
    await _act("wait_event")


# Synchronous
async def _get_tk():
    return await _act("get_tk")


# Synchronous
async def _new_task(coro, *, eventless=False):
    return await _act("new_task", coro, eventless)


# Synchronous
async def _this_task():
    return await _act("this_task")


# Synchronous
async def _cancel_task(task, *, exc=TaskCancelled, val=None):
    await _act("cancel_task", task, exc, val)


# Blocking
async def _wait_task(task):
    await _act("wait_task", task)


# Synchronous
async def _get_tasks():
    return await _act("get_tasks")
