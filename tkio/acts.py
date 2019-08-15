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
    "_get_loop",
    "_add_timeout",
    "_remove_timeout",
    "_wait_holder",
    "_wake_holder",
]


# This basic generator is the only way into the loop
@coroutine
def _act(*act):
    val = (yield act)
    if isinstance(val, BaseException):
        raise val
    else:
        return val


# --- Specialized acts ---
# (These are the coroutine side of acts)

# Synchronous means that it will return immediately.

# Blocking means that it may suspend the coroutine
# and a pending cancellation raised can be here.

# Synchronous
async def _get_time():
    return await _act("_act_get_time")


# Blocking
async def _sleep(tm, absolute):
    return await _act("_act_sleep", tm, absolute)


# Synchronous
async def _pop_event():
    return await _act("_act_pop_event")


# Blocking
async def _wait_event():
    return await _act("_act_wait_event")


# Synchronous
async def _get_tk():
    return await _act("_act_get_tk")


# Synchronous
async def _new_task(coro):
    return await _act("_act_new_task", coro)


# Synchronous
async def _this_task():
    return await _act("_act_this_task")


# Synchronous
async def _cancel_task(task, exc=TaskCancelled, val=None):
    return await _act("_act_cancel_task", task, exc, val)


# Synchronous
async def _get_loop():
    return await _act("_act_get_loop")


# Synchronous
async def _add_timeout(tm):
    return await _act("_act_add_timeout", tm)


# Synchronous
async def _remove_timeout(previous):
    return await _act("_act_remove_timeout", previous)


# Blocking
async def _wait_holder(holder, state):
    return await _act("_act_wait_holder", holder, state)


# Synchronous
async def _wake_holder(holder, n=1):
    return await _act("_act_wake_holder", holder, n)
