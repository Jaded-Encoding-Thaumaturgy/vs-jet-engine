# vs-engine
# Copyright (C) 2022  cid-chan
# Copyright (C) 2025  Jaded-Encoding-Thaumaturgy
# This project is licensed under the EUPL-1.2
# SPDX-License-Identifier: EUPL-1.2
"""Tests for the event loop API."""

import contextlib
import queue
import threading
from concurrent.futures import CancelledError, Future
from typing import Any, NoReturn

import pytest
import vapoursynth

from vsengine.loops import Cancelled, EventLoop, _NoEventLoop, from_thread, get_loop, set_loop, to_thread
from vsengine.policy import Policy, ThreadLocalStore


class FailingEventLoop:
    """Event loop that fails on attach."""

    def attach(self) -> NoReturn:
        raise RuntimeError()


class SomeOtherLoop:
    """A simple event loop for testing."""

    def attach(self) -> None: ...

    def detach(self) -> None: ...


class SpinLoop(EventLoop):
    """A spin-based event loop for testing."""

    def __init__(self) -> None:
        self.queue = queue.Queue[tuple[Future[Any], Any, tuple[Any, ...], dict[str, Any]] | None]()

    def attach(self) -> None: ...

    def detach(self) -> None: ...

    def run(self) -> None:
        while (value := self.queue.get()) is not None:
            future, func, args, kwargs = value
            try:
                result = func(*args, **kwargs)
            except BaseException as e:
                future.set_exception(e)
            else:
                future.set_result(result)

    def stop(self) -> None:
        self.queue.put(None)

    def from_thread(self, func: Any, *args: Any, **kwargs: Any) -> Future[Any]:
        fut = Future[Any]()
        self.queue.put((fut, func, args, kwargs))
        return fut


# NoLoop tests


def test_no_loop_wrap_cancelled_converts_the_exception() -> None:
    loop = _NoEventLoop()
    with pytest.raises(CancelledError), loop.wrap_cancelled():
        raise Cancelled


# Loop API tests


def test_loop_can_override() -> None:
    loop = _NoEventLoop()
    set_loop(loop)
    assert get_loop() is loop


def test_loop_reverts_to_no_on_error() -> None:
    try:
        set_loop(SomeOtherLoop())  # type: ignore[arg-type]
        loop = FailingEventLoop()
        with contextlib.suppress(RuntimeError):
            set_loop(loop)  # type: ignore[arg-type]

        assert isinstance(get_loop(), _NoEventLoop)
    finally:
        set_loop(_NoEventLoop())


def test_loop_from_thread_retains_environment() -> None:
    loop = SpinLoop()
    set_loop(loop)
    thr = threading.Thread(target=loop.run)
    thr.start()

    def test() -> vapoursynth.Environment:
        return vapoursynth.get_current_environment()

    try:
        with Policy(ThreadLocalStore()) as p, p.new_environment() as env1, env1.use():
            fut = from_thread(test)
            assert fut.result(timeout=0.1) == env1.vs_environment
    finally:
        loop.stop()
        thr.join()
        set_loop(_NoEventLoop())


def test_loop_from_thread_does_not_require_environment() -> None:
    loop = SpinLoop()
    set_loop(loop)
    thr = threading.Thread(target=loop.run)
    thr.start()

    def test() -> None:
        pass

    try:
        from_thread(test).result(timeout=0.1)
    finally:
        loop.stop()
        thr.join()
        set_loop(_NoEventLoop())


def test_loop_to_thread_retains_environment() -> None:
    def test() -> vapoursynth.Environment:
        return vapoursynth.get_current_environment()

    with Policy(ThreadLocalStore()) as p, p.new_environment() as env1, env1.use():
        fut = to_thread(test)
        assert fut.result(timeout=0.1) == env1.vs_environment


def test_loop_to_thread_does_not_require_environment() -> None:
    def test() -> None:
        pass

    fut = to_thread(test)
    fut.result(timeout=0.1)
