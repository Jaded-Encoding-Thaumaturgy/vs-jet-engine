# Event Loop Integration

The `vsengine.loops` module provides an abstraction layer to integrate VapourSynth with any event loop (asyncio, Qt, Trio, etc.).

---

## Quick Start

To use async features, you must first attach an event loop adapter. For example, using the standard `asyncio` adapter:

```python
from vsengine.adapters.asyncio import AsyncIOLoop
from vsengine.loops import set_loop

# Attach the asyncio event loop adapter
set_loop(AsyncIOLoop())
```

---

## Built-in Event Loop Adapters

`vs-engine` includes built-in adapters to bridge VapourSynth's background thread-based processing with Python's structured concurrency and async frameworks.

### AsyncIOLoop

The `AsyncIOLoop` adapter bridges `vs-engine` to Python's standard `asyncio` library.

#### Usage Example

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

The `TrioEventLoop` adapter bridges `vs-engine` to the `trio` library.

#### Usage Example

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

## Core Concepts & Event Loop Helpers

### `from_thread(func, *args, **kwargs)`

Run a function on the main event loop from any thread. Preserves the VapourSynth environment active in the calling thread.

```python
from vsengine.loops import from_thread


def callback_from_vs_thread() -> None:
    # This runs on a VapourSynth worker thread
    future = from_thread(update_ui, frame_number=42)
    future.result()  # Wait for completion
```

### `to_thread(func, *args, **kwargs)`

Run a function in a worker thread. Useful for offloading blocking operations from your event loop.

```python
from vsengine.loops import to_thread


async def process() -> None:
    # Run blocking operation in thread pool
    future = to_thread(heavy_computation, data)
    result = future.result()
```

### `keep_environment(func)`

Decorator that captures the VapourSynth environment at creation time and restores it when the function is run.

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

To integrate custom GUI event loops (e.g., Qt, wxPython), inherit from `vsengine.loops.EventLoop` and implement the abstract methods:

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

### EventLoop Interface

| Method                               | Status       | Description                                                                     |
| ------------------------------------ | ------------ | ------------------------------------------------------------------------------- |
| `from_thread(func, *args, **kwargs)` | **Required** | Schedules callable `func` to run on the main event loop thread.                 |
| `to_thread(func, *args, **kwargs)`   | Optional     | Runs `func` in a worker thread (default: spawns a standard `threading.Thread`). |
| `next_cycle()`                       | Optional     | Yields control to the event loop.                                               |
| `attach()`                           | Optional     | Callback when the loop is registered via `set_loop`.                            |
| `detach()`                           | Optional     | Callback when the loop is unregistered.                                         |
| `await_future(future)`               | Optional     | Make a `Future` awaitable (for async loops).                                    |
| `wrap_cancelled()`                   | Optional     | Context manager to translate internal cancellations to loop-native exceptions.  |

---

## The Cancelled Exception

When operations are cancelled, vsengine raises `vsengine.loops.Cancelled`. Event loop adapters translate this to their native cancellation type:

| Loop    | Exception                |
| ------- | ------------------------ |
| asyncio | `asyncio.CancelledError` |
| trio    | `trio.Cancelled`         |
