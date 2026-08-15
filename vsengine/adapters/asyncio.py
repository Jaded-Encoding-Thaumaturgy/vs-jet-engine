# vs-engine
# Copyright (C) 2022  cid-chan
# Copyright (C) 2025  Jaded-Encoding-Thaumaturgy
# This project is licensed under the EUPL-1.2
# SPDX-License-Identifier: EUPL-1.2

import asyncio
import contextlib
import contextvars
from collections.abc import Callable, Generator
from concurrent.futures import Future

from vsengine.loops import Cancelled, EventLoop


class AsyncIOLoop(EventLoop):
    """
    Bridges vs-engine to AsyncIO.
    """

    def __init__(self, loop: asyncio.AbstractEventLoop | None = None) -> None:
        self._loop = loop

    @property
    def loop(self) -> asyncio.AbstractEventLoop | None:
        if self._loop is not None and not self._loop.is_closed():
            return self._loop
        try:
            self._loop = asyncio.get_running_loop()
            return self._loop
        except RuntimeError:
            return None

    @loop.setter
    def loop(self, value: asyncio.AbstractEventLoop | None) -> None:
        self._loop = value

    def attach(self) -> None:
        with contextlib.suppress(RuntimeError):
            self._loop = asyncio.get_running_loop()

    def detach(self) -> None:
        self._loop = None

    def from_thread[**P, R](self, func: Callable[P, R], *args: P.args, **kwargs: P.kwargs) -> Future[R]:
        future = Future[R]()

        def wrap() -> Future[R]:
            if not future.set_running_or_notify_cancel():
                return future

            try:
                result = func(*args, **kwargs)
            except BaseException as e:
                future.set_exception(e)
            else:
                future.set_result(result)

            return future

        if (loop := self.loop) is None or loop.is_closed():
            return wrap()

        loop.call_soon_threadsafe(wrap, context=contextvars.copy_context())
        return future

    def to_thread[**P, R](self, func: Callable[P, R], *args: P.args, **kwargs: P.kwargs) -> Future[R]:
        if (loop := self.loop) is None or loop.is_closed():
            return super().to_thread(func, *args, **kwargs)

        future = Future[R]()

        async def run() -> None:
            try:
                result = await asyncio.to_thread(lambda: func(*args, **kwargs))
            except BaseException as e:
                future.set_exception(e)
            else:
                future.set_result(result)

        loop.create_task(run(), name=func.__name__, context=contextvars.copy_context())
        return future

    def next_cycle(self) -> Future[None]:
        if (loop := self.loop) is None or loop.is_closed():
            return super().next_cycle()

        future = Future[None]()
        task = asyncio.current_task(loop)

        def continuation() -> None:
            if task is None or not task.cancelled():
                future.set_result(None)
            else:
                future.set_exception(Cancelled())

        loop.call_soon(continuation)
        return future

    async def await_future[T](self, future: Future[T]) -> T:
        with self.wrap_cancelled():
            return await asyncio.wrap_future(future)

    @contextlib.contextmanager
    def wrap_cancelled(self) -> Generator[None]:
        try:
            yield
        except Cancelled:
            raise asyncio.CancelledError() from None
