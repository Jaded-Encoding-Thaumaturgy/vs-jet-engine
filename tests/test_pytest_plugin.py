# vs-engine
# Copyright (C) 2026  Jaded-Encoding-Thaumaturgy
# This project is licensed under the EUPL-1.2
# SPDX-License-Identifier: EUPL-1.2

import importlib
from collections.abc import Generator
from pathlib import Path
from typing import Any

import pytest
import vapoursynth

import vsengine._hospice as hospice
import vsengine.pytest as vpy_pytest
from tests._testutils import forcefully_unregister_policy
from vsengine.policy import GlobalStore, Policy

pytest_plugins = ["pytester"]


@pytest.fixture(autouse=True)
def clean_policy() -> None: ...


@pytest.fixture(autouse=True)
def reset_hospice_state() -> None: ...


@pytest.mark.vpy
def test_vpy_default_stages() -> None:
    # This should run twice: once for initial-core and once for reloaded-core
    assert vapoursynth.get_current_environment() is not None


@pytest.mark.vpy("unique-core")
def test_vpy_unique_core() -> None:
    assert vapoursynth.get_current_environment() is not None


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


@pytest.fixture
def restore_pytest_globals() -> Generator[None, None, None]:
    orig_policy = vpy_pytest.current_policy
    orig_env = vpy_pytest.current_env
    orig_stage = vpy_pytest.current_stage
    yield
    vpy_pytest.current_policy = orig_policy
    vpy_pytest.current_env = orig_env
    vpy_pytest.current_stage = orig_stage


def test_pytest_configure() -> None:
    class MockConfig:
        def __init__(self) -> None:
            self.added_lines: list[tuple[str, str]] = []

        def addinivalue_line(self, name: str, value: str) -> None:
            self.added_lines.append((name, value))

    config: Any = MockConfig()
    vpy_pytest.pytest_configure(config)
    assert len(config.added_lines) == 1
    assert config.added_lines[0][0] == "markers"
    assert "vpy" in config.added_lines[0][1]


@pytest.mark.vpy
def test_vpy_stage_fixture(vpy_stage: str) -> None:
    assert vpy_stage in vpy_pytest.DEFAULT_STAGES


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

    class MockSession: ...

    session: Any = MockSession()
    vpy_pytest.pytest_sessionstart(session)
    assert vpy_pytest.current_policy is not None
    assert registered

    class MockEnv:
        def __init__(self) -> None:
            self.disposed = False

        def dispose(self) -> None:
            self.disposed = True

    mock_env = MockEnv()
    vpy_pytest.current_env = mock_env  # type: ignore[assignment]

    class MockManaged: ...

    class MockPolicyForFinish:
        def __init__(self) -> None:
            self._managed = MockManaged()
            self.registered = False
            self.unregistered = False

        @property
        def is_registered(self) -> bool:
            return self.registered

        def register(self) -> None:
            self.registered = True

        def unregister(self) -> None:
            self.unregistered = True

    mock_policy = MockPolicyForFinish()
    vpy_pytest.current_policy = mock_policy  # type: ignore[assignment]
    vpy_pytest.pytest_sessionfinish(session, 0)
    assert vpy_pytest.current_policy is None
    assert vpy_pytest.current_env is None  # type: ignore[unreachable]
    assert mock_env.disposed
    assert mock_policy.registered
    assert mock_policy.unregistered


def test_pytest_generate_tests() -> None:
    class MockMarker:
        def __init__(self, args: tuple[Any, ...]) -> None:
            self.args = args

    class MockDefinition:
        def __init__(self, marker: MockMarker | None) -> None:
            self.marker = marker

        def get_closest_marker(self, name: str) -> MockMarker | None:
            return self.marker

    class MockMetafunc:
        def __init__(self, marker: MockMarker | None) -> None:
            self.definition = MockDefinition(marker)
            self.fixturenames: list[str] = []
            self.parametrized: list[tuple[str, list[Any], Any]] = []

        def parametrize(self, name: str, stages: list[Any], ids: Any) -> None:
            self.parametrized.append((name, stages, ids))

    # Case 1: no marker
    meta1 = MockMetafunc(None)
    vpy_pytest.pytest_generate_tests(meta1)  # type: ignore[arg-type]
    assert not meta1.fixturenames
    assert not meta1.parametrized

    # Case 2: marker with args
    meta2 = MockMetafunc(MockMarker(("stage-a", "stage-b")))
    vpy_pytest.pytest_generate_tests(meta2)  # type: ignore[arg-type]
    assert "vpy_stage" in meta2.fixturenames
    assert meta2.parametrized == [("vpy_stage", ["stage-a", "stage-b"], ("stage-a", "stage-b"))]

    # Case 3: marker with empty args (fallback to DEFAULT_STAGES)
    meta3 = MockMetafunc(MockMarker(()))
    vpy_pytest.pytest_generate_tests(meta3)  # type: ignore[arg-type]
    assert "vpy_stage" in meta3.fixturenames

    assert meta3.parametrized == [("vpy_stage", list(vpy_pytest.DEFAULT_STAGES), vpy_pytest.DEFAULT_STAGES)]


def test_pytest_collection_modifyitems(request: pytest.FixtureRequest) -> None:
    class MockOption:
        def __init__(self, collectonly: bool) -> None:
            self.collectonly = collectonly

    class MockConfig:
        def __init__(self, collectonly: bool) -> None:
            self.option = MockOption(collectonly)

    class MockCallspec:
        def __init__(self, params: dict[str, Any]) -> None:
            self.params = params

    class MockItem:
        def __init__(self, path: Path, callspec: MockCallspec | None = None, parent: Any = None) -> None:
            self.path = path
            if callspec is not None:
                self.callspec = callspec
            self.parent = parent

    # Case 1: collectonly is True
    config_collect = MockConfig(collectonly=True)
    items1 = [MockItem(Path("test.py"))]
    vpy_pytest.pytest_collection_modifyitems(request.session, config_collect, items1)  # type: ignore[arg-type]
    assert len(items1) == 1

    # Case 2: collectonly is False, with some vpy items, other items, and invalid stage item (line 101)
    config_run: Any = MockConfig(collectonly=False)
    p = Path("test.py")
    item_vpy_initial = MockItem(p, MockCallspec({"vpy_stage": "initial-core"}), parent=request.node)
    item_vpy_reloaded = MockItem(p, MockCallspec({"vpy_stage": "reloaded-core"}), parent=request.node)
    item_vpy_invalid = MockItem(
        p, MockCallspec({"vpy_stage": "invalid-stage"}), parent=request.node
    )  # Line 101 target
    item_other = MockItem(p)

    items2 = [item_vpy_initial, item_vpy_reloaded, item_vpy_invalid, item_other]
    vpy_pytest.pytest_collection_modifyitems(request.session, config_run, items2)  # type: ignore[arg-type]

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


def test_pytest_runtest_call(restore_pytest_globals: None, monkeypatch: pytest.MonkeyPatch) -> None:
    class MockCallspec:
        def __init__(self, params: dict[str, Any]) -> None:
            self.params = params

    class MockItem:
        def __init__(self, callspec: MockCallspec | None = None) -> None:
            if callspec is not None:
                self.callspec = callspec
            self._vpy_leaked = False

    class MockEnv:
        def __init__(self) -> None:
            self.disposed = False
            self.used = False

        def dispose(self) -> None:
            self.disposed = True

        def use(self) -> Any:
            env = self

            class Context:
                def __enter__(self) -> None:
                    env.used = True

                def __exit__(self, *args: object) -> None: ...

            return Context()

    class MockPolicy:
        def __init__(self) -> None:
            self.new_envs: list[MockEnv] = []

            class MockManaged: ...

            self._managed = MockManaged()

        @property
        def is_registered(self) -> bool:
            return False

        def new_environment(self) -> Any:
            env = MockEnv()
            self.new_envs.append(env)
            return env

        def register(self) -> None: ...

    # Case 1: no callspec
    item1 = MockItem(None)
    gen1 = vpy_pytest.pytest_runtest_call(item1)  # type: ignore[arg-type]
    assert next(gen1, None) is None

    # Case 2: stage != current_stage (initial-core)
    mock_policy = MockPolicy()
    vpy_pytest.current_policy = mock_policy  # type: ignore[assignment]
    vpy_pytest.current_stage = "no-core"

    item2 = MockItem(MockCallspec({"vpy_stage": "initial-core"}))
    gen2 = vpy_pytest.pytest_runtest_call(item2)  # type: ignore[arg-type]

    class DummyOutcome:
        def __init__(self) -> None:
            self.excinfo = None
            self.forced_exception = None

        def force_exception(self, exc: Any) -> None:
            self.forced_exception = exc

    next(gen2)
    outcome2 = DummyOutcome()
    with pytest.raises(StopIteration):
        gen2.send(outcome2)  # type: ignore[arg-type]

    assert len(mock_policy.new_envs) == 1
    current_env_any = vpy_pytest.current_env
    assert current_env_any is mock_policy.new_envs[0]  # type: ignore[comparison-overlap]
    assert vpy_pytest.current_stage == "initial-core"  # type: ignore[unreachable]

    # Case 3: stage != current_stage (reloaded-core) with current_env not None (covers line 152)
    item3 = MockItem(MockCallspec({"vpy_stage": "reloaded-core"}))
    gen3 = vpy_pytest.pytest_runtest_call(item3)  # pyright: ignore[reportArgumentType]
    next(gen3)
    outcome3 = DummyOutcome()
    with pytest.raises(StopIteration):
        gen3.send(outcome3)  # pyright: ignore[reportArgumentType]

    assert len(mock_policy.new_envs) == 2
    assert mock_policy.new_envs[0].disposed
    current_env_any = vpy_pytest.current_env
    assert current_env_any is mock_policy.new_envs[1]
    assert vpy_pytest.current_stage == "reloaded-core"

    # Case 4: stage is unique-core (covers line 161-177)
    orig_any_alive = hospice.any_alive
    orig_freeze = hospice.freeze
    try:
        hospice.any_alive = lambda: True
        hospice.freeze = lambda: None

        item4a = MockItem(MockCallspec({"vpy_stage": "unique-core"}))
        gen4a = vpy_pytest.pytest_runtest_call(item4a)  # pyright: ignore[reportArgumentType]
        next(gen4a)

        outcome4a = DummyOutcome()
        with pytest.raises(StopIteration):
            gen4a.send(outcome4a)  # pyright: ignore[reportArgumentType]

        assert item4a._vpy_leaked is True
        assert isinstance(outcome4a.forced_exception, AssertionError)
        assert "Expected all environments to be cleaned up" in str(outcome4a.forced_exception)

        item4b = MockItem(MockCallspec({"vpy_stage": "unique-core"}))
        gen4b = vpy_pytest.pytest_runtest_call(item4b)  # pyright: ignore[reportArgumentType]
        next(gen4b)

        outcome4b = DummyOutcome()
        outcome4b.excinfo = (AssertionError, AssertionError("Original test failure"), None)  # pyright: ignore[reportAttributeAccessIssue]
        with pytest.raises(StopIteration):
            gen4b.send(outcome4b)  # pyright: ignore[reportArgumentType]

        assert item4b._vpy_leaked is True
        assert outcome4b.forced_exception is None

    finally:
        hospice.any_alive = orig_any_alive
        hospice.freeze = orig_freeze

    # Case 5: current_policy is None (covers line 142)
    monkeypatch.setattr(Policy, "register", lambda self: None)
    monkeypatch.setattr(Policy, "new_environment", lambda self: MockEnv())

    vpy_pytest.current_policy = None
    item5 = MockItem(MockCallspec({"vpy_stage": "initial-core"}))
    gen5 = vpy_pytest.pytest_runtest_call(item5)  # pyright: ignore[reportArgumentType]
    next(gen5)
    outcome5 = DummyOutcome()
    with pytest.raises(StopIteration):
        gen5.send(outcome5)  # pyright: ignore[reportArgumentType]
    assert vpy_pytest.current_policy is not None


def test_pytest_module_reload(restore_pytest_globals: None) -> None:
    importlib.reload(vpy_pytest)


def test_pytest_runtest_makereport() -> None:
    class MockItem:
        def __init__(self, leaked: bool) -> None:
            self._vpy_leaked = leaked

    class MockReport:
        def __init__(self, when: str) -> None:
            self.when = when
            self.longrepr = "original longrepr"

    class DummyOutcome:
        def __init__(self, report: MockReport) -> None:
            self.report = report

        def get_result(self) -> MockReport:
            return self.report

    # Case 1: not when == 'call'
    item1 = MockItem(leaked=True)
    report1 = MockReport(when="setup")
    gen1 = vpy_pytest.pytest_runtest_makereport(item1, None)  # type: ignore[arg-type]
    next(gen1)
    outcome1 = DummyOutcome(report1)
    with pytest.raises(StopIteration):
        gen1.send(outcome1)  # type: ignore[arg-type]
    assert report1.longrepr == "original longrepr"

    # Case 2: not leaked
    item2 = MockItem(leaked=False)
    report2 = MockReport(when="call")
    gen2 = vpy_pytest.pytest_runtest_makereport(item2, None)  # type: ignore[arg-type]
    next(gen2)
    outcome2 = DummyOutcome(report2)
    with pytest.raises(StopIteration):
        gen2.send(outcome2)  # type: ignore[arg-type]
    assert report2.longrepr == "original longrepr"

    # Case 3: leaked and call (covers line 191-193)
    item3 = MockItem(leaked=True)
    report3 = MockReport(when="call")
    gen3 = vpy_pytest.pytest_runtest_makereport(item3, None)  # type: ignore[arg-type]
    next(gen3)
    outcome3 = DummyOutcome(report3)
    with pytest.raises(StopIteration):
        gen3.send(outcome3)  # type: ignore[arg-type]
    longrepr_any = report3.longrepr
    assert longrepr_any.__class__.__name__ == "CleanupFailed"
    assert longrepr_any.previous == "original longrepr"  # type: ignore[attr-defined]


def test_cleanup_failed() -> None:
    # Test __repr__
    cf = vpy_pytest.CleanupFailed("prev", "next")
    assert "CleanupFailed" in repr(cf)

    # Test __str__
    assert str(vpy_pytest.CleanupFailed(None, "next")) == "next"
    assert str(vpy_pytest.CleanupFailed("prev", "next")) == "prev\n\nnext"

    # Test toterminal
    class MockTerminalWriter:
        def __init__(self) -> None:
            self.lines: list[tuple[str, dict[str, Any]]] = []

        def line(self, text: str, **kwargs: Any) -> None:
            self.lines.append((text, kwargs))

    # Case A: previous is None
    tw_a = MockTerminalWriter()
    cf_a = vpy_pytest.CleanupFailed(None, "line1\nline2")
    cf_a.toterminal(tw_a)  # type: ignore[arg-type]
    assert tw_a.lines == [("line1", {"red": True}), ("line2", {"red": True})]

    # Case B: previous is string (not CleanupFailed)
    tw_b = MockTerminalWriter()
    cf_b = vpy_pytest.CleanupFailed("prev_error", "line1\nline2")
    cf_b.toterminal(tw_b)  # type: ignore[arg-type]
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
    cf_c.toterminal(tw_c)  # type: ignore[arg-type]
    assert len(tw_c.lines) > 0


def test_ensure_clean_environment(restore_pytest_globals: None, request: pytest.FixtureRequest) -> None:
    class MockEnv:
        def __init__(self) -> None:
            self.disposed = False

        def dispose(self) -> None:
            self.disposed = True

    class MockPolicy:
        def __init__(self) -> None:

            self._managed = MockManaged()
            self.registered = False

        @property
        def is_registered(self) -> bool:
            return self.registered

        def register(self) -> None:
            self.registered = True

    class MockManaged: ...

    # Setup environment
    mock_env = MockEnv()
    mock_policy = MockPolicy()
    vpy_pytest.current_env = mock_env  # type: ignore[assignment]
    vpy_pytest.current_policy = mock_policy  # type: ignore[assignment]

    ece = vpy_pytest.EnsureCleanEnvironment.from_parent(
        parent=request.node,
        stage="initial-core",
        name="@check-clean-environment[initial-core]",
        path=Path("test_file.py"),
    )

    orig_any_alive = hospice.any_alive
    orig_freeze = hospice.freeze
    try:
        hospice.any_alive = lambda: False
        hospice.freeze = lambda: None

        ece.runtest()
        assert mock_env.disposed
        assert vpy_pytest.current_env is None
        assert mock_policy.registered

        failure_repr = ece.repr_failure(None)  # type: ignore[arg-type]
        assert failure_repr.previous is None
        assert "dangling object" in failure_repr.next_text

        info = ece.reportinfo()
        assert info == (Path("test_file.py"), None, "cleaning up: initial-core")

        hospice.any_alive = lambda: True
        vpy_pytest.current_env = mock_env  # type: ignore[assignment] # reset env for another run
        with pytest.raises(AssertionError, match="Expected all environments to be cleaned up"):
            ece.runtest()

    finally:
        hospice.any_alive = orig_any_alive
        hospice.freeze = orig_freeze


def test_plugin_integration(pytester: pytest.Pytester) -> None:
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
        vpy_pytest.current_policy = Policy(GlobalStore())
        vpy_pytest.current_policy.register()
