# vs-engine
# Copyright (C) 2022  cid-chan
# Copyright (C) 2026  Jaded-Encoding-Thaumaturgy
# This project is licensed under the EUPL-1.2
# SPDX-License-Identifier: EUPL-1.2

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable, Collection, Generator, Mapping
from contextlib import ExitStack, nullcontext
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol, override, runtime_checkable

import pluggy
import pytest
import vapoursynth as vs

if TYPE_CHECKING:
    from _pytest._code.code import TerminalRepr, TracebackStyle
    from _pytest._io import TerminalWriter
    from _pytest.mark import ParameterSet
    from _pytest.python import CallSpec2
else:
    TerminalRepr = TracebackStyle = TerminalWriter = CallSpec2 = object


import vsengine._hospice as hospice
from vsengine.policy import GlobalStore, ManagedEnvironment, Policy

type PolicyFactory = Callable[[], Policy]
"""A callable without arguments that returns a new, unregistered Policy."""


@runtime_checkable
class _HasCallSpec(Protocol):
    callspec: CallSpec2


DEFAULT_STAGES = ("initial-core", "reloaded-core")
KNOWN_STAGES = ("no-policy", "no-core", "initial-core", "reloaded-core", "unique-core")
SHARED_STAGES = ("initial-core", "reloaded-core")
DEFAULT_POLICY_NAME = "default"
DEFAULT_ERROR_MESSAGE = (
    "Your test suite left a dangling object to a vapoursynth core.",
    "Please make sure this does not happen, as this might cause some previewers to crash after reloading a script.",
)

policy_key = pytest.StashKey[Policy | None]()
policy_group_key = pytest.StashKey[tuple[Path, str] | None]()
env_key = pytest.StashKey[ManagedEnvironment | None]()
item_env_key = pytest.StashKey[ManagedEnvironment]()
collect_env_stack_key = pytest.StashKey[ExitStack]()
stage_key = pytest.StashKey[str]()
leaked_key = pytest.StashKey[bool]()
failed_key = pytest.StashKey[bool]()


def default_policy() -> Policy:
    """
    The built-in policy factory.

    It creates a Policy backed by a GlobalStore.
    The ``--vpy-flags`` command line option only applies to policies created by this factory.
    Use it in ``pytest_vpy_policies`` to keep the default policy next to your own policies.
    """
    return Policy(GlobalStore())


class _VpyHookSpecs:
    @pytest.hookspec(firstresult=True)
    def pytest_vpy_policies(self, config: pytest.Config, item: pytest.Item) -> Mapping[str, PolicyFactory] | None:
        """
        Return the named policy factories for ``@pytest.mark.vpy`` tests.

        Each factory takes no arguments and returns a new, unregistered Policy.
        When a mapping is returned, every vpy test runs once per policy and stage.
        The ``no-policy`` stage does not run per policy.
        Use ``@pytest.mark.vpy(policies=[...])`` to select some of the names.

        The closest conftest that returns a mapping wins.
        If no mapping is returned, the tests use only the default policy.

        Args:
            config: The pytest config object.
            item: The test item. During collection, this is the function definition.
        """


def pytest_addhooks(pluginmanager: pytest.PytestPluginManager) -> None:
    # https://docs.pytest.org/en/stable/reference/reference.html#pytest.hookspec.pytest_addhooks
    pluginmanager.add_hookspecs(_VpyHookSpecs)


@pytest.fixture
def vpy_policy(request: pytest.FixtureRequest) -> Policy:
    """Returns the current VapourSynth environment policy."""
    return _vpy_policy_impl(request.session)


@pytest.fixture
def vpy_env(request: pytest.FixtureRequest) -> ManagedEnvironment | None:
    """Returns the ManagedEnvironment the test runs in or None in the no-core and no-policy stages."""
    if (env := request.node.stash.get(item_env_key, None)) is not None:
        return env
    return request.session.stash.get(env_key, None)


@pytest.fixture
def vpy_env_factory(request: pytest.FixtureRequest) -> Generator[Callable[[], ManagedEnvironment]]:
    """
    Returns a factory that creates new VapourSynth environments
    and automatically disposes of them at the end of the test.
    """
    yield from _vpy_env_factory_impl(request.session)


@pytest.fixture
def vpy_stage(request: pytest.FixtureRequest) -> str:
    """Returns the name of the current VapourSynth stage."""
    return request.session.stash.get(stage_key, "no-core")


@pytest.fixture
def vpy_policy_name(vpy_stage: str) -> str | None:
    """Returns the name of the policy the test runs with, or None in the no-policy stage."""
    return None if vpy_stage == "no-policy" else DEFAULT_POLICY_NAME


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--vpy-flags",
        action="store",
        type=int,
        default=None,
        help="Default creation flags for VapourSynth environments created by the default vsengine policy.",
    )


def pytest_configure(config: pytest.Config) -> None:
    # https://docs.pytest.org/en/stable/reference/reference.html#pytest.hookspec.pytest_configure
    config.addinivalue_line(
        "markers",
        'vpy(*stages: Literal["no-policy", "no-core", "initial-core", "reloaded-core", "unique-core"], '
        "flags_creation: int = ..., policies: Sequence[str] = ...): "
        "Mark what stages should be run, with optional core creation flags override "
        "and a selection of the policies returned by the pytest_vpy_policies hook. "
        "(Defaults to initial-core + reloaded-core)",
    )


def pytest_sessionstart(session: pytest.Session) -> None:
    # https://docs.pytest.org/en/stable/reference/reference.html#pytest.hookspec.pytest_sessionstart
    policy = None
    collect_stack = ExitStack()

    if not vs.has_policy():
        policy = _instantiate_policy(session.config, default_policy, DEFAULT_POLICY_NAME)
        policy.register()
        env = policy.new_environment()
        collect_stack.enter_context(env)
        collect_stack.enter_context(env.use())

    session.stash[policy_key] = policy
    session.stash[policy_group_key] = None
    session.stash[env_key] = None
    session.stash[collect_env_stack_key] = collect_stack
    session.stash[stage_key] = "no-core"


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    # https://docs.pytest.org/en/stable/reference/reference.html#pytest.hookspec.pytest_sessionfinish
    if (policy := session.stash.get(policy_key, None)) is None:
        return

    if not policy.is_registered:
        policy.register()

    if collect_stack := session.stash.get(collect_env_stack_key, None):
        collect_stack.close()

    if env := session.stash.get(env_key, None):
        env.dispose()
        session.stash[env_key] = None

    policy.unregister()
    session.stash[policy_key] = None
    session.stash[policy_group_key] = None


def pytest_generate_tests(metafunc: pytest.Metafunc) -> None:
    # https://docs.pytest.org/en/stable/reference/reference.html#pytest.hookspec.pytest_generate_tests
    if not (marker := metafunc.definition.get_closest_marker("vpy")):
        return

    if "vpy_stage" not in metafunc.fixturenames:
        metafunc.fixturenames.append("vpy_stage")
    stages = marker.args or DEFAULT_STAGES

    available = _get_policy_factories(metafunc.definition)
    names = _select_policy_names(marker.kwargs.get("policies"), available)

    # Without the hook, keep the test IDs unchanged.
    if available is None:
        metafunc.parametrize("vpy_stage", list(stages), ids=stages)
        return

    if "vpy_policy_name" not in metafunc.fixturenames:
        metafunc.fixturenames.append("vpy_policy_name")

    params: list[ParameterSet] = []
    for stage in stages:
        if stage == "no-policy":
            params.append(pytest.param(stage, None, id=stage))
        else:
            params.extend(pytest.param(stage, name, id=f"{stage}-{name}") for name in names)

    metafunc.parametrize(("vpy_stage", "vpy_policy_name"), params)


def pytest_collection_modifyitems(session: pytest.Session, config: pytest.Config, items: list[pytest.Item]) -> None:
    # https://docs.pytest.org/en/stable/reference/reference.html#pytest.hookspec.pytest_collection_modifyitems
    if config.option.collectonly:
        return

    # Clean up the collection environment
    if collect_stack := session.stash.get(collect_env_stack_key, None):
        collect_stack.close()

    policy = session.stash.get(policy_key, None)

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
        no_policy_items = list[pytest.Item]()
        # Policy name (None without the hook) -> stage -> items. Dicts keep the order in which names are seen.
        policy_groups = dict[str | None, dict[str, list[pytest.Item]]]()
        other_items = list[pytest.Item]()

        for item in m_items:
            callspec: CallSpec2 | None = getattr(item, "callspec", None)
            if not callspec or "vpy_stage" not in callspec.params:
                other_items.append(item)
                continue

            stage = str(callspec.params["vpy_stage"])
            if stage == "no-policy":
                no_policy_items.append(item)
            elif stage in KNOWN_STAGES:
                name = None if (n := callspec.params.get("vpy_policy_name")) is None else str(n)
                group = policy_groups.setdefault(name, {s: [] for s in KNOWN_STAGES if s != "no-policy"})
                group[stage].append(item)
            else:
                other_items.append(item)

        if not no_policy_items and not policy_groups:
            new_items.extend(m_items)
            continue

        # Find a suitable parent collector (the parent of the first vpy item, typically a Module)
        first_vpy_item = next(
            iter(no_policy_items or (item for group in policy_groups.values() for s in group for item in group[s]))
        )
        parent_collector = first_vpy_item.parent or session

        new_items.extend(no_policy_items)
        for name, group in policy_groups.items():
            for stage, stage_items in group.items():
                new_items.extend(stage_items)
                if stage in SHARED_STAGES and stage_items:
                    check_id = stage if name is None else f"{stage}-{name}"
                    new_items.append(
                        EnsureCleanEnvironment.from_parent(
                            parent_collector,
                            name=f"@check-clean-environment[{check_id}]",
                            stage=stage,
                            policy_name=name,
                            path=path,
                        )
                    )
        new_items.extend(other_items)

    items[:] = new_items


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_protocol(
    item: pytest.Item, nextitem: pytest.Item | None
) -> Generator[None, pluggy.Result[Any], None]:
    # https://docs.pytest.org/en/stable/reference/reference.html#pytest.hookspec.pytest_runtest_protocol
    callspec: CallSpec2 | None = getattr(item, "callspec", None)
    params = dict(callspec.params) if callspec else dict[str, object]()
    is_vpy_stage = "vpy_stage" in params
    marker = item.get_closest_marker("vpy")

    if not is_vpy_stage and marker is None:
        yield
        return

    stage_param = str(params["vpy_stage"]) if is_vpy_stage else "no-core"
    policy_name = params.get("vpy_policy_name")
    flags_creation = marker.kwargs.get("flags_creation", None) if marker else None

    # If it is a vpy test, make sure the right policy exists.
    # The no-policy stage unregisters whatever policy is active, so it does not need a specific one.
    if stage_param == "no-policy":
        if not (policy := item.session.stash.get(policy_key, None)):
            item.session.stash[policy_key] = policy = _instantiate_policy(
                item.session.config, default_policy, DEFAULT_POLICY_NAME
            )
    else:
        policy = _ensure_policy_group(item, None if policy_name is None else str(policy_name))

    env = item.session.stash.get(env_key, None)
    stage = item.session.stash.get(stage_key, "no-core")

    # Check flags_creation constraints for shared and core-less stages
    if stage_param != "unique-core" and flags_creation is not None and flags_creation != policy.flags_creation:
        raise ValueError(
            f"Cannot specify custom flags_creation={flags_creation} for stage '{stage_param}'. "
            f"Only 'unique-core' stage supports per-test creation flags overrides. "
            f"For shared stages, configure flags globally (e.g. via --vpy-flags) "
            f"or use a named policy from the pytest_vpy_policies hook."
        )

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
        env_unique = policy.new_environment(flags_creation=flags_creation)
        item.stash[item_env_key] = env_unique
        try:
            with env_unique.use():
                outcome = yield
        finally:
            del item.stash[item_env_key]
            env_unique.dispose()

        failed = outcome.excinfo[1] if outcome.excinfo else None
        if failed is None and item.stash.get(failed_key, False):
            failed = True

        if hospice.any_alive():
            hospice.freeze()
            item.stash[leaked_key] = True
            if failed is None:
                outcome.force_exception(AssertionError("Expected all environments to be cleaned up."))
        return

    # Case 2: Shared/Stateful run (initial-core, reloaded-core, no-core, or no-policy)
    if (
        stage_param == "reloaded-core"
        or stage_param != stage
        or (stage_param in SHARED_STAGES and (env is None or env.disposed))
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

    with env.use() if env else nullcontext():
        yield


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(
    item: pytest.Item, call: pytest.CallInfo[None]
) -> Generator[None, pluggy.Result[pytest.TestReport], None]:
    # https://docs.pytest.org/en/stable/reference/reference.html#pytest.hookspec.pytest_runtest_makereport
    outcome = yield
    report = outcome.get_result()
    if report.failed:
        item.stash[failed_key] = True
    if report.when == "call" and item.stash.get(leaked_key, False):
        err_message = "\n".join(DEFAULT_ERROR_MESSAGE)
        report.longrepr = CleanupFailed(report.longrepr, err_message)


def _instantiate_policy(config: pytest.Config, factory: PolicyFactory, name: str) -> Policy:
    policy = factory()

    if not isinstance(policy, Policy):
        raise TypeError(f"The factory of the vpy policy {name!r} must return a Policy, not {type(policy).__name__}.")
    if policy.is_registered:
        raise ValueError(
            f"The factory of the vpy policy {name!r} returned a registered Policy. "
            "Return an unregistered Policy: the vsengine pytest plugin registers it when needed."
        )

    # --vpy-flags only overrides the built-in default policy. Named policies keep their own flags.
    if factory is default_policy and (flags := config.getoption("--vpy-flags", default=None)) is not None:
        policy.flags_creation = flags

    return policy


def _get_policy_factories(item: pytest.Item) -> Mapping[str, PolicyFactory] | None:
    result: object = item.ihook.pytest_vpy_policies(config=item.config, item=item)

    if result is None:
        return None

    if not isinstance(result, Mapping) or not result:
        raise TypeError(
            f"pytest_vpy_policies must return a non-empty mapping of policy names to factories, not {result!r}."
        )

    factories = dict[str, PolicyFactory]()
    for name, factory in result.items():
        if not isinstance(name, str) or not name:
            raise TypeError(
                f"pytest_vpy_policies returned an invalid policy name: {name!r}. Names must be non-empty strings."
            )
        if not callable(factory):
            raise TypeError(f"pytest_vpy_policies returned a factory for {name!r} that is not callable: {factory!r}.")
        factories[name] = factory

    return factories


def _select_policy_names(selected: object, available: Mapping[str, PolicyFactory] | None) -> list[str]:
    known = list(available) if available is not None else [DEFAULT_POLICY_NAME]

    if selected is None:
        return known

    if isinstance(selected, str) or not isinstance(selected, Collection):
        raise TypeError(f"The 'policies' argument of the vpy marker must be a sequence of names, not {selected!r}.")
    if not selected:
        raise ValueError("The 'policies' argument of the vpy marker must not be empty.")

    if unknown := [name for name in selected if name not in known]:
        hint = "" if available is not None else " No pytest_vpy_policies hook returned policies for this test."
        raise ValueError(f"Unknown vpy policies {unknown!r}. Available policies: {known!r}.{hint}")

    return [name for name in known if name in selected]


def _ensure_policy_group(item: pytest.Item, policy_name: str | None) -> Policy:
    session = item.session
    group = None if policy_name is None else (item.path, policy_name)
    policy = session.stash.get(policy_key, None)

    if policy is not None and session.stash.get(policy_group_key, None) == group:
        return policy

    if policy is not None:
        if (env := session.stash.get(env_key, None)) is not None:
            env.dispose()
            session.stash[env_key] = None
        if policy.is_registered:
            policy.unregister()

    policy = _instantiate_policy(
        *(session.config, default_policy, DEFAULT_POLICY_NAME)
        if policy_name is None
        else (item.config, _resolve_policy_factory(item, policy_name), policy_name)
    )

    session.stash[policy_key] = policy
    session.stash[policy_group_key] = group
    session.stash[stage_key] = "no-core"
    return policy


def _resolve_policy_factory(item: pytest.Item, policy_name: str) -> PolicyFactory:
    if (available := _get_policy_factories(item)) is None:
        if policy_name == DEFAULT_POLICY_NAME:
            return default_policy
        raise ValueError(f"No pytest_vpy_policies hook returned policies for {item.nodeid}.")

    if (factory := available.get(policy_name)) is None:
        raise ValueError(
            f"The vpy policy {policy_name!r} is not returned by pytest_vpy_policies for {item.nodeid} anymore. "
            f"Available policies: {list(available)!r}."
        )

    return factory


def _vpy_policy_impl(session: pytest.Session | None = None) -> Policy:
    policy = session.stash.get(policy_key, None) if session else None

    if policy:
        return policy

    raise RuntimeError("No environment policy registered. Is the test marked with @pytest.mark.vpy?")


def _vpy_env_factory_impl(session: pytest.Session | None = None) -> Generator[Callable[[], ManagedEnvironment]]:
    envs = list[ManagedEnvironment]()

    def factory() -> ManagedEnvironment:
        policy = _vpy_policy_impl(session)
        env = policy.new_environment()
        envs.append(env)
        return env

    yield factory

    for env in envs:
        env.dispose()


class CleanupFailed(TerminalRepr):
    def __init__(self, previous: Any | None, next_text: str) -> None:
        self.previous = previous
        self.next_text = next_text

    @override
    def __repr__(self) -> str:
        return f"<CleanupFailed instance at {id(self):0x}>"

    @override
    def __str__(self) -> str:
        return self.next_text if self.previous is None else f"{self.previous}\n\n{self.next_text}"

    @override
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
    def __init__(self, *, stage: str, policy_name: str | None = None, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.stage = stage
        self.policy_name = policy_name

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

    @override
    def repr_failure(
        self,
        excinfo: pytest.ExceptionInfo[BaseException],
        style: TracebackStyle | None = None,
    ) -> CleanupFailed:
        return CleanupFailed(None, "\n".join(DEFAULT_ERROR_MESSAGE))

    @override
    def reportinfo(self) -> tuple[Path, None, str]:
        stage = self.stage if self.policy_name is None else f"{self.stage} ({self.policy_name})"
        return self.path, None, f"cleaning up: {stage}"
