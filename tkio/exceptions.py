# The red X is pressed.
class CloseWindow(Exception):
    ...

# There is no new event to return
class NoEvent(Exception):
    ...

# Raised inside a blocking function when task is cancelled
class TaskCancelled(Exception):
    ...

# When the joined task fails
# (to differentiate between the running task and the joined one)
class TaskError(Exception):
    ...

# When an eventless task tries to access events
class TaskEventless(Exception):
    ...
