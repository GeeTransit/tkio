from abc import ABC, abstractmethod
from collections import deque


# Abstract base class for holders
class Holder(ABC):

    __slots__ = ()

    @abstractmethod
    def __len__(self):
        raise NotImplementedError

    @abstractmethod
    def add(self, obj):
        raise NotImplementedError

    @abstractmethod
    def pop(self):
        raise NotImplementedError

    def popall(self):
        return [self.pop() for _ in range(len(self))]


# Set based holder (ordering is not important)
class SetHolder(Holder):

    __slots__ = ("_set",)

    def __init__(self):
        self._set = set()

    def __len__(self):
        return len(self._set)

    def add(self, obj):
        self._set.add(obj)
        return lambda: self._set.discard(obj)

    def pop(self):
        return self._set.pop()


# Deque based holder (ordering is first-in, first-out)
class FIFOHolder(Holder):

    __slots__ = ("_deque", "len")

    def __init__(self):
        self._deque = deque()
        self._len = 0

    def __len__(self):
        return self._len

    def add(self, obj):
        item = [obj]
        self._deque.append(item)
        self._len += 1

        def _remove():
            if item[0] is not None:
                item[0] = None
                self._len -= 1

        return _remove

    def pop(self):
        while True:
            try:
                obj, = self._deque.popleft()
            except IndexError as e:
                raise KeyError from e
            if obj:
                self._len -= 1
                return obj
