"""Task-local attribution shared by native adapters, model and tool recording."""
from contextlib import contextmanager
from contextvars import ContextVar

_identity = ContextVar('collaboration_call_identity', default={})


def call_identity():
    return dict(_identity.get())


@contextmanager
def call_scope(**fields):
    token = _identity.set({**_identity.get(), **fields})
    try:
        yield
    finally:
        _identity.reset(token)
