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
        self._tasks = None
        self._actions = None

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
                try:
                    self._runner.send(shutdown_coro())
                except BaseException:
                    break
            self._runner.close()
            self._closed = True

        else:
            try:
                val, exc = self._runner.send(coro)
            except BaseException as e:
                if isinstance(e, StopIteration):
                    val, exc = e.value
                else:
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

            def new_task(coro, /, *, eventless=False):
                task = Task(coro, eventless=eventless)
                tasks[task.id] = task
                reschedule(task)
                return task

            def cancel_task(task, /, *, exc=TaskCancelled, val=None):
                if task.cancelled:
                    return

                task.cancelled = True

                # Create the exception instance
                if isinstance(exc, BaseException):
                    task.cancel_pending = exc
                else:
                    task.cancel_pending = exc(exc.__name__ if val is None else val)

                # Check if task can be cancelled
                if not task.allow_cancel:
                    return

                # Check if task is suspended
                # Note: `cancel_func` is an indirect flag for whether the task
                # is ready or not. It is None when ready and a function
                # when suspended.
                if not task.cancel_func:
                    return

                # Prepare task with exception
                task.cancel_func()
                task._val = task.cancel_pending
                task.cancel_pending = None
                reschedule(task)

            # Helper function to filter events
            def check_event(event, widget):
                # All toplevel events are accepted
                if event.widget == tk:
                    return True

                # Accept any event
                if widget is None:
                    return True

                # Accept if widget matches request
                if event.widget == widget:
                    return True

                # Any other events can be ignored
                return False

            # Ensure a resumation of the cycle if required
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

            # Decorator to check current task's `cancel_pending` before continuing
            def blocking_act(func):
                @wraps(func)
                def _wrapper(*args):
                    if current.allow_cancel and current.cancel_pending:
                        current._val = current.cancel_pending
                        current.cancel_pending = None
                    else:
                        func(*args)
                return _wrapper

            # Decorator to restrict act to event tasks
            def event_act(func):
                @wraps(func)
                def _wrapper(*args):
                    if current._next_event == -1:
                        current._val = TaskEventless("Task is eventless")
                    else:
                        func(*args)
                return _wrapper

            # Decorator to return "break" for tkinter callbacks
            def tkinter_callback(func):
                @wraps(func)
                def _wrapper(*args):
                    func(*args)
                    return "break"
                return _wrapper

            # Functions invoked by the current task
            # (These are the loop side of acts)
            def _act_get_time():
                current._val = monotonic()

            @blocking_act
            def _act_sleep(tm):
                if tm > 0:
                    suspend_current("SLEEP", sleep_wait[monotonic() + tm].add(current))
                else:
                    nonlocal running
                    running = False
                    current._val = monotonic()
                    reschedule(current)

            @blocking_act
            @event_act
            def _act_wait_event():
                suspend_current("EVENT_WAIT", event_wait.add(current))

            @event_act
            def _act_pop_event(widget):
                while True:
                    try:
                        event = event_queue[current._next_event]
                    except IndexError:
                        current._val = NoEvent("No event available")
                        break
                    else:
                        current._next_event += 1
                        if check_event(event, widget):
                            current._val = event
                            break

            @event_act
            def _act_get_events(widget):
                start = current._next_event
                end = len(event_queue)
                current._next_event += len_event_queue
                events = []
                for i in range(start, end):
                    event = event_queue[i]
                    if check_event(event, widget):
                        events.append(event)
                current._val = events

            @event_act
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

            @blocking_act
            def _act_wait_task(task):
                suspend_current("TASK_WAIT", task.waiting.add(current))

            def _act_get_tasks():
                current._val = self._tasks

            # Send if the cycle is suspended
            def safe_send(gen, data):
                state = inspect.getcoroutinestate(gen)
                if state not in {"CORO_RUNNING", "CORO_CLOSED"}:
                    return send(gen, data)

            # Functions for event callbacks
            @tkinter_callback
            def send_tk_event(event):
                if event.widget is tk:
                    event_queue.append(event)
                    safe_send(cycle, "EVENT_WAKE")

            @tkinter_callback
            def send_other_event(event):
                if event.widget is not tk:
                    event_queue.append(event)
                    safe_send(cycle, "EVENT_WAKE")

            @tkinter_callback
            def send_destroy_event(event):
                if event.widget is tk:
                    event_queue.append(event)
                    frame.after(1, lambda: safe_send(cycle, "EVENT_WAKE"))

            @tkinter_callback
            def close_window():
                for task in self._tasks.values():
                    cancel_task(task, exc=CloseWindow("X was pressed"))
                safe_send(cycle, "CLOSE_WINDOW")

            # Mapping of act name to function
            self._actions = actions = {
                name[5:]: value
                for name, value in locals().items()
                if name.startswith("_act_")
            }

            # Mapping of task id to task
            tasks = self._tasks = {}

            # Result of task
            val = exc = None

            # Main task
            main_task = None

            # Deque of tasks ready to be run
            ready_tasks = collections.deque()

            # Deque of waiting events
            event_queue = collections.deque()

            # Holders for event waiting and sleeping
            event_wait = SetHolder()
            sleep_wait = collections.defaultdict(SetHolder)



            # Wrapping events callbacks with an unbind
            with prepare_bindings(
                tk,
                send_tk_event, send_other_event,
                send_destroy_event, close_window,
            ):

                # Main loop
                while True:

                    # Yield if just started or main task just terminated
                    if not main_task or main_task.terminated:
                        if main_task:
                            exc = main_task.exception
                            val = main_task.result if not exc else None
                        coro = (yield (val, exc))
                        main_task = new_task(coro)
                        main_task.report_crash = False
                        del coro

                    # Ensure resumation if needed (such as a nonempty ready queue)
                    with after_call():
                        # wait for either an event callback or a ready after call
                        info = await _suspend()

                    if info == "EVENT_WAKE":
                        for task in event_wait.popall():
                            reschedule(task)

                    # check for amount of events that have been consumed by all tasks
                    if event_tasks := [task for task in tasks.values() if task._next_event != -1]:
                        if leftover := min(task._next_event for task in event_tasks):
                            for _ in range(leftover):
                                event_queue.popleft()
                            for task in event_tasks:
                                task._next_event -= leftover

                    # Waking sleeping tasks here
                    now = monotonic()
                    for tm in list(sleep_wait.keys()):
                        if tm <= now:
                            for task in sleep_wait.pop(tm).popall():
                                task._val = now
                                reschedule(task)

                    # Add socket / selectors stuff here

                    # Run all ready tasks
                    for _ in range(len(ready_tasks)):
                        current = ready_tasks.popleft()
                        current.state = "RUNNING"
                        running = True

                        # Run the current task until it suspends or terminated
                        while running:

                            # Send the act result
                            try:
                                # Note: The `_act` generator will raise the result
                                # if it is an exception. No need for chucking
                                # exceptions deep into the stack.
                                act = current.coro.send(current._val)

                            # Task terminated
                            except BaseException as e:
                                running = False

                                for task in current.waiting.popall():
                                    reschedule(task)
                                current.state = "TERMINATED"
                                current.terminated = True
                                del tasks[current.id]

                                if isinstance(e, StopIteration):
                                    current.result = e.value
                                else:
                                    current.exception = e
                                    if current.report_crash and not isinstance(e, (TaskCancelled, SystemExit)):
                                        print(f"Task crash: {current}", file=sys.stderr)
                                        traceback.print_exc()
                                    # Re-raise the exception to end the loop immediately
                                    if not isinstance(e, Exception):
                                        raise

                                break

                            # Act received: time to run it
                            else:
                                current._val = None

                                # If this raises an exception, let it propagate
                                # as this is most likely a programming error on
                                # the loop, not the task.
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

        def getasyncgenstate(asyncgen):
            if asyncgen.ag_running:
                return "AGEN_RUNNING"
            if asyncgen.ag_frame is None:
                return "AGEN_CLOSED"
            if asyncgen.ag_frame.f_lasti == -1:
                return "AGEN_CREATED"
            return "AGEN_SUSPENDED"

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
                try:
                    for info in bindings:
                        widget_unbind(*info)
                except tkinter.TclError:
                    pass

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
                try:
                    toplevel.protocol("WM_DELETE_WINDOW", toplevel.destroy)
                except tkinter.TclError:
                    pass

        def send(gen, data):
            try:
                return gen.send(data)
            except BaseException as e:
                nonlocal val, exc
                destroy(frame)
                if isinstance(e, StopAsyncIteration):
                    pass
                elif isinstance(e, StopIteration):
                    val, exc = e.value
                else:
                    val, exc = None, e

        val = exc = None
        tk = frame = None
        loop = cycle = None

        with destroying(tkinter.Tk()) as tk:
            with prepare_loop() as loop:

                # Make sure toplevel and loop still exist
                while exists(tk) and getasyncgenstate(loop) != "AGEN_CLOSED":

                    # Get coro to run
                    coro = yield val, exc
                    cycle = wrap_coro(loop.asend(coro))
                    del coro

                    # Run until frame is destroyed
                    # Note: `wait_window` will spawn in tkinter's event loop
                    # but will end when the widget is destroyed. `frame` will
                    # be destroyed when an exception happens in sending a value
                    # to the cycle.
                    with destroying(tkinter.Frame(tk)) as frame:
                        send(cycle, None)
                        wait_window(frame)

                    # Check if cycle is still running
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
