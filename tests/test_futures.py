# vs-engine
# Copyright (C) 2022  cid-chan
# Copyright (C) 2025  Jaded-Encoding-Thaumaturgy
# This project is licensed under the EUPL-1.2
# SPDX-License-Identifier: EUPL-1.2
"""Tests for the unified future system."""

import asyncio
import contextlib
import threading
from collections.abc import AsyncGenerator, Generator, Iterator
from concurrent.futures import Future
from typing import TYPE_CHECKING, Any, Literal

import pytest

from vsengine.adapters.asyncio import AsyncIOLoop
from vsengine.futures import UnifiedFuture, UnifiedIterator, unified
from vsengine.loops import NO_LOOP, _NoEventLoop, set_loop


def resolve[T](value: T) -> Future[T]:
    fut = Future[T]()
    fut.set_result(value)
    return fut


def reject(err: BaseException) -> Future[None]:
    fut = Future[None]()
    fut.set_exception(err)
    return fut


def contextmanager_helper() -> Future[contextlib.AbstractContextManager[Literal[1]]]:
    @contextlib.contextmanager
    def noop() -> Generator[Literal[1]]:
        yield 1

    return resolve(noop())


def asynccontextmanager_helper() -> Future[contextlib.AbstractAsyncContextManager[Literal[2]]]:
    @contextlib.asynccontextmanager
    async def noop() -> AsyncGenerator[Literal[2]]:
        yield 2

    return resolve(noop())


def succeeds() -> Future[int]:
    return resolve(1)


def fails() -> Future[None]:
    return reject(RuntimeError())


def fails_early() -> Future[None]:
    raise RuntimeError()


def future_iterator() -> Iterator[Future[int]]:
    n = 0
    while True:
        yield resolve(n)
        n += 1


class WrappedUnifiedFuture[T](UnifiedFuture[T]): ...


class WrappedUnifiedIterable[T](UnifiedIterator[T]): ...


class FakeContextManager:
    if TYPE_CHECKING:

        def __enter__(self) -> None: ...
        def __exit__(self, *_: object) -> None: ...
        async def __aenter__(self) -> None: ...
        async def __aexit__(self, *_: object) -> None: ...


# UnifiedFuture tests


@pytest.mark.asyncio
async def test_unified_future_is_await() -> None:
    set_loop(AsyncIOLoop())
    await UnifiedFuture.from_call(succeeds)


@pytest.mark.asyncio
async def test_unified_future_awaitable() -> None:
    set_loop(AsyncIOLoop())
    await UnifiedFuture.from_call(succeeds).awaitable()


@pytest.mark.asyncio
async def test_unified_future_async_context_manager_async() -> None:
    set_loop(AsyncIOLoop())
    async with UnifiedFuture.from_call(asynccontextmanager_helper) as v:
        assert v == 2


@pytest.mark.asyncio
async def test_unified_future_context_manager_async() -> None:
    set_loop(AsyncIOLoop())
    async with UnifiedFuture.from_call(contextmanager_helper) as v:
        assert v == 1


def test_unified_future_context_manager() -> None:
    with UnifiedFuture.from_call(contextmanager_helper) as v:
        assert v == 1


def test_unified_future_map() -> None:
    def _crash(v: Any) -> str:
        raise RuntimeError(str(v))

    future0 = UnifiedFuture.from_call(succeeds)
    new_future0 = future0.map(lambda v: str(v))
    assert new_future0.result() == "1"

    new_future0 = future0.map(_crash)
    assert isinstance(new_future0.exception(), RuntimeError)

    future1 = UnifiedFuture.from_call(fails)
    new_future1 = future1.map(lambda v: str(v))
    assert isinstance(new_future1.exception(), RuntimeError)


def test_unified_future_catch() -> None:
    def _crash(_: BaseException) -> str:
        raise RuntimeError("test")

    future0 = UnifiedFuture.from_call(fails)
    new_future0 = future0.catch(lambda e: e.__class__.__name__)
    assert new_future0.result() == "RuntimeError"

    new_future0 = future0.catch(_crash)
    assert isinstance(new_future0.exception(), RuntimeError)

    future1 = UnifiedFuture.from_call(succeeds)
    new_future1 = future1.catch(lambda v: str(v))
    # Result is 1 because the future succeeded (no exception to catch)
    result = new_future1.result()
    assert result == 1


@pytest.mark.asyncio
async def test_unified_future_add_loop_callback() -> None:
    set_loop(AsyncIOLoop())

    def _init_thread(fut: Future[threading.Thread]) -> None:
        fut.set_result(threading.current_thread())

    fut: Future[threading.Thread] = Future()
    thr = threading.Thread(target=lambda: _init_thread(fut))

    def _wrapper() -> Future[threading.Thread]:
        return fut

    unified_fut = UnifiedFuture.from_call(_wrapper)

    loop_thread: threading.Thread | None = None

    def _record_loop_thr(_: Any) -> None:
        nonlocal loop_thread
        loop_thread = threading.current_thread()

    unified_fut.add_loop_callback(_record_loop_thr)
    thr.start()
    cb_thread = await unified_fut

    assert cb_thread != loop_thread


@pytest.mark.asyncio
async def test_unified_future_add_loop_callback_chaining() -> None:
    set_loop(AsyncIOLoop())

    fut = Future[int]()
    unified_fut = UnifiedFuture.from_future(fut)

    recorded_value: int | None = None

    def _side_effect(f: Future[int]) -> None:
        nonlocal recorded_value
        recorded_value = f.result() * 2

    chained_fut = unified_fut.add_loop_callback(_side_effect)

    results = list[int]()
    chained_fut.then(lambda val: results.append(val))

    fut.set_result(21)

    res = await chained_fut
    assert res == 21
    assert results == [21]
    assert recorded_value == 42


@pytest.mark.asyncio
async def test_unified_future_then_on_loop() -> None:
    set_loop(AsyncIOLoop())

    fut = Future[int]()
    unified_fut = UnifiedFuture.from_future(fut)

    loop_thread: threading.Thread | None = None
    callback_thread: threading.Thread | None = None

    def _init_thread() -> None:
        fut.set_result(42)

    def _on_success(v: int) -> int:
        nonlocal callback_thread
        callback_thread = threading.current_thread()
        return v * 2

    chained = unified_fut.then(_on_success, on_loop=True)

    thr = threading.Thread(target=_init_thread)
    thr.start()

    res = await chained
    loop_thread = threading.current_thread()

    assert callback_thread == loop_thread
    assert res == 84


@pytest.mark.asyncio
async def test_unified_future_map_on_loop() -> None:
    set_loop(AsyncIOLoop())

    fut = Future[int]()
    unified_fut = UnifiedFuture.from_future(fut)

    loop_thread: threading.Thread | None = None
    callback_thread: threading.Thread | None = None

    def _init_thread() -> None:
        fut.set_result(10)

    def _map_fn(v: int) -> str:
        nonlocal callback_thread
        callback_thread = threading.current_thread()
        return f"value: {v}"

    chained = unified_fut.map(_map_fn, on_loop=True)

    thr = threading.Thread(target=_init_thread)
    thr.start()

    res = await chained
    loop_thread = threading.current_thread()

    assert callback_thread == loop_thread
    assert res == "value: 10"


@pytest.mark.asyncio
async def test_unified_future_catch_on_loop() -> None:
    set_loop(AsyncIOLoop())

    fut = Future[int]()
    unified_fut = UnifiedFuture.from_future(fut)

    loop_thread: threading.Thread | None = None
    callback_thread: threading.Thread | None = None

    def _init_thread() -> None:
        fut.set_exception(ValueError("error!"))

    def _catch_fn(e: BaseException) -> str:
        nonlocal callback_thread
        callback_thread = threading.current_thread()
        return str(e)

    chained = unified_fut.catch(_catch_fn, on_loop=True)

    thr = threading.Thread(target=_init_thread)
    thr.start()

    res = await chained
    loop_thread = threading.current_thread()

    assert callback_thread == loop_thread
    assert res == "error!"


# UnifiedIterator tests


def test_unified_iterator_run_as_completed_succeeds() -> None:
    set_loop(NO_LOOP)
    my_futures: list[Future[int]] = [Future(), Future()]
    results: list[int] = []

    def _add_to_result(f: Future[int]) -> None:
        results.append(f.result())

    state = UnifiedIterator(iter(my_futures)).run_as_completed(_add_to_result)
    assert not state.done()
    my_futures[1].set_result(2)
    assert not state.done()
    my_futures[0].set_result(1)
    assert state.done()
    assert state.result() is None
    assert results == [1, 2]


def test_unified_iterator_run_as_completed_forwards_errors() -> None:
    set_loop(NO_LOOP)
    my_futures: list[Future[int]] = [Future(), Future()]
    results: list[int] = []
    errors: list[BaseException] = []

    def _add_to_result(f: Future[int]) -> None:
        if exc := f.exception():
            errors.append(exc)
        else:
            results.append(f.result())

    iterator = iter(my_futures)
    state = UnifiedIterator(iterator).run_as_completed(_add_to_result)
    assert not state.done()
    my_futures[0].set_exception(RuntimeError())
    assert not state.done()
    my_futures[1].set_result(2)
    assert state.done()
    assert state.result() is None

    assert results == [2]
    assert len(errors) == 1


def test_unified_iterator_run_as_completed_cancels() -> None:
    set_loop(NO_LOOP)
    my_futures: list[Future[int]] = [Future(), Future()]
    results: list[int] = []

    def _add_to_result(f: Future[int]) -> bool:
        results.append(f.result())
        return False

    iterator = iter(my_futures)
    state = UnifiedIterator(iterator).run_as_completed(_add_to_result)
    assert not state.done()
    my_futures[0].set_result(1)
    assert state.done()
    assert state.result() is None
    assert results == [1]


def test_unified_iterator_run_as_completed_cancels_on_crash() -> None:
    set_loop(NO_LOOP)
    my_futures: list[Future[int]] = [Future(), Future()]
    err = RuntimeError("test")

    def _crash(_: Future[int]) -> None:
        raise err

    iterator = iter(my_futures)
    state = UnifiedIterator(iterator).run_as_completed(_crash)
    assert not state.done()
    my_futures[0].set_result(1)
    assert state.done()
    assert state.exception() is err
    assert next(iterator) is not None


def test_unified_iterator_run_as_completed_requests_as_needed() -> None:
    my_futures: list[Future[int]] = [Future(), Future()]
    requested: list[Future[int]] = []
    continued: list[Future[int]] = []

    def _add_to_result(f: Future[int]) -> None: ...

    def _it() -> Iterator[Future[int]]:
        for fut in my_futures:
            requested.append(fut)
            yield fut
            continued.append(fut)

    state = UnifiedIterator(_it()).run_as_completed(_add_to_result)
    assert not state.done()
    assert requested == [my_futures[0]]
    assert continued == []

    my_futures[0].set_result(1)
    assert not state.done()
    assert requested == [my_futures[0], my_futures[1]]
    assert continued == [my_futures[0]]

    my_futures[1].set_result(1)
    assert state.done()
    assert requested == [my_futures[0], my_futures[1]]
    assert continued == [my_futures[0], my_futures[1]]


def test_unified_iterator_run_as_completed_cancels_on_iterator_crash() -> None:
    err = RuntimeError("test")

    def _it() -> Iterator[Future[int]]:
        if False:
            yield Future[int]()  # type:ignore[unreachable]
        raise err

    def noop(_: Future[int]) -> None: ...

    state = UnifiedIterator(_it()).run_as_completed(noop)
    assert state.done()
    assert state.exception() is err


def test_unified_iterator_can_iter_futures() -> None:
    for n, fut in enumerate(UnifiedIterator.from_call(future_iterator).futures):
        assert n == fut.result()
        if n > 100:
            break


def test_unified_iterator_can_iter() -> None:
    for n, n2 in enumerate(UnifiedIterator.from_call(future_iterator)):
        assert n == n2
        if n > 100:
            break


@pytest.mark.asyncio
async def test_unified_iterator_can_aiter() -> None:
    set_loop(AsyncIOLoop())
    n = 0
    async for n2 in UnifiedIterator.from_call(future_iterator):
        assert n == n2
        n += 1
        if n > 100:
            break


# unified decorator tests


def test_unified_auto_future_return_a_unified_future() -> None:
    @unified(kind="future")
    def test_func() -> Future[int]:
        return resolve(9999)

    f = test_func()
    assert isinstance(f, UnifiedFuture)
    assert f.result() == 9999


def test_unified_auto_generator_return_a_unified_iterable() -> None:
    @unified(kind="generator")
    def test_func() -> Iterator[Future[int]]:
        yield resolve(1)
        yield resolve(2)

    f = test_func()
    assert isinstance(f, UnifiedIterator)
    assert next(f) == 1
    assert next(f) == 2


def test_unified_generator_accepts_other_iterables() -> None:
    @unified(kind="generator")
    def test_func() -> Iterator[Future[int]]:
        return iter((resolve(1), resolve(2)))

    f = test_func()
    assert isinstance(f, UnifiedIterator)
    assert next(f) == 1
    assert next(f) == 2


def test_unified_custom_future() -> None:
    @unified(kind="future", future_class=WrappedUnifiedFuture[int])
    def test_func() -> Future[int]:
        return resolve(9999)

    f = test_func()
    assert isinstance(f, WrappedUnifiedFuture)


def test_unified_custom_generator() -> None:
    @unified(kind="generator", iterable_class=WrappedUnifiedIterable[int])
    def test_func() -> Iterator[Future[int]]:
        yield resolve(9999)

    f = test_func()
    assert isinstance(f, WrappedUnifiedIterable)


def test_from_call_sync_exception() -> None:
    def sync_fail() -> Future[None]:
        raise ValueError("fails_early")

    fut = UnifiedFuture.from_call(sync_fail)
    assert isinstance(fut.exception(), ValueError)


def test_reject_directly() -> None:
    fut = UnifiedFuture.reject(ValueError("rejected"))
    assert isinstance(fut.exception(), ValueError)


@pytest.mark.asyncio
async def test_add_loop_callback_failed_future() -> None:
    set_loop(AsyncIOLoop())
    fut = Future[None]()
    unified_fut = UnifiedFuture.from_future(fut)

    def noop(_: Future[None]) -> None: ...

    chained = unified_fut.add_loop_callback(noop)
    fut.set_exception(ValueError("original error"))

    with pytest.raises(ValueError, match="original error"):
        await chained


@pytest.mark.asyncio
async def test_add_loop_callback_callback_exception() -> None:
    set_loop(AsyncIOLoop())
    fut = Future[int]()
    unified_fut = UnifiedFuture.from_future(fut)

    def _callback_fail(_: Future[int]) -> None:
        raise ValueError("callback error")

    chained = unified_fut.add_loop_callback(_callback_fail)
    fut.set_result(42)

    with pytest.raises(ValueError, match="callback error"):
        await chained


@pytest.mark.asyncio
async def test_then_on_loop_callback_exception() -> None:
    set_loop(AsyncIOLoop())
    fut = Future[int]()
    unified_fut = UnifiedFuture.from_future(fut)

    def _callback_fail(_: int) -> int:
        raise ValueError("then callback error")

    chained = unified_fut.then(_callback_fail, on_loop=True)
    fut.set_result(42)

    with pytest.raises(ValueError, match="then callback error"):
        await chained


def test_context_manager_not_implemented() -> None:
    fut = UnifiedFuture.resolve(FakeContextManager())
    with pytest.raises(NotImplementedError), fut:
        ...

    with pytest.raises(NotImplementedError):
        fut.__exit__(None, None, None)


@pytest.mark.asyncio
async def test_async_context_manager_not_implemented() -> None:
    set_loop(AsyncIOLoop())
    fut = UnifiedFuture.resolve(FakeContextManager())
    with pytest.raises(NotImplementedError):
        async with fut:
            ...

    with pytest.raises(NotImplementedError):
        await fut.__aexit__(None, None, None)


def test_run_as_completed_external_cancel() -> None:
    set_loop(NO_LOOP)
    my_futures = [Future[int](), Future[int]()]

    def noop(_: Future[int]) -> None: ...

    state = UnifiedIterator(iter(my_futures)).run_as_completed(noop)
    assert not state.done()

    # Cancel the state future externally
    state.cancel()
    assert state.done()
    assert state.cancelled()

    # Resolving futures now should not raise error but also not run callback since state is done
    my_futures[0].set_result(1)


def test_run_as_completed_sync_cancel() -> None:
    set_loop(NO_LOOP)
    f1 = resolve(1)
    f2 = resolve(2)
    results = list[int]()

    def cb(f: Future[int]) -> bool:
        results.append(f.result())
        return False  # signal early stop

    state = UnifiedIterator(iter([f1, f2])).run_as_completed(cb)
    assert state.done()
    assert results == [1]


@pytest.mark.asyncio
async def test_run_as_completed_async_loop() -> None:
    set_loop(AsyncIOLoop())
    my_futures = [Future[int](), Future[int]()]
    results = list[int]()

    def cb(f: Future[int]) -> None:
        results.append(f.result())

    state = UnifiedIterator(iter(my_futures)).run_as_completed(cb)
    my_futures[0].set_result(10)
    my_futures[1].set_result(20)

    try:
        await state
        assert results == [10, 20]
    finally:
        set_loop(NO_LOOP)


def test_run_as_completed_next_cycle_fails() -> None:
    set_loop(NO_LOOP)

    class CustomFailedLoop(_NoEventLoop):
        def next_cycle(self) -> Future[None]:
            fut = Future[None]()
            fut.set_exception(ValueError("next_cycle failed"))
            return fut

    set_loop(CustomFailedLoop())

    try:
        f1 = resolve(1)
        f2 = resolve(2)

        def noop(_: Future[int]) -> None: ...

        state = UnifiedIterator(iter([f1, f2])).run_as_completed(noop)
        assert state.done()
        assert isinstance(state.exception(), ValueError)
    finally:
        set_loop(NO_LOOP)


def test_run_as_completed_next_cycle_fails_async() -> None:
    set_loop(NO_LOOP)

    class CustomPendingFailedLoop(_NoEventLoop):
        def __init__(self) -> None:
            self.next_cycle_futs = list[Future[None]]()

        def next_cycle(self) -> Future[None]:
            fut = Future[None]()
            self.next_cycle_futs.append(fut)
            return fut

    custom_loop = CustomPendingFailedLoop()
    set_loop(custom_loop)

    try:
        f1 = resolve(1)
        f2 = resolve(2)

        def noop(_: Future[int]) -> None: ...

        state = UnifiedIterator(iter([f1, f2])).run_as_completed(noop)
        assert not state.done()

        # Now fail the next_cycle future
        custom_loop.next_cycle_futs[0].set_exception(ValueError("next_cycle async failed"))
        assert state.done()
        assert isinstance(state.exception(), ValueError)
    finally:
        set_loop(NO_LOOP)


def test_unified_future_cancellation_propagation() -> None:
    raw = UnifiedFuture[int]()
    mapped = raw.map(lambda x: x * 2)
    chained = mapped.map(lambda x: x + 1)

    assert chained.cancel() is True
    assert chained.cancelled() is True
    assert mapped.cancelled() is True
    assert raw.cancelled() is True


def test_unified_future_child_cancelled_parent_resolves() -> None:
    raw = UnifiedFuture[int]()
    mapped = raw.map(lambda x: x * 2, cancel_cb=lambda: None)
    chained = mapped.map(lambda x: x + 1)

    assert chained.cancel() is True
    assert chained.cancelled() is True
    assert mapped.cancelled() is True
    assert raw.cancelled() is False

    # Resolving raw future whose downstream child was cancelled should not raise InvalidStateError in callbacks
    raw.set_result(10)
    assert raw.result() == 10


def test_unified_future_custom_on_cancel() -> None:
    cancelled_calls = list[str]()
    raw = UnifiedFuture[int]()
    mapped = raw.map(lambda x: x * 2, cancel_cb=lambda: cancelled_calls.append("custom_cancel"))

    assert mapped.cancel() is True
    assert mapped.cancelled() is True
    assert cancelled_calls == ["custom_cancel"]
    # Parent raw future was not cancelled because custom on_cancel intercepted it
    assert raw.cancelled() is False


def test_unified_future_parent_cancelled() -> None:
    raw = UnifiedFuture[int]()
    mapped = raw.map(lambda x: x * 2)
    caught = raw.catch(lambda _: 0)

    raw.cancel()
    assert mapped.cancelled() is True
    assert caught.cancelled() is True


def test_unified_future_from_future_cancellation() -> None:
    raw = Future[int]()
    uni = UnifiedFuture.from_future(raw)

    assert uni.cancel() is True
    assert uni.cancelled() is True
    assert raw.cancelled() is True


def test_unified_future_from_future_identity() -> None:
    uni = UnifiedFuture.resolve(42)
    assert UnifiedFuture.from_future(uni) is uni


def test_unified_future_from_already_cancelled_future() -> None:
    raw = Future[int]()
    raw.cancel()
    uni = UnifiedFuture.from_future(raw)
    assert uni.cancelled() is True

    loop = asyncio.new_event_loop()
    raw_async = loop.create_future()
    raw_async.cancel()
    loop.close()
    uni_async = UnifiedFuture.from_future(raw_async)
    assert uni_async.cancelled() is True


def test_unified_future_from_closed_asyncio_future() -> None:
    loop = asyncio.new_event_loop()
    raw = loop.create_future()
    raw.set_result(42)
    loop.close()

    uni = UnifiedFuture.from_future(raw)

    assert uni.result() == 42


def test_unified_future_from_closed_asyncio_future_incomplete() -> None:
    loop = asyncio.new_event_loop()
    raw = loop.create_future()
    loop.close()

    uni = UnifiedFuture.from_future(raw)
    assert uni.cancelled() is True


def test_unified_future_from_closed_asyncio_future_exception() -> None:
    loop = asyncio.new_event_loop()
    raw = loop.create_future()
    raw.set_exception(ValueError("closed error"))
    loop.close()

    uni = UnifiedFuture.from_future(raw)
    assert isinstance(uni.exception(), ValueError)


def test_unified_future_cancellation_propagates_to_closed_asyncio_future() -> None:
    loop = asyncio.new_event_loop()
    raw = loop.create_future()
    uni = UnifiedFuture.from_future(raw)
    loop.close()

    assert uni.cancel() is True
    assert raw.cancelled() is True


@pytest.mark.asyncio
async def test_unified_future_cancellation_prevents_asyncio_task_from_running() -> None:
    ran = False

    async def source() -> int:
        nonlocal ran
        ran = True
        return 42

    raw = asyncio.create_task(source())
    uni = UnifiedFuture.from_future(raw)

    assert uni.cancel() is True
    await asyncio.sleep(0)

    assert raw.cancelled() is True
    assert ran is False


@pytest.mark.asyncio
async def test_unified_future_from_asyncio_future_exception() -> None:
    set_loop(AsyncIOLoop())
    raw = asyncio.get_running_loop().create_future()
    uni = UnifiedFuture.from_future(raw)
    raw.set_exception(ZeroDivisionError("division by zero"))

    with pytest.raises(ZeroDivisionError):
        await uni


def test_unified_future_from_asyncio_foreign_thread() -> None:
    loop = asyncio.new_event_loop()
    t = threading.Thread(target=loop.run_forever, daemon=True)
    t.start()

    try:
        fut = loop.create_future()
        uni = UnifiedFuture[str].from_future(fut)

        loop.call_soon_threadsafe(fut.set_result, "cross-thread-result")
        assert uni.result(timeout=2.0) == "cross-thread-result"
    finally:
        loop.call_soon_threadsafe(loop.stop)
        t.join()
        loop.close()


def test_unified_future_from_asyncio_foreign_thread_cancellation() -> None:
    loop = asyncio.new_event_loop()
    t = threading.Thread(target=loop.run_forever, daemon=True)
    t.start()

    try:
        fut = loop.create_future()
        uni = UnifiedFuture[str].from_future(fut)

        assert uni.cancel() is True
        assert uni.cancelled() is True

        ev = threading.Event()
        loop.call_soon_threadsafe(ev.set)
        assert ev.wait(timeout=2.0)

        assert fut.cancelled() is True
    finally:
        loop.call_soon_threadsafe(loop.stop)
        t.join()
        loop.close()


def test_unified_future_from_asyncio_concurrent_close_done(monkeypatch: pytest.MonkeyPatch) -> None:
    loop = asyncio.new_event_loop()
    fut = loop.create_future()
    fut.set_result(123)

    def fake_call_soon_threadsafe(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("Event loop is closed")

    monkeypatch.setattr(loop, "call_soon_threadsafe", fake_call_soon_threadsafe)

    uni = UnifiedFuture.from_future(fut)
    assert uni.result() == 123


def test_unified_future_from_asyncio_concurrent_close_incomplete(monkeypatch: pytest.MonkeyPatch) -> None:
    loop = asyncio.new_event_loop()
    fut = loop.create_future()

    def fake_call_soon_threadsafe(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("Event loop is closed")

    monkeypatch.setattr(loop, "call_soon_threadsafe", fake_call_soon_threadsafe)

    uni = UnifiedFuture.from_future(fut)
    assert uni.cancelled() is True


def test_unified_future_from_asyncio_concurrent_close_cancellation(monkeypatch: pytest.MonkeyPatch) -> None:
    loop = asyncio.new_event_loop()
    fut = loop.create_future()
    uni = UnifiedFuture.from_future(fut)

    def fake_call_soon_threadsafe(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("Event loop is closed")

    monkeypatch.setattr(loop, "call_soon_threadsafe", fake_call_soon_threadsafe)

    assert uni.cancel() is True


@pytest.mark.asyncio
async def test_unified_future_add_loop_callback_cancellation_propagation() -> None:
    set_loop(AsyncIOLoop())
    raw = UnifiedFuture[int]()
    chained = raw.add_loop_callback(lambda f: None)

    assert chained.cancel() is True
    assert chained.cancelled() is True
    assert raw.cancelled() is True


@pytest.mark.asyncio
async def test_unified_future_add_loop_callback_custom_on_cancel() -> None:
    set_loop(AsyncIOLoop())
    cancelled_calls = list[threading.Thread]()
    loop_thread = threading.current_thread()
    raw = UnifiedFuture[int]()
    chained = raw.add_loop_callback(
        lambda f: None,
        cancel_cb=lambda: cancelled_calls.append(threading.current_thread()),
    )

    assert chained.cancel() is True
    assert chained.cancelled() is True
    await asyncio.sleep(0.01)
    assert cancelled_calls == [loop_thread]
    assert raw.cancelled() is False


@pytest.mark.asyncio
async def test_unified_future_then_custom_on_cancel_on_loop() -> None:
    set_loop(AsyncIOLoop())
    cancelled_calls = list[threading.Thread]()
    loop_thread = threading.current_thread()
    raw = UnifiedFuture[int]()
    chained = raw.then(
        lambda x: x * 2,
        cancel_cb=lambda: cancelled_calls.append(threading.current_thread()),
        on_loop=True,
    )

    # Cancel from a background thread
    t = threading.Thread(target=chained.cancel)
    t.start()
    t.join()

    await asyncio.sleep(0.01)
    assert chained.cancelled() is True
    assert cancelled_calls == [loop_thread]
    assert raw.cancelled() is False


@pytest.mark.asyncio
async def test_unified_future_add_loop_callback_parent_cancelled() -> None:
    set_loop(AsyncIOLoop())
    raw = UnifiedFuture[int]()
    called = list[bool]()

    def cb(f: Future[int]) -> None:
        called.append(f.cancelled())

    chained = raw.add_loop_callback(cb)
    raw.cancel()

    with pytest.raises(asyncio.CancelledError):
        await chained

    assert chained.cancelled() is True
    assert called == [True]
