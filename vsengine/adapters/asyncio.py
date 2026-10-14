# vs-engine
# Copyright (C) 2022  cid-chan
# Copyright (C) 2025  Jaded-Encoding-Thaumaturgy
# This project is licensed under the EUPL-1.2
# SPDX-License-Identifier: EUPL-1.2

import asyncio
import contextlib
import contextvars
from collections.abc import Callable, Generator
from concurrent.futures import Future, ThreadPoolExecutor
from functools import partial
from typing import override

from vsengine.loops import Cancelled, EventLoop


class AsyncIOLoop(EventLoop):
    """
    Bridges vs-engine to AsyncIO.
    """

    def __init__(
        self,
        loop: asyncio.AbstractEventLoop | None = None,
        *,
        executor: ThreadPoolExecutor | None = None,
        max_workers: int | None = None,
    ) -> None:
        self._loop = loop
        if self._loop is None:
            with contextlib.suppress(RuntimeError):
                self._loop = asyncio.get_running_loop()

        self._executor = executor
        if self._executor is None and max_workers is not None:
            self._executor = ThreadPoolExecutor(max_workers, thread_name_prefix="vsengine-worker")
            self._owns_executor = True
        else:
            self._owns_executor = False

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

    @override
    def attach(self) -> None:
        with contextlib.suppress(RuntimeError):
            self._loop = asyncio.get_running_loop()

    @override
    def detach(self) -> None:
        if self._owns_executor and self._executor is not None:
            self._executor.shutdown(wait=False, cancel_futures=True)
            self._executor = None
        self._loop = None

    def from_thread[**P, R](self, func: Callable[P, R], *args: P.args, **kwargs: P.kwargs) -> Future[R]:
        if (loop := self.loop) is None or loop.is_closed():
            raise RuntimeError("No running asyncio event loop or loop is closed.")

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

        loop.call_soon_threadsafe(wrap, context=contextvars.copy_context())
        return future

    @override
    def to_thread[**P, R](self, func: Callable[P, R], *args: P.args, **kwargs: P.kwargs) -> Future[R]:
        if (loop := self.loop) is None or loop.is_closed():
            raise RuntimeError("No running asyncio event loop or loop is closed.")

        future = Future[R]()
        ctx = contextvars.copy_context()

        async def run() -> None:
            if not future.set_running_or_notify_cancel():
                return

            try:
                result = await loop.run_in_executor(self._executor, partial(ctx.run, func, *args, **kwargs))
            except BaseException as e:
                future.set_exception(e)
            else:
                future.set_result(result)

        def schedule() -> None:
            loop.create_task(run(), name=getattr(func, "__name__", None))

        try:
            running_loop = asyncio.get_running_loop()
        except RuntimeError:
            running_loop = None

        if running_loop is loop:
            schedule()
        else:
            loop.call_soon_threadsafe(schedule)

        return future

    @override
    def next_cycle(self) -> Future[None]:
        if (loop := self.loop) is None or loop.is_closed():
            raise RuntimeError("No running asyncio event loop or loop is closed.")

        future = Future[None]()
        task = asyncio.current_task(loop)

        def continuation() -> None:
            if task is None or not task.cancelled():
                future.set_result(None)
            else:
                future.set_exception(Cancelled())

        loop.call_soon(continuation)
        return future

    @override
    async def await_future[T](self, future: Future[T] | asyncio.Future[T]) -> T:
        with self.wrap_cancelled():
            return await asyncio.wrap_future(future)

    @contextlib.contextmanager
    @override
    def wrap_cancelled(self) -> Generator[None]:
        try:
            yield
        except Cancelled:
            raise asyncio.CancelledError from None
