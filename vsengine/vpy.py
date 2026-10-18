# vs-engine
# Copyright (C) 2022  cid-chan
# Copyright (C) 2025  Jaded-Encoding-Thaumaturgy
# This project is licensed under the EUPL-1.2
# SPDX-License-Identifier: EUPL-1.2
"""This module provides functions to load and execute VapourSynth scripts (`.vpy` files) or inline code."""

from __future__ import annotations

__lazy_modules__ = ["traceback"]

import ast
import io
import os
import sys
import textwrap
import threading
import traceback
from collections.abc import Awaitable, Buffer, Callable, Generator
from concurrent.futures import Future
from contextlib import AbstractContextManager, chdir, nullcontext, suppress
from importlib.machinery import ModuleSpec, SourceFileLoader
from pathlib import Path
from types import CodeType, ModuleType, TracebackType
from typing import Any, Self, overload
from uuid import uuid4

import vapoursynth as vs

from .futures import UnifiedFuture, unified
from .loops import make_awaitable, to_thread
from .policy import ManagedEnvironment, Policy

__all__ = ["ExecutionError", "Script", "load_code", "load_script"]

type Runner[R] = Callable[[Callable[[], R]], Future[R]]


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
        return "".join(traceback.format_exception(type(error), error, error.__traceback__))


class WrapAllErrors(AbstractContextManager[None]):
    """
    Context manager that wraps exceptions in ExecutionError.
    """

    def __enter__(self) -> None: ...

    def __exit__(self, exc: type[BaseException] | None, val: BaseException | None, tb: TracebackType | None) -> None:
        if isinstance(val, ExecutionError):
            raise val
        if val is not None:
            raise ExecutionError(val) from None


class _TempModule(AbstractContextManager[None]):
    """
    Temporarily register a module in sys.modules.

    Ported from runpy.
    That ensures the module is available in sys.modules during execution and restored/cleaned up afterwards.
    """

    def __init__(self, mod_name: str, module: ModuleType | None = None) -> None:
        self.mod_name = mod_name
        self.module = module or ModuleType(mod_name)
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


class _ModifiedPath(AbstractContextManager[None]):
    """
    Temporarily add a path to sys.path.

    Ported from runpy.
    """

    def __init__(self, path: str | os.PathLike[str]) -> None:
        self.path = os.path.abspath(path)

    def __enter__(self) -> None:
        sys.path.insert(0, self.path)

    def __exit__(self, exc: type[BaseException] | None, val: BaseException | None, tb: TracebackType | None) -> None:
        with suppress(ValueError):
            sys.path.remove(self.path)


class _ModifiedArgv0(AbstractContextManager[None]):
    """
    Temporarily modify sys.argv[0].

    Ported from runpy.
    """

    def __init__(self, value: Any) -> None:
        self.value = str(value)
        self._saved_value = ""

    def __enter__(self) -> None:
        self._saved_value = sys.argv[0]
        sys.argv[0] = self.value

    def __exit__(self, exc: type[BaseException] | None, val: BaseException | None, tb: TracebackType | None) -> None:
        sys.argv[0] = self._saved_value


def _set_module_dunders(module: ModuleType, filename: str) -> None:
    dunders: dict[str, Any] = {
        "__name__": module.__name__,
        "__file__": filename,
        "__cached__": None,
        "__doc__": getattr(module, "__doc__", None),
        "__package__": getattr(module, "__package__", None),
    }

    if os.path.isfile(filename):
        loader = SourceFileLoader(module.__name__, filename)
        spec = ModuleSpec(module.__name__, loader, origin=filename)
        dunders["__spec__"] = spec
        dunders["__loader__"] = loader
        dunders["__cached__"] = spec.cached
    else:
        # Strip the default None values added by ModuleType
        if module.__dict__.get("__spec__") is None:
            module.__dict__.pop("__spec__", None)
        if module.__dict__.get("__loader__") is None:
            module.__dict__.pop("__loader__", None)

    for key, value in dunders.items():
        if module.__dict__.get(key) is None:
            module.__dict__[key] = value
        else:
            module.__dict__.setdefault(key, value)


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


_missing = object()


class Script[EnvT: (vs.Environment, ManagedEnvironment)](AbstractContextManager["Script[EnvT]"], Awaitable[None]):
    """VapourSynth script wrapper."""

    def __init__(
        self,
        code: CodeType,
        environment: EnvT,
        module: ModuleType,
        filename: str,
        *,
        inline: bool = True,
        chdir: str | os.PathLike[str] | None = None,
        path: str | os.PathLike[str] | None = None,
    ) -> None:
        self.code = code
        self.module = module
        self.environment = environment
        self.filename = filename
        self.inline = inline
        self.chdir = chdir
        self.path = Path(path).absolute() if path is not None else None
        self._lock = threading.Lock()

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
        with self._lock:
            if hasattr(self, "_future"):
                return self._future

            if self.inline:
                self._future = inline_runner(self._run_inline)
            else:
                self._future = to_thread(self._run_inline)

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
        with self._lock:
            self._del_future_refs()
            self.module.__dict__.clear()

        if isinstance(self.environment, ManagedEnvironment):
            self.environment.dispose()

    @overload
    @unified(kind="future")
    def get_variable(self, name: str) -> UnifiedFuture[Any]: ...
    @overload
    @unified(kind="future")
    def get_variable[T](self, name: str, default: T) -> UnifiedFuture[Any | T]: ...
    @unified(kind="future")
    def get_variable(self, name: str, default: Any = _missing) -> UnifiedFuture[Any]:
        """
        Retrieve a variable from the script's module.

        :param name: The name of the variable to retrieve.
        :param default: The default value if the variable is not found.
        :return: A future that resolves to the variable's value.
        """

        def lookup(_: Future[None] | None = None) -> Any:
            return getattr(self.module, name) if default is _missing else getattr(self.module, name, default)

        with self._lock:
            if hasattr(self, "_future") and not self._future.done():
                return UnifiedFuture.from_future(self._future).map(lookup)

        return UnifiedFuture.from_call(lookup)

    def _run_inline(self) -> None:
        with (
            WrapAllErrors(),
            self.environment.use(),
            chdir(self.chdir) if self.chdir is not None else nullcontext(),
            _TempModule(self.module.__name__, self.module),
            _ModifiedPath(self.path) if self.path is not None else nullcontext(),
            _ModifiedArgv0(self.filename) if (self.filename is not None and self.path is not None) else nullcontext(),
        ):
            _set_module_dunders(self.module, self.filename)

            exec(self.code, self.module.__dict__, self.module.__dict__)  # noqa: S102

    def _del_future_refs(self) -> None:
        if (fut := getattr(self, "_future", None)) is not None:
            if fut.done() and not fut.cancelled() and (exc := fut.exception()):
                exc.__traceback__ = None
                if isinstance(exc, ExecutionError) and (parent := exc.parent_error):
                    parent.__traceback__ = None
            with suppress(AttributeError):
                del self._future


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

    Note:
       Executing scripts modifies process-global interpreter state (`sys.modules`, `sys.path`, and `sys.argv[0]`).
       When running multiple scripts concurrently across threads, ensure each script uses a distinct `module` name,
       use a thread-isolated environment store (`ThreadLocalStore` or `ContextVarStore`), and avoid using `chdir`.

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
    rscript = os.path.abspath(os.path.normpath(str(script)))

    with io.open_code(rscript) as f:
        code = compile(f.read(), filename=rscript, dont_inherit=True, flags=0, mode="exec")

    return _load(
        code=code,
        environment=environment,
        filename=rscript,
        module=module,
        inline=inline,
        chdir=chdir,
        path=os.path.dirname(rscript),
    )


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

    Note:
       Executing scripts modifies process-global interpreter state (`sys.modules`, `sys.path`, and `sys.argv[0]`).
       When running multiple scripts concurrently across threads, ensure each script uses a distinct `module` name,
       use a thread-isolated environment store (`ThreadLocalStore` or `ContextVarStore`), and avoid using `chdir`.

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
    :kwargs: Arguments to pass to compile().
    """
    filename = kwargs.pop("filename", None)
    if filename is not None:
        filename = os.path.abspath(os.path.normpath(str(filename)))
    else:
        filename = f"<runvpy {uuid4().hex[:8]}>"

    path = os.path.dirname(filename) if os.path.exists(filename) else None

    nkwargs: dict[str, Any] = {"filename": filename, "dont_inherit": True, "flags": 0, "mode": "exec"} | kwargs
    code = compile(script, **nkwargs) if not isinstance(script, CodeType) else script

    return _load(
        code=code,
        environment=environment,
        filename=filename,
        module=module,
        inline=inline,
        chdir=chdir,
        path=path,
    )


def _load(
    code: CodeType,
    environment: Policy
    | vs.Environment
    | ManagedEnvironment
    | Script[vs.Environment]
    | Script[ManagedEnvironment]
    | None,
    filename: str,
    module: str | ModuleType,
    inline: bool = True,
    chdir: str | os.PathLike[str] | None = None,
    *,
    path: str | os.PathLike[str] | None = None,
) -> Script[Any]:
    if isinstance(environment, Script):
        module = environment.module
        environment = environment.environment
    elif isinstance(module, str):
        module = ModuleType(module)

    if environment is None:
        environment = vs.get_current_environment()
    elif isinstance(environment, Policy):
        environment = environment.new_environment()

    return Script[Any](
        code=code,
        module=module,
        environment=environment,
        filename=filename,
        inline=inline,
        chdir=chdir,
        path=path,
    )
