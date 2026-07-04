# vs-engine
# Copyright (C) 2022  cid-chan
# Copyright (C) 2026  Jaded-Encoding-Thaumaturgy
# This project is licensed under the EUPL-1.2
# SPDX-License-Identifier: EUPL-1.2
from __future__ import annotations

from collections import defaultdict
from collections.abc import Generator
from pathlib import Path
from typing import Any

import pluggy
import pytest
from _pytest._code.code import TerminalRepr, TracebackStyle
from _pytest._io import TerminalWriter

import vsengine._hospice as hospice
from vsengine.policy import GlobalStore, ManagedEnvironment, Policy

DEFAULT_STAGES = ("initial-core", "reloaded-core")
KNOWN_STAGES = ("no-core", "initial-core", "reloaded-core", "unique-core")

DEFAULT_ERROR_MESSAGE = (
    "Your test suite left a dangling object to a vapoursynth core.",
    "Please make sure this does not happen, as this might cause some previewers to crash after reloading a script.",
)


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers",
        'vpy(*stages: Literal["no-core", "initial-core", "reloaded-core", "unique-core"]): '
        "Mark what stages should be run. (Defaults to initial-core + reloaded-core)",
    )


current_policy: Policy | None = None
current_env: ManagedEnvironment | None = None
current_stage = "no-core"



def pytest_sessionstart(session: pytest.Session) -> None:
    global current_policy
    current_policy = Policy(GlobalStore())
    current_policy.register()


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    global current_policy, current_env
    if current_policy is not None and not current_policy.is_registered:
        current_policy.register()
    if current_env is not None:
        current_env.dispose()
        current_env = None
    if current_policy is not None:
        current_policy.unregister()
        current_policy = None


def pytest_generate_tests(metafunc: pytest.Metafunc) -> None:
    if (marker := metafunc.definition.get_closest_marker("vpy")) is not None:
        stages = marker.args or DEFAULT_STAGES
        metafunc.fixturenames.append("vpy_stage")
        metafunc.parametrize("vpy_stage", list(stages), ids=stages)


def pytest_collection_modifyitems(session: pytest.Session, config: pytest.Config, items: list[pytest.Item]) -> None:
    if config.option.collectonly:
        return

    # Group items by their file path to preserve module boundaries
    module_items = defaultdict[Path, list[pytest.Item]](list)
    for item in items:
        module_items[item.path].append(item)

    new_items: list[pytest.Item] = []

    for path, m_items in module_items.items():
        stages: dict[str, list[pytest.Item]] = {stage: [] for stage in KNOWN_STAGES}
        other_items: list[pytest.Item] = []

        for item in m_items:
            callspec = getattr(item, "callspec", None)
            if callspec is not None and "vpy_stage" in callspec.params:
                stage = callspec.params["vpy_stage"]
                if stage in stages:
                    stages[stage].append(item)
                else:
                    other_items.append(item)
            else:
                other_items.append(item)

        vpy_items_present = any(len(stages[stage]) > 0 for stage in stages)
        if vpy_items_present:
            # Find a suitable parent collector (the parent of the first vpy item, typically a Module)
            first_vpy_item = next(item for stage in stages for item in stages[stage] if item)
            parent_collector = first_vpy_item.parent or session

            for stage in KNOWN_STAGES:
                new_items.extend(stages[stage])
                if stage in ("initial-core", "reloaded-core") and len(stages[stage]) > 0:
                    new_items.append(
                        EnsureCleanEnvironment.from_parent(
                            parent_collector,
                            name=f"@check-clean-environment[{stage}]",
                            stage=stage,
                            path=path,
                        )
                    )
            new_items.extend(other_items)
        else:
            new_items.extend(m_items)

    items[:] = new_items


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_call(item: pytest.Item) -> Generator[None, pluggy.Result[Any], None]:

    global current_stage, current_env, current_policy

    callspec = getattr(item, "callspec", None)
    if callspec is None or "vpy_stage" not in callspec.params:
        yield
        return

    stage = str(callspec.params.get("vpy_stage", "no-core"))

    if current_policy is None:
        current_policy = Policy(GlobalStore())

    if not current_policy.is_registered:
        current_policy.register()

    if stage != current_stage:
        if stage == "initial-core":
            current_env = current_policy.new_environment()
        elif stage == "reloaded-core":
            if current_env is not None:
                current_env.dispose()
            current_env = current_policy.new_environment()
        elif stage in ("unique-core", "no-core"):
            if current_env is not None:
                current_env.dispose()
            current_env = None

        current_stage = stage

    if stage == "unique-core":
        env = current_policy.new_environment()
        try:
            with env.use():
                outcome = yield
        finally:
            env.dispose()

        failed = outcome.excinfo[1] if outcome.excinfo is not None else None

        if hospice.any_alive():
            hospice.freeze()
            setattr(item, "_vpy_leaked", True)
            if failed is None:
                outcome.force_exception(AssertionError("Expected all environments to be cleaned up."))

    elif current_env is not None:
        with current_env.use():
            yield
    else:
        yield


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(
    item: pytest.Item, call: pytest.CallInfo[None]
) -> Generator[None, pluggy.Result[pytest.TestReport], None]:
    outcome = yield
    report = outcome.get_result()
    if report.when == "call" and getattr(item, "_vpy_leaked", False):
        err_message = "\n".join(DEFAULT_ERROR_MESSAGE)
        report.longrepr = CleanupFailed(report.longrepr, err_message)


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
        global current_env, current_policy
        if current_policy is not None and not current_policy.is_registered:
            current_policy.register()
        try:
            if current_env is not None:
                current_env.dispose()
        finally:
            current_env = None
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
