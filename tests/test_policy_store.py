# vs-engine
# Copyright (C) 2022  cid-chan
# Copyright (C) 2025  Jaded-Encoding-Thaumaturgy
# This project is licensed under the EUPL-1.2
# SPDX-License-Identifier: EUPL-1.2
"""Tests for the policy environment stores."""

import concurrent.futures as futures
from collections.abc import Iterator
from contextvars import copy_context
from typing import Any

import pytest

from vsengine.policy import ContextVarStore, EnvironmentStore, GlobalStore, ThreadLocalStore


class BaseStoreTest:
    """Base class for environment store tests."""

    store: EnvironmentStore

    def create_store(self) -> EnvironmentStore:
        raise NotImplementedError

    @pytest.fixture(autouse=True)
    def setup_store(self) -> Iterator[None]:
        self.store = self.create_store()
        yield
        self.store.set_current_environment(None)

    def test_basic_functionality(self) -> None:
        assert self.store.get_current_environment() is None

        self.store.set_current_environment(1)  # type: ignore[arg-type]
        assert self.store.get_current_environment() == 1
        self.store.set_current_environment(2)  # type: ignore[arg-type]
        assert self.store.get_current_environment() == 2
        self.store.set_current_environment(None)
        assert self.store.get_current_environment() is None


class TestGlobalStore(BaseStoreTest):
    """Tests for GlobalStore."""

    def create_store(self) -> GlobalStore:
        return GlobalStore()


class TestThreadLocalStore(BaseStoreTest):
    """Tests for ThreadLocalStore."""

    def create_store(self) -> ThreadLocalStore:
        return ThreadLocalStore()

    def test_threads_do_not_influence_each_other(self) -> None:
        def thread() -> None:
            assert self.store.get_current_environment() is None
            self.store.set_current_environment(2)  # type: ignore[arg-type]
            assert self.store.get_current_environment() == 2

        with futures.ThreadPoolExecutor(max_workers=1) as e:
            self.store.set_current_environment(1)  # type: ignore[arg-type]
            e.submit(thread).result()
            assert self.store.get_current_environment() == 1


class TestContextVarStore(BaseStoreTest):
    """Tests for ContextVarStore."""

    def create_store(self) -> ContextVarStore:
        return ContextVarStore("store_test")

    def test_threads_do_not_influence_each_other(self) -> None:
        def thread() -> None:
            assert self.store.get_current_environment() is None
            self.store.set_current_environment(2)  # type: ignore[arg-type]
            assert self.store.get_current_environment() == 2

        with futures.ThreadPoolExecutor(max_workers=1) as e:
            self.store.set_current_environment(1)  # type: ignore[arg-type]
            e.submit(thread).result()
            assert self.store.get_current_environment() == 1

    def test_contexts_do_not_influence_each_other(self) -> None:
        def context(p: Any, n: Any) -> None:
            assert self.store.get_current_environment() == p
            self.store.set_current_environment(n)
            assert self.store.get_current_environment() == n

        ctx = copy_context()
        ctx.run(context, None, 1)
        assert self.store.get_current_environment() is None

        self.store.set_current_environment(2)  # type: ignore[arg-type]
        assert self.store.get_current_environment() == 2
        ctx.run(context, 1, 3)

        assert self.store.get_current_environment() == 2
