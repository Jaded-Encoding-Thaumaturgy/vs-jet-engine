# vs-engine
# Copyright (C) 2022  cid-chan
# Copyright (C) 2025  Jaded-Encoding-Thaumaturgy
# This project is licensed under the EUPL-1.2
# SPDX-License-Identifier: EUPL-1.2
"""This module provides functions to load and execute VapourSynth scripts (`.vpy` files) or inline code."""

from __future__ import annotations

import ast
import io
import os
import sys
from collections.abc import Awaitable, Buffer, Callable, Generator
from concurrent.futures import Future
from contextlib import AbstractContextManager
from types import CodeType, ModuleType, TracebackType
from typing import Any, Concatenate, Self, overload
from uuid import uuid4

import vapoursynth as vs

from ._futures import UnifiedFuture, unified
from .loops import make_awaitable, to_thread
from .policy import ManagedEnvironment, Policy

__all__ = ["ExecutionError", "Script", "load_code", "load_script"]

type Runner[R] = Callable[[Callable[[], R]], Future[R]]
type Executor[T] = Callable[[WrapAllErrors, ModuleType], T]


class ExecutionError(Exception):
    """
    Exception raised when script execution fails.
    """

    parent_error: BaseException
    """The actual exception that has been raised"""

    def __init__(self, parent_error: BaseException) -> None:
        """
        Initialize the ExecutionError exception.

        :param parent_error: The original exception that occurred.
        """
        import textwrap

        msg = textwrap.indent(self.extract_traceback(parent_error), "| ")
        super().__init__(f"An exception was raised while running the script.\n{msg}")
        self.parent_error = parent_error

    @staticmethod
    def extract_traceback(error: BaseException) -> str:
        """
        Extract and format the traceback from an exception.

        :param error: The exception to extract the traceback from.
        :return: A formatted string containing the traceback.
        """
        import traceback

        msg = traceback.format_exception(type(error), error, error.__traceback__)
        msg = "".join(msg)
        return msg


class WrapAllErrors(AbstractContextManager[None]):
    """
    Context manager that wraps exceptions in ExecutionError.
    """

    def __enter__(self) -> None: ...

    def __exit__(self, exc: type[BaseException] | None, val: BaseException | None, tb: TracebackType | None) -> None:
        if val is not None:
            raise ExecutionError(val) from None


class _TempModule(AbstractContextManager[None]):
    """
    Temporarily register a module in sys.modules.

    Ported from runpy.
    That ensures the module is available in sys.modules during execution and restored/cleaned up afterwards.
    """

    def __init__(self, mod_name: str, filename: str) -> None:
        self.mod_name = mod_name
        self.module = ModuleType(mod_name)
        self.module.__dict__["__file__"] = filename
        self._saved_module = list[ModuleType | None]()

    def __enter__(self) -> None:
        mod_name = self.mod_name

        self._saved_module.append(sys.modules.get(mod_name))

        sys.modules[mod_name] = self.module

    def __exit__(self, exc: type[BaseException] | None, val: BaseException | None, tb: TracebackType | None) -> None:
        mod = self._saved_module.pop()

        if mod:
            sys.modules[self.mod_name] = mod
        else:
            del sys.modules[self.mod_name]


def inline_runner[T](func: Callable[[], T]) -> Future[T]:
    """
    Runs a function inline and returns the result as a Future.

    :param func: The function to run.
    :return: A future containing the result or exception of the function.
    """
    fut = Future[T]()
    try:
        result = func()
    except BaseException as e:
        fut.set_exception(e)
    else:
        fut.set_result(result)
    return fut


def chdir_runner[**P, R](
    dir: str | os.PathLike[str], parent: Runner[R]
) -> Callable[Concatenate[Callable[P, R], P], Future[R]]:
    """
    Wraps a runner to change the current working directory during execution.

    :param dir: The directory to change to.
    :param parent: The runner to wrap.
    :return: A wrapped runner function.
    """

    def runner(func: Callable[P, R], *args: P.args, **kwargs: P.kwargs) -> Future[R]:
        def _wrapped() -> R:
            current = os.getcwd()
            os.chdir(dir)

            try:
                f = func(*args, **kwargs)
                return f
            except Exception:
                raise
            finally:
                os.chdir(current)

        return parent(_wrapped)

    return runner


_missing = object()


class Script[EnvT: (vs.Environment, ManagedEnvironment)](AbstractContextManager["Script[EnvT]"], Awaitable[None]):
    """VapourSynth script wrapper."""

    def __init__(self, executor: Executor[None], module: ModuleType, environment: EnvT, runner: Runner[None]) -> None:
        self.executor = executor
        self.environment: EnvT = environment
        self.runner = runner
        self.module = module

    def __enter__(self) -> Self:
        self.result()
        return self

    def __exit__(self, exc: type[BaseException] | None, val: BaseException | None, tb: TracebackType | None) -> None:
        self.dispose()

    def __await__(self) -> Generator[Any, None, None]:
        """
        Runs the script and waits until the script has completed.
        """
        return self.run_async().__await__()

    def run(self) -> Future[None]:
        """
        Runs the script.

        It returns a future which completes when the script completes.
        When the script fails, it raises a ExecutionError.
        """
        self._future: Future[None]

        if hasattr(self, "_future"):
            return self._future

        self._future = self.runner(self._run_inline)

        return self._future

    async def run_async(self) -> None:
        """
        Runs the script asynchronously, but it returns a coroutine.
        """
        return await make_awaitable(self.run())

    def result(self) -> None:
        """
        Runs the script and blocks until the script has finished running.
        """
        return self.run().result()

    def dispose(self) -> None:
        """Disposes the managed environment and clears the module globals."""
        self.module.__dict__.clear()

        if isinstance(self.environment, ManagedEnvironment):
            self.environment.dispose()

    @overload
    @unified(kind="future")
    def get_variable(self, name: str) -> Future[Any]: ...
    @overload
    @unified(kind="future")
    def get_variable[T](self, name: str, default: T) -> Future[Any | T]: ...
    @unified(kind="future")
    def get_variable(self, name: str, default: Any = _missing) -> Future[Any]:
        """
        Retrieve a variable from the script's module.

        :param name: The name of the variable to retrieve.
        :param default: The default value if the variable is not found.
        :return: A future that resolves to the variable's value.
        """
        return UnifiedFuture[Any].resolve(
            getattr(self.module, name) if default is _missing else getattr(self.module, name, default)
        )

    def _run_inline(self) -> None:
        with self.environment.use():
            self.executor(WrapAllErrors(), self.module)


@overload
def load_script(
    script: str | os.PathLike[str],
    environment: vs.Environment | None = None,
    *,
    module: str | ModuleType = "__vapoursynth__",
    inline: bool = True,
    chdir: str | os.PathLike[str] | None = None,
) -> Script[vs.Environment]: ...


@overload
def load_script(
    script: str | os.PathLike[str],
    environment: Script[vs.Environment],
    *,
    inline: bool = True,
    chdir: str | os.PathLike[str] | None = None,
) -> Script[vs.Environment]: ...


@overload
def load_script(
    script: str | os.PathLike[str],
    environment: Policy | ManagedEnvironment,
    *,
    module: str | ModuleType = "__vapoursynth__",
    inline: bool = True,
    chdir: str | os.PathLike[str] | None = None,
) -> Script[ManagedEnvironment]: ...


@overload
def load_script(
    script: str | os.PathLike[str],
    environment: Script[ManagedEnvironment],
    *,
    inline: bool = True,
    chdir: str | os.PathLike[str] | None = None,
) -> Script[ManagedEnvironment]: ...


def load_script(
    script: str | os.PathLike[str],
    environment: Policy | vs.Environment | ManagedEnvironment | Script[Any] | None = None,
    *,
    module: str | ModuleType = "__vapoursynth__",
    inline: bool = True,
    chdir: str | os.PathLike[str] | None = None,
) -> Script[Any]:
    """
    Runs the script at the given path.

    :param script: The path to the script file to run.
    :param environment: Defines the environment in which the code should run.
                        If passed a Policy, it will create a new environment from the policy,
                        which can be accessed using the environment attribute.
    :param module: The name the module should get. Defaults to __vapoursynth__.
    :param inline: Run the code inline, e.g. not in a separate thread.
    :param chdir: Change the currently running directory while the script is running.
                  This is unsafe when running multiple scripts at once.
    :returns: A script object. The script starts running when you call run() on it, or await it.
    """

    def _execute(ctx: WrapAllErrors, module: ModuleType) -> None:
        nonlocal script

        script = str(script)

        with ctx, io.open_code(script) as f, _TempModule(module.__name__, script):
            exec(
                compile(f.read(), filename=script, dont_inherit=True, flags=0, mode="exec"),
                module.__dict__,
                module.__dict__,
            )

    return _load(_execute, environment, module, inline, chdir)


@overload
def load_code(
    script: str | Buffer | ast.Module | CodeType,
    environment: vs.Environment | None = None,
    *,
    module: str | ModuleType = "__vapoursynth__",
    inline: bool = True,
    chdir: str | os.PathLike[str] | None = None,
    **kwargs: Any,
) -> Script[vs.Environment]: ...


@overload
def load_code(
    script: str | Buffer | ast.Module | CodeType,
    environment: Script[vs.Environment],
    *,
    inline: bool = True,
    chdir: str | os.PathLike[str] | None = None,
    **kwargs: Any,
) -> Script[vs.Environment]: ...


@overload
def load_code(
    script: str | Buffer | ast.Module | CodeType,
    environment: Policy | ManagedEnvironment,
    *,
    module: str | ModuleType = "__vapoursynth__",
    inline: bool = True,
    chdir: str | os.PathLike[str] | None = None,
    **kwargs: Any,
) -> Script[ManagedEnvironment]: ...


@overload
def load_code(
    script: str | Buffer | ast.Module | CodeType,
    environment: Script[ManagedEnvironment],
    *,
    inline: bool = True,
    chdir: str | os.PathLike[str] | None = None,
    **kwargs: Any,
) -> Script[ManagedEnvironment]: ...


def load_code(
    script: str | Buffer | ast.Module | CodeType,
    environment: Policy | vs.Environment | ManagedEnvironment | Script[Any] | None = None,
    *,
    module: str | ModuleType = "__vapoursynth__",
    inline: bool = True,
    chdir: str | os.PathLike[str] | None = None,
    **kwargs: Any,
) -> Script[Any]:
    """
    Runs the given code snippet.

    :param script: The code to run. Can be a string, bytes, AST, or compiled code.
    :param environment: Defines the environment in which the code should run. If passed a Policy,
                        it will create a new environment from the policy,
                        which can be accessed using the environment attribute.
                        If the environment is another Script, it will take the environment and module of the script.
    :param module: The name the module should get. Defaults to __vapoursynth__.
    :param inline: Run the code inline, e.g. not in a separate thread.
    :param chdir: Change the currently running directory while the script is running.
                  This is unsafe when running multiple scripts at once.
    :returns: A script object. The script starts running when you call run() on it, or await it.
    """

    def _execute(ctx: WrapAllErrors, module: ModuleType) -> None:
        nonlocal script, kwargs

        filename = kwargs.pop("filename", f"<runvpy {uuid4().hex[:8]}>")

        with ctx, _TempModule(module.__name__, filename):
            if isinstance(script, CodeType):
                code = script
            else:
                compile_args: dict[str, Any] = {
                    "filename": filename,
                    "dont_inherit": True,
                    "flags": 0,
                    "mode": "exec",
                } | kwargs
                code = compile(script, **compile_args)

            exec(code, module.__dict__, module.__dict__)

    return _load(_execute, environment, module, inline, chdir)


def _load(
    executor: Executor[None],
    environment: Policy
    | vs.Environment
    | ManagedEnvironment
    | Script[vs.Environment]
    | Script[ManagedEnvironment]
    | None,
    module: str | ModuleType,
    inline: bool,
    chdir: str | os.PathLike[str] | None,
) -> Script[Any]:
    runner = inline_runner if inline else to_thread

    if chdir is not None:
        runner = chdir_runner(chdir, runner)

    if isinstance(environment, Script):
        module = environment.module
        environment = environment.environment
    elif isinstance(module, str):
        module = ModuleType(module)

    if environment is None:
        environment = vs.get_current_environment()
    elif isinstance(environment, vs.Environment):
        return Script(executor, module, environment, runner)
    elif isinstance(environment, Policy):
        environment = environment.new_environment()

    return Script[Any](executor, module, environment, runner)
