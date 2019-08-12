# Base exception class for Tkio
class TkioError(Exception):
    ...


# The red X is pressed.
class CloseWindow(TkioError):
    ...


# There is no new event to return
class NoEvent(TkioError):
    ...


# Raised inside a blocking function when task is cancelled
class TaskCancelled(TkioError):
    ...


# When the joined task fails
# (to differentiate between the running task and the joined one)
class TaskError(TkioError):
    ...


# When an eventless task tries to access events
class TaskEventless(TkioError):
    ...
