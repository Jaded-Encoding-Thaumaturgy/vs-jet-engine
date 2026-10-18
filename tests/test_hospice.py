# vs-engine
# Copyright (C) 2022  cid-chan
# Copyright (C) 2025  Jaded-Encoding-Thaumaturgy
# This project is licensed under the EUPL-1.2
# SPDX-License-Identifier: EUPL-1.2
"""Tests for the hospice module (delayed object cleanup)."""

import contextlib
import gc
import logging
import weakref
from collections.abc import Callable, Generator
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import pytest
from pytest_mock import MockerFixture
from vapoursynth import Core, EnvironmentData

import vsengine._hospice as hospices


@pytest.fixture(autouse=True)
def reset_mock_registry() -> Generator[None]:
    """Reset the mock timings registry before each test."""
    mock_timings_registry.clear()
    try:
        yield
    finally:
        mock_timings_registry.clear()


# Global registry to simulate CoreTimings holding references to cores
# This adds the extra reference needed for the > 3 refcount check
mock_timings_registry = list[Any]()


@pytest.fixture
def fake_data_factory(mocker: MockerFixture) -> Callable[[], EnvironmentData]:
    return lambda: mocker.Mock(spec=EnvironmentData)


@pytest.fixture
def fake_core_factory(mocker: MockerFixture) -> Callable[[], Core]:
    def factory() -> Core:
        mock = mocker.Mock(spec=Core)
        mock_timings_registry.append(mock)
        return mock

    return factory


@contextlib.contextmanager
def hide_logs() -> Generator[None]:
    logging.disable(logging.CRITICAL)
    try:
        yield
    finally:
        logging.disable(logging.NOTSET)


def test_hospice_delays_connection(
    fake_data_factory: Callable[[], EnvironmentData],
    fake_core_factory: Callable[[], Core],
) -> None:
    o1 = fake_data_factory()
    o2 = fake_core_factory()
    o2_r = weakref.ref(o2)

    hospices.admit_environment(o1, o2)

    # Remove local ref to o2, but registry still holds it
    del o2
    del o1

    # Clear the mock registry to release the "CoreTimings" reference
    mock_timings_registry.clear()

    assert o2_r() is not None

    gc.collect()
    assert o2_r() is not None

    # Stage-2 add-queue + Stage 2 proper
    gc.collect()
    gc.collect()

    assert o2_r() is None


def test_hospice_is_delayed_on_alive_objects(
    caplog: pytest.LogCaptureFixture,
    fake_data_factory: Callable[[], EnvironmentData],
    fake_core_factory: Callable[[], Core],
) -> None:
    o1 = fake_data_factory()
    o2 = fake_core_factory()
    o2r = weakref.ref(o2)

    hospices.admit_environment(o1, o2)
    del o1

    # o2 is still held by local var AND registry, so refcount > 3
    with caplog.at_level(logging.WARNING, logger="vsengine._hospice"):
        gc.collect()

    assert len(caplog.records) > 0

    # Delete local ref but keep registry - still should delay collection
    del o2
    assert o2r() is not None

    # Now clear registry to allow collection
    mock_timings_registry.clear()

    gc.collect()
    gc.collect()
    gc.collect()

    assert o2r() is None


def test_hospice_reports_alive_objects_correctly(
    fake_data_factory: Callable[[], EnvironmentData],
    fake_core_factory: Callable[[], Core],
) -> None:
    o1 = fake_data_factory()
    o2 = fake_core_factory()
    hospices.admit_environment(o1, o2)
    del o1

    # o2 is still alive (local var + registry)
    with hide_logs():
        assert hospices.any_alive(), (
            "The hospice did report that all objects are not alive anymore. This is obviously not true."
        )

    # Delete local ref but keep registry - still delays as "alive" due to CoreTimings-like ref
    del o2

    # Now clear the registry to allow collection
    mock_timings_registry.clear()

    assert not hospices.any_alive(), (
        "The hospice did report that there are some objects left alive. This is obviously not true."
    )


def test_hospice_can_forget_about_cores_safely(
    fake_data_factory: Callable[[], EnvironmentData],
    fake_core_factory: Callable[[], Core],
) -> None:
    o1 = fake_data_factory()
    o2 = fake_core_factory()
    hospices.admit_environment(o1, o2)
    del o1

    with hide_logs():
        assert hospices.any_alive(), (
            "The hospice did report that all objects are not alive anymore. This is obviously not true."
        )
    hospices.freeze()
    assert not hospices.any_alive(), (
        "The hospice did report that there are some objects left alive. This is obviously not true."
    )

    hospices.unfreeze()
    with hide_logs():
        assert hospices.any_alive(), (
            "The hospice did report that all objects are not alive anymore. This is obviously not true."
        )
    del o2
    mock_timings_registry.clear()

    gc.collect()


def test_hospice_concurrent_access(
    fake_data_factory: Callable[[], EnvironmentData],
    fake_core_factory: Callable[[], Core],
) -> None:
    def worker(i: int) -> None:
        env = fake_data_factory()
        core = fake_core_factory()
        hospices.admit_environment(env, core)
        del env
        if i % 3 == 0:
            hospices.freeze()
            hospices.unfreeze()
        elif i % 3 == 1:
            _ = hospices.any_alive()
        else:
            gc.collect()

    with ThreadPoolExecutor(max_workers=8) as ex:
        list(ex.map(worker, range(40)))

    mock_timings_registry.clear()
    gc.collect()


def test_hospice_refnanny_cleanup(
    fake_data_factory: Callable[[], EnvironmentData],
    fake_core_factory: Callable[[], Core],
) -> None:
    data = fake_data_factory()
    data_ref = weakref.ref(data)
    core = fake_core_factory()

    hospices.admit_environment(data, core)
    assert any(r() is data for r in hospices.refnanny.values())

    del data
    mock_timings_registry.clear()
    gc.collect()

    assert data_ref() is None
    assert len(hospices.refnanny) == 0
