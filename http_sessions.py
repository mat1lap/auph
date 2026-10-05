"""Isolate Requests session state while sharing its thread-safe adapter pools."""

from __future__ import annotations

import copy
import inspect
from collections.abc import Callable
from functools import wraps
from typing import ParamSpec, TypeVar

import requests

P = ParamSpec("P")
T = TypeVar("T")


def clone_session(source: requests.Session) -> requests.Session:
    """Copy per-request state; the caller retains ownership of shared adapters.

    Custom auth callbacks/adapters keep their existing behavior. Their own
    thread-safety is outside Requests session isolation; cookies and headers
    are independent. Never close shared adapters from a worker.
    """
    clone = requests.Session()
    clone.close()
    clone.headers = requests.structures.CaseInsensitiveDict(source.headers)
    clone.cookies = source.cookies.copy()
    clone.auth = source.auth
    clone.proxies = dict(source.proxies)
    clone.params = copy.copy(source.params)
    clone.hooks = {name: list(callbacks) for name, callbacks in source.hooks.items()}
    clone.verify, clone.cert = source.verify, source.cert
    clone.trust_env, clone.max_redirects = source.trust_env, source.max_redirects
    clone.adapters = dict(source.adapters)
    return clone


def isolated_worker_session(function: Callable[P, T]) -> Callable[P, T]:
    signature = inspect.signature(function)

    @wraps(function)
    def wrapped(*args: P.args, **kwargs: P.kwargs) -> T:
        bound = signature.bind(*args, **kwargs)
        session = bound.arguments.get("session")
        # Duck-typed injected transports and subclasses may implement their
        # own state/transport. Preserve them rather than erase that behavior.
        if type(session) is not requests.Session:
            return function(*args, **kwargs)
        clone = clone_session(session)
        bound.arguments["session"] = clone
        try:
            return function(*bound.args, **bound.kwargs)
        finally:
            clone.adapters.clear()

    return wrapped
