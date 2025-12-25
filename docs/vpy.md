# Script Execution

The `vsengine.vpy` module provides functions to load and execute VapourSynth scripts (`.vpy` files) or inline code.

## Quick Start

```python
import vapoursynth as vs

from vsengine.vpy import load_code, load_script

# Run a .vpy file
with load_script("/path/to/script.vpy") as script:
    print(vs.get_outputs())

# Run inline code
with load_code("vs.core.std.BlankClip().set_output()") as script:
    print(vs.get_outputs())

```

---

## Loading Scripts

### `load_script(path, environment, ...)`

Load and execute a `.vpy` file:

```python
from vsengine.policy import GlobalStore, Policy
from vsengine.vpy import load_script

with Policy(GlobalStore()) as policy:
    # Creates a new environment from the policy
    script = load_script("/path/to/script.vpy", policy)
    script.result()  # Execute and wait

    print(script.environment.outputs)
    script.dispose()

```

### `load_code(code, environment, ...)`

Execute inline VapourSynth code:

```python
from vsengine.policy import GlobalStore, Policy
from vsengine.vpy import load_code

code = """
import vapoursynth as vs
clip = vs.core.std.BlankClip(width=1920, height=1080)
clip.set_output()
"""

with Policy(GlobalStore()) as policy, load_code(code, policy) as script:
    print(script.environment.outputs)
```

The `code` parameter accepts:

- `str` - Python source code
- `bytes` - Encoded source code
- `ast.Module` - Parsed AST
- `CodeType` - Compiled code object

---

## Parameters

Both functions accept these parameters:

| Parameter       | Type                                   | Default                        | Description                                               |
| --------------- | -------------------------------------- | ------------------------------ | --------------------------------------------------------- |
| `script`/`code` | `str`/`PathLike`/etc.                  | _required_                     | Script path or code to execute                            |
| `environment`   | `Policy`/`Environment`/`Script`/`None` | `vs.get_current_environment()` | Where to run the script                                   |
| `module`        | `str`/`ModuleType`                     | `"__vapoursynth__"`            | Module name for the script                                |
| `inline`        | `bool`                                 | `True`                         | Run on current thread (`True`) or worker thread (`False`) |
| `chdir`         | `PathLike`/`None`                      | `None`                         | Change working directory during execution                 |

### Environment Options

The `environment` parameter controls where the script runs:

```python
# 1. None - Use current VapourSynth environment
script = load_script("script.vpy")

# 2. Policy - Create a new environment (you must dispose it!)
script = load_script("script.vpy", policy)

# 3. ManagedEnvironment - Use an existing managed environment
with policy.new_environment() as env:
    script = load_script("script.vpy", env)

# 4. Script - Reuse another script's environment and module
script2 = load_code("clip.set_output(1)", script1)
```

---

## The Script Object

`load_script()` and `load_code()` return a `Script` object that controls execution.

### Execution Methods

#### `run()` → `Future[None]`

Start script execution, returning a Future:

```python
future = script.run()
# Do other work...
future.result()  # Wait for completion
```

#### `result()` → `None`

Run and block until complete:

```python
script.result()  # Blocks until done
```

#### `await script`

Scripts are awaitable in async contexts:

```python
async def main():
    script = load_script("script.vpy", policy)
    await script  # Wait for completion
```

### Accessing Results

#### `environment`

The environment where the script ran if a Policy or ManagedEnvironment was used:

```python
script.result()
outputs = script.environment.outputs  # {0: VideoOutputTuple, ...}
core = script.environment.core
```

#### `get_variable(name, default=...)`

Retrieve a variable from the script's module:

```python
script.result()
clip = script.get_variable("my_clip").result()
fps = script.get_variable("fps", 24).result()
```

### Cleanup

#### `dispose()`

Release the environment and clear the module:

```python
script.dispose()
```

> [!WARNING]
> Always call `dispose()` or use the context manager to avoid resource leaks.

---

## Context Manager Usage

The recommended pattern uses context managers for automatic cleanup:

```python
from vsengine.policy import GlobalStore, Policy
from vsengine.vpy import load_script

with Policy(GlobalStore()) as policy, load_script("script.vpy", policy) as script:
    # script.result() is called automatically on __enter__
    outputs = script.environment.outputs
    print(f"Found {len(outputs)} outputs")
    # script.dispose() is called automatically on __exit__
```

---

## Error Handling

When a script fails, an `ExecutionError` is raised wrapping the original exception:

```python
from vsengine.vpy import ExecutionError, load_code


script = load_code("raise ValueError('oops')", policy)
try:
    with script:
        pass
except ExecutionError as e:
    # Access the formatted traceback
    print(e)
finally:
    script.dispose()
```

---

## Threading Options

### Inline Execution (Default)

Scripts run on the calling thread:

```python
script = load_script("script.vpy", policy, inline=True)
script.result()  # Runs here, blocks until done
```

### Background Execution

Scripts run on a worker thread:

```python
script = load_script("script.vpy", policy, inline=False)
future = script.run()  # Returns immediately
# Do other work...
future.result()  # Wait when needed
```

---

## Working Directory

Use `chdir` to temporarily change the working directory:

```python
script = load_script(
    "script.vpy",
    policy,
    chdir="/path/to/script/directory"
)
```

> [!CAUTION]
> Changing the working directory is not safe when running multiple scripts concurrently.
