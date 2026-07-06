# vs-engine
# Copyright (C) 2022  cid-chan
# Copyright (C) 2026  Jaded-Encoding-Thaumaturgy
# This project is licensed under the EUPL-1.2
# SPDX-License-Identifier: EUPL-1.2
from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable, Generator, Iterator
from contextlib import AbstractContextManager
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

import pluggy
import pytest
import vapoursynth as vs

if TYPE_CHECKING:
    from _pytest._code.code import TerminalRepr, TracebackStyle
    from _pytest._io import TerminalWriter
    from _pytest.python import CallSpec2
else:
    TerminalRepr = TracebackStyle = TerminalWriter = CallSpec2 = object


import vsengine._hospice as hospice
from vsengine.policy import GlobalStore, ManagedEnvironment, Policy


@runtime_checkable
class _HasCallSpec(Protocol):
    callspec: CallSpec2


DEFAULT_STAGES = ("initial-core", "reloaded-core")
KNOWN_STAGES = ("no-policy", "no-core", "initial-core", "reloaded-core", "unique-core")

DEFAULT_ERROR_MESSAGE = (
    "Your test suite left a dangling object to a vapoursynth core.",
    "Please make sure this does not happen, as this might cause some previewers to crash after reloading a script.",
)

policy_key = pytest.StashKey[Policy | None]()
env_key = pytest.StashKey[ManagedEnvironment | None]()
env_ctx_key = pytest.StashKey[AbstractContextManager[None] | None]()
stage_key = pytest.StashKey[str]()
leaked_key = pytest.StashKey[bool]()


@pytest.fixture
def vpy_policy(request: pytest.FixtureRequest) -> Policy:
    """Returns the current VapourSynth environment policy."""
    return _vpy_policy_impl(request.session)


@pytest.fixture
def vpy_env_factory(request: pytest.FixtureRequest) -> Iterator[Callable[[], ManagedEnvironment]]:
    """
    Returns a factory that creates new VapourSynth environments
    and automatically disposes of them at the end of the test.
    """
    yield from _vpy_env_factory_impl(request.session)


@pytest.fixture
def vpy_stage(request: pytest.FixtureRequest) -> str:
    """Returns the name of the current VapourSynth stage."""
    return request.session.stash.get(stage_key, "no-core")


def pytest_configure(config: pytest.Config) -> None:
    # https://docs.pytest.org/en/stable/reference/reference.html#pytest.hookspec.pytest_configure
    config.addinivalue_line(
        "markers",
        'vpy(*stages: Literal["no-policy", "no-core", "initial-core", "reloaded-core", "unique-core"]): '
        "Mark what stages should be run. (Defaults to initial-core + reloaded-core)",
    )


def pytest_sessionstart(session: pytest.Session) -> None:
    # https://docs.pytest.org/en/stable/reference/reference.html#pytest.hookspec.pytest_sessionstart
    policy = None
    env = None
    env_ctx = None

    if not vs.has_policy():
        policy = Policy(GlobalStore())
        policy.register()
        env = policy.new_environment()
        env_ctx = env.use()
        env_ctx.__enter__()

    session.stash[policy_key] = policy
    session.stash[env_key] = env
    session.stash[env_ctx_key] = env_ctx
    session.stash[stage_key] = "no-core"


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    # https://docs.pytest.org/en/stable/reference/reference.html#pytest.hookspec.pytest_sessionfinish
    if (policy := session.stash.get(policy_key, None)) is None:
        return

    if not policy.is_registered:
        policy.register()

    if env_ctx := session.stash.get(env_ctx_key, None):
        env_ctx.__exit__(None, None, None)
        session.stash[env_ctx_key] = None

    if env := session.stash.get(env_key, None):
        env.dispose()
        session.stash[env_key] = None

    policy.unregister()
    session.stash[policy_key] = None


def pytest_generate_tests(metafunc: pytest.Metafunc) -> None:
    # https://docs.pytest.org/en/stable/reference/reference.html#pytest.hookspec.pytest_generate_tests
    if marker := metafunc.definition.get_closest_marker("vpy"):
        if "vpy_stage" not in metafunc.fixturenames:
            metafunc.fixturenames.append("vpy_stage")
        stages = marker.args or DEFAULT_STAGES
        metafunc.parametrize("vpy_stage", list(stages), ids=stages)


def pytest_collection_modifyitems(session: pytest.Session, config: pytest.Config, items: list[pytest.Item]) -> None:
    # https://docs.pytest.org/en/stable/reference/reference.html#pytest.hookspec.pytest_collection_modifyitems
    if config.option.collectonly:
        return

    # Clean up the collection environment and context manager
    policy = session.stash.get(policy_key, None)
    env = session.stash.get(env_key, None)
    env_ctx = session.stash.get(env_ctx_key, None)

    if env_ctx:
        env_ctx.__exit__(None, None, None)
        session.stash[env_ctx_key] = None
    if env:
        env.dispose()
        session.stash[env_key] = None

    # Check if any collected item has the vpy marker or uses the vpy_stage parameter
    vpy_items_present = any(
        item.get_closest_marker("vpy") is not None
        or (isinstance(item, _HasCallSpec) and item.callspec and "vpy_stage" in item.callspec.params)
        for item in items
    )

    if not vpy_items_present:
        if policy:
            if policy.is_registered:
                policy.unregister()
            session.stash[policy_key] = None
        return

    # Group items by their file path to preserve module boundaries
    module_items = defaultdict[Path, list[pytest.Item]](list)
    for item in items:
        module_items[item.path].append(item)

    new_items = list[pytest.Item]()

    for path, m_items in module_items.items():
        stages = {stage: list[pytest.Item]() for stage in KNOWN_STAGES}
        other_items = list[pytest.Item]()

        for item in m_items:
            callspec: CallSpec2 | None = getattr(item, "callspec", None)
            if callspec and "vpy_stage" in callspec.params:
                if (s := str(callspec.params["vpy_stage"])) in stages:
                    stages[s].append(item)
                else:
                    other_items.append(item)
            else:
                other_items.append(item)

        if any(len(stages[s]) > 0 for s in stages):
            # Find a suitable parent collector (the parent of the first vpy item, typically a Module)
            first_vpy_item = next(item for stage in stages for item in stages[stage] if item)
            parent_collector = first_vpy_item.parent or session

            for s in KNOWN_STAGES:
                new_items.extend(stages[s])
                if s in ("initial-core", "reloaded-core") and len(stages[s]) > 0:
                    new_items.append(
                        EnsureCleanEnvironment.from_parent(
                            parent_collector,
                            name=f"@check-clean-environment[{s}]",
                            stage=s,
                            path=path,
                        )
                    )
            new_items.extend(other_items)
        else:
            new_items.extend(m_items)

    items[:] = new_items


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_protocol(
    item: pytest.Item, nextitem: pytest.Item | None
) -> Generator[None, pluggy.Result[Any], None]:
    # https://docs.pytest.org/en/stable/reference/reference.html#pytest.hookspec.pytest_runtest_protocol
    policy = item.session.stash.get(policy_key, None)

    callspec: CallSpec2 | None = getattr(item, "callspec", None)
    is_vpy_stage = callspec and "vpy_stage" in callspec.params

    if not is_vpy_stage and item.get_closest_marker("vpy") is None:
        yield
        return

    # If it is a vpy test, make sure policy is registered
    if policy is None:
        item.session.stash[policy_key] = policy = Policy(GlobalStore())

    env = item.session.stash.get(env_key, None)
    stage = item.session.stash.get(stage_key, "no-core")

    stage_param = str(callspec.params["vpy_stage"]) if is_vpy_stage and callspec else "no-core"

    # Ensure policy registration matches the stage
    if stage_param == "no-policy":
        if policy.is_registered:
            policy.unregister()
        if env:
            env.dispose()
            item.session.stash[env_key] = None
    elif not policy.is_registered:
        policy.register()
        # If we register the policy, make sure there is no stale env stored
        item.session.stash[env_key] = None

    # Case 1: Isolated run (no stage parameter or unique-core)
    if not is_vpy_stage or stage_param == "unique-core":
        env_unique = policy.new_environment()
        try:
            with env_unique.use():
                outcome = yield
        finally:
            env_unique.dispose()

        failed = outcome.excinfo[1] if outcome.excinfo else None
        if hospice.any_alive():
            hospice.freeze()
            item.stash[leaked_key] = True
            if failed is None:
                outcome.force_exception(AssertionError("Expected all environments to be cleaned up."))
        return

    # Case 2: Shared/Stateful run (initial-core, reloaded-core, no-core, or no-policy)
    if (
        stage_param != stage
        or (stage_param in ("initial-core", "reloaded-core") and (env is None or env.disposed))
        or (stage_param in ("no-core", "no-policy") and env)
    ):
        if stage_param == "initial-core":
            if env is None or env.disposed:
                env = policy.new_environment()
        elif stage_param == "reloaded-core":
            if env:
                env.dispose()
            env = policy.new_environment()
        elif stage_param in ("no-core", "no-policy"):
            if env:
                env.dispose()
            env = None

        item.session.stash[env_key] = env
        stage = stage_param
        item.session.stash[stage_key] = stage

    if env:
        with env.use():
            yield
    else:
        yield


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(
    item: pytest.Item, call: pytest.CallInfo[None]
) -> Generator[None, pluggy.Result[pytest.TestReport], None]:
    # https://docs.pytest.org/en/stable/reference/reference.html#pytest.hookspec.pytest_runtest_makereport
    outcome = yield
    report = outcome.get_result()
    if report.when == "call" and item.stash.get(leaked_key, False):
        err_message = "\n".join(DEFAULT_ERROR_MESSAGE)
        report.longrepr = CleanupFailed(report.longrepr, err_message)


def _vpy_policy_impl(session: pytest.Session | None = None) -> Policy:
    policy = session.stash.get(policy_key, None) if session else None

    if policy:
        return policy

    raise RuntimeError("No environment policy registered. Is the test marked with @pytest.mark.vpy?")


def _vpy_env_factory_impl(session: pytest.Session | None = None) -> Iterator[Callable[[], ManagedEnvironment]]:
    envs = list[ManagedEnvironment]()

    def factory() -> ManagedEnvironment:
        policy = _vpy_policy_impl(session)
        env = policy.new_environment()
        envs.append(env)
        return env

    yield factory

    for env in envs:
        if not env.disposed:
            env.dispose()


class CleanupFailed(TerminalRepr):
    def __init__(self, previous: Any | None, next_text: str) -> None:
        self.previous = previous
        self.next_text = next_text

    def __repr__(self) -> str:
        return f"<CleanupFailed instance at {id(self):0x}>"

    def __str__(self) -> str:
        return self.next_text if self.previous is None else f"{self.previous}\n\n{self.next_text}"

    def toterminal(self, tw: TerminalWriter) -> None:
        if self.previous is not None:
            if isinstance(self.previous, CleanupFailed):
                self.previous.toterminal(tw)
            else:
                tw.line(str(self.previous))
            tw.line("")
            tw.line("vs-engine has detected an additional problem with this test:", yellow=True, bold=True)
            indent = "  "
        else:
            indent = ""

        color = {"yellow": True} if self.previous is not None else {"red": True}
        for line in self.next_text.split("\n"):
            tw.line(indent + line, **color)


class EnsureCleanEnvironment(pytest.Item):
    def __init__(self, *, stage: str, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.stage = stage

    def runtest(self) -> None:
        policy = self.session.stash.get(policy_key, None)
        env = self.session.stash.get(env_key, None)
        if policy and not policy.is_registered:
            policy.register()
        if env:
            try:
                env.dispose()
            finally:
                self.session.stash[env_key] = None
        any_alive_left = hospice.any_alive()
        hospice.freeze()
        assert not any_alive_left, "Expected all environments to be cleaned up."

    def repr_failure(
        self,
        excinfo: pytest.ExceptionInfo[BaseException],
        style: TracebackStyle | None = None,
    ) -> CleanupFailed:
        return CleanupFailed(None, "\n".join(DEFAULT_ERROR_MESSAGE))

    def reportinfo(self) -> tuple[Path, None, str]:
        return self.path, None, f"cleaning up: {self.stage}"
