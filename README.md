tkio - asynchronous tkinter
===========================

Tkio is a library for asynchronous code that works with tkinter. As
tkinter doesn't like working in multiple threads, multitasking in a
single thread is a solution that doesn't anger it. Tkio uses Python
coroutines and the async / await syntax from Python 3.5.

Note: This library isn't even version 0.1, so expect frequent
changes to the API.

Special thanks to David Beazley's curio project that inspired this
library to be what it is.
