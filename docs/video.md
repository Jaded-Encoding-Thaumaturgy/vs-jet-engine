# Video Rendering & Frame Evaluation

`vsengine.video` provides unified synchronous and asynchronous APIs for requesting VapourSynth frames, extracting plane data, and streaming rendered video.

All functions accept an optional `env`. Pass a `vs.Environment` or `ManagedEnvironment` when the request must run in a particular VapourSynth environment; otherwise the currently active environment is used.

## Quick Start

Set up a vs-engine loop and policy, then iterate without blocking the event loop:

```python
import asyncio
import vapoursynth as vs
from vsengine.adapters.asyncio import AsyncIOLoop
from vsengine.loops import set_loop
from vsengine.policy import ContextVarStore, Policy
from vsengine.video import frames


async def main() -> None:
    set_loop(AsyncIOLoop())
    policy = Policy(ContextVarStore())

    with policy, policy.new_environment() as env, env.use():
        clip = vs.core.std.BlankClip(length=100)

        async for frame in frames(clip, env, prefetch=4, backlog=8):
            print(f"Rendered frame: {frame.width}x{frame.height}")


asyncio.run(main())
```

## Single Frame Requests

### `frame(node, frameno, env=None)`

Returns a `UnifiedFuture[vs.VideoFrame]` for one frame. Use `.result()` in synchronous code or `await` it in asynchronous code. The caller owns the returned frame and must close it.

```python
# Synchronous
with frame(clip, 42).result() as frame_obj:
    print(frame_obj.width)

# Asynchronous
with await frame(clip, 42) as frame_obj:
    print(frame_obj.width)
```

### `planes(node, frameno, env=None, *, planes=None)`

Returns a `UnifiedFuture[tuple[bytes, ...]]` containing the requested planes. `planes=None` (the default) extracts every plane, including from variable-format clips. The source frame is closed automatically after its data is copied.

```python
# Extract the luma plane of frame 100.
luma_data = planes(clip, 100, planes=[0]).result()[0]
```

## Sequential Frames

### `frames(node, env=None, *, prefetch=0, backlog=None, close=True)`

Returns a `UnifiedIterator[vs.VideoFrame]` in display order.

- `prefetch` controls how many future frame requests are started ahead of consumption.
- `backlog` limits queued requests. `None` means no limit; `0` disables buffering.
- With the default `close=True`, advancing the iterator closes the previously yielded frame. Process each frame before requesting the next one. Set `close=False` when you need to retain frames, and close every retained frame yourself.

```python
# Synchronous iteration blocks until each next frame is ready.
for frame_obj in frames(clip, prefetch=4, backlog=8):
    print(frame_obj.width, frame_obj.height)


async def process_video(clip):
    # Async iteration cooperates with the configured vs-engine event loop.
    async for frame_obj in frames(clip, prefetch=4, backlog=8):
        await process_frame(frame_obj)
```

## Rendering Byte Streams

### `render(node, env=None, *, prefetch=None, backlog=0, y4m=False)`

Returns a `UnifiedIterator[tuple[int, bytes]]`. Each video-frame chunk is `(frame_number, data)`, where frame numbers are **1-based**. The raw data is the concatenation of every frame plane.

With `y4m=True`, the iterator first yields `(0, header)` and prefixes each video-frame chunk with `b"FRAME\\n"`. Y4M requires integer sample types (`vs.INTEGER`) and fixed resolution/format.

```python
# Stream raw frame data to a binary file.
with open("video.raw", "wb") as output:
    for _, chunk in render(clip, prefetch=4):
        output.write(chunk)

# Write a Y4M stream. The first chunk is its Y4M header.
with open("video.y4m", "wb") as output:
    for _, chunk in render(clip, y4m=True):
        output.write(chunk)
```

`render()` uses `frames(..., close=False)` internally and closes each frame safely as its bytes are extracted.

## UnifiedIterator Async Processing

Both `frames()` and `render()` return a `UnifiedIterator`, which supports several async consumption patterns:

### `as_completed(*, concurrency=None)`

Yields futures in order of completion with a bounded sliding window of size `concurrency` (defaults to CPU count):

```python
async def render_as_completed(clip: vs.VideoNode) -> None:
    # Yields frames as soon as they finish rendering
    async for fut in frames(clip, prefetch=8).as_completed(concurrency=4):
        frame_obj = await fut
        print(f"Finished frame {frame_obj.width}x{frame_obj.height}")
```

### `for_each(callback, *, yield_cycles=True)`

Asynchronously awaits each future sequentially and invokes `callback(future)`:

```python
from concurrent.futures import Future
import vapoursynth as vs
from vsengine.video import frames


async def on_frame_completed(fut: UnifiedFuture[vs.VideoFrame]) -> bool:
    try:
        frame_obj = fut.result()
        print(frame_obj.width)
        return True
    except Exception as error:
        print(f"Frame error: {error}")
        return False


async def run_rendering(clip: vs.VideoNode) -> None:
    await frames(clip, prefetch=8).for_each(on_frame_completed)
```

### `consume(callback)`

Dispatches sequential frame processing onto the event loop cycle and returns a `UnifiedFuture[None]` that resolves upon completion:

```python
# Assuming `on_frame_completed` is a synchronous callback here
def run_pipeline(clip: vs.VideoNode) -> None:
    frames(clip, prefetch=8).consume(on_frame_completed).result()
```
