# Base exception class for Tkio
class TkioError(Exception):
    pass


# The red X is pressed.
class CloseWindow(TkioError):
    pass


# There is no new event to return
class NoEvent(TkioError):
    pass


# When an eventless task tries to access events
class TaskEventless(TkioError):
    pass


# When the joined task fails
# (to differentiate between the running task and the joined one)
class TaskError(TkioError):
    pass


# Base exception class for cancellation exceptions
class CancelledError(TkioError):
    pass


# Raised inside a blocking function when task is cancelled
class TaskCancelled(CancelledError):
    pass


# When a timeout expires
class TaskTimeout(CancelledError):
    pass


# When a timeout expires (but is caused by an enclosing timeout)
class TimeoutCancellation(CancelledError):
    pass
