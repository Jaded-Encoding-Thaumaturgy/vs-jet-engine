# vs-engine
# Copyright (C) 2022  cid-chan
# Copyright (C) 2025  Jaded-Encoding-Thaumaturgy
# This project is licensed under the EUPL-1.2
# SPDX-License-Identifier: EUPL-1.2
"""Tests for the policy system."""

import contextlib
from collections.abc import Iterator

import pytest
import vapoursynth

from vsengine.policy import GlobalStore, Policy


@pytest.fixture
def policy() -> Iterator[Policy]:
    """Fixture that provides a fresh Policy instance."""
    p = Policy(GlobalStore())
    yield p
    with contextlib.suppress(RuntimeError):
        p.unregister()


class TestPolicy:
    """Tests for basic Policy functionality."""

    def test_register(self, policy: Policy) -> None:
        policy.register()
        try:
            assert policy.api is not None
        finally:
            policy.unregister()

    def test_unregister(self, policy: Policy) -> None:
        policy.register()
        policy.unregister()

        with pytest.raises(RuntimeError):
            policy.api.create_environment()

    def test_context_manager(self, policy: Policy) -> None:
        with policy:
            policy.api.create_environment()

        with pytest.raises(RuntimeError):
            policy.api.create_environment()

    def test_context_manager_on_error(self, policy: Policy) -> None:
        try:
            with policy:
                raise RuntimeError()
        except RuntimeError:
            pass

        with pytest.raises(RuntimeError):
            policy.api.create_environment()


class TestManagedEnvironment:
    """Tests for ManagedEnvironment functionality."""

    @pytest.fixture
    def store(self) -> GlobalStore:
        return GlobalStore()

    @pytest.fixture
    def registered_policy(self, store: GlobalStore) -> Iterator[Policy]:
        """Fixture that provides a registered Policy."""
        p = Policy(store)
        p.register()
        yield p
        with contextlib.suppress(RuntimeError):
            p.unregister()

    def test_new_environment_warns_on_del(self, registered_policy: Policy) -> None:
        env = registered_policy.new_environment()
        with pytest.warns(ResourceWarning):
            del env

    def test_new_environment_can_dispose(self, registered_policy: Policy) -> None:
        env = registered_policy.new_environment()
        env.dispose()
        with pytest.raises(RuntimeError), env.use():
            pass

    def test_new_environment_can_use_context(self, registered_policy: Policy) -> None:
        with registered_policy.new_environment() as env:
            with pytest.raises(vapoursynth.Error):
                vapoursynth.core.std.BlankClip().set_output(0)

            with env.use():
                vapoursynth.core.std.BlankClip().set_output(0)

            with pytest.raises(vapoursynth.Error):
                vapoursynth.core.std.BlankClip().set_output(0)

    def test_environment_can_switch(self, registered_policy: Policy) -> None:
        env = registered_policy.new_environment()
        with pytest.raises(vapoursynth.Error):
            vapoursynth.core.std.BlankClip().set_output(0)
        env.switch()
        vapoursynth.core.std.BlankClip().set_output(0)
        env.dispose()

    def test_environment_can_capture_outputs(self, registered_policy: Policy) -> None:
        with registered_policy.new_environment() as env1, registered_policy.new_environment() as env2:
            with env1.use():
                vapoursynth.core.std.BlankClip().set_output(0)

            assert len(env1.outputs) == 1
            assert len(env2.outputs) == 0

    def test_environment_can_capture_cores(self, registered_policy: Policy) -> None:
        with registered_policy.new_environment() as env1, registered_policy.new_environment() as env2:
            assert env1.core != env2.core

    def test_inline_section_is_invisible(self, store: GlobalStore, registered_policy: Policy) -> None:
        with registered_policy.new_environment() as env1, registered_policy.new_environment() as env2:
            env1.switch()

            env_before = store.get_current_environment()

            with env2.inline_section():
                assert vapoursynth.get_current_environment() != env1.vs_environment
                assert env_before == store.get_current_environment()

            assert vapoursynth.get_current_environment() == env1.vs_environment
            assert env_before == store.get_current_environment()
