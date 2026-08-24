# vs-engine
# Copyright (C) 2022  cid-chan
# Copyright (C) 2025  Jaded-Encoding-Thaumaturgy
# This project is licensed under the EUPL-1.2
# SPDX-License-Identifier: EUPL-1.2

from collections.abc import Generator, Iterable
from concurrent.futures import Future
from threading import Condition
from typing import override

from vapoursynth import RawFrame, core


class _PrefetchBuffer[FrameT: RawFrame](Generator[Future[FrameT]]):
    __slots__ = ("backlog", "cond", "finished", "prefetch", "refilling", "reorder", "running", "sidx", "source")

    def __init__(self, source: Iterable[Future[FrameT]], prefetch: int, backlog: int) -> None:
        self.source = enumerate(source)
        self.prefetch = prefetch
        self.backlog = backlog
        self.cond = Condition()
        self.reorder = dict[int, Future[FrameT]]()
        self.running = 0
        self.sidx = 0
        self.finished = False
        self.refilling = False

        # Initial prefetch fill
        with self.cond:
            self.refill()

    @override
    def send(self, value: None) -> Future[FrameT]:
        with self.cond:
            # Wait until the next sequential frame is available or all work is done
            self.cond.wait_for(lambda: self.sidx in self.reorder or (self.finished and not self.reorder))

            if self.finished and not self.reorder:
                self.close()
                raise StopIteration

            fut = self.reorder.pop(self.sidx)
            self.sidx += 1
            # Slot freed from reorder; request next future to replenish the backlog
            self.refill()
            self.cond.notify_all()
            return fut

    @override
    def throw(self, typ: type[BaseException] | BaseException, *_: object) -> Future[FrameT]:
        self.close()
        raise typ

    @override
    def close(self) -> None:
        with self.cond:
            self.finished = True
            remaining = list(self.reorder.values())
            self.reorder.clear()
            self.cond.notify_all()

        for f in remaining:
            f.cancel()

    def refill(self) -> None:
        """
        Request new futures up to concurrency 'prefetch' and memory 'backlog' limits.
        Must be called with lock held.
        """
        if self.finished or self.refilling:
            return

        self.refilling = True
        try:
            # Two rules:
            # - Don't exceed running concurrency.
            # - Don't exceed unconsumed backlog.
            while not self.finished and self.running < self.prefetch and len(self.reorder) < self.backlog:
                self._request_next()
        finally:
            self.refilling = False

    def _request_next(self) -> None:
        # Must be called with lock held
        if self.finished:
            return

        try:
            ni = next(self.source, None)
        except BaseException:
            self.finished = True
            self.cond.notify_all()
            raise

        if ni is None:
            self.finished = True
            return

        self.running += 1
        idx, fut = ni
        self.reorder[idx] = fut
        fut.add_done_callback(self._on_done)

    def _on_done(self, fut: Future[FrameT]) -> None:
        with self.cond:
            self.running -= 1
            if self.finished:
                return self.cond.notify_all()

            # If a future fails or was cancelled, stop requesting further work
            if fut.cancelled() or fut.exception() is not None:
                self.finished = True
                return self.cond.notify_all()

            # Top up the prefetch pipeline if not already inside a refill loop
            if not self.refilling:
                self.refill()
            self.cond.notify_all()


def buffer_futures[FrameT: RawFrame](
    futures: Iterable[Future[FrameT]],
    prefetch: int | None = None,
    backlog: int | None = None,
) -> _PrefetchBuffer[FrameT]:
    if prefetch is None or prefetch <= 0:
        prefetch = core.num_threads
    if backlog is None or backlog < 0:
        backlog = prefetch * 3

    return _PrefetchBuffer(futures, prefetch, max(backlog, prefetch))


def close_when_needed[FrameT: RawFrame](future_iterable: Iterable[Future[FrameT]]) -> Generator[Future[FrameT]]:
    def wrap_open_future(fut: Future[FrameT]) -> Future[FrameT]:
        f = Future[FrameT]()

        def as_completed(target: Future[FrameT]) -> None:
            if target.cancelled():
                f.cancel()
                return

            if (exc := target.exception()) is not None:
                f.set_exception(exc)
                return

            try:
                new_r = target.result().__enter__()
            except BaseException as e:
                f.set_exception(e)
            else:
                f.set_result(new_r)

        fut.add_done_callback(as_completed)
        return f

    for fut in future_iterable:
        yield wrap_open_future(fut)
        fut.add_done_callback(lambda f: f.result().__exit__() if not f.cancelled() and f.exception() is None else None)
