# Event Loop Integration

The `vsengine.loops` module provides an abstraction layer to integrate VapourSynth with any event loop (asyncio, Qt, Trio, etc.).

## Quick Start

```python
from vsengine.adapters.asyncio import AsyncIOLoop
from vsengine.loops import set_loop

# Attach asyncio event loop
set_loop(AsyncIOLoop())
```

## Core Concepts

### The Event Loop Abstraction

VapourSynth runs frame processing on its own threads, but your application likely has its own event loop (asyncio, Qt's event loop, etc.). The `EventLoop` class bridges these two worlds:

- **`from_thread`** - Schedule work from a VapourSynth thread onto your main event loop
- **`to_thread`** - Offload blocking work from your main loop to a worker thread
- **`next_cycle`** - Yield control to let the event loop process events

---

## Built-in Adapters

### AsyncIOLoop

For asyncio-based applications:

```python
import asyncio

from vsengine.adapters.asyncio import AsyncIOLoop
from vsengine.loops import set_loop


async def main() -> None:
    set_loop(AsyncIOLoop())
    # Your async VapourSynth code here


asyncio.run(main())

```

### TrioEventLoop

For Trio-based applications:

```python
import trio

from vsengine.adapters.trio import TrioEventLoop
from vsengine.loops import set_loop


async def main() -> None:
    async with trio.open_nursery() as nursery:
        set_loop(TrioEventLoop(nursery))
        # Your async VapourSynth code here


trio.run(main)
```

---

## Helper Functions

### `from_thread(func, *args, **kwargs)`

Run a function on the main event loop from any thread. Preserves the VapourSynth environment.

```python
from vsengine.loops import from_thread


def callback_from_vs_thread() -> None:
    # This runs on a VapourSynth worker thread
    future = from_thread(update_ui, frame_number=42)
    future.result()  # Wait for completion
```

### `to_thread(func, *args, **kwargs)`

Run a function in a worker thread. Useful for offloading blocking operations.

```python
from vsengine.loops import to_thread


async def process() -> None:
    # Run blocking operation in thread pool
    future = to_thread(heavy_computation, data)
    result = future.result()
```

### `keep_environment(func)`

Decorator that captures and restores the VapourSynth environment when the function runs.

```python
import vapoursynth as vs

from vsengine.loops import keep_environment


@keep_environment
def my_callback() -> vs.VideoNode:
    # VapourSynth environment is preserved here,
    # even if called from a different context
    return vs.core.std.BlankClip()

```

### `get_loop()` / `set_loop(loop)`

Get or set the currently active event loop:

```python
from vsengine.loops import get_loop, set_loop

current = get_loop()
set_loop(my_custom_loop)
```

---

## Creating Custom Event Loops

For GUI frameworks or custom event loops, implement the `EventLoop` abstract class:

```python
from collections.abc import Callable
from concurrent.futures import Future

from PySide6.QtCore import QObject, Signal, Slot
from PySide6.QtWidgets import QApplication
from vsengine.loops import EventLoop


class QtEventLoop(QObject, EventLoop):
    _invoke = Signal(object)  # Signal carries the callable

    def __init__(self, app: QApplication) -> None:
        super().__init__()
        self.app = app
        self._invoke.connect(self._run_on_main)

    @Slot(object)
    def _run_on_main(self, task: Callable[[], None]) -> None:
        task()

    def from_thread[**P, R](self, func: Callable[P, R], *args: P.args, **kwargs: P.kwargs) -> Future[R]:
        future = Future[R]()

        def wrapper() -> None:
            if not future.set_running_or_notify_cancel():
                return
            try:
                future.set_result(func(*args, **kwargs))
            except BaseException as e:
                future.set_exception(e)

        self._invoke.emit(wrapper)
        return future

    # Optionally override: to_thread, next_cycle, attach, detach
    # A more complete example can be found in the vsview/vspreview repositories.
```

### Required Methods

| Method                               | Description                                  |
| ------------------------------------ | -------------------------------------------- |
| `from_thread(func, *args, **kwargs)` | **Required.** Schedule work on the main loop |

### Optional Methods

| Method                             | Description                                                |
| ---------------------------------- | ---------------------------------------------------------- |
| `to_thread(func, *args, **kwargs)` | Run in worker thread (default: uses `threading.Thread`)    |
| `next_cycle()`                     | Yield to event loop (default: schedules via `from_thread`) |
| `attach()`                         | Called when loop is set active                             |
| `detach()`                         | Called when loop is replaced                               |
| `await_future(future)`             | Make a `Future` awaitable (for async loops)                |
| `wrap_cancelled()`                 | Translate `Cancelled` exceptions                           |

---

## The Cancelled Exception

When operations are cancelled, vsengine raises `vsengine.loops.Cancelled`. Event loop adapters translate this to their native cancellation type:

| Loop    | Exception                |
| ------- | ------------------------ |
| asyncio | `asyncio.CancelledError` |
| trio    | `trio.Cancelled`         |
