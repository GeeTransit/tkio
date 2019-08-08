import collections
import contextlib
import inspect
import operator
import sys
import tkinter
import traceback
import types

from functools import wraps
from time import monotonic_ns



class CloseWindow(Exception):
    ...

class TaskCancelled(Exception):
    ...

class InvalidState(Exception):
    ...

class NoEvent(Exception):
    ...

class TaskIsEventless(Exception):
    ...



class Holder:

    __slots__ = ()

    def __len__(self, /):
        raise NotImplementedError

    def add(self, obj, /):
        raise NotImplementedError

    def pop(self, /):
        raise NotImplementedError

    def popall(self, /):
        return [self.pop() for _ in range(len(self))]



class SetHolder(Holder):

    __slots__ = ("_set",)

    def __new__(cls, /):
        inst = super().__new__(cls)
        inst._set = set()
        return inst

    def __len__(self, /):
        return len(self._set)

    def add(self, obj, /):
        self._set.add(obj)
        return lambda: self._set.discard(obj)

    def pop(self, /):
        return self._set.pop()



class FIFOHolder(Holder):

    __slots__ = ("_deque", "len")

    def __new__(cls, /):
        inst = super().__new__(cls)
        inst._deque = collections.deque()
        inst._len = 0
        return inst

    def __len__(self, /):
        return self._len

    def add(self, obj, /):
        self._deque.append(item := [obj])
        self._len += 1
        def _remove():
            if item[0] is not None:
                item[0] = None
                self._len -= 1
        return _remove

    def pop(self, /):
        while True:
            try:
                obj, = self._deque.popleft()
            except IndexError as e:
                raise KeyError from e
            if obj:
                self._len -= 1
                return obj



class ITask:

    _next_id = 0

    __slots__ = (
        "_coro", "_val", "_exc", "_next_event", "_task_wait",
        "_cancel_func", "terminated", "state", "id",
    )

    def __new__(cls, coro, /, *, eventless=False):
        inst = super().__new__(cls)

        inst._coro = coro
        inst._val = inst._exc = None
        inst._task_wait = SetHolder()
        inst._cancel_func = None

        if eventless:
            inst._next_event = -1
        else:
            inst._next_event = 0

        inst.terminated = False
        inst.state = "INITIAL"

        inst.id = cls._next_id
        cls._next_id += 1
        return inst

    def __init__(self, coro, /, *, eventless=False):
        pass

    def __repr__(self):
        coro = self._coro.__qualname__
        state = self.state
        id = self.id
        return f"<{type(self).__qualname__} {id=} {coro=!s} {state=!s}>"

    async def cancel(self, *, exc=TaskCancelled, blocking=True):
        if self.terminated:
            return False
        await IWindow._cancel_task(self, exc=exc)
        if blocking:
            await IWindow._wait_task(self)
        return True

    async def join(self):
        await IWindow._wait_task(self)
        return self.result

    @property
    def result(self):
        if not self.terminated:
            raise InvalidState("task still running")
        if self._exc:
            raise self._exc
        return self._val

    @property
    def exception(self):
        if not self.terminated:
            raise InvalidState("task still running")
        return self._exc



class IWindow:

    tk_events = (
        '<Activate>', '<Circulate>', '<Colormap>', '<Deactivate>',
        '<FocusIn>', '<FocusOut>', '<Gravity>', '<Key>', '<KeyPress>',
        '<KeyRelease>', '<MouseWheel>', '<Property>',
    )

    other_events = (
        '<Button>', '<ButtonPress>', '<ButtonRelease>', '<Configure>',
        '<Enter>', '<Expose>', '<Leave>', '<Map>', '<Motion>',
        '<Reparent>', '<Unmap>', '<Visibility>',
    )

    __slots__ = ("_runner", "_closed")

    def __init__(self, /):
        super().__init__()
        self._runner = None
        self._closed = False

    def run(self, coro=None, /, *, shutdown=False):

        if self._closed:
            if shutdown:
                return
            raise RuntimeError("loop already shut down")

        if shutdown:
            if coro:
                raise ValueError("Cannot supply `coro` and `shutdown` flag in same call")
            async def corofunc():
                tasks = await self._get_tasks()
                this = await self._this_task()
                for task in tasks - {this}:
                    await task.cancel()
            coro = corofunc()

        if not self._runner:
            self._runner = self._run_coro()
            self._runner.send(None)

        try:
            val, exc = self._runner.send(coro)
        except StopIteration as e:
            val, exc = e.value
        except Exception as e:
            val, exc = None, e
            self._closed = True

        if shutdown:
            self._runner.close()
            self._closed = True

        if exc:
            raise exc
        return val

    @classmethod
    def _run_coro(cls, /):

        # print = lambda *_, **__: None

        async def _run_loop():

            @types.coroutine
            def _suspend(data=None, /):
                return (yield data)

            def reschedule(task, /):
                ready_tasks.append(task)
                task.state = "READY"
                task._cancel_func = None

            def suspend_current(state, cancel_func, /):
                nonlocal running
                current.state = state
                current._cancel_func = cancel_func
                running = False

            @contextlib.contextmanager
            def after_call():
                if ready_tasks or sleep_wait:
                    if ready_tasks:
                        timeout = 0
                        data = "READY"
                    elif sleep_wait:
                        timeout = (min(sleep_wait) - _get_time())*1000
                        data = "SLEEP_WAKE"
                    with after(frame, timeout, lambda: send(cycle, data)):
                        yield
                else:
                    yield

            def event_tasks_only(func):
                @wraps(func)
                def _wrapper(*args, **kwargs):
                    if current._next_event == -1:
                        raise TaskIsEventless
                    return func(*args, **kwargs)
                return _wrapper

            def _get_time():
                return monotonic_ns()/1_000_000_000

            def _sleep(tm):
                if tm > 0:
                    suspend_current("SLEEP", sleep_wait[_get_time() + tm].add(current))
                else:
                    nonlocal running
                    running = False
                    reschedule(current)

            @event_tasks_only
            def _wait_event():
                suspend_current("EVENT_WAIT", event_wait.add(current))

            @event_tasks_only
            def _pop_event():
                try:
                    event = event_queue[current._next_event]
                except IndexError:
                    raise NoEvent
                current._next_event += 1
                return event

            @event_tasks_only
            def _get_events():
                current._next_event += len(event_queue)
                return event_queue[:]

            @event_tasks_only
            def _clear_events():
                current._next_event += len(event_queue)

            @event_tasks_only
            def _check_events():
                return current._next_event < len(event_queue)

            def _get_tk():
                return tk

            def _new_task(coro, eventless=False):
                task = ITask(coro, eventless=eventless)
                tasks.add(task)
                reschedule(task)
                return task

            def _this_task():
                return current

            def _cancel_task(task, exc):
                if cancel_func := task._cancel_func:
                    cancel_func()
                    task._cancel_func = None
                task.state = "CANCELLED"
                task._exc = exc
                reschedule(task)

            def _wait_task(task):
                suspend_current("TASK_WAIT", task._task_wait.add(current))

            def _get_tasks():
                return tasks

            def safe_send(gen, data):
                state = inspect.getcoroutinestate(gen)
                if state not in {"CORO_RUNNING", "CORO_CLOSED"}:
                    return send(gen, data)

            def send_tk_event(event):
                if event.widget is tk:
                    event_queue.append(event)
                    safe_send(cycle, "EVENT_WAKE")
                return "break"

            def send_other_event(event):
                if event.widget is not tk:
                    event_queue.append(event)
                    safe_send(cycle, "EVENT_WAKE")
                return "break"

            def send_destroy_event(event):
                if event.widget is tk:
                    event_queue.append(event)
                    frame.after(1, lambda: safe_send(cycle, "EVENT_WAKE"))
                return "break"

            def close_window():
                for task in tasks:
                    _cancel_task(task, CloseWindow)
                safe_send(cycle, "CLOSE_WINDOW")
                return "break"

            actions = {
                "get_time": _get_time,
                "sleep": _sleep,

                "wait_event": _wait_event,
                "pop_event": _pop_event,
                "get_events": _get_events,
                "clear_events": _clear_events,
                "check_events": _check_events,

                "get_tk": _get_tk,

                "new_task": _new_task,
                "this_task": _this_task,
                "cancel_task": _cancel_task,
                "wait_task": _wait_task,
                "get_tasks": _get_tasks,
            }

            val = exc = None
            main_task = None

            tasks = set()
            ready_tasks = collections.deque()
            event_queue = collections.deque()

            event_wait = SetHolder()
            sleep_wait = collections.defaultdict(SetHolder)

            with prepare_bindings(
                tk,
                send_tk_event, send_other_event,
                send_destroy_event, close_window,
            ):

                while True:
                    if not main_task or main_task.terminated:
                        if main_task:
                            val = main_task._val
                            exc = main_task._exc
                        coro = yield val, exc
                        main_task = _new_task(coro)
                        
                    with after_call():
                        info = await _suspend()

                    if info == "EVENT_WAKE":
                        for etask in event_wait.popall():
                            reschedule(etask)

                    # check for amount of events that have been consumed by all tasks
                    if event_tasks := [task for task in tasks if task._next_event != -1]:
                        if leftover := min(task._next_event for task in event_tasks):
                            for _ in range(leftover):
                                event_queue.popleft()
                            for task in event_tasks:
                                task._next_event -= leftover

                    now = _get_time()
                    for tm in list(sleep_wait.keys()):
                        if tm <= now:
                            for stask in sleep_wait.pop(tm).popall():
                                reschedule(stask)

                    for _ in range(len(ready_tasks)):
                        current = ready_tasks.popleft()
                        if current.terminated:
                            continue
                        current.state = "RUNNING"
                        running = True

                        try:
                            while running:
                                if exc := current._exc:
                                    act = current._coro.throw(exc)
                                else:
                                    act = current._coro.send(current._val)
                                try:
                                    current._val = actions[act[0]](*act[1:])
                                except Exception as e:
                                    current._exc = e
                                else:
                                    current._exc = None

                        except BaseException as e:
                            current.state = "TERMINATED"
                            current.terminated = True
                            if isinstance(e, StopIteration):
                                current._val = e.value
                                current._exc = None
                            else:
                                current._val = None
                                current._exc = e
                            for wtask in current._task_wait.popall():
                                reschedule(wtask)
                            tasks.discard(current)
                            if not isinstance(e, Exception):
                                raise
                            if not isinstance(e, (StopIteration, TaskCancelled)):
                                if current is not main_task:
                                    print(f"Task crash: {current}", file=sys.stderr)
                                    traceback.print_exc()

        def exists(widget):
            try:
                return bool(widget.winfo_exists())
            except tkinter.TclError:
                return False

        def destroy(widget):
            try:
                widget.destroy()
            except tkinter.TclError:
                pass

        def wait_window(widget):
            try:
                widget.wait_window()
            except tkinter.TclError:
                pass

        async def wrap_coro(coro):
            return await coro

        @contextlib.contextmanager
        def destroying(*widgets):
            try:
                if len(widgets) == 1:
                    yield widgets[0]
                else:
                    yield widgets
            finally:
                for w in reversed(widgets):
                    destroy(w)

        @contextlib.contextmanager
        def prepare_bindings(tk, tkfunc, otherfunc, destroyfunc, closefunc):
            with contextlib.ExitStack() as stack:
                stack.enter_context(binding(tk, tkfunc, *cls.tk_events))
                stack.enter_context(binding(tk, otherfunc, *cls.other_events))
                stack.enter_context(binding(tk, destroyfunc, "<Destroy>"))
                stack.enter_context(protocol(tk, closefunc))
                yield

        @contextlib.contextmanager
        def binding(widget, func, *events):
            widget_bind = widget.bind
            widget_unbind = widget.unbind
            bindings = [(event, widget_bind(event, func)) for event in events]
            try:
                if len(bindings) == 1:
                    yield bindings[0]
                else:
                    yield bindings
            finally:
                for info in bindings:
                    widget_unbind(*info)

        @contextlib.contextmanager
        def after(widget, ms, func):
            id_ = widget.after(max(int(ms), 1), func)
            try:
                yield id_
            finally:
                try:
                    widget.after_cancel(id_)
                except tkinter.TclError:
                    pass

        @contextlib.contextmanager
        def prepare_loop():
            loop = _run_loop()

            cycle = loop.asend(None)
            try:
                cycle.send(None)
            except StopIteration:
                pass
            else:
                raise RuntimeError("first cycle didn't stop at initiation")

            try:
                yield loop
            finally:
                cycle = loop.aclose()
                try:
                    cycle.send(None)
                except StopIteration:
                    pass
                else:
                    raise RuntimeError("final cycle didn't stop at finalization")

        @contextlib.contextmanager
        def protocol(toplevel, func):
            toplevel.protocol("WM_DELETE_WINDOW", func)
            try:
                yield
            finally:
                toplevel.protocol("WM_DELETE_WINDOW", toplevel.destroy)

        def send(gen, data):
            try:
                return gen.send(data)
            except BaseException as e:
                nonlocal val, exc
                destroy(frame)
                if isinstance(e, StopIteration):
                    val, exc = e.value
                else:
                    val, exc = None, e

        val = exc = None
        tk = frame = None
        loop = cycle = None

        with destroying(tkinter.Tk()) as tk:
            with prepare_loop() as loop:
                while exists(tk):
                    main_coro = yield val, exc
                    cycle = wrap_coro(loop.asend(main_coro))
                    with destroying(tkinter.Frame(tk)) as frame:
                        send(cycle, None)
                        wait_window(frame)
                    if inspect.getcoroutinestate(cycle) != "CORO_CLOSED":
                        cycle.close()
                        raise RuntimeError("frame closed before main coro finished") from exc

        return val, exc

    def __enter__(self):
        return self

    def __exit__(self, exc, val, tb):
        self.run(shutdown=True)

    def __del__(self):
        if not self._closed:
            raise RuntimeError("IWindow wasn't shutdown with IWindow.run(shutdown=True)")

    @staticmethod
    @types.coroutine
    def _sleep(tm, /):
        yield ("sleep", tm)

    @staticmethod
    @types.coroutine
    def _wait_event():
        yield ("wait_event",)

    @staticmethod
    @types.coroutine
    def _pop_event():
        return (yield ("pop_event",))

    @staticmethod
    @types.coroutine
    def _get_events():
        return (yield ("get_events",))

    @staticmethod
    @types.coroutine
    def _clear_events():
        yield ("clear_events",)

    @staticmethod
    @types.coroutine
    def _check_events():
        return (yield ("check_events",))

    @staticmethod
    @types.coroutine
    def _get_time():
        return (yield ("get_time",))

    @staticmethod
    @types.coroutine
    def _get_tk():
        return (yield ("get_tk",))

    @staticmethod
    @types.coroutine
    def _new_task(coro, eventless, /):
        return (yield ("new_task", coro))

    @staticmethod
    @types.coroutine
    def _this_task():
        return (yield ("this_task",))

    @staticmethod
    @types.coroutine
    def _cancel_task(task, exc, /):
        yield ("cancel_task", task, exc)

    @staticmethod
    @types.coroutine
    def _wait_task(task, /):
        yield ("wait_task", task)

    @staticmethod
    @types.coroutine
    def _get_tasks():
        return (yield ("get_tasks",))



async def sleep(tm=0, /):
    await IWindow._sleep(tm)

async def wait_event():
    await IWindow._wait_event()

async def pop_event(*, blocking=True):
    if not blocking:
        return await IWindow._pop_event()
    while True:
        try:
            return await IWindow._pop_event()
        except NoEvent:
            await wait_event()

async def get_events():
    return await IWindow._get_events()

async def clear_events():
    await IWindow._clear_events()

async def check_events():
    return await IWindow._check_events()

async def get_time():
    return await IWindow._get_time()

async def get_tk():
    return await IWindow._get_tk()

async def new_task(coro, /, *, eventless=False):
    return await IWindow._new_task(coro, eventless)

async def this_task():
    return await IWindow._this_task()



def run(coro):
    with IWindow() as iw:
        return iw.run(coro)
