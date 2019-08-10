import collections
import contextlib
import inspect
import operator
import selectors
import sys
import tkinter
import traceback

from functools import wraps
from time import monotonic
from types import coroutine

from .exceptions import *
from .holders import *
from .task import *



class TkLoop:

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

    def __init__(self, /):

        self._runner = None
        self._closed = False
        self._tasks = {}

    def run(self, coro=None, /, *, shutdown=False):

        if self._closed:
            if shutdown:
                return
            raise RuntimeError("loop already shut down")

        if not self._runner:
            self._runner = self._run_coro()
            self._runner.send(None)

        val = exc = None

        if shutdown:
            if coro:
                raise ValueError("Cannot supply `coro` and `shutdown` in same call")

            async def shutdown_coro():
                for task in self._tasks.values():
                    await task.cancel(blocking=False)

            while self._tasks:
                self._runner.send(shutdown_coro())
            self._runner.close()
            self._closed = True

        else:
            try:
                val, exc = self._runner.send(coro)
            except BaseException as e:
                val, exc = None, e
                self._runner.close()
                self._closed = True

        if exc:
            raise exc
        return val

    def _run_coro(self, /):

        async def _run_loop():

            @coroutine
            def _suspend(data=None, /):
                return (yield data)

            def reschedule(task, /):
                ready_tasks.append(task)
                task.state = "READY"
                task.cancel_func = None

            def suspend_current(state, cancel_func, /):
                nonlocal running
                current.state = state
                current.cancel_func = cancel_func
                running = False

            def check_cancel():
                if current.allow_cancel and current.cancel_pending:
                    current._val = current.cancel_pending
                    current.cancel_pending = None
                    return True
                else:
                    return False

            def new_task(coro, /, *, eventless=False):
                task = Task(coro, eventless=eventless)
                self._tasks[task.id] = task
                reschedule(task)
                return task

            def cancel_task(task, /, *, exc=TaskCancelled, val=None):
                if task.cancelled:
                    return

                task.cancelled = True

                if isinstance(exc, BaseException):
                    task.cancel_pending = exc
                else:
                    task.cancel_pending = exc(exc.__name__ if val is None else val)

                if not task.allow_cancel:
                    return

                if not task.cancel_func:
                    return

                task.cancel_func()
                task._val = task.cancel_pending
                task.cancel_pending = None
                reschedule(task)

            @contextlib.contextmanager
            def after_call():
                if ready_tasks or sleep_wait:
                    if ready_tasks:
                        timeout = 0
                        data = "READY"
                    elif sleep_wait:
                        timeout = (min(sleep_wait) - monotonic())*1000
                        data = "SLEEP_WAKE"
                    with after(frame, timeout, lambda: send(cycle, data)):
                        yield
                else:
                    yield

            def event_tasks_only(func):
                @wraps(func)
                def _wrapper(*args, **kwargs):
                    if current._next_event == -1:
                        current._val = TaskEventless("Task is eventless")
                    else:
                        func(*args, **kwargs)
                return _wrapper

            def _act_get_time():
                current._val = monotonic()

            def _act_sleep(tm):
                if check_cancel():
                    return

                if tm > 0:
                    suspend_current("SLEEP", sleep_wait[monotonic() + tm].add(current))
                else:
                    nonlocal running
                    running = False
                    current._val = monotonic()
                    reschedule(current)

            @event_tasks_only
            def _act_wait_event():
                if check_cancel():
                    return
                if not current.terminated:
                    suspend_current("EVENT_WAIT", event_wait.add(current))

            @event_tasks_only
            def _act_pop_event():
                try:
                    event = event_queue[current._next_event]
                except IndexError:
                    current._val = NoEvent("No event available")
                else:
                    current._next_event += 1
                    current._val = event

            @event_tasks_only
            def _act_get_events():
                start = current._next_event
                current._next_event += len(event_queue)
                current._val = event_queue[start:]

            @event_tasks_only
            def _act_clear_events():
                current._next_event += len(event_queue)

            def _act_get_tk():
                current._val = tk

            def _act_new_task(coro, eventless):
                current._val = new_task(coro, eventless=eventless)

            def _act_this_task():
                current._val = current

            def _act_cancel_task(task, exc, val):
                cancel_task(task, exc=exc, val=val)

            def _act_wait_task(task):
                if check_cancel():
                    return
                suspend_current("TASK_WAIT", task.waiting.add(current))

            def _act_get_tasks():
                current._val = self._tasks

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
                for task in self._tasks.values():
                    cancel_task(task, exc=CloseWindow("X was pressed"))
                safe_send(cycle, "CLOSE_WINDOW")
                return "break"

            actions = {
                name[5:]: value
                for name, value in locals().items()
                if name.startswith("_act_")
            }

            val = exc = None
            main_task = None

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
                            exc = main_task.exception
                            val = main_task.result if not exc else None
                        coro = (yield (val, exc))
                        main_task = new_task(coro)
                        main_task.report_crash = False
                        del coro

                    with after_call():
                        # wait for either an event callback or a ready after call
                        info = await _suspend()

                    if info == "EVENT_WAKE":
                        for task in event_wait.popall():
                            reschedule(task)

                    # check for amount of events that have been consumed by all tasks
                    if event_tasks := [task for task in self._tasks.values() if task._next_event != -1]:
                        if leftover := min(task._next_event for task in event_tasks):
                            for _ in range(leftover):
                                event_queue.popleft()
                            for task in event_tasks:
                                task._next_event -= leftover

                    now = monotonic()
                    for tm in list(sleep_wait.keys()):
                        if tm <= now:
                            for tid in sleep_wait.pop(tm).popall():
                                if (task := self._tasks.get(tid)):
                                    task._val = now
                                    reschedule(task)

                    for _ in range(len(ready_tasks)):
                        current = ready_tasks.popleft()
                        current.state = "RUNNING"
                        running = True

                        while running:

                            try:
                                act = current.coro.send(current._val)

                            except BaseException as e:
                                running = False

                                for task in current.waiting.popall():
                                    reschedule(task)
                                current.state = "TERMINATED"
                                current.terminated = True
                                del self._tasks[current.id]

                                if isinstance(e, StopIteration):
                                    current.result = e.value
                                else:
                                    current.exception = e
                                    if current.report_crash and not isinstance(e, (StopIteration, TaskCancelled)):
                                        print(f"Task crash: {current}", file=sys.stderr)
                                        traceback.print_exc()
                                    if not isinstance(e, Exception):
                                        raise
                                break

                            else:
                                current._val = None
                                actions[act[0]](*act[1:])

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
                stack.enter_context(binding(tk, tkfunc, *self.tk_events))
                stack.enter_context(binding(tk, otherfunc, *self.other_events))
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
                elif isinstance(e, StopAsyncIteration):
                    val, exc = None, None
                else:
                    val, exc = None, e

        val = exc = None
        tk = frame = None
        loop = cycle = None

        with destroying(tkinter.Tk()) as tk:
            with prepare_loop() as loop:
                while exists(tk):
                    coro = yield val, exc
                    cycle = wrap_coro(loop.asend(coro))
                    del coro
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



def run(coro):
    with TkLoop() as iw:
        return iw.run(coro)
