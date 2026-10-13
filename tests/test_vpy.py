# vs-engine
# Copyright (C) 2022  cid-chan
# Copyright (C) 2025  Jaded-Encoding-Thaumaturgy
# This project is licensed under the EUPL-1.2
# SPDX-License-Identifier: EUPL-1.2
"""Tests for the vpy module (script loading and execution)."""

import ast
import contextlib
import gc
import inspect
import logging
import os
import sys
import textwrap
import threading
import types
import weakref
from collections.abc import Callable, Generator
from concurrent.futures import ThreadPoolExecutor, wait
from pathlib import Path
from typing import Any

import pytest
import vapoursynth

from tests._testutils import BLACKBOARD
from vsengine.adapters.asyncio import AsyncIOLoop
from vsengine.loops import NO_LOOP, set_loop
from vsengine.policy import ManagedEnvironment, Policy, ThreadLocalStore
from vsengine.vpy import (
    ExecutionError,
    Script,
    WrapAllErrors,
    _ModifiedArgv0,
    _ModifiedPath,
    chdir_runner,
    inline_runner,
    load_code,
    load_script,
)

DIR: str = os.path.dirname(__file__)
PATH: str = os.path.join(DIR, "fixtures", "test.vpy")


@pytest.fixture
def asyncio_loop() -> Generator[None]:
    set_loop(AsyncIOLoop())
    try:
        yield
    finally:
        set_loop(NO_LOOP)


@contextlib.contextmanager
def noop() -> Generator[None]:
    yield


class VpyTestError(Exception): ...


class Module(types.ModuleType):
    def __init__(self, name: str, inherit_globals: bool = False) -> None:
        super().__init__(name)
        if inherit_globals:
            self.__dict__.update(globals())


def as_code(func: Callable[[], Any]) -> types.CodeType:
    source = textwrap.dedent(inspect.getsource(func))
    func_node = ast.parse(source).body[0]
    assert isinstance(func_node, (ast.FunctionDef, ast.AsyncFunctionDef))

    # Wrap function body as a top-level Module
    mod_ast = ast.Module(body=func_node.body, type_ignores=[])
    ast.fix_missing_locations(mod_ast)
    return compile(mod_ast, filename=inspect.getfile(func), mode="exec")


def test_run_executes_successfully() -> None:
    @as_code
    def test_code() -> None:
        run = True  # noqa: F841

    with Policy() as p, p.new_environment() as env:
        s = load_code(test_code, env.vs_environment, module=Module("__test__"))
        s.run()

    assert s.get_variable("run", False).result() is True


def test_run_wraps_exception() -> None:
    @as_code
    def test_code() -> None:
        raise VpyTestError

    with Policy() as p, p.new_environment() as env:
        s = load_code(test_code, env.vs_environment, module=Module("__test__", inherit_globals=True))
        fut = s.run()

        exc = fut.exception()
        assert isinstance(exc, ExecutionError)
        assert isinstance(exc.parent_error, VpyTestError)


def test_execute_resolves_immediately() -> None:

    @as_code
    def test_code() -> None:
        run = True  # noqa: F841

    with Policy() as p, p.new_environment() as env:
        s = load_code(test_code, env.vs_environment, module=Module("__test__"))
        s.result()

    assert s.get_variable("run", False).result() is True


def test_execute_resolves_immediately_when_raising() -> None:
    @as_code
    def test_code() -> None:
        raise VpyTestError

    with Policy() as p, p.new_environment() as env:
        s = load_code(test_code, env.vs_environment, module=Module("__test__", inherit_globals=True))

        with pytest.raises(ExecutionError, check=lambda e: isinstance(e.parent_error, VpyTestError)):
            s.result()


@pytest.mark.asyncio
async def test_run_async(asyncio_loop: None) -> None:
    @as_code
    def test_code() -> None:
        run = True  # noqa: F841

    with Policy() as p, p.new_environment() as env:
        s = load_code(test_code, env, module=Module("__test__"))
        await s.run_async()

    assert await s.get_variable("run") is True


@pytest.mark.asyncio
async def test_await_directly(asyncio_loop: None) -> None:
    @as_code
    def test_code() -> None:
        run = True  # noqa: F841

    with Policy() as p, p.new_environment() as env:
        s = load_code(test_code, env, module=Module("__test__"))
        await s

    assert await s.get_variable("run") is True


def test_disposes_managed_environment() -> None:
    @as_code
    def test_code() -> None: ...

    with Policy() as p:
        env = p.new_environment()
        s = load_code(test_code, env, module=Module("__test__"))

        try:
            s.dispose()
        except Exception:
            env.dispose()
            raise


def test_disposing_context_manager_for_managed_environments() -> None:
    @as_code
    def test_code() -> None: ...

    with Policy() as p:
        env = p.new_environment()
        with load_code(test_code, env, module=Module("__test__")):
            ...

        try:
            assert env.disposed
        except Exception:
            env.dispose()
            raise


def test_chdir_changes_chdir() -> None:
    curdir: str | None = None

    def test_code() -> None:
        nonlocal curdir
        curdir = os.getcwd()

    wrapped = chdir_runner(DIR, inline_runner)
    wrapped(test_code)
    assert curdir == DIR


def test_chdir_changes_chdir_back() -> None:
    def test_code() -> None: ...

    wrapped = chdir_runner(DIR, inline_runner)

    before = os.getcwd()
    wrapped(test_code)
    assert os.getcwd() == before


def test_load_uses_current_environment() -> None:
    @as_code
    def test_code() -> None:
        import vapoursynth

        vpy_env = vapoursynth.get_current_environment()  # noqa: F841

    with Policy() as p, p.new_environment() as env, env.use():
        script = load_code(test_code, inline=False)
        script.result()
        assert script.get_variable("vpy_env").result() == env.vs_environment


def test_load_creates_new_environment() -> None:
    @as_code
    def test_code() -> None:
        import vapoursynth

        vpy_env = vapoursynth.get_current_environment()  # noqa: F841

    with Policy() as p, load_code(test_code, p, inline=True) as script:
        assert script.get_variable("vpy_env").result() == script.environment.vs_environment


def test_load_chains_script() -> None:
    @as_code
    def test_code_1() -> None:
        import sys

        module = sys.modules["__test__"]
        assert not hasattr(module, "test")
        module.__dict__["test"] = True

    @as_code
    def test_code_2() -> None:
        import sys

        assert sys.modules["__test__"].test is True

    mod = Module("__test__")

    with Policy() as p:
        script1 = load_code(test_code_1, p, module=mod, inline=True)
        env1 = script1.environment

        with env1:
            script1.result()
            script2 = load_code(test_code_2, env1, module=mod, inline=True)
            script2.result()


def test_load_with_custom_name() -> None:
    @as_code
    def test_code_1() -> None:
        assert __name__ == "__test_1__"

    @as_code
    def test_code_2() -> None:
        assert __name__ == "__test_2__"

    with Policy() as p:
        script = load_code(test_code_1, p, module="__test_1__")
        try:
            script.result()
        finally:
            script.dispose()

        script = load_code(test_code_2, p, module="__test_2__")
        try:
            script.result()
        finally:
            script.dispose()


def test_load_runs_chdir() -> None:
    @as_code
    def test_code() -> None:
        import os

        curdir = os.getcwd()  # noqa: F841

    with Policy() as p, p.new_environment() as env, env.use():
        previous = os.getcwd()
        script = load_code(test_code, chdir=DIR)
        script.result()
        assert script.get_variable("curdir").result() == DIR
        assert os.getcwd() == previous


def test_load_runs_in_thread_when_requested() -> None:

    @as_code
    def test_code() -> None:
        import threading

        thread = threading.current_thread()  # noqa: F841

    with Policy() as p, p.new_environment() as env, env.use():
        script = load_code(test_code, inline=False)
        script.result()
        assert script.get_variable("thread").result() is not threading.current_thread()


def test_load_runs_inline_by_default() -> None:
    @as_code
    def test_code() -> None:
        import threading

        thread = threading.current_thread()  # noqa: F841

    with Policy() as p, p.new_environment() as env, env.use():
        script = load_code(test_code, inline=True)
        script.result()
        assert script.get_variable("thread").result() is threading.current_thread()


def test_code_runs_string() -> None:
    code = textwrap.dedent("""
        from tests._testutils import BLACKBOARD
        BLACKBOARD["vpy_test_runs_raw_code_str"] = True
    """)

    with Policy() as p, p.new_environment() as env, env.use():
        load_code(code).result()
        assert BLACKBOARD.get("vpy_test_runs_raw_code_str") is True


def test_code_runs_bytes() -> None:
    code = textwrap.dedent("""
        # encoding: latin-1
        from tests._testutils import BLACKBOARD
        BLACKBOARD["vpy_test_runs_raw_code_bytes"] = True
    """).encode("latin-1")

    with Policy() as p, p.new_environment() as env, env.use():
        load_code(code).result()
        assert BLACKBOARD.get("vpy_test_runs_raw_code_bytes") is True


def test_code_runs_ast() -> None:
    code = ast.parse(
        textwrap.dedent("""
        from tests._testutils import BLACKBOARD
        BLACKBOARD["vpy_test_runs_raw_code_ast"] = True
    """)
    )

    with Policy() as p, p.new_environment() as env, env.use():
        load_code(code).result()
        assert BLACKBOARD.get("vpy_test_runs_raw_code_ast") is True


def test_script_runs() -> None:
    BLACKBOARD.clear()
    with Policy() as p, p.new_environment() as env, env.use():
        load_script(PATH).result()
        assert BLACKBOARD.get("vpy_run_script") is True


def test_script_runs_with_custom_name() -> None:
    BLACKBOARD.clear()
    with Policy() as p, p.new_environment() as env, env.use():
        load_script(PATH, module="__test__").result()
        assert BLACKBOARD.get("vpy_run_script_name") == "__test__"


def test_wrap_exceptions_wraps_exception() -> None:
    err = RuntimeError()

    with pytest.raises(ExecutionError, check=lambda e: e.parent_error is err), WrapAllErrors():
        raise err


@pytest.mark.parametrize("fail", [False, True])
def test_dispose_clears_future(fail: bool) -> None:
    code = "raise RuntimeError()" if fail else "pass"
    with Policy() as p:
        s = load_code(code, p)
        with contextlib.suppress(ExecutionError):
            s.result()

        assert hasattr(s, "_future")
        s.dispose()
        assert not hasattr(s, "_future")


def test_dispose_prevents_hospice_warning_on_error(caplog: pytest.LogCaptureFixture) -> None:
    @as_code
    def test_code() -> None:
        import vapoursynth

        leak = vapoursynth.core.std.BlankClip()  # noqa: F841
        raise VpyTestError

    with Policy() as p:
        script = load_code(test_code, p.new_environment(), module=Module("__test__", inherit_globals=True))
        with pytest.raises(ExecutionError):
            script.result()

        script.dispose()

        with caplog.at_level(logging.WARNING, logger="vsengine._hospice"):
            for _ in range(5):
                gc.collect()

        assert not any("Core is still in use" in r.message for r in caplog.records if "vsengine._hospice" in r.name)


@pytest.mark.skipif(vapoursynth.__version__ >= (78, 0), reason="VapourSynth R78 or later")
def test_hospice_warns_if_future_not_deleted(caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch) -> None:
    @as_code
    def test_code() -> None:
        import vapoursynth

        clip = vapoursynth.core.std.BlankClip()  # noqa: F841
        raise VpyTestError

    with Policy() as p:
        script = load_code(test_code, p.new_environment(), module=Module("__test__", inherit_globals=True))
        fut = script.run()
        with pytest.raises(ExecutionError):
            fut.result()

        def leaky_dispose() -> None:
            # We ONLY dispose the environment, skipping del self._future and module.clear()
            if isinstance(script.environment, ManagedEnvironment):
                script.environment.dispose()

        # Monkeypatch dispose to NOT delete the future and NOT clear module dict
        with monkeypatch.context():
            monkeypatch.setattr(script, "dispose", leaky_dispose)

            # Clear local ref to future. It should still be held by s._future
            del fut

            script.dispose()

            # Trigger hospice collection
            with caplog.at_level(logging.WARNING, logger="vsengine._hospice"):
                gc.collect()

            # There should be a warning from hospice now because s._future still exists
            # and is holding onto the traceback, which holds onto the module, which holds the core (via mod.clip)
            assert any("Core is still in use" in r.message for r in caplog.records)

        script.dispose()


def test_script_is_collectible_after_dispose() -> None:
    def run_and_dispose() -> weakref.ReferenceType[Script[Any]]:
        with Policy() as p:
            s = load_code("raise RuntimeError()", p)
            s.run()
            s_ref = weakref.ref(s)
            s.dispose()
            return s_ref

    s_ref = run_and_dispose()

    gc.collect()

    assert s_ref() is None


def test_modified_path() -> None:
    original_path = sys.path.copy()
    test_path = os.path.abspath("test_path_dummy")
    with _ModifiedPath(test_path):
        assert sys.path[0] == test_path
        assert test_path in sys.path
    assert sys.path == original_path
    assert test_path not in sys.path


def test_modified_argv0() -> None:
    original_argv0 = sys.argv[0]
    test_argv0 = "test_script_argv0.py"
    with _ModifiedArgv0(test_argv0):
        assert sys.argv[0] == test_argv0
    assert sys.argv[0] == original_argv0


def test_load_script_dunder_attributes(tmp_path: Path) -> None:
    script_path = tmp_path / "test_script.vpy"
    script_path.write_text(
        "from tests._testutils import BLACKBOARD; "
        "BLACKBOARD['script_dunders'] = {k: v for k, v in globals().items() if k.startswith('__')}"
    )

    BLACKBOARD.clear()

    with Policy() as p, p.new_environment() as env, env.use():
        load_script(script_path).result()

    dunders = BLACKBOARD["script_dunders"]
    assert dunders["__file__"] == os.path.abspath(os.path.normpath(str(script_path)))
    assert dunders["__name__"] == "__vapoursynth__"
    assert "__cached__" in dunders
    assert "__doc__" in dunders
    assert "__loader__" in dunders
    assert "__package__" in dunders
    assert "__spec__" in dunders


def test_load_code_dunder_attributes() -> None:
    code = (
        "from tests._testutils import BLACKBOARD; "
        "BLACKBOARD['code_dunders'] = {k: v for k, v in globals().items() if k.startswith('__')}"
    )

    BLACKBOARD.clear()
    filename = "test_code_attributes.py"
    # Create the file so resolve works as expected if it exists check is used
    Path(filename).touch()
    try:
        with Policy() as p, p.new_environment() as env, env.use():
            load_code(code, filename=filename).result()

        dunders = BLACKBOARD["code_dunders"]
        assert dunders["__file__"] == os.path.abspath(os.path.normpath(filename))
        assert dunders["__name__"] == "__vapoursynth__"
    finally:
        if os.path.exists(filename):
            os.remove(filename)


def test_load_script_adds_to_path(tmp_path: Path) -> None:
    # Create a submodule in a temp directory
    sub_dir = tmp_path / "my_module"
    sub_dir.mkdir()
    (sub_dir / "__init__.py").write_text("VAL = 42")

    script_path = tmp_path / "main_script.vpy"
    script_path.write_text(
        "from tests._testutils import BLACKBOARD; import my_module; BLACKBOARD['module_val'] = my_module.VAL"
    )

    BLACKBOARD.clear()

    with Policy() as p, p.new_environment() as env, env.use():
        load_script(script_path).result()

    assert BLACKBOARD["module_val"] == 42


def test_chdir_runner_on_error() -> None:
    def fail() -> None:
        raise ValueError("test_error")

    wrapped = chdir_runner(DIR, inline_runner)
    before = os.getcwd()
    fut = wrapped(fail)
    assert isinstance(fut.exception(), ValueError)
    assert os.getcwd() == before


def test_script_get_variable() -> None:
    with Policy() as p, load_code("val = 100", p) as s:
        assert s.get_variable("val").result() == 100
        # Test default value when not found
        assert s.get_variable("missing_val", 42).result() == 42
        # Test raising AttributeError when not found and no default
        with pytest.raises(AttributeError):
            s.get_variable("missing_val").result()


def test_load_code_with_script_as_environment() -> None:
    with (
        Policy() as p,
        load_code("val = 42", p) as s1,
        load_code("assert val == 42", s1),
    ):
        ...


def test_concurrent_script_run() -> None:
    @as_code
    def slow_exec() -> None:
        globals()["calls"] += 1

    mod = Module("__concurrent__")
    mod.__dict__["calls"] = 0

    with Policy() as p:
        script = load_code(slow_exec, p, module=mod, inline=False)
        try:
            with ThreadPoolExecutor() as ex:
                futs = [ex.submit(lambda: script.run().result()) for _ in range(16)]
                wait(futs)

            assert mod.__dict__["calls"] == 1
        finally:
            script.dispose()


def test_concurrent_script_execution() -> None:
    with Policy(ThreadLocalStore()) as p:

        def run_one(i: int) -> None:
            with load_code(f"x = {i}", p, inline=False, module=f"__concurrent_{i}__") as s:
                s.result()
                assert s.get_variable("x").result() == i

        with ThreadPoolExecutor(max_workers=4) as ex:
            futs = [ex.submit(run_one, i) for i in range(8)]
            for f in futs:
                f.result()
