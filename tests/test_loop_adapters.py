# vs-engine
# Copyright (C) 2022  cid-chan
# Copyright (C) 2025  Jaded-Encoding-Thaumaturgy
# This project is licensed under the EUPL-1.2
# SPDX-License-Identifier: EUPL-1.2
"""Tests for event loop adapters."""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import functools
import threading
import unittest.mock
from collections.abc import Awaitable, Callable, Generator, Iterator
from concurrent.futures import CancelledError, Future
from functools import wraps
from inspect import isgenerator
from typing import Any, Concatenate, Self

import pytest
import trio

from vsengine.adapters.asyncio import AsyncIOLoop
from vsengine.adapters.trio import TrioEventLoop
from vsengine.loops import NO_LOOP, Cancelled, EventLoop, _NoEventLoop, set_loop


def make_async[T: AdapterTest, **P](func: Callable[Concatenate[T, P], Any]) -> Callable[Concatenate[T, P], None]:
    """Decorator to run a generator-based test within a loop."""

    @wraps(func)
    def _wrapped(self: T, *args: P.args, **kwargs: P.kwargs) -> None:
        self.run_within_loop(func, *args, **kwargs)

    return _wrapped


def is_async[T: AsyncAdapterTest, **P](
    func: Callable[Concatenate[T, P], Awaitable[Any]],
) -> Callable[Concatenate[T, P], None]:
    """Decorator to run an async test within a loop."""

    @wraps(func)
    def _wrapped(self: T, *args: P.args, **kwargs: P.kwargs) -> None:
        self.run_within_loop_async(func, *args, **kwargs)

    return _wrapped


class AdapterTest:
    """Base class for event loop adapter tests."""

    @contextlib.contextmanager
    def with_loop(self) -> Generator[EventLoop]:
        loop = self.make_loop()
        set_loop(loop)
        try:
            yield loop
        finally:
            set_loop(NO_LOOP)

    def make_loop(self) -> EventLoop:
        raise NotImplementedError

    def run_within_loop[**P](
        self,
        func: Callable[Concatenate[Self, P], Any],
        *args: P.args,
        **kwargs: P.kwargs,
    ) -> None:
        raise NotImplementedError

    def resolve_to_thread_future[T](self, fut: Future[T]) -> Generator[None, None, T]:
        raise NotImplementedError

    @contextlib.contextmanager
    def assert_cancelled(self) -> Generator[None]:
        raise NotImplementedError

    @make_async
    def test_wrap_cancelled_without_cancellation(self) -> None:
        with self.with_loop() as loop, loop.wrap_cancelled():
            ...

    @make_async
    def test_wrap_cancelled_with_cancellation(self) -> Iterator[None]:
        with self.with_loop() as loop, self.assert_cancelled(), loop.wrap_cancelled():
            raise Cancelled

    @make_async
    def test_wrap_cancelled_with_other_exception(self) -> Iterator[None]:
        with self.with_loop() as loop, pytest.raises(RuntimeError), loop.wrap_cancelled():
            raise RuntimeError()
        yield

    @make_async
    def test_next_cycle_doesnt_throw_when_not_cancelled(self) -> Iterator[None]:
        with self.with_loop() as loop:
            fut = loop.next_cycle()
            yield
            assert fut.done()
            assert fut.result() is None

    @make_async
    def test_from_thread_with_success(self) -> Iterator[None]:
        def test_func() -> AdapterTest:
            return self

        with self.with_loop() as loop:
            fut = loop.from_thread(test_func)
            yield
            assert fut.result(timeout=0.5) is self

    @make_async
    def test_from_thread_with_failure(self) -> Iterator[None]:
        def test_func() -> None:
            raise RuntimeError

        with self.with_loop() as loop:
            fut = loop.from_thread(test_func)
            yield
            with pytest.raises(RuntimeError):
                fut.result(timeout=0.5)

    @make_async
    def test_from_thread_forwards_correctly(self) -> Iterator[None]:
        def test_func(*args: Any, **kwargs: Any) -> tuple[tuple[Any, ...], dict[str, Any]]:
            return args, kwargs

        with self.with_loop() as loop:
            fut = loop.from_thread(test_func, 1, 2, 3, a="b", c="d")
            yield
            args, kwargs = fut.result(timeout=0.5)
            assert args == (1, 2, 3)
            assert kwargs == {"a": "b", "c": "d"}

    @make_async
    def test_to_thread_spawns_a_new_thread(self) -> Iterator[None]:
        def test_func() -> threading.Thread:
            return threading.current_thread()

        with self.with_loop() as loop:
            t2 = yield from self.resolve_to_thread_future(loop.to_thread(test_func))
            assert threading.current_thread() != t2

    @make_async
    def test_to_thread_runs_inline_with_failure(self) -> Iterator[None]:
        def test_func() -> None:
            raise RuntimeError

        with self.with_loop() as loop, pytest.raises(RuntimeError):
            yield from self.resolve_to_thread_future(loop.to_thread(test_func))

    @make_async
    def test_to_thread_forwards_correctly(self) -> Iterator[None]:
        def test_func(*args: Any, **kwargs: Any) -> tuple[tuple[Any, ...], dict[str, Any]]:
            return args, kwargs

        with self.with_loop() as loop:
            args, kwargs = yield from self.resolve_to_thread_future(loop.to_thread(test_func, 1, 2, 3, a="b", c="d"))
            assert args == (1, 2, 3)
            assert kwargs == {"a": "b", "c": "d"}


class AsyncAdapterTest(AdapterTest):
    """Base class for async event loop adapter tests."""

    def run_within_loop[**P](
        self,
        func: Callable[Concatenate[Self, P], Any],
        *args: P.args,
        **kwargs: P.kwargs,
    ) -> None:
        async def wrapped(self: Self) -> None:
            result = func(self, *args, **kwargs)
            if isgenerator(result):
                for _ in result:
                    await self.next_cycle()

        return self.run_within_loop_async(wrapped)

    def run_within_loop_async[**P](
        self,
        func: Callable[Concatenate[Self, P], Awaitable[Any]],
        *args: P.args,
        **kwargs: P.kwargs,
    ) -> None:
        raise NotImplementedError

    async def wait_for[T](self, coro: Awaitable[T], timeout: float) -> T:
        raise NotImplementedError

    async def next_cycle(self) -> None: ...

    def resolve_to_thread_future[T](self, fut: Future[T]) -> Generator[None, None, T]:
        while not fut.done():
            yield
        return fut.result()

    @is_async
    async def test_await_future_success(self) -> None:
        with self.with_loop() as loop:
            fut: Future[int] = Future()

            def _setter() -> None:
                fut.set_result(1)

            threading.Thread(target=_setter).start()
            assert await self.wait_for(loop.await_future(fut), 0.5) == 1

    @is_async
    async def test_await_future_failure(self) -> None:
        with self.with_loop() as loop:
            fut: Future[int] = Future()

            def _setter() -> None:
                fut.set_exception(RuntimeError())

            threading.Thread(target=_setter).start()
            with pytest.raises(RuntimeError):
                await self.wait_for(loop.await_future(fut), 0.5)


class TestNoLoop(AdapterTest):
    """Tests for the no-event-loop adapter."""

    def make_loop(self) -> EventLoop:
        return _NoEventLoop()

    def run_within_loop[**P](
        self,
        func: Callable[Concatenate[Self, P], Any],
        *args: P.args,
        **kwargs: P.kwargs,
    ) -> None:
        result = func(self, *args, **kwargs)
        if isgenerator(result):
            for _ in result:
                ...

    @contextlib.contextmanager
    def assert_cancelled(self) -> Generator[None]:
        with pytest.raises(CancelledError):
            yield

    def resolve_to_thread_future[T](self, fut: Future[T]) -> Generator[None, None, T]:
        yield
        return fut.result(timeout=0.5)


class TestAsyncIO(AsyncAdapterTest):
    """Tests for the asyncio event loop adapter."""

    def make_loop(self) -> AsyncIOLoop:
        return AsyncIOLoop()

    def run_within_loop_async[**P](
        self,
        func: Callable[Concatenate[Self, P], Awaitable[Any]],
        *args: P.args,
        **kwargs: P.kwargs,
    ) -> None:
        async def wrapped() -> None:
            await func(self, *args, **kwargs)

        asyncio.run(wrapped())

    async def next_cycle(self) -> None:
        await asyncio.sleep(0.01)

    async def wait_for[T](self, coro: Awaitable[T], timeout: float) -> T:
        return await asyncio.wait_for(coro, timeout)

    @contextlib.contextmanager
    def assert_cancelled(self) -> Generator[None]:
        with pytest.raises(asyncio.CancelledError):
            yield


class TestTrio(AsyncAdapterTest):
    """Tests for the trio event loop adapter."""

    nursery: trio.Nursery

    def make_loop(self) -> TrioEventLoop:
        return TrioEventLoop(self.nursery)

    async def next_cycle(self) -> None:
        await trio.sleep(0.01)

    def run_within_loop_async[**P](
        self,
        func: Callable[Concatenate[Self, P], Awaitable[Any]],
        *args: P.args,
        **kwargs: P.kwargs,
    ) -> None:
        async def wrapped() -> None:
            async with trio.open_nursery() as nursery:
                self.nursery = nursery
                await func(self, *args, **kwargs)

        trio.run(wrapped)

    async def wait_for[T](self, coro: Awaitable[T], timeout: float) -> T:
        with trio.fail_after(timeout):
            return await coro

    @contextlib.contextmanager
    def assert_cancelled(self) -> Generator[None]:
        with pytest.raises(trio.Cancelled):
            yield


def test_asyncio_from_thread_without_running_loop() -> None:
    """Ensure AsyncIOLoop gracefully executes inline when no asyncio loop is running."""
    loop = AsyncIOLoop()
    fut = loop.from_thread(lambda x: x * 2, 21)
    assert fut.done()
    assert fut.result() == 42


def test_trio_from_thread_without_running_token() -> None:
    """Ensure TrioEventLoop gracefully executes inline when no Trio token is active."""
    nursery = unittest.mock.MagicMock()
    nursery.cancel_scope.cancel_called = False
    loop = TrioEventLoop(nursery)
    fut = loop.from_thread(lambda x: x + 1, 10)
    assert fut.done()
    assert fut.result() == 11


def test_trio_from_thread_contextvars() -> None:
    """Ensure TrioEventLoop propagates contextvars from the calling thread."""
    var: contextvars.ContextVar[str] = contextvars.ContextVar("var", default="default")

    async def main() -> None:
        async with trio.open_nursery() as nursery:
            loop = TrioEventLoop(nursery)
            results = list[str]()

            def worker() -> None:
                var.set("custom_val")
                fut = loop.from_thread(var.get)
                results.append(fut.result(timeout=0.5))

            t = threading.Thread(target=worker)
            t.start()
            t.join()

            assert results == ["custom_val"]

    trio.run(main)


def test_to_thread_with_partial() -> None:
    """Ensure to_thread handles functools.partial and callables without __name__."""

    def add(a: int, b: int) -> int:
        return a + b

    part = functools.partial(add, 10, 20)

    # Test with TrioEventLoop
    async def trio_main() -> None:
        async with trio.open_nursery() as nursery:
            loop = TrioEventLoop(nursery)
            fut = loop.to_thread(part)
            while not fut.done():
                await trio.sleep(0.01)
            assert fut.result() == 30

    trio.run(trio_main)

    # Test with AsyncIOLoop
    async def asyncio_main() -> None:
        loop = AsyncIOLoop()
        fut = loop.to_thread(part)
        while not fut.done():
            await asyncio.sleep(0.01)
        assert fut.result() == 30

    asyncio.run(asyncio_main())


def test_trio_detach_lifecycle() -> None:
    """Ensure detach cleanly resets token and limiter."""
    nursery = unittest.mock.MagicMock()
    loop = TrioEventLoop(nursery)
    loop.token = unittest.mock.MagicMock()
    loop.limiter = unittest.mock.MagicMock()

    loop.detach()
    nursery.cancel_scope.cancel.assert_called_once()
    assert loop._token is None
    assert loop._limiter is None
