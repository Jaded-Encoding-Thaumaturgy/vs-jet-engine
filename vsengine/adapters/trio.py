# vs-engine
# Copyright (C) 2022  cid-chan
# Copyright (C) 2025  Jaded-Encoding-Thaumaturgy
# This project is licensed under the EUPL-1.2
# SPDX-License-Identifier: EUPL-1.2

from __future__ import annotations

import contextlib
from collections.abc import Callable, Generator
from concurrent.futures import Future

import trio

from vsengine.loops import Cancelled, EventLoop


class TrioEventLoop(EventLoop):
    """
    Bridges vs-engine to Trio.
    """

    def __init__(self, nursery: trio.Nursery, limiter: trio.CapacityLimiter | None = None) -> None:
        self.nursery = nursery
        self._limiter = limiter
        self._token: trio.lowlevel.TrioToken | None = None

    @property
    def limiter(self) -> trio.CapacityLimiter | None:
        if self._limiter is not None:
            return self._limiter
        with contextlib.suppress(RuntimeError):
            self._limiter = trio.to_thread.current_default_thread_limiter()
        return self._limiter

    @limiter.setter
    def limiter(self, value: trio.CapacityLimiter | None) -> None:
        self._limiter = value

    @property
    def token(self) -> trio.lowlevel.TrioToken | None:
        if self._token is not None:
            return self._token
        try:
            self._token = trio.lowlevel.current_trio_token()
            return self._token
        except RuntimeError:
            return None

    @token.setter
    def token(self, value: trio.lowlevel.TrioToken | None) -> None:
        self._token = value

    def attach(self) -> None:
        with contextlib.suppress(RuntimeError):
            self._token = trio.lowlevel.current_trio_token()

    def detach(self) -> None:
        self.nursery.cancel_scope.cancel()

    def from_thread[**P, R](self, func: Callable[P, R], *args: P.args, **kwargs: P.kwargs) -> Future[R]:
        future = Future[R]()

        def executor() -> Future[R]:
            if not future.set_running_or_notify_cancel():
                return future

            try:
                result = func(*args, **kwargs)
            except BaseException as e:
                future.set_exception(e)
            else:
                future.set_result(result)
            return future

        if token := self.token:
            token.run_sync_soon(executor)
            return future

        return executor()

    def to_thread[**P, R](self, func: Callable[P, R], *args: P.args, **kwargs: P.kwargs) -> Future[R]:
        if self.nursery.cancel_scope.cancel_called:
            return super().to_thread(func, *args, **kwargs)

        future = Future[R]()

        async def run() -> None:
            def executor() -> None:
                try:
                    result = func(*args, **kwargs)
                    future.set_result(result)
                except BaseException as e:
                    future.set_exception(e)

            await trio.to_thread.run_sync(executor, limiter=self.limiter)

        self.nursery.start_soon(run, name=func.__name__)
        return future

    def next_cycle(self) -> Future[None]:
        scope = trio.CancelScope()
        future = Future[None]()

        def continuation() -> None:
            if scope.cancel_called:
                future.set_exception(Cancelled())
            else:
                future.set_result(None)

        self.from_thread(continuation)
        return future

    async def await_future[T](self, future: Future[T]) -> T:
        event = trio.Event()

        def _when_done(_: Future[T]) -> None:
            self.from_thread(event.set)

        future.add_done_callback(_when_done)

        await event.wait()

        try:
            return future.result()
        except BaseException:
            with self.wrap_cancelled():
                raise

    @contextlib.contextmanager
    def wrap_cancelled(self) -> Generator[None]:
        try:
            yield
        except Cancelled:
            raise trio.Cancelled.__new__(trio.Cancelled) from None
