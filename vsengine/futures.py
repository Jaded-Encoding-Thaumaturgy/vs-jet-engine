# vs-engine
# Copyright (C) 2022  cid-chan
# Copyright (C) 2025  Jaded-Encoding-Thaumaturgy
# This project is licensed under the EUPL-1.2
# SPDX-License-Identifier: EUPL-1.2
from __future__ import annotations

__lazy_modules__ = ["asyncio", "multiprocessing"]

import asyncio
import contextvars
import inspect
import multiprocessing
from collections import deque
from collections.abc import AsyncGenerator, AsyncIterator, Awaitable, Callable, Generator, Iterable, Iterator
from concurrent.futures import CancelledError, Future, InvalidStateError
from contextlib import AbstractAsyncContextManager, AbstractContextManager, suppress
from functools import wraps
from types import TracebackType
from typing import TYPE_CHECKING, Any, Literal, Protocol, Self, overload, override

if TYPE_CHECKING:
    import trio

from vsengine.loops import get_loop, keep_environment

__all__ = ["UnifiedFuture", "UnifiedIterator", "as_completed", "unified"]


class FutureLike[V](Protocol):
    def result(self) -> V: ...


class AsyncFutureLike[V](Protocol):
    def __await__(self) -> Generator[Any, None, V]: ...


type UnifiedFutureLike[T] = UnifiedFuture[T]
type UnifiedIteratorLike[T] = UnifiedIterator[T]


class UnifiedFuture[T](Future[T], AbstractContextManager[Any], AbstractAsyncContextManager[Any], Awaitable[T]):
    """
    A Promise-inspired Future that unifies concurrent.futures.Future
    with Python's synchronous and asynchronous context manager and awaitable protocols.
    """

    def __enter__[EnterT](self: FutureLike[AbstractContextManager[EnterT, Any]]) -> EnterT:
        obj = self.result()

        if isinstance(obj, AbstractContextManager):
            return obj.__enter__()

        raise NotImplementedError("(async) with is not implemented for this object")

    def __exit__(
        self,
        exc: type[BaseException] | None,
        val: BaseException | None,
        tb: TracebackType | None,
    ) -> bool | None:
        obj = self.result()

        if isinstance(obj, AbstractContextManager):
            return obj.__exit__(exc, val, tb)

        raise NotImplementedError("(async) with is not implemented for this object")

    async def __aenter__[EnterT](
        self: AsyncFutureLike[AbstractAsyncContextManager[EnterT, Any] | AbstractContextManager[EnterT, Any]],
    ) -> EnterT:
        result = await self

        if isinstance(result, AbstractAsyncContextManager):
            return await result.__aenter__()
        if isinstance(result, AbstractContextManager):
            return result.__enter__()

        raise NotImplementedError("(async) with is not implemented for this object")

    async def __aexit__(
        self,
        exc: type[BaseException] | None,
        val: BaseException | None,
        tb: TracebackType | None,
    ) -> bool | None:
        result = await self

        if isinstance(result, AbstractAsyncContextManager):
            return await result.__aexit__(exc, val, tb)
        if isinstance(result, AbstractContextManager):
            return result.__exit__(exc, val, tb)

        raise NotImplementedError("(async) with is not implemented for this object")

    def __await__(self) -> Generator[Any, None, T]:
        return get_loop().await_future(self).__await__()

    def try_set_result(self, result: T) -> bool:
        """
        Attempt to set the return value of the Future.

        Args:
            result: The result value to assign to the Future.

        Returns:
            True if the result was successfully set, or False if the Future
            was already cancelled or completed (suppressing InvalidStateError).
        """
        try:
            self.set_result(result)
            return True
        except InvalidStateError:
            return False

    def try_set_exception(self, exc: BaseException) -> bool:
        """
        Attempt to set the exception of the Future.

        Args:
            exc: The exception to assign to the Future.

        Returns:
            True if the exception was successfully set, or False if the Future
            was already cancelled or completed (suppressing InvalidStateError).
        """
        try:
            self.set_exception(exc)
            return True
        except InvalidStateError:
            return False

    # Adding callbacks
    @override
    def add_done_callback(self, fn: Callable[[UnifiedFuture[T]], Any]) -> None:
        """
        Register a callback to be called when this future completes.

        Wraps the callback in `keep_environment` so that the VapourSynth environment active at registration time
        is restored when the callback fires (potentially from a worker thread).

        :param fn: A callable that receives the completed future.
        """

        # The done_callback should inherit the environment of the current call.
        @keep_environment
        def wrapper(f: Future[T]) -> None:
            fn(self.from_future(f))

        super().add_done_callback(wrapper)

    def add_loop_callback(
        self,
        func: Callable[[Future[T]], Any],
        cancel_cb: Callable[[], None] | None = None,
    ) -> UnifiedFuture[T]:
        """
        Register a callback that is guaranteed to run on the event-loop thread.

        Unlike `add_done_callback`, which may fire from any thread,
        this method marshals `func` back to the main event loop via `EventLoop.from_thread`.

        :param func: A callable that receives the completed future.
        :param cancel_cb: Optional callback invoked when the returned future is cancelled.
            Defaults to cancelling the parent future.
        :return: A UnifiedFuture that resolves to the original future's result once the callback completes
            on the event loop.
        """
        result = type(self)()

        def forward_parent(_: Future[Any]) -> None:
            if self.cancelled():
                result.cancel()
            elif (orig_exc := self.exception()) is not None:
                result.try_set_exception(orig_exc)
            else:
                result.try_set_result(self.result())

        def wrapper(future: Future[T]) -> None:
            if result.cancelled():
                return
            self._forward_loop_task(get_loop().from_thread(func, future), result, forward_parent)

        self.add_done_callback(wrapper)
        self._link_cancellation(result, cancel_cb, on_loop=True)
        return result

    # Manipulating futures
    @overload
    def then[S](
        self,
        success_cb: Callable[[T], S],
        *,
        cancel_cb: Callable[[], None] | None = ...,
        on_loop: bool = ...,
    ) -> UnifiedFuture[S]: ...
    @overload
    def then[S](
        self,
        success_cb: Callable[[T], S],
        err_cb: None = ...,
        cancel_cb: Callable[[], None] | None = ...,
        *,
        on_loop: bool = ...,
    ) -> UnifiedFuture[S]: ...
    @overload
    def then[V](
        self,
        success_cb: None,
        err_cb: Callable[[BaseException], V],
        cancel_cb: Callable[[], None] | None = ...,
        *,
        on_loop: bool = ...,
    ) -> UnifiedFuture[T | V]: ...
    @overload
    def then[V](
        self,
        *,
        err_cb: Callable[[BaseException], V],
        cancel_cb: Callable[[], None] | None = ...,
        on_loop: bool = ...,
    ) -> UnifiedFuture[T | V]: ...
    @overload
    def then[S, V](
        self,
        success_cb: Callable[[T], S],
        err_cb: Callable[[BaseException], V],
        cancel_cb: Callable[[], None] | None = ...,
        *,
        on_loop: bool = ...,
    ) -> UnifiedFuture[S | V]: ...
    def then[S, V](
        self,
        success_cb: Callable[[T], S] | None = None,
        err_cb: Callable[[BaseException], V] | None = None,
        cancel_cb: Callable[[], None] | None = None,
        *,
        on_loop: bool = False,
    ) -> Any:
        """
        Attach fulfilment and/or rejection handlers, returning a new future.

        * If this future resolves successfully, `success_cb` is called with the result value.
          If `success_cb` is `None` the result is forwarded as-is.
        * If this future is rejected, `err_cb` is called with the exception.
          If `err_cb` is `None` the exception is forwarded as-is.

        Exceptions raised inside either callback are captured and stored as rejections on the returned future.

        :param success_cb: Called with the resolved value, or `None` to passthrough.
        :param err_cb: Called with the exception, or `None` to passthrough.
        :param cancel_cb: Optional callback invoked when the returned future is cancelled.
            Defaults to cancelling the parent future.
        :param on_loop: If True, execute the callback on the main loop.
        :return: A new `UnifiedFuture` carrying the callback's return value.
        """
        result = type(self)()

        def run_cb(cb: Callable[[Any], Any], v: T | BaseException) -> None:
            if result.cancelled():
                return
            if on_loop:
                self._forward_loop_task(get_loop().from_thread(cb, v), result)
            else:
                try:
                    r = cb(v)
                except Exception as e:
                    result.try_set_exception(e)
                else:
                    result.try_set_result(r)

        def done(_: Future[T]) -> None:
            if self.cancelled():
                result.cancel()
                return
            if (exc := self.exception()) is not None:
                if err_cb is not None:
                    run_cb(err_cb, exc)
                else:
                    result.try_set_exception(exc)
            elif not result.cancelled():
                if success_cb is not None:
                    run_cb(success_cb, self.result())
                else:
                    result.try_set_result(self.result())

        self.add_done_callback(done)
        self._link_cancellation(result, cancel_cb, on_loop=on_loop)
        return result

    def map[V](
        self,
        cb: Callable[[T], V],
        cancel_cb: Callable[[], None] | None = None,
        *,
        on_loop: bool = False,
    ) -> UnifiedFuture[V]:
        """
        Transform the resolved value with `cb`, returning a new future.

        :param cb: A callable that transforms the resolved value.
        :param cancel_cb: Optional callback invoked when the returned future is cancelled.
        :param on_loop: If True, execute the callback on the main loop.
        :return: A new `UnifiedFuture` carrying the transformed value.
        """
        return self.then(cb, None, cancel_cb, on_loop=on_loop)

    def catch[V](
        self,
        cb: Callable[[BaseException], V],
        cancel_cb: Callable[[], None] | None = None,
        *,
        on_loop: bool = False,
    ) -> UnifiedFuture[T | V]:
        """
        Recover from a rejection by handling the exception with `cb`.

        :param cb: A callable that handles the exception and returns a recovery value.
        :param cancel_cb: Optional callback invoked when the returned future is cancelled.
        :param on_loop: If True, execute the callback on the main loop.
        :return: A new `UnifiedFuture` carrying the recovery value on error, or the original resolved value on success.
        """
        return self.then(None, cb, cancel_cb, on_loop=on_loop)

    @classmethod
    def from_call[S, **P](
        cls: type[UnifiedFutureLike[S]],
        func: Callable[P, Future[S]] | Callable[P, S],
        *args: P.args,
        **kwargs: P.kwargs,
    ) -> UnifiedFutureLike[S]:
        """
        Call `func` and wrap the returned result as a `UnifiedFuture`.

        Any exception raised synchronously by `func` is captured and stored as a rejection
        on the returned result rather than propagating to the caller.

        :param func: A callable that returns a `Future` or a result.
        :param args: Positional arguments forwarded to `func`.
        :param kwargs: Keyword arguments forwarded to `func`.
        :return: A `UnifiedFuture` that mirrors the result of `func`.
        """
        try:
            res = func(*args, **kwargs)
        except Exception as e:
            return cls.reject(e)

        return cls.from_future(res) if isinstance(res, Future) else cls.resolve(res)

    @classmethod
    def from_future[S](cls: type[UnifiedFutureLike[S]], future: Future[S] | asyncio.Future[S]) -> UnifiedFutureLike[S]:
        """
        Wrap an existing `Future` as a `UnifiedFuture`.

        If `future` is already an instance of this class it is returned unchanged.

        :param future: The future to wrap.
        :return: A `UnifiedFuture` that mirrors `future`.
        """
        if isinstance(future, cls):
            return future

        result = cls()

        def receive(_: Future[S] | asyncio.Future[S]) -> None:
            if result.done():
                return
            if future.cancelled():
                result.cancel()
                return
            try:
                exc = future.exception()
            except (CancelledError, asyncio.CancelledError):
                # Catch any cancellation race condition
                result.cancel()
                return

            if exc is not None:
                result.try_set_exception(exc)
            else:
                result.try_set_result(future.result())

        # Safely propagate cancellation back to the original future
        def on_result_cancelled(f: Future[S]) -> None:
            if not f.cancelled() or future.done():
                return

            if isinstance(future, asyncio.Future):
                cls._dispatch_to_loop(future, future.cancel)
            else:
                future.cancel()

        result.add_done_callback(on_result_cancelled)

        # Safely register the completion callback
        if isinstance(future, asyncio.Future):
            cls._dispatch_to_loop(
                future,
                future.add_done_callback,
                receive,
                on_closed_done=receive,
                on_closed_incomplete=result.cancel,
            )
        else:
            future.add_done_callback(receive)
        return result

    @classmethod
    def from_trio_task[*Ts, S](
        cls: type[UnifiedFutureLike[S]],
        nursery: trio.Nursery,
        async_fn: Callable[[*Ts], Awaitable[S]],
        *args: *Ts,
        trio_token: trio.lowlevel.TrioToken | None = None,
    ) -> UnifiedFutureLike[S]:
        """
        Spawn `async_fn(*args)` in `nursery` and return a `UnifiedFuture` mirroring its outcome
        with bidirectional cancellation.

        :param nursery: Trio nursery to spawn the task in.
        :param async_fn: Async callable to execute.
        :param args: Positional arguments passed to `async_fn`.
        :param trio_token: Token for scheduling thread-safe cancellation.
            Defaults to the current Trio loop's token.
        :return: A `UnifiedFuture` tracking the task's result, exception, and cancellation state.
        """
        import trio

        result = cls()
        token = trio_token or trio.lowlevel.current_trio_token()
        cancel_scope = trio.CancelScope()

        async def runner() -> None:
            if result.cancelled():
                return

            try:
                with cancel_scope:
                    val = await async_fn(*args)
            except trio.Cancelled:
                result.cancel()
                raise  # Always re-raise trio.Cancelled so Trio's nursery stays healthy
            except BaseException as exc:
                result.try_set_exception(exc)
            else:
                if cancel_scope.cancelled_caught:
                    result.cancel()
                else:
                    result.try_set_result(val)

        # Propagate cancellation from UnifiedFuture -> Trio CancelScope
        def on_result_cancelled(f: Future[S]) -> None:
            if f.cancelled():
                with suppress(trio.RunFinishedError):
                    token.run_sync_soon(cancel_scope.cancel)

        result.add_done_callback(on_result_cancelled)
        nursery.start_soon(runner)
        return result

    @classmethod
    def resolve[S](cls: type[UnifiedFutureLike[S]], value: S) -> UnifiedFutureLike[S]:
        """
        Return an already-resolved `UnifiedFuture` carrying `value`.

        :param value: The value to resolve with.
        :return: A resolved `UnifiedFuture`.
        """
        future = cls()
        future.set_result(value)
        return future

    @classmethod
    def reject(cls: type[UnifiedFutureLike[Any]], error: BaseException) -> UnifiedFutureLike[Any]:
        """
        Return an already-rejected `UnifiedFuture` carrying `error`.

        :param error: The exception to reject with.
        :return: A rejected `UnifiedFuture`.
        """
        future = cls()
        future.set_exception(error)
        return future

    @staticmethod
    def _dispatch_to_loop[*Ts](
        fut: asyncio.Future[Any],
        action: Callable[[*Ts], Any],
        *args: *Ts,
        on_closed_done: Callable[[asyncio.Future[Any]], Any] | None = None,
        on_closed_incomplete: Callable[[], Any] | None = None,
    ) -> None:

        def when_loop_is_closed() -> None:
            if fut.done() and on_closed_done:
                on_closed_done(fut)
            elif on_closed_incomplete:
                on_closed_incomplete()
            else:
                with suppress(RuntimeError):
                    action(*args)

        f_loop = fut.get_loop()
        try:
            current_loop = asyncio.get_running_loop()
        except RuntimeError:
            current_loop = None

        if current_loop is f_loop:
            action(*args)
        elif f_loop.is_closed():
            when_loop_is_closed()
        else:
            try:
                f_loop.call_soon_threadsafe(action, *args, context=contextvars.copy_context())
            except RuntimeError:
                when_loop_is_closed()

    def _link_cancellation(
        self,
        target: Future[Any],
        cancel_cb: Callable[[], None] | None = None,
        *,
        on_loop: bool = False,
    ) -> None:
        if cancel_cb is not None:

            def on_cancel(f: Future[Any]) -> None:
                if f.cancelled():
                    if on_loop:
                        get_loop().from_thread(cancel_cb)
                    else:
                        cancel_cb()

            target.add_done_callback(on_cancel)
        else:
            target.add_done_callback(lambda f: self.cancel() if f.cancelled() else None)

    @staticmethod
    def _forward_loop_task(
        loop_fut: Future[Any],
        target: UnifiedFuture[Any],
        on_done: Callable[[Future[Any]], None] | None = None,
    ) -> None:
        def forward(lf: Future[Any]) -> None:
            if target.cancelled():
                return
            if lf.cancelled():
                target.cancel()
            elif (exc := lf.exception()) is not None:
                target.try_set_exception(exc)
            elif on_done is not None:
                on_done(lf)
            else:
                target.try_set_result(lf.result())

        loop_fut.add_done_callback(forward)


class UnifiedIterator[T](Iterator[T], AsyncIterator[T]):
    """
    A dual-mode iterator that wraps an `Iterator[Future[T]]`
    and exposes it as both a synchronous `Iterator` and an asynchronous `AsyncIterator`.

    In synchronous mode (`__next__`), each future is resolved by calling `Future.result()`.
    This blocks if the future is not yet done.

    In asynchronous mode (`__anext__`), each future is awaited via `EventLoop.await_future`,
    cooperating with the active event loop.
    """

    def __init__(self, future_iterable: Iterable[Future[T]]) -> None:
        self.futures = iter(future_iterable)

    def __iter__(self) -> Self:
        return self

    def __aiter__(self) -> Self:
        return self

    def __next__(self) -> T:
        return next(self.futures).result()

    def __anext__(self) -> Awaitable[T]:
        try:
            fut = next(self.futures)
        except StopIteration:
            raise StopAsyncIteration
        return get_loop().await_future(fut)

    @overload
    def consume(self, cb: Callable[[UnifiedFuture[T]], Any]) -> UnifiedFuture[None]: ...
    @overload
    def consume[U: UnifiedFuture[None]](
        self, cb: Callable[[UnifiedFuture[T]], Any], future_factory: Callable[[], U]
    ) -> U: ...
    def consume(
        self, cb: Callable[[UnifiedFuture[T]], Any], future_factory: Callable[[], UnifiedFuture[None]] | None = None
    ) -> UnifiedFuture[None]:
        """
        Consume the iterator sequentially, invoking `callback` for each future.

        The loop is event-loop-cooperative:
        after each callback it calls `EventLoop.next_cycle` to yield control back to the event loop.

        The returned `UnifiedFuture` resolves to `None` when all futures have been processed, or is rejected if:

        * the iterator raises,
        * `callback` raises,
        * `callback` returns a falsy non-`None` value (signals an early stop), or
        * a loop cycle future fails.

        Cancellation is detected by checking `Future.cancelled()` on the state future.

        :param cb: Called for each completed `Future`.
            Return `None` or a truthy value to continue; return a falsy value to stop iteration early.
        :param future_factory: Future class used to carry the state.
        :return: A `UnifiedFuture` that resolves when iteration is complete.
        """
        state = (future_factory or UnifiedFuture[None])()
        cb = keep_environment(cb)

        def pipeline() -> Generator[Future[Any]]:
            for fut in self.futures:
                if state.done():
                    return

                if not fut.done():
                    yield fut

                res = cb(UnifiedFuture.from_future(fut))
                if res is not None and not res:
                    return

                cycle = get_loop().next_cycle()

                if not cycle.done():
                    yield cycle

                cycle.result()

        gen = pipeline()

        def step() -> None:
            if state.done():
                return

            try:
                pending = next(gen)
            except StopIteration:
                state.try_set_result(None)
            except BaseException as e:
                state.try_set_exception(e)
            else:
                pending.add_done_callback(lambda _: get_loop().from_thread(step) if not state.done() else None)

        get_loop().from_thread(step)
        return state

    async def for_each(
        self,
        callback: Callable[[UnifiedFuture[T]], Awaitable[Any] | Any],
        *,
        yield_cycles: bool = True,
    ) -> None:
        """
        Consume the iterator sequentially in an async context, invoking `callback` for each future.

        :param callback: Called for each completed `Future`.
            Return `None` or a truthy value to continue; return a falsy value to stop iteration early.
        :param yield_cycles: Whether to yield to the event loop between iterations via `EventLoop.next_cycle`.
        """
        callback = keep_environment(callback)
        loop = get_loop()

        for fut in self.futures:
            if not fut.done():
                waiter = UnifiedFuture[None]()
                fut.add_done_callback(lambda _, w=waiter: w.try_set_result(None))
                await waiter

            res = callback(UnifiedFuture.from_future(fut))

            if inspect.isawaitable(res):
                res = await res

            if res is not None and not bool(res):
                break

            if yield_cycles:
                await loop.await_future(loop.next_cycle())

    def as_completed(self, *, concurrency: int | None = None) -> AsyncGenerator[UnifiedFuture[T]]:
        """
        Yield futures as they complete, maintaining at most `concurrency` in-flight requests.

        :param concurrency: Maximum number of in-flight futures.
            Defaults to `multiprocessing.cpu_count()` (minimum 1) if None.
        :return: An async generator yielding futures in order of completion.
        """
        return as_completed(self.futures, concurrency=concurrency)

    @classmethod
    def from_call[S, **P](
        cls: type[UnifiedIteratorLike[S]],
        func: Callable[P, Iterator[Future[S]]],
        *args: P.args,
        **kwargs: P.kwargs,
    ) -> UnifiedIteratorLike[S]:
        """
        Call `func` and wrap the returned iterator as a `UnifiedIterator`.

        :param func: A callable that returns an `Iterator[Future[S]]`.
        :param args: Positional arguments forwarded to `func`.
        :param kwargs: Keyword arguments forwarded to `func`.
        :return: A `UnifiedIterator` wrapping the returned iterator.
        """
        return cls(func(*args, **kwargs))


async def as_completed[S](
    iterable: Iterable[Future[S]],
    *,
    concurrency: int | None = None,
) -> AsyncGenerator[UnifiedFuture[S]]:
    """
    Yield futures as they complete, maintaining at most `concurrency` in-flight requests.

    This ensures bounded memory usage when processing long or infinite streams of futures
    (e.g., video frames), while still yielding completed futures as soon as they become ready.

    :param iterable: Iterable yielding futures to process.
    :param concurrency: Maximum number of in-flight futures. Defaults to
        `multiprocessing.cpu_count()` (minimum 1) if None.
    :return: An async generator yielding futures in order of completion.
    """
    if concurrency is None:
        try:
            concurrency = multiprocessing.cpu_count()
        except NotImplementedError:
            concurrency = 1

    if concurrency <= 0:
        raise ValueError("concurrency must be greater than 0")

    iterator = iter(iterable)
    done_queue = deque[Future[S]]()
    active_futures = set[Future[S]]()

    waiter: UnifiedFuture[None] | None = None
    in_flight = 0

    def on_done(fut: Future[S]) -> None:
        done_queue.append(fut)
        if (w := waiter) is not None:
            w.try_set_result(None)

    def refill() -> None:
        nonlocal in_flight

        while in_flight < concurrency:
            try:
                fut = next(iterator)
            except StopIteration:
                break

            in_flight += 1
            active_futures.add(fut)
            fut.add_done_callback(on_done)

    refill()

    try:
        while in_flight > 0:
            while not done_queue:
                waiter = UnifiedFuture[None]()
                if done_queue:
                    break
                await waiter
            waiter = None
            finished = done_queue.popleft()
            active_futures.remove(finished)
            in_flight -= 1
            refill()
            yield UnifiedFuture.from_future(finished)
    finally:
        for fut in active_futures:
            fut.cancel()


class _UnifiedIteratorDecorator[U: UnifiedIterator[Any]](Protocol):
    def __call__[**P](self, func: Callable[P, Iterator[Future[Any]]]) -> Callable[P, U]: ...


class _UnifiedFutureDecorator[U: UnifiedFuture[Any]](Protocol):
    def __call__[**P](self, func: Callable[P, Future[Any]]) -> Callable[P, U]: ...


@overload
def unified[T, **P](
    *,
    kind: Literal["generator"],
) -> Callable[
    [Callable[P, Iterator[Future[T]]]],
    Callable[P, UnifiedIterator[T]],
]: ...


@overload
def unified[T, **P](
    *,
    kind: Literal["future"],
) -> Callable[
    [Callable[P, Future[T]]],
    Callable[P, UnifiedFuture[T]],
]: ...


@overload
def unified[U: UnifiedIterator[Any]](
    *,
    kind: Literal["generator"],
    iterable_class: type[U],
) -> _UnifiedIteratorDecorator[U]: ...


@overload
def unified[U: UnifiedFuture[Any]](
    *,
    kind: Literal["future"],
    future_class: type[U],
) -> _UnifiedFutureDecorator[U]: ...


# Implementation
def unified[T, **P](
    *,
    kind: Literal["future", "generator"],
    iterable_class: type[UnifiedIterator[Any]] = UnifiedIterator[Any],
    future_class: type[UnifiedFuture[Any]] = UnifiedFuture[Any],
) -> Any:
    """
    Decorator factory to normalize functions that return raw futures or iterators of futures
    into functions that return `UnifiedFuture` or `UnifiedIterator`.

    :param kind: Controls which wrapper is applied.

        `"future"`
            Always wrap as `UnifiedFuture`.

        `"generator"`
            Always wrap as `UnifiedIterator`.

    :param future_class: The concrete `UnifiedFuture` subclass to use when wrapping single-value futures.
    :param iterable_class: The concrete `UnifiedIterator` subclass to use when wrapping generators.
    :return: A decorator that wraps the target function.

    Example usage:
    ```python
    @unified(kind="future")
    def request_frame(index: int) -> Future[vs.VideoFrame]:
        return node.get_frame_async(index)


    @unified(kind="generator")
    def request_all_frames(node: vs.VideoNode) -> Iterator[Future[vs.VideoFrame]]:
        for i in range(node.num_frames):
            yield node.get_frame_async(i)
    ```
    """

    def _decorator_generator(func: Callable[P, Iterator[Future[T]]]) -> Callable[P, UnifiedIterator[T]]:
        @wraps(func)
        def _wrapped(*args: P.args, **kwargs: P.kwargs) -> UnifiedIterator[T]:
            return iterable_class.from_call(func, *args, **kwargs)

        return _wrapped

    def _decorator_future(func: Callable[P, Future[T]]) -> Callable[P, UnifiedFuture[T]]:
        @wraps(func)
        def _wrapped(*args: P.args, **kwargs: P.kwargs) -> UnifiedFuture[T]:
            return future_class.from_call(func, *args, **kwargs)

        return _wrapped

    def decorator(
        func: Callable[P, Iterator[Future[T]]] | Callable[P, Future[T]],
    ) -> Callable[P, UnifiedIterator[T]] | Callable[P, UnifiedFuture[T]]:
        if kind == "generator":
            # pyrefly: ignore [bad-argument-type]
            return _decorator_generator(func)

        if kind == "future":
            # pyrefly: ignore [bad-argument-type]
            return _decorator_future(func)

        raise NotImplementedError

    return decorator
