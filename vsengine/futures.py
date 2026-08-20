# vs-engine
# Copyright (C) 2022  cid-chan
# Copyright (C) 2025  Jaded-Encoding-Thaumaturgy
# This project is licensed under the EUPL-1.2
# SPDX-License-Identifier: EUPL-1.2
from __future__ import annotations

import traceback
from collections.abc import AsyncIterator, Awaitable, Callable, Generator, Iterator
from concurrent.futures import Future
from contextlib import AbstractAsyncContextManager, AbstractContextManager
from functools import wraps
from inspect import isgeneratorfunction
from types import TracebackType
from typing import Any, Literal, Protocol, Self, overload

from vsengine.loops import get_loop, keep_environment

__all__ = ["UnifiedFuture", "UnifiedIterator", "unified"]


class FutureLike[V](Protocol):
    def result(self) -> V: ...


class AsyncFutureLike[V](Protocol):
    async def awaitable(self) -> V: ...


type UnifiedFutureLike[T] = UnifiedFuture[T]
type UnifiedIteratorLike[T] = UnifiedIterator[T]


class UnifiedFuture[T](Future[T], AbstractContextManager[Any], AbstractAsyncContextManager[Any], Awaitable[T]):
    """
    A Promise-inspired Future that unifies concurrent.futures.Future
    with Python's synchronous and asynchronous context manager and awaitable protocols.
    """

    @classmethod
    def from_call[S, **P](
        cls: type[UnifiedFutureLike[S]],
        func: Callable[P, Future[S]],
        *args: P.args,
        **kwargs: P.kwargs,
    ) -> UnifiedFutureLike[S]:
        """
        Call `func` and wrap the returned `Future` as a `UnifiedFuture`.

        Any exception raised synchronously by `func` is captured and stored as a rejection
        on the returned future rather than propagating to the caller.

        :param func: A callable that returns a `Future`.
        :param args: Positional arguments forwarded to `func`.
        :param kwargs: Keyword arguments forwarded to `func`.
        :return: A `UnifiedFuture` that mirrors the result of `func`.
        """
        try:
            future = func(*args, **kwargs)
        except Exception as e:
            return cls.reject(e)

        return cls.from_future(future)

    @classmethod
    def from_future[S](cls: type[UnifiedFutureLike[S]], future: Future[S]) -> UnifiedFutureLike[S]:
        """
        Wrap an existing `Future` as a `UnifiedFuture`.

        If `future` is already an instance of this class it is returned unchanged.

        :param future: The future to wrap.
        :return: A `UnifiedFuture` that mirrors `future`.
        """
        if isinstance(future, cls):
            return future

        result = cls()

        def _receive(fn: Future[S]) -> None:
            if future.cancelled():
                result.cancel()
                return
            if (exc := future.exception()) is not None:
                result.set_exception(exc)
            elif not result.cancelled():
                result.set_result(future.result())

        future.add_done_callback(_receive)
        result.add_done_callback(lambda f: future.cancel() if f.cancelled() else None)
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

    # Adding callbacks
    def add_done_callback(self, fn: Callable[[Future[T]], Any]) -> None:
        """
        Register a callback to be called when this future completes.

        Wraps the callback in `keep_environment` so that the VapourSynth environment active at registration time
        is restored when the callback fires (potentially from a worker thread).

        :param fn: A callable that receives the completed future.
        """
        # The done_callback should inherit the environment of the current call.
        super().add_done_callback(keep_environment(fn))

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
        result = UnifiedFuture[T]()

        def forward_parent(_: Future[Any]) -> None:
            if self.cancelled():
                result.cancel()
            elif (orig_exc := self.exception()) is not None:
                result.set_exception(orig_exc)
            else:
                result.set_result(self.result())

        def wrapper(future: Future[T]) -> None:
            if result.cancelled():
                return
            self._forward_loop_task(get_loop().from_thread(func, future), result, forward_parent)

        self.add_done_callback(wrapper)
        self._link_cancellation(result, cancel_cb)
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
        result = UnifiedFuture[Any]()

        def run_cb(cb: Callable[[Any], Any], v: T | BaseException) -> None:
            if result.cancelled():
                return
            if on_loop:
                self._forward_loop_task(get_loop().from_thread(cb, v), result)
            else:
                try:
                    r = cb(v)
                except Exception as e:
                    result.set_exception(e)
                else:
                    result.set_result(r)

        def done(_: Future[T]) -> None:
            if self.cancelled():
                result.cancel()
                return
            if (exc := self.exception()) is not None:
                if err_cb is not None:
                    run_cb(err_cb, exc)
                else:
                    result.set_exception(exc)
            elif not result.cancelled():
                if success_cb is not None:
                    run_cb(success_cb, self.result())
                else:
                    result.set_result(self.result())

        self.add_done_callback(done)
        self._link_cancellation(result, cancel_cb)
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

    # Nicer Syntax
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

    async def awaitable(self) -> T:
        """
        Await this future using the currently active event loop.

        :return: The resolved value of this future.
        :raises: Whatever exception this future was rejected with.
        """
        return await get_loop().await_future(self)

    def __await__(self) -> Generator[Any, None, T]:
        return self.awaitable().__await__()

    async def __aenter__[EnterT](
        self: AsyncFutureLike[AbstractAsyncContextManager[EnterT, Any] | AbstractContextManager[EnterT, Any]],
    ) -> EnterT:
        result = await self.awaitable()

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
        result = await self.awaitable()

        if isinstance(result, AbstractAsyncContextManager):
            return await result.__aexit__(exc, val, tb)
        if isinstance(result, AbstractContextManager):
            return result.__exit__(exc, val, tb)

        raise NotImplementedError("(async) with is not implemented for this object")

    def _link_cancellation(self, target: Future[Any], cancel_cb: Callable[[], None] | None = None) -> None:
        if cancel_cb is not None:
            target.add_done_callback(lambda f: cancel_cb() if f.cancelled() else None)
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
                target.set_exception(exc)
            elif on_done is not None:
                on_done(lf)
            else:
                target.set_result(lf.result())

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

    def __init__(self, future_iterable: Iterator[Future[T]]) -> None:
        self.future_iterable = future_iterable

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

    @property
    def futures(self) -> Iterator[Future[T]]:
        """The raw underlying `Iterator[Future[T]]`."""
        return self.future_iterable

    def run_as_completed(self, callback: Callable[[Future[T]], Any]) -> UnifiedFuture[None]:
        """
        Consume the iterator and invoke `callback` for each future as it completes.

        The loop is event-loop-cooperative:
        after each callback it calls `EventLoop.next_cycle` to yield control back to the event loop
        so that other work can proceed.

        The returned `UnifiedFuture` resolves to `None` when all futures have been processed, or is rejected if:

        * the iterator raises,
        * `callback` raises, or
        * `callback` returns a falsy non-`None` value (signals an early stop).

        Cancellation is detected by checking `Future.cancelled()` on the state future; raise `Cancelled` to abort.

        :param callback: Called for each completed `Future`.
            Return `None` or a truthy value to continue; return a falsy value to stop iteration early.
        :return: A `UnifiedFuture` that resolves when iteration is complete.
        """
        state = UnifiedFuture[None]()

        def _get_next_future() -> Future[T] | None:
            if state.done():
                return None

            try:
                next_future = self.future_iterable.__next__()
            except StopIteration:
                state.set_result(None)
                return None
            except BaseException as e:
                state.set_exception(e)
                return None
            return next_future

        def _run_callbacks() -> None:
            try:
                while (future := _get_next_future()) is not None:
                    # Wait for the future to finish.
                    if not future.done():
                        future.add_done_callback(_continuation_in_foreign_thread)
                        return

                    # Run the callback.
                    if not _run_single_callback(future):
                        return

                    # Try to give control back to the event loop.
                    next_cycle = get_loop().next_cycle()
                    if not next_cycle.done():
                        next_cycle.add_done_callback(_continuation_from_next_cycle)
                        return

                    # We do not have a real event loop here.
                    # If the next_cycle causes an error to bubble, forward it to the state future.
                    if next_cycle.exception() is not None:
                        state.set_exception(next_cycle.exception())
                        return
            except Exception as e:
                traceback.print_exception(e)
                state.set_exception(e)

        def _continuation_from_next_cycle(fut: Future[None]) -> None:
            if fut.exception() is not None:
                state.set_exception(fut.exception())
            else:
                _run_callbacks()

        def _continuation_in_foreign_thread(fut: Future[T]) -> None:
            # Optimization, see below.
            get_loop().from_thread(_continuation, fut)

        def _continuation(fut: Future[T]) -> None:
            if _run_single_callback(fut):
                _run_callbacks()

        @keep_environment
        def _run_single_callback(fut: Future[T]) -> bool:
            # True   => Schedule next future.
            # False  => Cancel the loop.
            if state.done():
                return False

            try:
                result = callback(fut)
            except Exception as e:
                state.set_exception(e)
                return False
            else:
                if result is None or bool(result):
                    return True
                else:
                    state.set_result(None)
                    return False

        # Optimization:
        # We do not need to inherit any kind of environment as
        # _run_single_callback will automatically set the environment for us.
        get_loop().from_thread(_run_callbacks)
        return state

    def __iter__(self) -> Self:
        return self

    def __next__(self) -> T:
        fut = self.future_iterable.__next__()
        return fut.result()

    def __aiter__(self) -> Self:
        return self

    async def __anext__(self) -> T:
        try:
            fut = self.future_iterable.__next__()
        except StopIteration:
            raise StopAsyncIteration
        return await get_loop().await_future(fut)


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
    kind: str = "auto",
    iterable_class: type[UnifiedIterator[Any]] = UnifiedIterator[Any],
    future_class: type[UnifiedFuture[Any]] = UnifiedFuture[Any],
) -> Any:
    """
    Decorator factory to normalize functions that return raw futures or iterators of futures
    into functions that return `UnifiedFuture` or `UnifiedIterator`.

    :param kind: Controls which wrapper is applied.

        `"auto"` (default)
            Automatically detects generator functions (via `isgeneratorfunction`)
            and wraps them as `UnifiedIterator`; all other callables are wrapped as
            `UnifiedFuture`.

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
        if kind == "auto":
            return _decorator_generator(func) if isgeneratorfunction(func) else _decorator_future(func)  # type:ignore[arg-type]

        if kind == "generator":
            return _decorator_generator(func)  # type:ignore[arg-type]

        if kind == "future":
            return _decorator_future(func)  # type:ignore[arg-type]

        raise NotImplementedError

    return decorator
