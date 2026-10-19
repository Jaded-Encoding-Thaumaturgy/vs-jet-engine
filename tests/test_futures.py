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
from concurrent.futures import CancelledError, Future, InvalidStateError, ThreadPoolExecutor
from contextlib import suppress
from typing import TYPE_CHECKING, Any, Literal, Self, override

import pytest
import pytest_asyncio
import pytest_mock
import trio
import trio.testing

from vsengine.adapters.asyncio import AsyncIOLoop
from vsengine.futures import UnifiedFuture, UnifiedIterator, as_completed, unified
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
    raise RuntimeError


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


@pytest.fixture(autouse=True)
def ensure_no_loop() -> Generator[None]:
    try:
        yield
    finally:
        set_loop(NO_LOOP)


@pytest_asyncio.fixture
async def asyncio_loop() -> AsyncGenerator[AsyncIOLoop]:
    loop = AsyncIOLoop()
    set_loop(loop)
    try:
        yield loop
    finally:
        set_loop(NO_LOOP)


# UnifiedFuture tests


@pytest.mark.asyncio
async def test_unified_future_is_await(asyncio_loop: AsyncIOLoop) -> None:
    await UnifiedFuture.from_call(succeeds)


@pytest.mark.asyncio
async def test_unified_future_async_context_manager_async(asyncio_loop: AsyncIOLoop) -> None:
    async with UnifiedFuture.from_call(asynccontextmanager_helper) as v:
        assert v == 2


@pytest.mark.asyncio
async def test_unified_future_context_manager_async(asyncio_loop: AsyncIOLoop) -> None:
    async with UnifiedFuture.from_call(contextmanager_helper) as v:
        assert v == 1


def test_unified_future_context_manager() -> None:
    with UnifiedFuture.from_call(contextmanager_helper) as v:
        assert v == 1


def test_unified_future_map() -> None:
    def crash(v: Any) -> str:
        raise RuntimeError(str(v))

    future0 = UnifiedFuture.from_call(succeeds)
    new_future0 = future0.map(lambda v: str(v))
    assert new_future0.result() == "1"

    new_future0 = future0.map(crash)
    assert isinstance(new_future0.exception(), RuntimeError)

    future1 = UnifiedFuture.from_call(fails)
    new_future1 = future1.map(lambda v: str(v))
    assert isinstance(new_future1.exception(), RuntimeError)


def test_unified_future_catch() -> None:
    def crash(_: BaseException) -> str:
        raise RuntimeError("test")

    future0 = UnifiedFuture.from_call(fails)
    new_future0 = future0.catch(lambda e: e.__class__.__name__)
    assert new_future0.result() == "RuntimeError"

    new_future0 = future0.catch(crash)
    assert isinstance(new_future0.exception(), RuntimeError)

    future1 = UnifiedFuture.from_call(succeeds)
    new_future1 = future1.catch(lambda v: str(v))
    # Result is 1 because the future succeeded (no exception to catch)
    result = new_future1.result()
    assert result == 1


@pytest.mark.asyncio
async def test_unified_future_add_loop_callback(asyncio_loop: AsyncIOLoop) -> None:
    def init_thread(fut: Future[threading.Thread]) -> None:
        fut.set_result(threading.current_thread())

    fut: Future[threading.Thread] = Future()
    thr = threading.Thread(target=lambda: init_thread(fut))

    def wrapper() -> Future[threading.Thread]:
        return fut

    unified_fut = UnifiedFuture.from_call(wrapper)

    loop_thread: threading.Thread | None = None

    def record_loop_thr(_: Any) -> None:
        nonlocal loop_thread
        loop_thread = threading.current_thread()

    unified_fut.add_loop_callback(record_loop_thr)
    thr.start()
    cb_thread = await unified_fut

    assert cb_thread != loop_thread


@pytest.mark.asyncio
async def test_unified_future_add_loop_callback_chaining(asyncio_loop: AsyncIOLoop) -> None:
    fut = Future[int]()
    unified_fut = UnifiedFuture.from_future(fut)

    recorded_value: int | None = None

    def side_effect(f: Future[int]) -> None:
        nonlocal recorded_value
        recorded_value = f.result() * 2

    chained_fut = unified_fut.add_loop_callback(side_effect)

    results = list[int]()
    chained_fut.then(lambda val: results.append(val))

    fut.set_result(21)

    res = await chained_fut
    assert res == 21
    assert results == [21]
    assert recorded_value == 42


@pytest.mark.asyncio
async def test_unified_future_then_on_loop(asyncio_loop: AsyncIOLoop) -> None:
    fut = Future[int]()
    unified_fut = UnifiedFuture.from_future(fut)

    loop_thread: threading.Thread | None = None
    callback_thread: threading.Thread | None = None

    def init_thread() -> None:
        fut.set_result(42)

    def on_success(v: int) -> int:
        nonlocal callback_thread
        callback_thread = threading.current_thread()
        return v * 2

    chained = unified_fut.then(on_success, on_loop=True)

    thr = threading.Thread(target=init_thread)
    thr.start()

    res = await chained
    loop_thread = threading.current_thread()

    assert callback_thread == loop_thread
    assert res == 84


@pytest.mark.asyncio
async def test_unified_future_map_on_loop(asyncio_loop: AsyncIOLoop) -> None:
    fut = Future[int]()
    unified_fut = UnifiedFuture.from_future(fut)

    loop_thread: threading.Thread | None = None
    callback_thread: threading.Thread | None = None

    def init_thread() -> None:
        fut.set_result(10)

    def map_fn(v: int) -> str:
        nonlocal callback_thread
        callback_thread = threading.current_thread()
        return f"value: {v}"

    chained = unified_fut.map(map_fn, on_loop=True)

    thr = threading.Thread(target=init_thread)
    thr.start()

    res = await chained
    loop_thread = threading.current_thread()

    assert callback_thread == loop_thread
    assert res == "value: 10"


@pytest.mark.asyncio
async def test_unified_future_catch_on_loop(asyncio_loop: AsyncIOLoop) -> None:
    fut = Future[int]()
    unified_fut = UnifiedFuture.from_future(fut)

    loop_thread: threading.Thread | None = None
    callback_thread: threading.Thread | None = None

    def init_thread() -> None:
        fut.set_exception(ValueError("error!"))

    def catch_fn(e: BaseException) -> str:
        nonlocal callback_thread
        callback_thread = threading.current_thread()
        return str(e)

    chained = unified_fut.catch(catch_fn, on_loop=True)

    thr = threading.Thread(target=init_thread)
    thr.start()

    res = await chained
    loop_thread = threading.current_thread()

    assert callback_thread == loop_thread
    assert res == "error!"


# UnifiedIterator tests


def test_unified_iterator_consume_succeeds() -> None:
    set_loop(NO_LOOP)
    my_futures: list[Future[int]] = [Future(), Future()]
    results = list[int]()

    def add_to_result(f: Future[int]) -> None:
        results.append(f.result())

    state = UnifiedIterator(iter(my_futures)).consume(add_to_result)
    assert not state.done()
    my_futures[1].set_result(2)
    assert not state.done()
    my_futures[0].set_result(1)
    assert state.done()
    assert state.result() is None
    assert results == [1, 2]


def test_unified_iterator_consume_forwards_errors() -> None:
    set_loop(NO_LOOP)
    my_futures: list[Future[int]] = [Future(), Future()]
    results = list[int]()
    errors: list[BaseException] = []

    def add_to_result(f: Future[int]) -> None:
        if exc := f.exception():
            errors.append(exc)
        else:
            results.append(f.result())

    iterator = iter(my_futures)
    state = UnifiedIterator(iterator).consume(add_to_result)
    assert not state.done()
    my_futures[0].set_exception(RuntimeError())
    assert not state.done()
    my_futures[1].set_result(2)
    assert state.done()
    assert state.result() is None

    assert results == [2]
    assert len(errors) == 1


def test_unified_iterator_consume_cancels() -> None:
    set_loop(NO_LOOP)
    my_futures: list[Future[int]] = [Future(), Future()]
    results = list[int]()

    def add_to_result(f: Future[int]) -> bool:
        results.append(f.result())
        return False

    iterator = iter(my_futures)
    state = UnifiedIterator(iterator).consume(add_to_result)
    assert not state.done()
    my_futures[0].set_result(1)
    assert state.done()
    assert state.result() is None
    assert results == [1]

    # Test cancelling the returned state future directly
    futs = [Future[int](), Future[int]()]
    cancelled_results = list[int]()
    state2 = UnifiedIterator(futs).consume(lambda f: cancelled_results.append(f.result()))
    state2.cancel()
    futs[0].set_result(1)
    futs[1].set_result(2)
    assert state2.cancelled()
    assert cancelled_results == []


def test_unified_iterator_consume_cancels_oncrash() -> None:
    set_loop(NO_LOOP)
    my_futures: list[Future[int]] = [Future(), Future()]
    err = RuntimeError("test")

    def crash(_: Future[int]) -> None:
        raise err

    iterator = iter(my_futures)
    state = UnifiedIterator(iterator).consume(crash)
    assert not state.done()
    my_futures[0].set_result(1)
    assert state.done()
    assert state.exception() is err
    assert next(iterator) is not None


def test_unified_iterator_consume_requests_as_needed() -> None:
    my_futures: list[Future[int]] = [Future(), Future()]
    requested: list[Future[int]] = []
    continued: list[Future[int]] = []

    def add_to_result(f: Future[int]) -> None: ...

    def it() -> Iterator[Future[int]]:
        for fut in my_futures:
            requested.append(fut)
            yield fut
            continued.append(fut)

    state = UnifiedIterator(it()).consume(add_to_result)
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


def test_unified_iterator_consume_cancels_on_iteratorcrash() -> None:
    err = RuntimeError("test")

    class It:
        def __iter__(self) -> Self:
            return self

        def __next__(self) -> Future[int]:
            raise err

    state = UnifiedIterator(It()).consume(lambda _: None)
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
async def test_unified_iterator_can_aiter(asyncio_loop: AsyncIOLoop) -> None:
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
async def test_add_loop_callback_failed_future(asyncio_loop: AsyncIOLoop) -> None:
    fut = Future[None]()
    unified_fut = UnifiedFuture.from_future(fut)

    def noop(_: Future[None]) -> None: ...

    chained = unified_fut.add_loop_callback(noop)
    fut.set_exception(ValueError("original error"))

    with pytest.raises(ValueError, match="original error"):
        await chained


@pytest.mark.asyncio
async def test_add_loop_callback_callback_exception(asyncio_loop: AsyncIOLoop) -> None:
    fut = Future[int]()
    unified_fut = UnifiedFuture.from_future(fut)

    def callback_fail(_: Future[int]) -> None:
        raise ValueError("callback error")

    chained = unified_fut.add_loop_callback(callback_fail)
    fut.set_result(42)

    with pytest.raises(ValueError, match="callback error"):
        await chained


@pytest.mark.asyncio
async def test_then_on_loop_callback_exception(asyncio_loop: AsyncIOLoop) -> None:
    fut = Future[int]()
    unified_fut = UnifiedFuture.from_future(fut)

    def callback_fail(_: int) -> int:
        raise ValueError("then callback error")

    chained = unified_fut.then(callback_fail, on_loop=True)
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
async def test_async_context_manager_not_implemented(asyncio_loop: AsyncIOLoop) -> None:
    fut = UnifiedFuture.resolve(FakeContextManager())
    with pytest.raises(NotImplementedError):
        async with fut:
            ...

    with pytest.raises(NotImplementedError):
        await fut.__aexit__(None, None, None)


def test_consume_external_cancel() -> None:
    set_loop(NO_LOOP)
    my_futures = [Future[int](), Future[int]()]

    def noop(_: Future[int]) -> None: ...

    state = UnifiedIterator(iter(my_futures)).consume(noop)
    assert not state.done()

    # Cancel the state future externally
    state.cancel()
    assert state.done()
    assert state.cancelled()

    # Resolving futures now should not raise error but also not run callback since state is done
    my_futures[0].set_result(1)


def test_consume_sync_cancel() -> None:
    set_loop(NO_LOOP)
    f1 = resolve(1)
    f2 = resolve(2)
    results = list[int]()

    def cb(f: Future[int]) -> bool:
        results.append(f.result())
        return False  # signal early stop

    state = UnifiedIterator(iter([f1, f2])).consume(cb)
    assert state.done()
    assert results == [1]


@pytest.mark.asyncio
async def test_consume_async_loop(asyncio_loop: AsyncIOLoop) -> None:
    my_futures = [Future[int](), Future[int]()]
    results = list[int]()

    def cb(f: Future[int]) -> None:
        results.append(f.result())

    state = UnifiedIterator(iter(my_futures)).consume(cb)
    my_futures[0].set_result(10)
    my_futures[1].set_result(20)

    try:
        await state
        assert results == [10, 20]
    finally:
        set_loop(NO_LOOP)


def test_consume_next_cycle_fails() -> None:
    set_loop(NO_LOOP)

    class CustomFailedLoop(_NoEventLoop):
        @override
        def next_cycle(self) -> Future[None]:
            fut = Future[None]()
            fut.set_exception(ValueError("next_cycle failed"))
            return fut

    set_loop(CustomFailedLoop())

    try:
        f1 = resolve(1)
        f2 = resolve(2)

        def noop(_: Future[int]) -> None: ...

        state = UnifiedIterator(iter([f1, f2])).consume(noop)
        assert state.done()
        assert isinstance(state.exception(), ValueError)
    finally:
        set_loop(NO_LOOP)


def test_consume_next_cycle_fails_async() -> None:
    set_loop(NO_LOOP)

    class CustomPendingFailedLoop(_NoEventLoop):
        def __init__(self) -> None:
            self.next_cycle_futs = list[Future[None]]()

        @override
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

        state = UnifiedIterator(iter([f1, f2])).consume(noop)
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
async def test_unified_future_cancellation_prevents_asyncio_task_from_running(asyncio_loop: AsyncIOLoop) -> None:
    ran: bool = False

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
async def test_unified_future_from_asyncio_future_exception(asyncio_loop: AsyncIOLoop) -> None:
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
    try:
        fut = loop.create_future()
        fut.set_result(123)

        def fake_call_soon_threadsafe(*args: Any, **kwargs: Any) -> None:
            raise RuntimeError("Event loop is closed")

        monkeypatch.setattr(loop, "call_soon_threadsafe", fake_call_soon_threadsafe)

        uni = UnifiedFuture.from_future(fut)
        assert uni.result() == 123
    finally:
        loop.close()


def test_unified_future_from_asyncio_concurrent_close_incomplete(monkeypatch: pytest.MonkeyPatch) -> None:
    loop = asyncio.new_event_loop()
    try:
        fut = loop.create_future()

        def fake_call_soon_threadsafe(*args: Any, **kwargs: Any) -> None:
            raise RuntimeError("Event loop is closed")

        monkeypatch.setattr(loop, "call_soon_threadsafe", fake_call_soon_threadsafe)

        uni = UnifiedFuture.from_future(fut)
        assert uni.cancelled() is True
    finally:
        loop.close()


def test_unified_future_from_asyncio_concurrent_close_cancellation(monkeypatch: pytest.MonkeyPatch) -> None:
    loop = asyncio.new_event_loop()
    try:
        fut = loop.create_future()
        uni = UnifiedFuture.from_future(fut)

        def fake_call_soon_threadsafe(*args: Any, **kwargs: Any) -> None:
            raise RuntimeError("Event loop is closed")

        monkeypatch.setattr(loop, "call_soon_threadsafe", fake_call_soon_threadsafe)

        assert uni.cancel() is True
    finally:
        loop.close()


@pytest.mark.trio
async def test_unified_future_from_trio_task_success(nursery: trio.Nursery) -> None:
    async def add(a: int, b: int) -> int:
        return a + b

    fut = UnifiedFuture.from_trio_task(nursery, add, 10, 20)
    wrapped_fut = WrappedUnifiedFuture.from_trio_task(nursery, add, 1, 2)

    await trio.testing.wait_all_tasks_blocked()

    assert fut.done() is True
    assert fut.cancelled() is False
    assert fut.result() == 30

    assert wrapped_fut.done() is True
    assert wrapped_fut.cancelled() is False
    assert wrapped_fut.result() == 3
    assert isinstance(wrapped_fut, WrappedUnifiedFuture)


@pytest.mark.trio
async def test_unified_future_from_trio_task_exception(nursery: trio.Nursery) -> None:
    async def fail() -> None:
        raise ValueError("trio task failed")

    fut = UnifiedFuture.from_trio_task(nursery, fail)
    await trio.testing.wait_all_tasks_blocked()

    assert fut.done() is True
    assert fut.cancelled() is False
    exc = fut.exception()
    assert isinstance(exc, ValueError)
    assert str(exc) == "trio task failed"
    with pytest.raises(ValueError, match="trio task failed"):
        fut.result()


@pytest.mark.trio
async def test_unified_future_from_trio_task_pre_cancelled(nursery: trio.Nursery) -> None:
    ran: bool = False

    async def fn() -> int:
        nonlocal ran
        ran = True
        return 42

    fut = UnifiedFuture.from_trio_task(nursery, fn)
    assert fut.cancel() is True
    await trio.testing.wait_all_tasks_blocked()

    assert ran is False
    assert fut.cancelled() is True


@pytest.mark.trio
async def test_unified_future_from_trio_task_cancel_from_future(nursery: trio.Nursery) -> None:
    started = trio.Event()

    async def fn() -> int:
        started.set()
        await trio.sleep_forever()
        return 42

    fut = UnifiedFuture.from_trio_task(nursery, fn)
    await started.wait()
    assert fut.cancel() is True
    await trio.testing.wait_all_tasks_blocked()

    assert fut.cancelled() is True


@pytest.mark.trio
async def test_unified_future_from_trio_task_cancel_from_trio_outer() -> None:
    started = trio.Event()

    async def fn() -> None:
        started.set()
        await trio.sleep_forever()

    fut: UnifiedFuture[None] | None = None
    async with trio.open_nursery() as nursery:
        fut = UnifiedFuture.from_trio_task(nursery, fn)
        await started.wait()
        nursery.cancel_scope.cancel()

    assert fut is not None
    assert fut.cancelled() is True


def test_unified_future_from_trio_task_run_finished_suppressed(mocker: pytest_mock.MockerFixture) -> None:
    token: trio.lowlevel.TrioToken | None = None

    async def get_token() -> None:
        nonlocal token
        token = trio.lowlevel.current_trio_token()

    trio.run(get_token)
    assert token is not None

    mock_nursery = mocker.MagicMock()

    async def dummy() -> None:
        pass

    fut = UnifiedFuture.from_trio_task(mock_nursery, dummy, trio_token=token)
    assert fut.cancel() is True
    assert fut.cancelled() is True


@pytest.mark.asyncio
async def test_unified_future_add_loop_callback_cancellation_propagation(asyncio_loop: AsyncIOLoop) -> None:
    raw = UnifiedFuture[int]()
    chained = raw.add_loop_callback(lambda f: None)

    assert chained.cancel() is True
    assert chained.cancelled() is True
    assert raw.cancelled() is True


@pytest.mark.asyncio
async def test_unified_future_add_loop_callback_custom_on_cancel(asyncio_loop: AsyncIOLoop) -> None:
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
async def test_unified_future_then_custom_on_cancel_on_loop(asyncio_loop: AsyncIOLoop) -> None:
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

    # Resolving parent after child was cancelled should safely suppress callback
    raw.set_result(10)
    await asyncio.sleep(0.01)
    assert chained.cancelled() is True


@pytest.mark.asyncio
async def test_unified_future_add_loop_callback_parent_cancelled(asyncio_loop: AsyncIOLoop) -> None:
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


def test_concurrent_cancellation_and_resolution() -> None:
    def race_scenario(_: int) -> None:
        raw = Future[int]()
        uf = UnifiedFuture.from_future(raw)
        ch = uf.then(lambda x: x + 1)

        def resolve() -> None:
            with suppress(InvalidStateError):
                raw.set_result(42)

        with ThreadPoolExecutor(max_workers=3) as ex:
            f1 = ex.submit(uf.cancel)
            f2 = ex.submit(ch.cancel)
            f3 = ex.submit(resolve)
            f1.result()
            f2.result()
            f3.result()

    for i in range(30):
        race_scenario(i)


# for_each and as_completed tests
@pytest.mark.asyncio
@pytest.mark.parametrize("is_async", [False, True], ids=["sync", "async"])
@pytest.mark.parametrize("yield_cycles", [False, True], ids=["no_yield", "yield"])
async def test_for_each_consumption(asyncio_loop: AsyncIOLoop, is_async: bool, yield_cycles: bool) -> None:
    pending = Future[int]()
    futs = [resolve(1), pending, resolve(3)]

    asyncio.get_running_loop().call_soon(pending.set_result, 2)

    results = list[int]()
    types = list[type]()

    if is_async:

        async def cb(f: UnifiedFuture[int]) -> None:
            await asyncio.sleep(0.001)
            types.append(type(f))
            results.append(f.result())
    else:

        def cb(f: UnifiedFuture[int]) -> None:
            types.append(type(f))
            results.append(f.result())

    await UnifiedIterator(futs).for_each(cb, yield_cycles=yield_cycles)
    assert results == [1, 2, 3]
    assert types == [UnifiedFuture, UnifiedFuture, UnifiedFuture]


@pytest.mark.asyncio
@pytest.mark.parametrize("is_async", [False, True], ids=["sync", "async"])
async def test_for_each_early_stop(asyncio_loop: AsyncIOLoop, is_async: bool) -> None:
    futs = [resolve(1), resolve(2), resolve(3)]
    results = list[int]()

    if is_async:

        async def cb(f: UnifiedFuture[int]) -> bool | None:
            results.append(f.result())
            return False if len(results) == 2 else None
    else:

        def cb(f: UnifiedFuture[int]) -> bool | None:
            results.append(f.result())
            return False if len(results) == 2 else None

    await UnifiedIterator(futs).for_each(cb)
    assert results == [1, 2]


@pytest.mark.asyncio
async def test_for_each_forwards_errors(asyncio_loop: AsyncIOLoop) -> None:
    f1 = Future[int]()
    f2 = resolve(2)

    asyncio.get_running_loop().call_soon(f1.set_exception, RuntimeError("test error"))

    errors = list[BaseException]()
    results = list[int]()

    def cb(f: UnifiedFuture[int]) -> None:
        if exc := f.exception():
            errors.append(exc)
        else:
            results.append(f.result())

    await UnifiedIterator([f1, f2]).for_each(cb)
    assert len(errors) == 1
    assert results == [2]


@pytest.mark.asyncio
@pytest.mark.parametrize("is_async", [False, True], ids=["sync", "async"])
async def test_for_each_callback_exception(asyncio_loop: AsyncIOLoop, is_async: bool) -> None:
    futs = [resolve(1), resolve(2)]

    if is_async:

        async def cb(f: UnifiedFuture[int]) -> None:
            await asyncio.sleep(0.001)
            raise ValueError("async callback error")
    else:

        def cb(f: UnifiedFuture[int]) -> None:
            raise ValueError("sync callback error")

    with pytest.raises(ValueError, match="callback error"):
        await UnifiedIterator(futs).for_each(cb)


@pytest.mark.asyncio
async def test_as_completed_out_of_order(asyncio_loop: AsyncIOLoop) -> None:
    f0 = Future[int]()
    f1 = Future[int]()
    f2 = Future[int]()

    async def consumer() -> list[int]:
        yielded = list[int]()
        async for fut in as_completed([f0, f1, f2], concurrency=3):
            yielded.append(fut.result())
        return yielded

    task = asyncio.create_task(consumer())
    await asyncio.sleep(0.01)

    # Resolve out of order: f1, then f2, then f0
    f1.set_result(20)
    await asyncio.sleep(0.01)
    f2.set_result(30)
    await asyncio.sleep(0.01)
    f0.set_result(10)

    results = await task
    assert results == [20, 30, 10]


@pytest.mark.asyncio
async def test_as_completed_concurrency_bound(asyncio_loop: AsyncIOLoop) -> None:
    futs = [Future[int]() for _ in range(5)]
    requested = list[int]()

    def gen() -> Iterator[Future[int]]:
        for idx, f in enumerate(futs):
            requested.append(idx)
            yield f

    async def consumer() -> list[int]:
        results = list[int]()
        async for fut in as_completed(gen(), concurrency=2):
            results.append(fut.result())
        return results

    task = asyncio.create_task(consumer())
    await asyncio.sleep(0.01)

    # With concurrency=2, only indices 0 and 1 should have been requested so far
    assert requested == [0, 1]

    # Resolve index 0 -> index 2 should now be requested
    futs[0].set_result(100)
    await asyncio.sleep(0.01)
    assert requested == [0, 1, 2]

    # Resolve index 1 and 2
    futs[1].set_result(101)
    futs[2].set_result(102)
    await asyncio.sleep(0.01)
    assert requested == [0, 1, 2, 3, 4]

    # Resolve the rest
    futs[3].set_result(103)
    futs[4].set_result(104)

    res = await task
    assert res == [100, 101, 102, 103, 104]


@pytest.mark.asyncio
async def test_as_completed_invalid_concurrency(asyncio_loop: AsyncIOLoop) -> None:
    with pytest.raises(ValueError, match="concurrency must be greater than 0"):
        async for _ in as_completed([], concurrency=0):
            pass

    with pytest.raises(ValueError, match="concurrency must be greater than 0"):
        async for _ in as_completed([], concurrency=-1):
            pass


@pytest.mark.asyncio
async def test_as_completed_empty(asyncio_loop: AsyncIOLoop) -> None:
    items = list[Future[int]]()
    async for fut in as_completed(items):
        items.append(fut)
    assert items == []


@pytest.mark.asyncio
async def test_unified_iterator_as_completed_method(asyncio_loop: AsyncIOLoop) -> None:
    f1 = resolve(1)
    f2 = resolve(2)
    it = UnifiedIterator(iter([f1, f2]))

    results = list[int]()
    async for fut in it.as_completed(concurrency=2):
        results.append(fut.result())

    assert sorted(results) == [1, 2]


@pytest.mark.asyncio
async def test_as_completed_early_break_cancels_active_and_closes_iterator(asyncio_loop: AsyncIOLoop) -> None:
    futs = [Future[int]() for _ in range(5)]

    futs[0].set_result(42)
    ag = as_completed(futs, concurrency=3)

    async for fut in ag:
        assert fut.result() == 42
        break

    await ag.aclose()

    with pytest.raises(StopAsyncIteration):
        await anext(ag)

    # In-flight uncompleted futures should have been cancelled
    assert futs[1].cancelled() is True
    assert futs[2].cancelled() is True


def test_unified_future_try_set_state_transitions() -> None:
    """Ensure try_set_result and try_set_exception return False when future is already done."""
    fut = UnifiedFuture[int]()
    assert fut.try_set_result(42) is True
    assert fut.try_set_result(99) is False
    assert fut.try_set_exception(ValueError("error")) is False


@pytest.mark.asyncio
async def test_then_on_loop_cancellation_propagation(
    asyncio_loop: AsyncIOLoop, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ensure cancellation propagates properly when chaining callbacks on the event loop."""
    # Target cancelled while callback is queued/running on loop
    parent1 = UnifiedFuture[int]()
    blocked = threading.Event()

    def cb(x: int) -> int:
        blocked.wait(timeout=1.0)
        return x * 2

    child1 = parent1.then(cb, cancel_cb=lambda: None, on_loop=True)
    parent1.set_result(10)
    child1.cancel()
    blocked.set()

    await asyncio.sleep(0.01)
    assert child1.cancelled()

    # Event loop task cancelled propagates cancellation to target
    cancelled_lf = Future[int]()
    cancelled_lf.cancel()
    monkeypatch.setattr(asyncio_loop, "from_thread", lambda *args, **kwargs: cancelled_lf)

    parent2 = UnifiedFuture[int]()
    child2 = parent2.then(lambda x: x * 2, on_loop=True)
    parent2.set_result(20)

    await asyncio.sleep(0.01)
    assert child2.cancelled()


def test_from_future_cancellation_race_handling(mocker: pytest_mock.MockerFixture) -> None:
    """Ensure from_future handles CancelledError raised by exception() call."""
    raw = Future[int]()
    mocker.patch.object(raw, "cancelled", return_value=False)
    mocker.patch.object(raw, "exception", side_effect=CancelledError())
    uf = UnifiedFuture.from_future(raw)
    raw.set_result(100)
    assert uf.cancelled()
