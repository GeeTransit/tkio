from types import coroutine

from .exceptions import TaskCancelled



__all__ = [
    "_get_time", "_sleep", "_wait_event", "_pop_event", "_get_events",
    "_clear_events", "_get_tk", "_new_task", "_this_task",
    "_cancel_task", "_wait_task", "_get_tasks",
]



# This basic generator is the only way into the loop
@coroutine
def _act(*data):
    result = (yield data)
    if isinstance(result, BaseException):
        raise result
    return result

# Specialized acts
async def _get_time():
    return await _act("get_time")

async def _sleep(tm, /):
    return await _act("sleep", tm)

async def _wait_event():
    await _act("wait_event")

async def _pop_event():
    return await _act("pop_event")

async def _get_events():
    return await _act("_get_events")

async def _clear_events():
    await _act("clear_events")

async def _get_tk():
    return await _act("get_tk")

async def _new_task(coro, /, *, eventless=False):
    return await _act("new_task", coro, eventless)

async def _this_task():
    return await _act("this_task")

async def _cancel_task(task, /, *, exc=TaskCancelled, val=None):
    await _act("cancel_task", task, exc, val)

async def _wait_task(task, /):
    await _act("wait_task", task)

async def _get_tasks():
    return await _act("get_tasks")
