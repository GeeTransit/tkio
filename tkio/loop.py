import collections
import contextlib
import functools
import inspect
import logging
import operator
import selectors
import time
import tkinter
import types

from .exceptions import *
from .holders import *
from .task import *


__all__ = ["TkLoop", "run"]


logger = logging.getLogger(__name__)


class TkLoop:

    _tk_events = (
        "<Activate>",
        "<Circulate>",
        "<Colormap>",
        "<Deactivate>",
        "<FocusIn>",
        "<FocusOut>",
        "<Gravity>",
        "<Key>",
        "<KeyPress>",
        "<KeyRelease>",
        "<MouseWheel>",
        "<Property>",
    )

    _other_events = (
        "<Button>",
        "<ButtonPress>",
        "<ButtonRelease>",
        "<Configure>",
        "<Enter>",
        "<Expose>",
        "<Leave>",
        "<Map>",
        "<Motion>",
        "<Reparent>",
        "<Unmap>",
        "<Visibility>",
    )


    def __init__(self, *, selector=None):

        # Functions to run at shutdown
        self._shutdown_funcs = []

        # Selector initiation
        self._selector = (selector if selector else selectors.DefaultSelector())
        self._call_at_shutdown(self._selector.close)

        # Task and act dicts
        self._tasks = {}
        self._acts = {}

        # Loop runner
        self._runner = None

        # Ready tasks to be run
        self._ready_tasks = collections.deque()

        # Events received by tkinter (and aren't read by all tasks yet)
        self._event_queue = collections.deque()

        # Holders for event and time based waiting
        self._event_wait = SetHolder()
        self._time_wait = collections.defaultdict(SetHolder)


    def __del__(self):
        if self._shutdown_funcs is not None:
            raise RuntimeError("TkLoop wasn't shutdown with TkLoop.run(shutdown=True)")


    def __enter__(self):
        return self


    def __exit__(self, exc, val, tb):
        if self._shutdown_funcs is not None:
            self.run(shutdown=True)


    def _call_at_shutdown(self, func):
        self._shutdown_funcs.append(func)


    def run(self, coro=None, *, shutdown=False):

        if self._closed:
            raise RuntimeError("loop already shut down")

        # Create a new runner if needed
        if not self._runner or not self._runner.gi_frame:
            self._runner = self._run_coro()
            try:
                self._runner.send(None)
            except BaseException as e:
                self._runner.close()
                self._closed = True
                raise TkLoopError("Loop failed to initialize") from e

        val = exc = None

        if coro or not shutdown:
            try:
                val, exc = self._runner.send(coro)
            except BaseException as e:
                raise TkLoopError("Loop exited with error") from e

        if shutdown:
            async def shutdown_coro(tasks):
                for task in tasks:
                    await task.cancel(blocking=False)

            while self._tasks:
                self._runner.send(shutdown_coro(self._tasks.values()))

            self._runner.close()
            self._runner = None

            for func in self._shutdown_funcs:
                 func()
            self._shutdown_funcs = None

        if exc:
            raise exc
        else:
            return val


    def _run_coro(self):


        # --- Constants ---

        EVENT_READ = selectors.EVENT_READ
        EVENT_WRITE = selectors.EVENT_WRITE


        # --- Loop states ---

        # Current task
        current = None

        # Flag for if the current task is running
        running = True

        # Rebindings from loop
        tasks = self._tasks                 # Mapping of task id to task
        acts = self._acts                   # Mapping of trap name to function
        selector = self._selector           # Kernel selector for I/O
        ready_tasks = self._ready_tasks     # Deque of tasks ready to be run
        event_queue = self._event_queue     # Deque of waiting events
        event_wait = self._event_wait       # Holder for event waiting
        time_wait = self._time_wait         # Holder for time based waiting


        # --- Bound methods ---

        selector_register = selector.register
        selector_unregister = selector.unregister
        selector_modify = selector.modify
        selector_select = selector.select
        selector_getkey = selector.get_key
        selector_getmap = selector.get_map

        ready_tasks_append = ready_tasks.append
        ready_tasks_popleft = ready_tasks.popleft

        event_queue_append = event_queue.append
        event_queue_popleft = event_queue.popleft

        contextlib_contextmanager = contextlib.contextmanager
        functools_wraps = functools.wraps
        time_monotonic = time.monotonic
        types_coroutine = types.coroutine


        # --- Acts helper functions ---

        def reschedule(task, val=None):
            ready_tasks_append(task)
            task.state = "READY"
            task.cancel_func = None
            task._val = val

        def suspend_current(state, cancel_func):
            nonlocal running
            current.state = state
            current.cancel_func = cancel_func
            running = False

        def new_task(coro):
            task = Task(coro)
            tasks[task.id] = task
            reschedule(task)
            return task

        def cancel_task(task, *, exc=TaskCancelled, val=None):

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
            reschedule(task, task.cancel_pending)
            task.cancel_pending = None

        def add_timeout(tm, ty, task):
            # Update task state if `tm` is now the earliest one
            previous = getattr(task, ty)
            if previous is None or tm <= previous:
                setattr(task, ty, tm)

            # Set timeout and get removing function
            remove = time_wait[tm].add((task, ty))

            # Return function that removes time and returns the previous state
            return (lambda: remove_timeout(previous, ty, task, remove))

        # Helper function for `add_timeout`
        def remove_timeout(previous, ty, task, remove):
            setattr(task, ty, previous)
            remove()
            return previous

        def register_event(fileobj, event, task):
            try:
                key = selector_getkey(fileobj)

            except KeyError:
                data = ((task, None) if event == EVENT_READ else (None, task))
                selector_register(fileobj, event, data)

            else:
                mask = key.events
                rtask, wtask = key.data

                if event == EVENT_READ and rtask:
                    raise RuntimeError(
                        "Multiple tasks cannot wait to read on "
                        f"the same file descriptor {fileobj}"
                    )
                if event == EVENT_WRITE and wtask:
                    raise RuntimeError(
                        "Multiple tasks cannot wait to write on "
                        f"the same file descriptor {fileobj}"
                    )

                data = ((task, wtask) if event == EVENT_READ else (rtask, task))
                selector_modify(fileobj, mask|event, data)

            return (lambda: unregister_event(fileobj, event))

        # Helper function for `register_event`
        def unregister_event(fileobj, event):
            key = selector_getkey(fileobj)
            mask = key.events
            rtask, wtask = key.data
            mask &= ~event
            if not mask:
                selector_unregister(fileobj)
            else:
                data = ((None, wtask) if event == EVENT_READ else (rtask, None))
                selector_modify(fileobj, mask, data)


        # --- Act decorators ---

        # Check current task's `cancel_pending` before continuing
        def blocking_act(func):
            @functools_wraps(func)
            def _wrapper(*args):
                if current.allow_cancel and current.cancel_pending:
                    exc = current.cancel_pending
                    current.cancel_pending = None
                    return exc
                else:
                    return func(*args)

            return _wrapper

        # Restrict act to event tasks only
        def event_act(func):
            @functools_wraps(func)
            def _wrapper(*args):
                if current.next_event == -1:
                    return TaskEventless("Task is eventless")
                else:
                    return func(*args)

            return _wrapper


        # --- Acts ---

        # Functions invoked by the current task
        # (These are the loop side of acts)
        def _act_get_time():
            return time_monotonic()

        @blocking_act
        def _act_sleep(tm, absolute):
            # A time of 0 means to let other tasks run
            if tm == 0:
                nonlocal running
                running = False
                reschedule(current, time_monotonic())
            else:
                if not absolute:
                    tm += time_monotonic()
                suspend_current("SLEEP", add_timeout(tm, "sleep", current))

        @event_act
        def _act_pop_event():
            try:
                event = event_queue[current.next_event]
            except IndexError:
                return NoEvent("No event available")
            else:
                current.next_event += 1
                return event

        @blocking_act
        @event_act
        def _act_wait_event():
            suspend_current("EVENT_WAIT", event_wait.add(current))

        def _act_get_tk():
            return tk

        def _act_new_task(coro):
            return new_task(coro)

        def _act_this_task():
            return current

        def _act_cancel_task(task, exc, val):
            # Only cancel if not already cancelled
            if not task.cancelled:
                task.cancelled = True
                cancel_task(task, exc=exc, val=val)

        def _act_get_loop():
            return self

        def _act_add_timeout(tm):
            return add_timeout(tm, "timeout", current)

        def _act_remove_timeout(remove_func):
            now = time_monotonic()
            last = remove_func()
            # Check if there is another timeout
            if last and last >= now:
                # Remove the pending exception if it is a timeout
                # as it is most likely the timeout after this
                if isinstance(current.cancel_pending, TaskTimeout):
                    current.cancel_pending = None
            return now

        @blocking_act
        def _act_wait_holder(holder, state):
            suspend_current(state, holder.add(current))

        def _act_wake_holder(holder, n):
            for _ in range(n):
                reschedule(holder.pop())

        @blocking_act
        def _act_io(fileobj, event, state):
            suspend_current(state, register_event(fileobj, event))

        def _act_io_waiting(fileobj):
            try:
                key = selector_getkey(fileobj)
            except KeyError:
                return (None, None)
            else:
                rtask, wtask = key.data
                rtask = (rtask if rtask and rtask.cancel_func else None)
                wtask = (wtask if wtask and wtask.cancel_func else None)
                return (rtask, wtask)

        # Mapping of act name to function
        acts = self._acts = {
            name: value
            for name, value in locals().items()
            if name.startswith("_act_")
        }


        # --- Tkinter helper functions ---

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

        def send(gen, data):
            try:
                return gen.send(data)

            except BaseException as e:
                nonlocal val, exc

                # Tell tkinter to stop running the `wait_window` call
                destroy(frame)

                # The gen was already closed meaning the exception
                # has been saved beforehand.
                # Note: The cycle doesn't have a `return`, meaning
                # the only way to exit is using an exception.
                if isinstance(e, StopAsyncIteration):
                    pass

                elif isinstance(e, StopIteration):
                    val, exc = e.value
                else:
                    val, exc = None, e

        @contextlib_contextmanager
        def bind(widget, func, events):
            widget_bind = widget.bind
            widget_unbind = widget.unbind
            bindings = [(event, widget_bind(event, func, "+")) for event in events]
            try:
                if len(bindings) == 1:
                    yield bindings[0]
                else:
                    yield bindings
            finally:
                for info in bindings:
                    widget_unbind(*info)

        @contextlib_contextmanager
        def protocol(toplevel, func):
            toplevel.protocol("WM_DELETE_WINDOW", func)
            try:
                yield
            finally:
                toplevel.protocol("WM_DELETE_WINDOW", toplevel.destroy)


        # --- Tkinter loop helper functions ---

        # Ensure a resumation of the cycle if required
        @contextlib_contextmanager
        def after_call():
            if ready_tasks or time_wait or selector_getmap():
                if ready_tasks:
                    timeout = 0
                    data = "READY"
                elif time_wait:
                    timeout = (min(time_wait) - time_monotonic()) * 1000
                    data = "SLEEP_WAKE"
                else:
                    timeout = 0
                    data = "SELECT"

                id_ = frame.after(max(int(timeout), 1), lambda: safe_send(cycle, data))

                try:
                    yield
                finally:
                    frame.after_cancel(id_)

            else:
                yield

        # Send if the cycle is suspended
        def safe_send(gen, data):
            state = inspect.getcoroutinestate(gen)
            if state not in {"CORO_RUNNING", "CORO_CLOSED"}:
                return send(gen, data)


        # --- Tkinter callbacks ---

        # Decorator to return "break" for tkinter callbacks
        def tkinter_callback(func):
            @functools_wraps(func)
            def _wrapper(*args):
                func(*args)
                return "break"

            return _wrapper

        # Functions for event callbacks
        @tkinter_callback
        def send_tk_event(event):
            if event.widget is tk:
                event_queue_append(event)
                if event_wait:
                    safe_send(cycle, "EVENT_WAKE")

        @tkinter_callback
        def send_other_event(event):
            if event.widget is not tk:
                event_queue_append(event)
                if event_wait:
                    safe_send(cycle, "EVENT_WAKE")

        @tkinter_callback
        def send_destroy_event(event):
            if event.widget is tk:
                event_queue_append(event)
                if event_wait:
                    frame.after(1, lambda: safe_send(cycle, "EVENT_WAKE"))

        @tkinter_callback
        def close_window():
            if event_wait:
                safe_send(cycle, "CLOSE_WINDOW")


        # --- Outer loop helper functions ---

        @contextlib_contextmanager
        def destroying(*widgets):
            try:
                if len(widgets) == 1:
                    yield widgets[0]
                else:
                    yield widgets
            finally:
                for w in reversed(widgets):
                    destroy(w)

        @contextlib_contextmanager
        def prepare_loop():
            loop = _run_loop()

            try:
                loop.asend(None).send(None)
            except StopIteration:
                pass
            else:
                raise RuntimeError("first cycle didn't stop at initiation")

            try:
                yield loop
            finally:
                try:
                    loop.aclose().send(None)
                except StopIteration as e:
                    pass
                else:
                    raise RuntimeError("final cycle didn't stop at finalization")

        @contextlib_contextmanager
        def prepare_tk():
            with contextlib.ExitStack() as stack:
                stack_enter_context = stack.enter_context
                stack_enter_context(bind(tk, send_tk_event, self._tk_events))
                stack_enter_context(bind(tk, send_other_event, self._other_events))
                stack_enter_context(bind(tk, send_destroy_event, ("<Destroy>",)))
                stack_enter_context(protocol(tk, close_window))
                yield


        # --- Tkinter loop (run using tkinter's mainloop) ---

        async def _run_loop():


            # --- Main loop preparation ---

            # Get loop variables
            nonlocal current, running

            # This is the only way to suspend and wait for a callback
            @types_coroutine
            def _suspend():
                return (yield)

            # Result of main task
            val = exc = None

            # Main task
            main_task = None


            # --- Main loop ---

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

                elif info == "CLOSE_WINDOW":
                    for task in event_wait.popall():
                        cancel_task(task, exc=CloseWindow("X was pressed"))

                # check for amount of events that have been consumed by all event tasks
                event_tasks = [task for task in tasks.values() if task.next_event >= 0]
                if event_tasks:
                    offset = min(task.next_event for task in event_tasks)
                    if offset:
                        for _ in range(offset):
                            event_queue.popleft()
                        for task in event_tasks:
                            task.next_event -= offset

                # Clear the queue if there aren't any tasks to collect events
                # Note: This will leave at most 50 events on the queue.
                elif len(event_queue) > 50:
                    offset = len(event_queue) - 50
                    logger.info("Clearing %s events from event queue.", offset)
                    for _ in range(offset):
                        event_queue.popleft()

                # Do a poll for events (if needed)
                if selector_getmap():

                    # Not blocking call to poll for events
                    events = selector_select(0)

                    # Reschedule events
                    for key, mask in events:
                        rtask, wtask = key.data

                        if mask & EVENT_READ:
                            reschedule(rtask)
                            mask &= ~EVENT_READ
                            rtask = None

                        if mask & EVENT_WRITE:
                            reschedule(wtask)
                            mask &= ~EVENT_WRITE
                            wtask = None

                        if mask:
                            selector_modify(key.fileobj, mask, (rtask, wtask))
                        else:
                            selector_unregister(key.fileobj)

                # Wake sleeps and timeouts here
                # Note: We do timeouts here as the previous actions may
                # reschedule a task that has hit a timeout. We'll let the
                # task run and set a pending exception.
                now = time_monotonic()
                for tm in list(time_wait.keys()):

                    # Only check for ones that have expired
                    if tm > now:
                        continue

                    for task, ty in time_wait.pop(tm).popall():

                        # Check if the times match
                        if getattr(task, ty) != tm:
                            continue

                        setattr(task, ty, None)
                        if ty == "sleep":
                            reschedule(task, now)
                        else:
                            cancel_task(task, exc=TaskTimeout(f"Task timed out at {now}"))

                # Run all ready tasks
                for _ in range(len(ready_tasks)):
                    current = ready_tasks_popleft()
                    current.state = "RUNNING"
                    running = True

                    # Run the current task until it suspends or terminated
                    while running:

                        # Send the act result
                        try:
                            # Note: The `_act` generator will raise the
                            # result if it is an exception. No need for
                            # chucking exceptions deep into the stack.
                            act = current._send(current._val)

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
                                if current.report_crash and not isinstance(
                                    e, (TaskCancelled, SystemExit)
                                ):
                                    logger.error(
                                        "Task crash: %r", current, exc_info=True
                                    )
                                # Re-raise the exception to end the loop immediately
                                if not isinstance(e, Exception):
                                    raise

                            break

                        # Act received: time to run it
                        else:

                            # If this raises an exception, let it propagate
                            # as this is most likely a programming error on
                            # the loop, not the task.
                            current._val = acts[act[0]](*act[1:])


        # --- Outer loop preparation ---

        val = exc = None
        tk = frame = None
        loop = cycle = None

        # Wrap toplevel and loop with a context manager
        with contextlib.ExitStack() as stack:
            stack_enter_context = stack.enter_context
            tk = stack_enter_context(destroying(tkinter.Tk()))
            loop = stack_enter_context(prepare_loop())
            stack_enter_context(prepare_tk())


            # --- Outer loop ---

            while True:

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

                # Check if cycle has closed
                if inspect.getcoroutinestate(cycle) != "CORO_CLOSED":
                    cycle.close()
                    raise RuntimeError(
                        "frame closed before main coro finished"
                    ) from exc

                # Check if toplevel exists
                if not exists(tk):
                    raise RuntimeError("Toplevel was destroyed") from exc

                # Check if loop is still running
                if getasyncgenstate(loop) == "AGEN_CLOSED":
                    raise RuntimeError("Loop was closed") from exc


def run(coro):
    with TkLoop() as iw:
        return iw.run(coro)
