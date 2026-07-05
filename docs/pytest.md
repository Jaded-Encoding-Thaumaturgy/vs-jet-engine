# Pytest Integration

`vsengine.pytest` is a built-in pytest plugin that manages VapourSynth environments during test execution.
It isolates tests across configurable stages, automatically detects dangling core references, and requires zero boilerplate beyond a single marker.

## Installation

Install the plugin with the `[pytest]` extra:

```bash
pip install vsjetengine[pytest]
```

The plugin registers itself via entry-points (`pytest11`). No `conftest.py` changes are needed.

## Usage

Mark any test with `@pytest.mark.vpy` to opt in. Unmarked tests are never touched by the plugin.

```python
import pytest
import vapoursynth as vs

@pytest.mark.vpy
def test_blank_clip():
    clip = vs.core.std.BlankClip()
    assert clip.width == 640
```

By default this runs the test **twice**: once under `initial-core` and once under `reloaded-core`.

## The `@pytest.mark.vpy` Marker

```python
@pytest.mark.vpy                             # default: initial-core + reloaded-core
@pytest.mark.vpy("unique-core")              # only unique-core
@pytest.mark.vpy("unique-core", "no-core")   # unique-core then no-core
```

Pass one or more stage names as positional arguments to override which stages the test runs under.

## Stages

| Stage           | Environment Lifecycle                                                        | Use Case                                                                          |
| :-------------- | :--------------------------------------------------------------------------- | :-------------------------------------------------------------------------------- |
| `initial-core`  | A shared environment created once and kept alive for all tests in the stage. | Fast tests that don't alter global state.                                         |
| `reloaded-core` | A shared environment that is disposed and recreated before each test.        | Simulating script-reload behavior.                                                |
| `unique-core`   | A private environment created and disposed for each individual test.         | Tests that mutate core settings, register custom plugins, or need full isolation. |
| `no-core`       | No environment is active.                                                    | Testing error handling or manual environment initialization.                      |

Within a module, tests are sorted by stage in the order above (`no-core` → `initial-core` → `reloaded-core` → `unique-core`).

## Leak Detection

The plugin checks for dangling VapourSynth core references (objects kept alive after a test that should have been released).

### Shared Stages (`initial-core`, `reloaded-core`)

After all tests in a shared stage finish, the plugin appends a synthetic test named `@check-clean-environment[<stage>]`.
This test disposes the shared environment and queries the internal hospice system.
If any core reference is still alive, the check fails with:

```text
Your test suite left a dangling object to a vapoursynth core.
Please make sure this does not happen, as this might cause some previewers to crash after reloading a script.
```

### Isolated Stage (`unique-core`)

Each `unique-core` test is checked individually right after it finishes.
If dangling references are detected, the test itself is marked as failed (or an additional error is appended if the test already failed).
The same error message is shown.

## Fixtures

### `vpy_stage`

Returns the name of the current stage as a `str`. Only meaningful inside a `@pytest.mark.vpy` test.

```python
@pytest.mark.vpy
def test_stage_info(vpy_stage: str) -> None:
    print(f"Running in: {vpy_stage}")  # "initial-core" or "reloaded-core"
```

### `vpy_policy`

Returns the active environment Policy instance managed by the session.

```python
from vsengine.policy import Policy

@pytest.mark.vpy
def test_policy_info(vpy_policy: Policy) -> None:
    assert vpy_policy.is_registered
```

### `vpy_env_factory`

A factory function (`Callable[[], ManagedEnvironment]`) used to create fresh, isolated ManagedEnvironment instances.
Any environments created by this factory are automatically tracked and disposed of at the end of the test to prevent leaks.

This is particularly useful when testing lifecycle events or when running in the `no-core` stage.

```python
from collections.abc import Callable
from vsengine.policy import ManagedEnvironment

@pytest.mark.vpy("no-core")
def test_manual_creation(vpy_env_factory: Callable[[], ManagedEnvironment]) -> None:
    env = vpy_env_factory()
    with env.use():
        # Environment is now active
        ...
```

## Session Lifecycle

1. **Session start** — A `Policy` is created and registered with VapourSynth.
2. **Test execution** — The `pytest_runtest_call` hook sets up / tears down environments based on each test's stage.
3. **Session finish** — Any remaining environment is disposed and the policy is unregistered, restoring VapourSynth to its default state.
