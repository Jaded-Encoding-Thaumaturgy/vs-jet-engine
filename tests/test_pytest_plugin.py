# vs-engine
# Copyright (C) 2026  Jaded-Encoding-Thaumaturgy
# This project is licensed under the EUPL-1.2
# SPDX-License-Identifier: EUPL-1.2

# pyrefly: ignore-errors [bad-argument-type]

from __future__ import annotations

import importlib
from collections.abc import Callable, Generator
from contextlib import ExitStack
from pathlib import Path
from types import TracebackType
from typing import Any, Self, cast, override

import pytest
import vapoursynth

import vsengine._hospice as hospice
import vsengine.pytest as vpy_pytest
from tests._testutils import forcefully_unregister_policy
from vsengine.policy import GlobalStore, ManagedEnvironment, Policy

pytest_plugins = ["pytester"]


# Shadow to avoid conflicts with other potential uses of Policy in the codebase.
@pytest.fixture(autouse=True)
def clean_policy() -> None: ...


@pytest.fixture(autouse=True)
def reset_hospice_state() -> None: ...


@pytest.fixture
def restore_pytest_globals(request: pytest.FixtureRequest) -> Generator[None]:
    session = request.session
    orig_policy = session.stash.get(vpy_pytest.policy_key, None)
    orig_group = session.stash.get(vpy_pytest.policy_group_key, None)
    orig_env = session.stash.get(vpy_pytest.env_key, None)
    orig_stage = session.stash.get(vpy_pytest.stage_key, "no-core")

    try:
        yield
    finally:
        session.stash[vpy_pytest.policy_key] = orig_policy
        session.stash[vpy_pytest.policy_group_key] = orig_group
        session.stash[vpy_pytest.env_key] = orig_env
        session.stash[vpy_pytest.stage_key] = orig_stage


# All mock objects
class MockConfig:
    def __init__(self, collectonly: bool = False) -> None:
        self.added_lines = list[tuple[str, str]]()
        self.option = MockOption(collectonly)

    def addinivalue_line(self, name: str, value: str) -> None:
        self.added_lines.append((name, value))

    def getoption(self, name: str, default: Any, skip: bool = False) -> Any | None:
        try:
            return getattr(self.option, name)
        except AttributeError:
            return default


class MockOption:
    def __init__(self, collectonly: bool) -> None:
        self.collectonly = collectonly


class MockSession:
    def __init__(self) -> None:
        self.stash = pytest.Stash()
        self.config = MockConfig()


class MockCallspec:
    def __init__(self, **params: Any) -> None:
        self.params = params


class MockMarker:
    def __init__(self, args: tuple[Any, ...] | None = None, kwargs: dict[str, Any] | None = None) -> None:
        self.args = args or ()
        self.kwargs = kwargs or {}


class MockHook:
    def __init__(self, policies: Any = None) -> None:
        self.policies = policies

    def pytest_vpy_policies(self, config: Any, item: Any) -> Any:
        return self.policies


class MockDefinition:
    def __init__(self, marker: MockMarker | None, policies: Any = None) -> None:
        self.marker = marker
        self.config = MockConfig()
        self.ihook = MockHook(policies)
        self.nodeid = "test.py::test_dummy"

    def get_closest_marker(self, name: str) -> MockMarker | None:
        return self.marker


class MockMetafunc:
    def __init__(self, marker: MockMarker | None, policies: Any = None) -> None:
        self.definition = MockDefinition(marker, policies)
        self.fixturenames = list[str]()
        self.parametrized = list[tuple[Any, list[Any], Any]]()

    def parametrize(self, argnames: Any, argvalues: list[Any], ids: Any = None) -> None:
        self.parametrized.append((argnames, argvalues, ids))


class MockItem:
    def __init__(
        self,
        session: pytest.Session | None = None,
        callspec: MockCallspec | None = None,
        marker: MockMarker | bool = False,
        path: Path | None = None,
        parent: Any = None,
        leaked: bool = False,
    ) -> None:
        self.session = session
        if callspec is not None:
            self.callspec = callspec
        self.marker = marker
        self.path = path or Path("test.py")
        self.parent = parent
        self.stash = pytest.Stash()
        if leaked:
            self.stash[vpy_pytest.leaked_key] = True

    def get_closest_marker(self, name: str) -> MockMarker | None:
        return (
            self.marker
            if isinstance(self.marker, MockMarker)
            else MockMarker()
            if name == "vpy" and self.marker
            else None
        )


class MockReport:
    def __init__(self, when: str, failed: bool = False) -> None:
        self.when = when
        self.failed = failed
        self.longrepr = "original longrepr"


class DummyOutcome:
    def __init__(self, report: MockReport | None = None) -> None:
        self.report = report
        self.excinfo: tuple[type[BaseException], BaseException, TracebackType | None] | None = None
        self.forced_exception: Exception | None = None

    def force_exception(self, exc: Exception) -> None:
        self.forced_exception = exc

    def get_result(self) -> MockReport:
        if self.report:
            return self.report
        raise AssertionError


class MockTerminalWriter:
    def __init__(self) -> None:
        self.lines = list[tuple[str, dict[str, Any]]]()

    def line(self, text: str, **kwargs: Any) -> None:
        self.lines.append((text, kwargs))


class MockEnv:
    def __init__(self) -> None:
        self.disposed = False
        self.used = False

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_: object) -> None:
        self.dispose()

    def dispose(self) -> None:
        self.disposed = True

    def use(self) -> MockContext:
        return MockContext(self)


class MockContext:
    def __init__(self, env: MockEnv) -> None:
        self.status = "init"
        self.env = env

    def __enter__(self) -> None:
        self.status = "entered"
        self.env.used = True

    def __exit__(self, *args: object) -> None:
        self.status = "exited"


class MockPolicy:
    def __init__(self, registered: bool = True) -> None:
        self.registered = registered
        self.new_envs = list[ManagedEnvironment]()
        self.flags_creation = 0

        class MockManaged: ...

        self._managed = MockManaged()

    @property
    def is_registered(self) -> bool:
        return self.registered

    def new_environment(self, flags_creation: int | None = None) -> ManagedEnvironment:
        env = cast(ManagedEnvironment, MockEnv())
        self.new_envs.append(env)
        return env

    def register(self) -> None:
        self.registered = True

    def unregister(self) -> None:
        self.registered = False


@pytest.fixture
def fake_session_factory() -> Callable[[], pytest.Session]:
    return lambda: cast(pytest.Session, MockSession())


@pytest.fixture
def fake_policy_factory() -> Callable[[], Policy]:
    return lambda: cast(Policy, MockPolicy())


@pytest.fixture
def fake_env_factory() -> Callable[[], ManagedEnvironment]:
    return lambda: cast(ManagedEnvironment, MockEnv())


@pytest.mark.vpy
def test_vpy_policy_fixture(vpy_policy: Policy, request: pytest.FixtureRequest) -> None:
    assert vpy_policy is request.session.stash[vpy_pytest.policy_key]


@pytest.mark.vpy("unique-core")
def test_vpy_env_fixture_unique_core(vpy_env: ManagedEnvironment | None) -> None:
    assert vpy_env is not None
    assert not vpy_env.disposed
    assert vpy_env.vs_environment == vapoursynth.get_current_environment()


@pytest.mark.vpy
def test_vpy_env_fixture_shared_stages(vpy_env: ManagedEnvironment | None) -> None:
    assert vpy_env is not None
    assert not vpy_env.disposed
    assert vpy_env.vs_environment == vapoursynth.get_current_environment()


@pytest.mark.vpy("no-core", "no-policy")
def test_vpy_env_fixture_no_core(vpy_env: ManagedEnvironment | None) -> None:
    assert vpy_env is None


@pytest.mark.vpy("no-core")
def test_vpy_env_factory_fixture(vpy_env_factory: Callable[[], ManagedEnvironment]) -> None:
    env = vpy_env_factory()
    assert not env.disposed
    # The fixture will automatically dispose of it, but we can verify it works with manual disposal as well.
    env.dispose()
    assert env.disposed


@pytest.mark.vpy("no-policy")
def test_vpy_no_policy_stage(vpy_policy: Policy) -> None:
    # Verify that the policy is unregistered
    assert not vpy_policy.is_registered


@pytest.mark.vpy("no-policy", "no-core")
def test_vpy_no_policy_transition(vpy_policy: Policy, vpy_stage: str) -> None:
    if vpy_stage == "no-policy":
        assert not vpy_policy.is_registered
    elif vpy_stage == "no-core":
        assert vpy_policy.is_registered


def test_vpy_policy_no_policy() -> None:
    with pytest.raises(RuntimeError, match="No environment policy registered"):
        vpy_pytest._vpy_policy_impl()


def test_vpy_env_factory_no_policy() -> None:
    generator = vpy_pytest._vpy_env_factory_impl()

    factory = next(generator)
    with pytest.raises(RuntimeError, match="No environment policy registered"):
        factory()


def test_vpy_env_factory_automatic_disposal(
    restore_pytest_globals: None,
    fake_session_factory: Callable[[], pytest.Session],
    fake_policy_factory: Callable[[], Policy],
) -> None:
    session = fake_session_factory()
    session.stash[vpy_pytest.policy_key] = fake_policy_factory()

    generator = vpy_pytest._vpy_env_factory_impl(session)
    factory = next(generator)

    env = factory()
    assert not env.disposed

    with pytest.raises(StopIteration):
        next(generator)

    assert env.disposed


@pytest.mark.vpy
def test_vpy_default_stages() -> None:
    # This should run twice: once for initial-core and once for reloaded-core
    assert vapoursynth.get_current_environment() is not None


@pytest.mark.vpy("unique-core")
def test_vpy_unique_core() -> None:
    assert vapoursynth.get_current_environment() is not None


@pytest.mark.vpy("unique-core", flags_creation=vapoursynth.ENABLE_GRAPH_INSPECTION)
def test_vpy_creation_flags() -> None:
    assert vapoursynth.get_current_environment() is not None
    assert vapoursynth.core.std.BlankClip().is_inspectable(0)


def test_vpy_creation_flags_shared_stage_error(
    restore_pytest_globals: None,
    request: pytest.FixtureRequest,
    fake_policy_factory: Callable[[], Policy],
) -> None:
    session = request.session
    session.stash[vpy_pytest.policy_key] = fake_policy_factory()

    bad_item = MockItem(
        session,
        callspec=MockCallspec(vpy_stage="initial-core"),
        marker=MockMarker(("initial-core",), {"flags_creation": 1}),
    )

    gen = vpy_pytest.pytest_runtest_protocol(bad_item, None)
    with pytest.raises(ValueError, match="Cannot specify custom flags_creation"):
        next(gen)


@pytest.mark.vpy("no-core")
def test_vpy_no_core() -> None:
    with pytest.raises(RuntimeError):
        vapoursynth.get_current_environment()


def test_vpy_non_marked_test() -> None:
    # A test without the vpy marker should not have an environment active
    with pytest.raises(RuntimeError):
        vapoursynth.get_current_environment()


class TestVpyInClass:
    @pytest.mark.vpy
    def test_vpy_class_method(self) -> None:
        assert vapoursynth.get_current_environment() is not None

    @pytest.mark.vpy
    @pytest.mark.asyncio
    async def test_vpy_async(self) -> None:
        assert vapoursynth.get_current_environment() is not None


def test_pytest_configure() -> None:
    config = MockConfig()
    vpy_pytest.pytest_configure(config)
    assert len(config.added_lines) == 1
    assert config.added_lines[0][0] == "markers"
    assert "vpy" in config.added_lines[0][1]


@pytest.mark.vpy
def test_vpy_stage_fixture(vpy_stage: str, request: pytest.FixtureRequest) -> None:
    assert vpy_stage in vpy_pytest.DEFAULT_STAGES
    assert vpy_stage == request.session.stash[vpy_pytest.stage_key]


def test_vpy_stage_fixture_direct(vpy_stage: str, request: pytest.FixtureRequest) -> None:
    assert vpy_stage == request.session.stash.get(vpy_pytest.stage_key, "no-core")


def test_pytest_session_hooks(restore_pytest_globals: None, monkeypatch: pytest.MonkeyPatch) -> None:
    # Mock Policy.register and unregister to avoid registering a second policy on the thread
    registered = False
    unregistered = False

    def mock_register(self: Any) -> None:
        nonlocal registered
        registered = True

    def mock_unregister(self: Any) -> None:
        nonlocal unregistered
        unregistered = True

    monkeypatch.setattr(Policy, "register", mock_register)
    monkeypatch.setattr(Policy, "unregister", mock_unregister)
    monkeypatch.setattr(vapoursynth, "has_policy", lambda: False)
    collect_env = MockEnv()
    monkeypatch.setattr(Policy, "new_environment", lambda self: collect_env)

    session = cast(pytest.Session, MockSession())
    vpy_pytest.pytest_sessionstart(session)
    assert session.stash.get(vpy_pytest.policy_key, None) is not None
    assert registered
    # The collection environment is in use, but it is not the shared environment.
    assert collect_env.used
    assert not collect_env.disposed
    assert session.stash.get(vpy_pytest.env_key, None) is None

    mock_env = cast(ManagedEnvironment, MockEnv())
    session.stash[vpy_pytest.env_key] = mock_env

    mock_policy = MockPolicy(registered=False)
    session.stash[vpy_pytest.policy_key] = cast(Policy, mock_policy)
    vpy_pytest.pytest_sessionfinish(session, 0)
    assert session.stash.get(vpy_pytest.policy_key, None) is None
    assert session.stash.get(vpy_pytest.env_key, None) is None
    assert mock_env.disposed
    # In --collect-only mode, the collection environment is only cleaned up here.
    assert collect_env.disposed
    assert not mock_policy.registered


def test_pytest_sessionfinish_no_policy(
    restore_pytest_globals: None, fake_session_factory: Callable[[], pytest.Session]
) -> None:
    session = fake_session_factory()
    session.stash[vpy_pytest.policy_key] = None
    vpy_pytest.pytest_sessionfinish(session, 0)


def test_pytest_generate_tests() -> None:
    # Case 1: no marker
    meta1 = MockMetafunc(None)
    vpy_pytest.pytest_generate_tests(meta1)
    assert not meta1.fixturenames
    assert not meta1.parametrized

    # Case 2: marker with args
    meta2 = MockMetafunc(MockMarker(("stage-a", "stage-b")))
    vpy_pytest.pytest_generate_tests(meta2)
    assert "vpy_stage" in meta2.fixturenames
    assert meta2.parametrized == [("vpy_stage", ["stage-a", "stage-b"], ("stage-a", "stage-b"))]

    # Case 3: marker with empty args (fallback to DEFAULT_STAGES)
    meta3 = MockMetafunc(MockMarker(()))
    vpy_pytest.pytest_generate_tests(meta3)
    assert "vpy_stage" in meta3.fixturenames

    assert meta3.parametrized == [("vpy_stage", list(vpy_pytest.DEFAULT_STAGES), vpy_pytest.DEFAULT_STAGES)]


def test_pytest_collection_modifyitems(request: pytest.FixtureRequest) -> None:
    # Case 1: collectonly is True
    config_collect = MockConfig(collectonly=True)
    items1 = [MockItem(path=Path("test.py"))]
    vpy_pytest.pytest_collection_modifyitems(request.session, config_collect, items1)
    assert len(items1) == 1

    # Case 2: collectonly is False, with some vpy items, other items, and invalid stage item (line 101)
    config_run = MockConfig(collectonly=False)
    p = Path("test.py")
    item_vpy_initial = MockItem(path=p, callspec=MockCallspec(vpy_stage="initial-core"), parent=request.node)
    item_vpy_reloaded = MockItem(path=p, callspec=MockCallspec(vpy_stage="reloaded-core"), parent=request.node)
    item_vpy_invalid = MockItem(path=p, callspec=MockCallspec(vpy_stage="invalid-stage"), parent=request.node)
    item_other = MockItem(path=p)

    items2: list[Any] = [item_vpy_initial, item_vpy_reloaded, item_vpy_invalid, item_other]
    vpy_pytest.pytest_collection_modifyitems(request.session, config_run, items2)

    # Verify modification
    assert len(items2) == 6
    assert items2[0] is item_vpy_initial
    assert isinstance(items2[1], vpy_pytest.EnsureCleanEnvironment)
    assert items2[1].stage == "initial-core"
    assert items2[2] is item_vpy_reloaded
    assert isinstance(items2[3], vpy_pytest.EnsureCleanEnvironment)
    assert items2[3].stage == "reloaded-core"
    assert items2[4] is item_vpy_invalid
    assert items2[5] is item_other


def test_pytest_collection_modifyitems_cleanup(
    restore_pytest_globals: None, fake_session_factory: Callable[[], pytest.Session]
) -> None:
    config_run = MockConfig(collectonly=False)
    session = fake_session_factory()

    mock_env = MockEnv()
    mock_ctx = MockContext(mock_env)
    disposed_while_entered = list[bool]()

    def dispose() -> None:
        disposed_while_entered.append(mock_ctx.status == "entered")
        mock_env.dispose()

    stack = ExitStack()
    stack.callback(dispose)
    stack.enter_context(mock_ctx)
    session.stash[vpy_pytest.collect_env_stack_key] = stack

    items = [MockItem(path=Path("test.py"))]
    vpy_pytest.pytest_collection_modifyitems(session, config_run, items)

    assert mock_ctx.status == "exited"
    assert mock_env.disposed
    # The environment is exited before it is disposed of.
    assert disposed_while_entered == [False]

    # Closing again (as pytest_sessionfinish does) is a no-op.
    stack.close()
    assert disposed_while_entered == [False]


def test_pytest_collection_modifyitems_no_vpy_items(
    restore_pytest_globals: None, fake_session_factory: Callable[[], pytest.Session]
) -> None:
    config_run = MockConfig(collectonly=False)
    session = fake_session_factory()
    mock_policy = MockPolicy(registered=True)
    session.stash[vpy_pytest.policy_key] = cast(Policy, mock_policy)

    items = [MockItem(path=Path("test.py"), marker=False)]
    vpy_pytest.pytest_collection_modifyitems(session, config_run, items)

    assert not mock_policy.registered
    assert session.stash.get(vpy_pytest.policy_key, None) is None


def test_pytest_runtest_protocol_no_callspec(restore_pytest_globals: None, request: pytest.FixtureRequest) -> None:
    session = request.session
    item = MockItem(session, None)
    gen = vpy_pytest.pytest_runtest_protocol(item, None)
    assert next(gen, None) is None


def test_pytest_runtest_protocol_policy_none_in_stash(
    restore_pytest_globals: None,
    monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest,
) -> None:
    session = request.session
    monkeypatch.setattr(Policy, "register", lambda self: None)
    monkeypatch.setattr(Policy, "new_environment", lambda self: MockEnv())

    session.stash[vpy_pytest.policy_key] = None
    item = MockItem(session, MockCallspec(vpy_stage="initial-core"))
    gen = vpy_pytest.pytest_runtest_protocol(item, None)
    next(gen)
    outcome = DummyOutcome()
    with pytest.raises(StopIteration):
        gen.send(outcome)
    assert session.stash[vpy_pytest.policy_key] is not None


def test_pytest_runtest_protocol_unparameterized_leak(
    monkeypatch: pytest.MonkeyPatch,
    restore_pytest_globals: None,
    request: pytest.FixtureRequest,
    fake_policy_factory: Callable[[], Policy],
) -> None:
    monkeypatch.setattr(hospice, "any_alive", lambda: True)
    monkeypatch.setattr(hospice, "freeze", lambda: None)
    session = request.session
    session.stash[vpy_pytest.policy_key] = fake_policy_factory()

    item = MockItem(session, None, marker=True)
    gen = vpy_pytest.pytest_runtest_protocol(item, None)
    next(gen)
    outcome = DummyOutcome()
    with pytest.raises(StopIteration):
        gen.send(outcome)

    assert item.stash.get(vpy_pytest.leaked_key, False) is True
    assert isinstance(outcome.forced_exception, AssertionError)
    assert "Expected all environments to be cleaned up" in str(outcome.forced_exception)


def test_pytest_runtest_protocol_stage_change_initial_core(
    restore_pytest_globals: None, request: pytest.FixtureRequest
) -> None:
    session = request.session
    mock_policy = MockPolicy()
    session.stash[vpy_pytest.policy_key] = cast(Policy, mock_policy)
    session.stash[vpy_pytest.stage_key] = "no-core"

    item = MockItem(session, MockCallspec(vpy_stage="initial-core"))
    gen = vpy_pytest.pytest_runtest_protocol(item, None)

    next(gen)
    outcome = DummyOutcome()
    with pytest.raises(StopIteration):
        gen.send(outcome)

    assert len(mock_policy.new_envs) == 1
    current_env_any = session.stash[vpy_pytest.env_key]
    assert current_env_any is mock_policy.new_envs[0]
    assert session.stash[vpy_pytest.stage_key] == "initial-core"


def test_pytest_runtest_protocol_stage_initial_core_env_none(
    restore_pytest_globals: None, request: pytest.FixtureRequest
) -> None:
    session = request.session
    mock_policy = MockPolicy()
    session.stash[vpy_pytest.policy_key] = cast(Policy, mock_policy)
    session.stash[vpy_pytest.stage_key] = "initial-core"
    session.stash[vpy_pytest.env_key] = None

    item = MockItem(session, MockCallspec(vpy_stage="initial-core"))
    gen = vpy_pytest.pytest_runtest_protocol(item, None)

    next(gen)
    outcome = DummyOutcome()
    with pytest.raises(StopIteration):
        gen.send(outcome)

    assert len(mock_policy.new_envs) == 1
    current_env_any = session.stash[vpy_pytest.env_key]
    assert current_env_any is mock_policy.new_envs[0]
    assert session.stash[vpy_pytest.stage_key] == "initial-core"


def test_pytest_runtest_protocol_stage_reloaded_core(
    restore_pytest_globals: None, request: pytest.FixtureRequest
) -> None:
    session = request.session
    mock_policy = MockPolicy()
    session.stash[vpy_pytest.policy_key] = cast(Policy, mock_policy)
    mock_env = mock_policy.new_environment()
    session.stash[vpy_pytest.env_key] = mock_env
    session.stash[vpy_pytest.stage_key] = "initial-core"

    item = MockItem(session, MockCallspec(vpy_stage="reloaded-core"))
    gen = vpy_pytest.pytest_runtest_protocol(item, None)
    next(gen)
    outcome = DummyOutcome()
    with pytest.raises(StopIteration):
        gen.send(outcome)

    assert len(mock_policy.new_envs) == 2
    assert mock_env.disposed
    current_env_any = session.stash[vpy_pytest.env_key]
    assert current_env_any is mock_policy.new_envs[1]
    assert session.stash[vpy_pytest.stage_key] == "reloaded-core"


def test_pytest_runtest_protocol_stage_no_core_env_not_none(
    restore_pytest_globals: None, request: pytest.FixtureRequest
) -> None:
    session = request.session
    mock_policy = MockPolicy()
    session.stash[vpy_pytest.policy_key] = cast(Policy, mock_policy)
    session.stash[vpy_pytest.stage_key] = "no-core"
    mock_env = mock_policy.new_environment()
    session.stash[vpy_pytest.env_key] = mock_env

    item = MockItem(session, MockCallspec(vpy_stage="no-core"))
    gen = vpy_pytest.pytest_runtest_protocol(item, None)
    next(gen)
    outcome = DummyOutcome()
    with pytest.raises(StopIteration):
        gen.send(outcome)
    assert mock_env.disposed
    assert session.stash[vpy_pytest.env_key] is None
    assert session.stash[vpy_pytest.stage_key] == "no-core"


def test_pytest_runtest_protocol_stage_no_policy(restore_pytest_globals: None, request: pytest.FixtureRequest) -> None:
    session = request.session
    mock_policy = MockPolicy(registered=True)
    session.stash[vpy_pytest.policy_key] = cast(Policy, mock_policy)
    session.stash[vpy_pytest.stage_key] = "no-core"
    mock_env = mock_policy.new_environment()
    session.stash[vpy_pytest.env_key] = mock_env

    item = MockItem(session, MockCallspec(vpy_stage="no-policy"))
    gen = vpy_pytest.pytest_runtest_protocol(item, None)
    next(gen)
    outcome = DummyOutcome()
    with pytest.raises(StopIteration):
        gen.send(outcome)
    assert not mock_policy.registered
    assert mock_env.disposed
    assert session.stash[vpy_pytest.env_key] is None
    assert session.stash[vpy_pytest.stage_key] == "no-policy"


def test_pytest_runtest_protocol_stage_unique_core(
    monkeypatch: pytest.MonkeyPatch,
    restore_pytest_globals: None,
    request: pytest.FixtureRequest,
) -> None:
    session = request.session
    mock_policy = MockPolicy()
    session.stash[vpy_pytest.policy_key] = cast(Policy, mock_policy)

    monkeypatch.setattr(hospice, "any_alive", lambda: True)
    monkeypatch.setattr(hospice, "freeze", lambda: None)

    item4a = MockItem(session, MockCallspec(vpy_stage="unique-core"))
    gen4a = vpy_pytest.pytest_runtest_protocol(item4a, None)
    next(gen4a)

    outcome4a = DummyOutcome()
    with pytest.raises(StopIteration):
        gen4a.send(outcome4a)

    assert item4a.stash.get(vpy_pytest.leaked_key, False) is True
    assert isinstance(outcome4a.forced_exception, AssertionError)
    assert "Expected all environments to be cleaned up" in str(outcome4a.forced_exception)

    item4b = MockItem(session, MockCallspec(vpy_stage="unique-core"))
    gen4b = vpy_pytest.pytest_runtest_protocol(item4b, None)
    next(gen4b)

    outcome4b = DummyOutcome()
    outcome4b.excinfo = (AssertionError, AssertionError("Original test failure"), None)  # pyright: ignore[reportAttributeAccessIssue]
    with pytest.raises(StopIteration):
        gen4b.send(outcome4b)

    assert item4b.stash.get(vpy_pytest.leaked_key, False) is True
    assert outcome4b.forced_exception is None

    item4c = MockItem(session, MockCallspec(vpy_stage="unique-core"))
    item4c.stash[vpy_pytest.failed_key] = True
    gen4c = vpy_pytest.pytest_runtest_protocol(item4c, None)
    next(gen4c)

    outcome4c = DummyOutcome()
    with pytest.raises(StopIteration):
        gen4c.send(outcome4c)

    assert item4c.stash.get(vpy_pytest.leaked_key, False) is True
    assert outcome4c.forced_exception is None


def test_pytest_module_reload(restore_pytest_globals: None) -> None:
    importlib.reload(vpy_pytest)


def test_pytest_runtest_makereport() -> None:
    # Case 1: not when == 'call'
    item1 = MockItem(leaked=True)
    report1 = MockReport(when="setup")
    gen1 = vpy_pytest.pytest_runtest_makereport(item1, None)
    next(gen1)
    outcome1 = DummyOutcome(report1)
    with pytest.raises(StopIteration):
        gen1.send(outcome1)
    assert report1.longrepr == "original longrepr"

    # Case 2: not leaked
    item2 = MockItem(leaked=False)
    report2 = MockReport(when="call")
    gen2 = vpy_pytest.pytest_runtest_makereport(item2, None)
    next(gen2)
    outcome2 = DummyOutcome(report2)
    with pytest.raises(StopIteration):
        gen2.send(outcome2)
    assert report2.longrepr == "original longrepr"

    # Case 3: leaked and call (covers line 191-193)
    item3 = MockItem(leaked=True)
    report3 = MockReport(when="call")
    gen3 = vpy_pytest.pytest_runtest_makereport(item3, None)
    next(gen3)
    outcome3 = DummyOutcome(report3)
    with pytest.raises(StopIteration):
        gen3.send(outcome3)
    longrepr_any = report3.longrepr
    assert longrepr_any.__class__.__name__ == "CleanupFailed"
    assert longrepr_any.previous == "original longrepr"  # pyrefly: ignore [missing-attribute]

    # Case 4: report failed, check stash[failed_key]
    item4 = MockItem(leaked=False)
    report4 = MockReport(when="call", failed=True)
    gen4 = vpy_pytest.pytest_runtest_makereport(item4, None)
    next(gen4)
    outcome4 = DummyOutcome(report4)
    with pytest.raises(StopIteration):
        gen4.send(outcome4)
    assert item4.stash.get(vpy_pytest.failed_key, False) is True

    # Case 5: report passed, check stash[failed_key]
    item5 = MockItem(leaked=False)
    report5 = MockReport(when="call", failed=False)
    gen5 = vpy_pytest.pytest_runtest_makereport(item5, None)
    next(gen5)
    outcome5 = DummyOutcome(report5)
    with pytest.raises(StopIteration):
        gen5.send(outcome5)
    assert item5.stash.get(vpy_pytest.failed_key, False) is False


def test_cleanup_failed() -> None:
    # Test __repr__
    cf = vpy_pytest.CleanupFailed("prev", "next")
    assert "CleanupFailed" in repr(cf)

    # Test __str__
    assert str(vpy_pytest.CleanupFailed(None, "next")) == "next"
    assert str(vpy_pytest.CleanupFailed("prev", "next")) == "prev\n\nnext"

    # Test toterminal
    # Case A: previous is None
    tw_a = MockTerminalWriter()
    cf_a = vpy_pytest.CleanupFailed(None, "line1\nline2")
    cf_a.toterminal(tw_a)
    assert tw_a.lines == [("line1", {"red": True}), ("line2", {"red": True})]

    # Case B: previous is string (not CleanupFailed)
    tw_b = MockTerminalWriter()
    cf_b = vpy_pytest.CleanupFailed("prev_error", "line1\nline2")
    cf_b.toterminal(tw_b)
    assert len(tw_b.lines) == 5
    assert tw_b.lines[0] == ("prev_error", {})
    assert tw_b.lines[1] == ("", {})
    assert "detected an additional problem" in tw_b.lines[2][0]
    assert tw_b.lines[2][1] == {"yellow": True, "bold": True}
    assert tw_b.lines[3] == ("  line1", {"yellow": True})
    assert tw_b.lines[4] == ("  line2", {"yellow": True})

    # Case C: previous is CleanupFailed (recursive!)
    tw_c = MockTerminalWriter()
    cf_parent = vpy_pytest.CleanupFailed("grandparent", "parent_msg")
    cf_c = vpy_pytest.CleanupFailed(cf_parent, "child_msg")
    cf_c.toterminal(tw_c)
    assert len(tw_c.lines) > 0


def test_ensure_clean_environment(
    monkeypatch: pytest.MonkeyPatch, restore_pytest_globals: None, request: pytest.FixtureRequest
) -> None:
    # Setup environment
    mock_env = cast(ManagedEnvironment, MockEnv())
    mock_policy = MockPolicy(registered=False)
    session = request.session
    session.stash[vpy_pytest.env_key] = mock_env
    session.stash[vpy_pytest.policy_key] = cast(Policy, mock_policy)

    ece = vpy_pytest.EnsureCleanEnvironment.from_parent(
        parent=request.node,
        stage="initial-core",
        name="@check-clean-environment[initial-core]",
        path=Path("test_file.py"),
    )

    monkeypatch.setattr(hospice, "any_alive", lambda: False)
    monkeypatch.setattr(hospice, "freeze", lambda: None)

    ece.runtest()
    assert mock_env.disposed
    assert session.stash.get(vpy_pytest.env_key, None) is None
    assert mock_policy.registered

    failure_repr = ece.repr_failure(None)
    assert failure_repr.previous is None
    assert "dangling object" in failure_repr.next_text

    info = ece.reportinfo()
    assert info == (Path("test_file.py"), None, "cleaning up: initial-core")

    monkeypatch.setattr(hospice, "any_alive", lambda: True)
    session.stash[vpy_pytest.env_key] = mock_env  # reset env for another run
    with pytest.raises(AssertionError, match="Expected all environments to be cleaned up"):
        ece.runtest()


def test_plugin_integration(pytester: pytest.Pytester, request: pytest.FixtureRequest) -> None:
    forcefully_unregister_policy()

    pytester.makeini(
        """
        [pytest]
        asyncio_mode = auto
        asyncio_default_fixture_loop_scope = function
        """
    )

    pytester.makepyfile(
        """
        import pytest
        import vapoursynth

        @pytest.mark.vpy
        def test_dummy():
            assert vapoursynth.get_current_environment() is not None
        """
    )
    try:
        result = pytester.inline_run()
        result.assertoutcome(passed=4)
    finally:
        policy = Policy(GlobalStore())
        policy.register()
        request.session.stash[vpy_pytest.policy_key] = policy


# Named policies (pytest_vpy_policies hook)
class RecordingPolicy(Policy):
    """A real Policy that never touches VapourSynth's registration."""

    def __init__(self, flags_creation: int | None = None) -> None:
        super().__init__(GlobalStore(), flags_creation=flags_creation)
        self.registered = False
        self.new_envs = list[MockEnv]()

    @property
    @override
    def is_registered(self) -> bool:
        return self.registered

    @override
    def register(self) -> None:
        self.registered = True

    @override
    def unregister(self) -> None:
        self.registered = False

    @override
    def new_environment(self, flags_creation: int | None = None, logger: Any = None) -> ManagedEnvironment:
        env = MockEnv()
        self.new_envs.append(env)
        return cast(ManagedEnvironment, env)


class MockPolicyItem(MockItem):
    def __init__(self, session: pytest.Session, policies: Any, **kwargs: Any) -> None:
        super().__init__(session, **kwargs)
        self.config = session.config
        self.ihook = MockHook(policies)
        self.nodeid = f"{self.path}::test_dummy"


def _custom_factory() -> Policy:
    return RecordingPolicy()


def test_pytest_generate_tests_with_policies() -> None:
    policies = {"default": vpy_pytest.default_policy, "custom": _custom_factory}

    meta = MockMetafunc(MockMarker(("no-policy", "initial-core")), policies)
    vpy_pytest.pytest_generate_tests(meta)

    assert meta.fixturenames == ["vpy_stage", "vpy_policy_name"]
    [(argnames, argvalues, _)] = meta.parametrized
    assert argnames == ("vpy_stage", "vpy_policy_name")
    assert [p.id for p in argvalues] == ["no-policy", "initial-core-default", "initial-core-custom"]
    assert [tuple(p.values) for p in argvalues] == [
        ("no-policy", None),
        ("initial-core", "default"),
        ("initial-core", "custom"),
    ]


def test_pytest_generate_tests_policy_selection() -> None:
    policies = {"default": vpy_pytest.default_policy, "a": _custom_factory, "b": _custom_factory}

    # The selection follows the order of the hook result, not the marker.
    meta = MockMetafunc(MockMarker(("initial-core",), {"policies": ["b", "a", "b"]}), policies)
    vpy_pytest.pytest_generate_tests(meta)
    [(_, argvalues, _)] = meta.parametrized
    assert [p.id for p in argvalues] == ["initial-core-a", "initial-core-b"]


def test_pytest_generate_tests_default_selection_without_hook() -> None:
    meta = MockMetafunc(MockMarker((), {"policies": ["default"]}))
    vpy_pytest.pytest_generate_tests(meta)
    assert meta.fixturenames == ["vpy_stage"]
    assert meta.parametrized == [("vpy_stage", list(vpy_pytest.DEFAULT_STAGES), vpy_pytest.DEFAULT_STAGES)]


@pytest.mark.parametrize(
    ("policies", "selected", "error", "match"),
    [
        (None, ["custom"], ValueError, "No pytest_vpy_policies hook"),
        ({"custom": _custom_factory}, ["typo"], ValueError, r"Unknown vpy policies \['typo'\]"),
        ({"custom": _custom_factory}, "custom", TypeError, "must be a sequence of names"),
        ({"custom": _custom_factory}, [], ValueError, "must not be empty"),
    ],
)
def test_pytest_generate_tests_invalid_selection(
    policies: Any, selected: Any, error: type[Exception], match: str
) -> None:
    meta = MockMetafunc(MockMarker((), {"policies": selected}), policies)
    with pytest.raises(error, match=match):
        vpy_pytest.pytest_generate_tests(meta)


@pytest.mark.parametrize(
    ("policies", "match"),
    [
        ({}, "non-empty mapping"),
        ([("custom", _custom_factory)], "non-empty mapping"),
        ({"": _custom_factory}, "invalid policy name"),
        ({1: _custom_factory}, "invalid policy name"),
        ({"custom": "not callable"}, "not callable"),
    ],
)
def test_pytest_generate_tests_invalid_hook_result(policies: Any, match: str) -> None:
    meta = MockMetafunc(MockMarker(()), policies)
    with pytest.raises(TypeError, match=match):
        vpy_pytest.pytest_generate_tests(meta)


def test_instantiate_policy_flags_only_for_default() -> None:
    config = MockConfig()
    setattr(config.option, "--vpy-flags", 4)

    default = vpy_pytest._instantiate_policy(cast(pytest.Config, config), vpy_pytest.default_policy, "default")
    assert default.flags_creation == 4

    custom = vpy_pytest._instantiate_policy(cast(pytest.Config, config), lambda: RecordingPolicy(1), "custom")
    assert custom.flags_creation == 1


def test_instantiate_policy_invalid_results() -> None:
    config = cast(pytest.Config, MockConfig())

    with pytest.raises(TypeError, match="must return a Policy, not str"):
        vpy_pytest._instantiate_policy(config, lambda: cast(Policy, "nope"), "custom")

    def registered() -> Policy:
        policy = RecordingPolicy()
        policy.register()
        return policy

    with pytest.raises(ValueError, match="returned a registered Policy"):
        vpy_pytest._instantiate_policy(config, registered, "custom")


def test_ensure_policy_group_switch(fake_session_factory: Callable[[], pytest.Session]) -> None:
    session = fake_session_factory()
    old_policy = RecordingPolicy()
    old_policy.register()
    old_env = MockEnv()
    session.stash[vpy_pytest.policy_key] = old_policy
    session.stash[vpy_pytest.policy_group_key] = None
    session.stash[vpy_pytest.env_key] = cast(ManagedEnvironment, old_env)
    session.stash[vpy_pytest.stage_key] = "initial-core"

    item = MockPolicyItem(session, {"custom": _custom_factory}, path=Path("test_a.py"))
    policy = vpy_pytest._ensure_policy_group(cast(pytest.Item, item), "custom")

    assert isinstance(policy, RecordingPolicy)
    assert policy is not old_policy
    assert not old_policy.registered
    assert old_env.disposed
    assert session.stash[vpy_pytest.env_key] is None
    assert session.stash[vpy_pytest.stage_key] == "no-core"
    assert session.stash[vpy_pytest.policy_group_key] == (Path("test_a.py"), "custom")

    # Same group: the policy is reused.
    assert vpy_pytest._ensure_policy_group(cast(pytest.Item, item), "custom") is policy

    # Same name in another module: a new policy is created.
    other = MockPolicyItem(session, {"custom": _custom_factory}, path=Path("test_b.py"))
    assert vpy_pytest._ensure_policy_group(cast(pytest.Item, other), "custom") is not policy


def test_ensure_policy_group_missing_name(fake_session_factory: Callable[[], pytest.Session]) -> None:
    session = fake_session_factory()

    item = MockPolicyItem(session, {"custom": _custom_factory})
    with pytest.raises(ValueError, match="is not returned by pytest_vpy_policies"):
        vpy_pytest._ensure_policy_group(cast(pytest.Item, item), "gone")

    no_hook = MockPolicyItem(session, None)
    with pytest.raises(ValueError, match="No pytest_vpy_policies hook"):
        vpy_pytest._ensure_policy_group(cast(pytest.Item, no_hook), "custom")


def test_pytest_collection_modifyitems_policy_groups(request: pytest.FixtureRequest) -> None:
    config_run = MockConfig(collectonly=False)
    p = Path("test.py")

    def make(stage: str, name: str | None = None) -> MockItem:
        params: dict[str, Any] = {"vpy_stage": stage}
        if stage != "no-policy":
            params["vpy_policy_name"] = name
        return MockItem(path=p, callspec=MockCallspec(**params), parent=request.node)

    a_initial = make("initial-core", "a")
    b_initial = make("initial-core", "b")
    a_unique = make("unique-core", "a")
    b_reloaded = make("reloaded-core", "b")
    no_policy = make("no-policy")
    other = MockItem(path=p)

    items: list[Any] = [a_initial, b_initial, a_unique, b_reloaded, no_policy, other]
    vpy_pytest.pytest_collection_modifyitems(request.session, config_run, items)

    def describe(item: Any) -> Any:
        if isinstance(item, vpy_pytest.EnsureCleanEnvironment):
            return (item.name, item.stage, item.policy_name)
        return item

    assert [describe(i) for i in items] == [
        no_policy,
        a_initial,
        ("@check-clean-environment[initial-core-a]", "initial-core", "a"),
        a_unique,
        b_initial,
        ("@check-clean-environment[initial-core-b]", "initial-core", "b"),
        b_reloaded,
        ("@check-clean-environment[reloaded-core-b]", "reloaded-core", "b"),
        other,
    ]
    assert items[2].reportinfo()[2] == "cleaning up: initial-core (a)"


@pytest.fixture
def vpy_pytester(pytester: pytest.Pytester, request: pytest.FixtureRequest) -> Generator[pytest.Pytester]:
    """A pytester that can run vpy tests in-process, and restores the outer session policy afterwards."""
    forcefully_unregister_policy()

    pytester.makeini(
        """
        [pytest]
        asyncio_mode = auto
        asyncio_default_fixture_loop_scope = function
        """
    )
    try:
        yield pytester
    finally:
        policy = Policy(GlobalStore())
        policy.register()
        request.session.stash[vpy_pytest.policy_key] = policy


def test_plugin_policies_integration(vpy_pytester: pytest.Pytester) -> None:
    vpy_pytester.makeconftest(
        """
        import vapoursynth
        from vsengine.policy import GlobalStore, Policy
        from vsengine.pytest import default_policy

        class CustomPolicy(Policy):
            pass

        def custom_policy():
            return CustomPolicy(GlobalStore(), flags_creation=vapoursynth.ENABLE_GRAPH_INSPECTION)

        def pytest_vpy_policies(config, item):
            return {"default": default_policy, "custom": custom_policy}
        """
    )
    vpy_pytester.makepyfile(
        """
        import pytest
        import vapoursynth
        from conftest import CustomPolicy

        SEEN = {}

        @pytest.mark.vpy
        def test_both(vpy_policy, vpy_policy_name, vpy_stage):
            assert vpy_policy.is_registered
            assert vapoursynth.get_current_environment() is not None
            assert isinstance(vpy_policy, CustomPolicy) == (vpy_policy_name == "custom")
            # The custom policy flags apply to shared stages too.
            inspectable = vapoursynth.core.std.BlankClip().is_inspectable(0)
            assert inspectable == (vpy_policy_name == "custom")
            # One policy instance per policy name and module.
            assert SEEN.setdefault(vpy_policy_name, vpy_policy) is vpy_policy

        @pytest.mark.vpy("no-policy", "unique-core", policies=["custom"])
        def test_selected(vpy_policy, vpy_policy_name, vpy_stage):
            if vpy_stage == "no-policy":
                assert vpy_policy_name is None
                assert not vpy_policy.is_registered
            else:
                assert vpy_policy_name == "custom"
                assert vpy_policy is SEEN["custom"]
                assert vpy_policy.is_registered
        """
    )

    result = vpy_pytester.runpytest_inprocess("-v")
    result.assert_outcomes(passed=10)
    result.stdout.fnmatch_lines(
        [
            "*::test_selected[[]no-policy[]] PASSED*",
            "*::test_both[[]initial-core-default[]] PASSED*",
            "*::@check-clean-environment[[]initial-core-default[]] PASSED*",
            "*::test_both[[]reloaded-core-default[]] PASSED*",
            "*::@check-clean-environment[[]reloaded-core-default[]] PASSED*",
            "*::test_both[[]initial-core-custom[]] PASSED*",
            "*::@check-clean-environment[[]initial-core-custom[]] PASSED*",
            "*::test_both[[]reloaded-core-custom[]] PASSED*",
            "*::@check-clean-environment[[]reloaded-core-custom[]] PASSED*",
            "*::test_selected[[]unique-core-custom[]] PASSED*",
        ]
    )


def test_plugin_policies_nested_conftest(vpy_pytester: pytest.Pytester) -> None:
    vpy_pytester.makeconftest(
        """
        from vsengine.pytest import default_policy

        def pytest_vpy_policies(config, item):
            return {"root": default_policy}
        """
    )
    vpy_pytester.mkpydir("sub")
    vpy_pytester.makepyfile(
        **{
            "sub/conftest": """
                from vsengine.pytest import default_policy

                def pytest_vpy_policies(config, item):
                    return {"sub": default_policy}
            """,
            "sub/test_sub": """
                import pytest

                @pytest.mark.vpy("initial-core")
                def test_sub(vpy_policy_name):
                    assert vpy_policy_name == "sub"
            """,
            "test_root": """
                import pytest

                @pytest.mark.vpy("initial-core")
                def test_root(vpy_policy_name):
                    assert vpy_policy_name == "root"
            """,
        }
    )

    result = vpy_pytester.runpytest_inprocess("-v")
    result.assert_outcomes(passed=4)
    result.stdout.fnmatch_lines_random(
        [
            "*test_sub.py::test_sub[[]initial-core-sub[]] PASSED*",
            "*test_root.py::test_root[[]initial-core-root[]] PASSED*",
        ]
    )


def test_plugin_policies_unknown_name(vpy_pytester: pytest.Pytester) -> None:
    vpy_pytester.makeconftest(
        """
        from vsengine.pytest import default_policy

        def pytest_vpy_policies(config, item):
            return {"custom": default_policy}
        """
    )
    vpy_pytester.makepyfile(
        """
        import pytest

        @pytest.mark.vpy(policies=["typo"])
        def test_dummy():
            pass
        """
    )

    result = vpy_pytester.runpytest_inprocess()
    result.assert_outcomes(errors=1)
    result.stdout.fnmatch_lines(["*Unknown vpy policies [[]'typo'[]]. Available policies: [[]'custom'[]].*"])


def test_plugin_policies_leak_per_group(vpy_pytester: pytest.Pytester) -> None:
    vpy_pytester.makeconftest(
        """
        from vsengine.pytest import default_policy

        def pytest_vpy_policies(config, item):
            return {"default": default_policy, "custom": default_policy}
        """
    )
    vpy_pytester.makepyfile(
        """
        import pytest
        import vapoursynth

        LEAK = []

        @pytest.mark.vpy("initial-core")
        def test_leak(vpy_policy_name):
            if vpy_policy_name == "custom":
                LEAK.append(vapoursynth.core.core)
        """
    )

    result = vpy_pytester.runpytest_inprocess("-v")
    result.assert_outcomes(passed=3, failed=1)
    result.stdout.fnmatch_lines(
        [
            "*::@check-clean-environment[[]initial-core-default[]] PASSED*",
            "*::@check-clean-environment[[]initial-core-custom[]] FAILED*",
        ]
    )


def test_plugin_policies_vpy_flags_default_only(vpy_pytester: pytest.Pytester) -> None:
    vpy_pytester.makeconftest(
        """
        from vsengine.policy import Policy
        from vsengine.pytest import default_policy

        def pytest_vpy_policies(config, item):
            return {"default": default_policy, "custom": lambda: Policy()}
        """
    )
    vpy_pytester.makepyfile(
        """
        import pytest
        import vapoursynth

        @pytest.mark.vpy("initial-core")
        def test_flags(vpy_policy, vpy_policy_name):
            expected = int(vapoursynth.ENABLE_GRAPH_INSPECTION) if vpy_policy_name == "default" else 0
            assert vpy_policy.flags_creation == expected
        """
    )

    result = vpy_pytester.runpytest_inprocess(f"--vpy-flags={int(vapoursynth.ENABLE_GRAPH_INSPECTION)}")
    result.assert_outcomes(passed=4)


def test_plugin_without_hook_keeps_ids(vpy_pytester: pytest.Pytester) -> None:
    vpy_pytester.makepyfile(
        """
        import pytest

        @pytest.mark.vpy
        def test_dummy(vpy_policy_name):
            assert vpy_policy_name == "default"
        """
    )

    result = vpy_pytester.runpytest_inprocess("-v")
    result.assert_outcomes(passed=4)
    result.stdout.fnmatch_lines(
        [
            "*::test_dummy[[]initial-core[]] PASSED*",
            "*::@check-clean-environment[[]initial-core[]] PASSED*",
            "*::test_dummy[[]reloaded-core[]] PASSED*",
            "*::@check-clean-environment[[]reloaded-core[]] PASSED*",
        ]
    )
