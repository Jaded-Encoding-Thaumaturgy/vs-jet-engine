# vs-engine
# Copyright (C) 2022  cid-chan
# Copyright (C) 2025  Jaded-Encoding-Thaumaturgy
# This project is licensed under the EUPL-1.2
# SPDX-License-Identifier: EUPL-1.2
"""Tests for the policy environment stores."""

import sys
from abc import abstractmethod
from collections.abc import Generator
from concurrent import futures
from contextvars import copy_context
from typing import Any
from unittest.mock import MagicMock

import pytest
from pytest_mock import MockerFixture

from vsengine.policy import ContextVarStore, EnvironmentStore, GlobalStore, ThreadLocalStore


class BaseStoreTest:
    """Base class for environment store tests."""

    store: EnvironmentStore

    @abstractmethod
    def create_store(self) -> EnvironmentStore: ...

    @pytest.fixture(autouse=True)
    def setup_store(self) -> Generator[None]:
        self.store = self.create_store()
        try:
            yield
        finally:
            self.store.set_current_environment(None)

    def test_basic_functionality(self, mocker: MockerFixture) -> None:
        assert self.store.get_current_environment() is None

        one = mocker.MagicMock()
        self.store.set_current_environment(one)
        assert self.store.get_current_environment() == one

        two = mocker.MagicMock()
        self.store.set_current_environment(two)
        assert self.store.get_current_environment() == two

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

    def test_threads_do_not_influence_each_other(self, mocker: MockerFixture) -> None:
        def thread() -> None:
            assert self.store.get_current_environment() is None
            mock_two = mocker.MagicMock()
            self.store.set_current_environment(mock_two)
            assert self.store.get_current_environment() == mock_two

        with futures.ThreadPoolExecutor(max_workers=1) as e:
            mock_one = mocker.MagicMock()
            self.store.set_current_environment(mock_one)
            e.submit(thread).result()
            assert self.store.get_current_environment() == mock_one


class TestContextVarStore(BaseStoreTest):
    """Tests for ContextVarStore."""

    def create_store(self) -> ContextVarStore:
        return ContextVarStore("store_test")

    def test_threads_do_not_influence_each_other(self, mocker: MockerFixture) -> None:
        mock_one = mocker.MagicMock(name="one")
        expected_initial = mock_one if getattr(sys.flags, "thread_inherit_context", 0) else None

        def thread() -> None:
            assert self.store.get_current_environment() == expected_initial
            mock_two = mocker.MagicMock(name="two")
            self.store.set_current_environment(mock_two)
            assert self.store.get_current_environment() == mock_two

        with futures.ThreadPoolExecutor(max_workers=1) as e:
            self.store.set_current_environment(mock_one)
            e.submit(thread).result()
            assert self.store.get_current_environment() == mock_one

    def test_contexts_do_not_influence_each_other(self, mocker: MockerFixture) -> None:
        def context(p: Any, n: MagicMock) -> None:
            assert self.store.get_current_environment() == p
            self.store.set_current_environment(n)
            assert self.store.get_current_environment() == n

        mock_one = mocker.MagicMock()
        mock_two = mocker.MagicMock()
        mock_three = mocker.MagicMock()
        ctx = copy_context()

        ctx.run(context, None, mock_one)
        assert self.store.get_current_environment() is None

        self.store.set_current_environment(mock_two)
        assert self.store.get_current_environment() == mock_two
        ctx.run(context, mock_one, mock_three)

        assert self.store.get_current_environment() == mock_two
