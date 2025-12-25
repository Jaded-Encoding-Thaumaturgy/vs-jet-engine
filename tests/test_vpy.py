# vs-engine
# Copyright (C) 2022  cid-chan
# Copyright (C) 2025  Jaded-Encoding-Thaumaturgy
# This project is licensed under the EUPL-1.2
# SPDX-License-Identifier: EUPL-1.2
"""Tests for the vpy module (script loading and execution)."""

import ast
import contextlib
import os
import textwrap
import threading
import types
from collections.abc import Callable, Iterator
from typing import Any

import pytest
import vapoursynth

from tests._testutils import BLACKBOARD
from vsengine.adapters.asyncio import AsyncIOLoop
from vsengine.loops import set_loop
from vsengine.policy import GlobalStore, Policy
from vsengine.vpy import (
    ExecutionError,
    Script,
    WrapAllErrors,
    _load,
    chdir_runner,
    inline_runner,
    load_code,
    load_script,
)

DIR: str = os.path.dirname(__file__)
PATH: str = os.path.join(DIR, "fixtures", "test.vpy")


@contextlib.contextmanager
def noop() -> Iterator[None]:
    yield


class TestError(Exception):
    pass


def callback_script(
    func: Callable[[types.ModuleType], None],
) -> Callable[[contextlib.AbstractContextManager[None], types.ModuleType], None]:
    def _script(ctx: contextlib.AbstractContextManager[None], module: types.ModuleType) -> None:
        with ctx:
            func(module)

    return _script


def test_run_executes_successfully() -> None:
    run = False

    @callback_script
    def test_code(_: types.ModuleType) -> None:
        nonlocal run
        run = True

    with Policy(GlobalStore()) as p, p.new_environment() as env:
        s = Script(test_code, types.ModuleType("__test__"), env.vs_environment, inline_runner)
        s.run()

    assert run


def test_run_wraps_exception() -> None:
    @callback_script
    def test_code(_: types.ModuleType) -> None:
        raise TestError()

    with Policy(GlobalStore()) as p, p.new_environment() as env:
        s = Script(test_code, types.ModuleType("__test__"), env.vs_environment, inline_runner)
        fut = s.run()

        exc = fut.exception()
        assert isinstance(exc, ExecutionError)
        assert isinstance(exc.parent_error, TestError)


def test_execute_resolves_immediately() -> None:
    run = False

    @callback_script
    def test_code(_: types.ModuleType) -> None:
        nonlocal run
        run = True

    with Policy(GlobalStore()) as p, p.new_environment() as env:
        s = Script(test_code, types.ModuleType("__test__"), env.vs_environment, inline_runner)
        s.result()

    assert run


def test_execute_resolves_to_script() -> None:
    @callback_script
    def test_code(_: types.ModuleType) -> None:
        pass

    with Policy(GlobalStore()) as p, p.new_environment() as env:
        s = Script(test_code, types.ModuleType("__test__"), env.vs_environment, inline_runner)
        s.result()


def test_execute_resolves_immediately_when_raising() -> None:
    @callback_script
    def test_code(_: types.ModuleType) -> None:
        raise TestError

    with Policy(GlobalStore()) as p, p.new_environment() as env:
        s = Script(test_code, types.ModuleType("__test__"), env.vs_environment, inline_runner)
        try:
            s.result()
        except ExecutionError as err:
            assert isinstance(err.parent_error, TestError)
        except Exception as e:
            pytest.fail(f"Wrong exception: {e!r}")
        else:
            pytest.fail("Test execution didn't fail properly.")


@pytest.mark.asyncio
async def test_run_async() -> None:
    set_loop(AsyncIOLoop())
    run = False

    @callback_script
    def test_code(_: types.ModuleType) -> None:
        nonlocal run
        run = True

    with Policy(GlobalStore()) as p, p.new_environment() as env:
        s = Script(test_code, types.ModuleType("__test__"), env.vs_environment, inline_runner)
        await s.run_async()

    assert run


@pytest.mark.asyncio
async def test_await_directly() -> None:
    set_loop(AsyncIOLoop())
    run = False

    @callback_script
    def test_code(_: types.ModuleType) -> None:
        nonlocal run
        run = True

    with Policy(GlobalStore()) as p, p.new_environment() as env:
        await Script(test_code, types.ModuleType("__test__"), env.vs_environment, inline_runner)

    assert run


def test_disposes_managed_environment() -> None:
    @callback_script
    def test_code(_: types.ModuleType) -> None:
        pass

    with Policy(GlobalStore()) as p:
        env = p.new_environment()
        s = Script(test_code, types.ModuleType("__test__"), env, inline_runner)

        try:
            s.dispose()
        except Exception:
            env.dispose()
            raise


def test_disposing_context_manager_for_managed_environments() -> None:
    @callback_script
    def test_code(_: types.ModuleType) -> None:
        pass

    with Policy(GlobalStore()) as p:
        env = p.new_environment()
        with Script(test_code, types.ModuleType("__test__"), env, inline_runner):
            pass

        try:
            assert env.disposed
        except Exception:
            env.dispose()
            raise


def test_chdir_changes_chdir() -> None:
    curdir: str | None = None

    @callback_script
    def test_code(_: types.ModuleType) -> None:
        nonlocal curdir
        curdir = os.getcwd()

    wrapped = chdir_runner(DIR, inline_runner)
    wrapped(test_code, noop(), 2)  # type: ignore[arg-type]
    assert curdir == DIR


def test_chdir_changes_chdir_back() -> None:
    @callback_script
    def test_code(_: types.ModuleType) -> None:
        pass

    wrapped = chdir_runner(DIR, inline_runner)

    before = os.getcwd()
    wrapped(test_code, noop(), None)  # type: ignore[arg-type]
    assert os.getcwd() == before


def test_load_uses_current_environment() -> None:
    vpy_env: Any = None

    @callback_script
    def test_code(_: types.ModuleType) -> None:
        nonlocal vpy_env
        vpy_env = vapoursynth.get_current_environment()

    with Policy(GlobalStore()) as p, p.new_environment() as env, env.use():
        _load(test_code, None, "__vapoursynth__", inline=False, chdir=None).result()
        assert vpy_env == env.vs_environment


def test_load_creates_new_environment() -> None:
    vpy_env: Any = None

    @callback_script
    def test_code(_: types.ModuleType) -> None:
        nonlocal vpy_env
        vpy_env = vapoursynth.get_current_environment()

    with Policy(GlobalStore()) as p:
        s = _load(test_code, p, "__vapoursynth__", inline=True, chdir=None)
        try:
            s.result()
            assert vpy_env == s.environment.vs_environment
        finally:
            s.dispose()


def test_load_chains_script() -> None:
    @callback_script
    def test_code_1(module: types.ModuleType) -> None:
        assert not hasattr(module, "test")
        module.test = True  # type: ignore[attr-defined]

    @callback_script
    def test_code_2(module: types.ModuleType) -> None:
        assert module.test is True

    with Policy(GlobalStore()) as p:
        script1 = _load(test_code_1, p, "__test_1__", inline=True, chdir=None)
        env = script1.environment
        try:
            script1.result()
            script2 = _load(test_code_2, script1, "__test_2__", inline=True, chdir=None)
            script2.result()
        finally:
            env.dispose()


def test_load_with_custom_name() -> None:
    @callback_script
    def test_code_1(module: types.ModuleType) -> None:
        assert module.__name__ == "__test_1__"

    @callback_script
    def test_code_2(module: types.ModuleType) -> None:
        assert module.__name__ == "__test_2__"

    with Policy(GlobalStore()) as p:
        try:
            script1 = _load(test_code_1, p, "__test_1__", inline=True, chdir=None)
            script1.result()
        finally:
            script1.dispose()  # pyright: ignore[reportPossiblyUnboundVariable]

        try:
            script2 = _load(test_code_2, p, "__test_2__", inline=True, chdir=None)
            script2.result()
        finally:
            script2.dispose()  # pyright: ignore[reportPossiblyUnboundVariable]


def test_load_runs_chdir() -> None:
    curdir: str | None = None

    @callback_script
    def test_code(_: types.ModuleType) -> None:
        nonlocal curdir
        curdir = os.getcwd()

    with Policy(GlobalStore()) as p, p.new_environment() as env, env.use():
        previous = os.getcwd()
        _load(test_code, None, "__vapoursynth__", inline=True, chdir=DIR).result()
        assert curdir == DIR
        assert os.getcwd() == previous


def test_load_runs_in_thread_when_requested() -> None:
    thread: threading.Thread | None = None

    @callback_script
    def test_code(_: types.ModuleType) -> None:
        nonlocal thread
        thread = threading.current_thread()

    with Policy(GlobalStore()) as p, p.new_environment() as env, env.use():
        _load(test_code, None, "__vapoursynth__", inline=False, chdir=None).result()
        assert thread is not threading.current_thread()


def test_load_runs_inline_by_default() -> None:
    thread: threading.Thread | None = None

    @callback_script
    def test_code(_: types.ModuleType) -> None:
        nonlocal thread
        thread = threading.current_thread()

    with Policy(GlobalStore()) as p, p.new_environment() as env, env.use():
        _load(test_code, None, "__vapoursynth__", True, chdir=None).result()
        assert thread is threading.current_thread()


def test_code_runs_string() -> None:
    code = textwrap.dedent("""
        from tests._testutils import BLACKBOARD
        BLACKBOARD["vpy_test_runs_raw_code_str"] = True
    """)

    with Policy(GlobalStore()) as p, p.new_environment() as env, env.use():
        load_code(code).result()
        assert BLACKBOARD.get("vpy_test_runs_raw_code_str") is True


def test_code_runs_bytes() -> None:
    code = textwrap.dedent("""
        # encoding: latin-1
        from tests._testutils import BLACKBOARD
        BLACKBOARD["vpy_test_runs_raw_code_bytes"] = True
    """).encode("latin-1")

    with Policy(GlobalStore()) as p, p.new_environment() as env, env.use():
        load_code(code).result()
        assert BLACKBOARD.get("vpy_test_runs_raw_code_bytes") is True


def test_code_runs_ast() -> None:
    code = ast.parse(
        textwrap.dedent("""
        from tests._testutils import BLACKBOARD
        BLACKBOARD["vpy_test_runs_raw_code_ast"] = True
    """)
    )

    with Policy(GlobalStore()) as p, p.new_environment() as env, env.use():
        load_code(code).result()
        assert BLACKBOARD.get("vpy_test_runs_raw_code_ast") is True


def test_script_runs() -> None:
    BLACKBOARD.clear()
    with Policy(GlobalStore()) as p, p.new_environment() as env, env.use():
        load_script(PATH).result()
        assert BLACKBOARD.get("vpy_run_script") is True


def test_script_runs_with_custom_name() -> None:
    BLACKBOARD.clear()
    with Policy(GlobalStore()) as p, p.new_environment() as env, env.use():
        load_script(PATH, module="__test__").result()
        assert BLACKBOARD.get("vpy_run_script_name") == "__test__"


def test_wrap_exceptions_wraps_exception() -> None:
    err = RuntimeError()
    try:
        with WrapAllErrors():
            raise err
    except ExecutionError as e:
        assert e.parent_error is err
    else:
        pytest.fail("Wrap all errors swallowed the exception")
