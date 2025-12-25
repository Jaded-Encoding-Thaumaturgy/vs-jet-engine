# Environment Policy

The `vsengine.policy` module implements VapourSynth's Environment Policy system, allowing you to manage multiple VapourSynth environments within a single application.

## Quick Start

```python
import vapoursynth as vs

from vsengine.policy import GlobalStore, Policy

with Policy(GlobalStore()) as policy, policy.new_environment() as env, env.use():
    vs.core.std.BlankClip().set_output()
print(env.outputs)  # {0: <vapoursynth.VideoOutputTuple ...>}
```

## Policy

The `Policy` class registers an environment policy with VapourSynth and manages environment lifecycles.

### Creating a Policy

```python
from vsengine.policy import GlobalStore, Policy

# Manual registration
policy = Policy(GlobalStore())
policy.register()
# ... use policy ...
policy.unregister()

# Or as a context manager (recommended)
with Policy(GlobalStore()) as policy:
    # Policy is registered here
    pass
# Policy is unregistered when exiting
```

### Creating Environments

```python
with Policy(GlobalStore()) as policy:
    env = policy.new_environment()
    try:
        with env.use():
            # VapourSynth code here
            vs.core.std.BlankClip().set_output()
    finally:
        env.dispose()  # Always dispose when done!

    # Or use as context manager
    with policy.new_environment() as env, env.use():
        vs.core.std.BlankClip().set_output()
        # env.dispose() called automatically
```

---

## ManagedEnvironment

A `ManagedEnvironment` wraps a VapourSynth environment with lifecycle management.

### Properties

| Property         | Type               | Description                               |
| ---------------- | ------------------ | ----------------------------------------- |
| `vs_environment` | `vs.Environment`   | The underlying VapourSynth environment    |
| `core`           | `vs.Core`          | The VapourSynth core for this environment |
| `outputs`        | `MappingProxyType` | Registered outputs (video/audio nodes)    |
| `disposed`       | `bool`             | Whether the environment has been disposed |

### Methods

#### `use()`

Context manager to switch to this environment:

```python
with env.use():
    # All VapourSynth operations use this environment
    clip = vs.core.std.BlankClip()
    clip.set_output()
```

#### `switch()`

Switch to this environment without tracking the previous one (use with caution):

```python
env.switch()
# Now in this environment until another switch
```

#### `dispose()`

Release the environment. **Must be called** when finished, or use the context manager:

```python
env.dispose()
```

> [!WARNING]
> Failing to dispose environments will trigger a `ResourceWarning` and may cause memory leaks.

## Environment Stores

An `EnvironmentStore` determines how the "current environment" state is tracked. Choose based on your application's concurrency model:

### GlobalStore

The simplest store. It uses a single variable to track the active environment.

```python
from vsengine.policy import GlobalStore, Policy

policy = Policy(GlobalStore())
```

**Use when:**

- Single-threaded scripts
- Only one environment active at a time
- Maximum performance is needed (lowest overhead)

**Concurrency:** ❌ Not thread-safe or async-safe

---

### ThreadLocalStore

Stores the environment in thread-local storage, giving each thread its own independent environment.

```python
from vsengine.policy import Policy, ThreadLocalStore

policy = Policy(ThreadLocalStore())
```

**Use when:**

- Multi-threaded applications (encoding GUIs, render workers)
- Different threads need different environments simultaneously
- You need vsscript-like behavior

**Concurrency:** ✅ Thread-safe

#### Example: Multi-threaded Processing

```python
import threading
import time

import vapoursynth as vs

from vsengine.policy import Policy, ThreadLocalStore


def worker(policy: Policy, name: str) -> None:
    with policy.new_environment() as env, env.use():
        print(f"[{name}] Started with env {id(env)}")
        time.sleep(0.1)
        # Access the underlying core from the proxy
        is_correct = vs.core.core == env.core
        print(f"[{name}] Finished. Correct Core? {is_correct}")


policy = Policy(ThreadLocalStore())

with policy:
    t1 = threading.Thread(target=worker, args=(policy, "Thread-1"))
    t2 = threading.Thread(target=worker, args=(policy, "Thread-2"))

    t1.start()
    t2.start()

    t1.join()
    t2.join()

```

---

### ContextVarStore

Uses Python's `contextvars` to maintain environment state across async boundaries.

```python
from vsengine.policy import ContextVarStore, Policy

policy = Policy(ContextVarStore())
```

**Use when:**

- AsyncIO, Trio, or other async frameworks
- Event-loop based applications (web servers, bots)
- Context must be preserved across `await` points

**Concurrency:** ✅ Async-safe

#### Example: AsyncIO Processing

```python
import asyncio

import vapoursynth as vs

from vsengine.policy import ContextVarStore, Policy


async def async_worker(policy: Policy, name: str) -> None:
    with policy.new_environment() as env, env.use():
        print(f"[{name}] Started with env {id(env)}")

        # Yield control to allow other tasks to run.
        # This simulates I/O or other async work.
        # If ContextVarStore wasn't working, the context might get mixed up here
        # when other tasks run on this same thread.
        await asyncio.sleep(0.1)  # Context preserved across await

        # Access the underlying core from the proxy
        is_correct = vs.core.core == env.core
        print(f"[{name}] Finished. Correct Core? {is_correct}")


async def main() -> None:
    policy = Policy(ContextVarStore())

    with policy:
        # Structured concurrency: if any task raises, all others are cancelled
        # Raises ExceptionGroup on failure
        async with asyncio.TaskGroup() as group:
            group.create_task(async_worker(policy, "Task-A"))
            group.create_task(async_worker(policy, "Task-B"))
            group.create_task(async_worker(policy, "Task-C"))

        # All tasks run to completion even if one fails
        # Raises the first exception after all finish (or use return_exceptions=True)
        await asyncio.gather(
            async_worker(policy, "Task-A"), async_worker(policy, "Task-B"), async_worker(policy, "Task-C")
        )


asyncio.run(main())
```

#### Example: Trio Processing

```python
import trio
import vapoursynth as vs

from vsengine.policy import ContextVarStore, Policy


async def trio_worker(policy: Policy, name: str) -> None:
    with policy.new_environment() as env, env.use():
        print(f"[{name}] Started with env {id(env)}")

        # Yield control to allow other tasks to run.
        # Context is preserved across await points with ContextVarStore
        await trio.sleep(0.1)

        # Access the underlying core from the proxy
        is_correct = vs.core.core == env.core
        print(f"[{name}] Finished. Correct Core? {is_correct}")


async def main() -> None:
    policy = Policy(ContextVarStore())

    with policy:
        async with trio.open_nursery() as nursery:
            nursery.start_soon(trio_worker, policy, "Task-A")
            nursery.start_soon(trio_worker, policy, "Task-B")
            nursery.start_soon(trio_worker, policy, "Task-C")


trio.run(main)
```

---

## Store Selection Guide

| Use Case              | Recommended Store  |
| --------------------- | ------------------ |
| Simple script / CLI   | `GlobalStore`      |
| Multi-threaded worker | `ThreadLocalStore` |
| AsyncIO / Trio        | `ContextVarStore`  |
