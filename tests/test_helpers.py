# vs-engine
# Copyright (C) 2022  cid-chan
# Copyright (C) 2025  Jaded-Encoding-Thaumaturgy
# This project is licensed under the EUPL-1.2
# SPDX-License-Identifier: EUPL-1.2

import pytest
import vapoursynth as vs

from tests._testutils import use_standalone_policy
from vsengine._helpers import use_inline
from vsengine.policy import GlobalStore, Policy


def test_use_inline_with_standalone() -> None:
    use_standalone_policy()
    with use_inline("test_with_standalone", None):
        pass


def test_use_inline_with_set_environment() -> None:
    with (
        Policy(GlobalStore()) as p,
        p.new_environment() as env,
        env.use(),
        use_inline("test_with_set_environment", None),
    ):
        pass


def test_use_inline_fails_without_an_environment() -> None:
    with (
        Policy(GlobalStore()),
        pytest.raises(OSError),
        use_inline("test_fails_without_an_environment", None),
    ):
        pass


def test_use_inline_accepts_a_managed_environment() -> None:
    with (
        Policy(GlobalStore()) as p,
        p.new_environment() as env,
        use_inline("test_accepts_a_managed_environment", env),
    ):
        assert env.vs_environment == vs.get_current_environment()


def test_use_inline_accepts_a_standard_environment() -> None:
    with (
        Policy(GlobalStore()) as p,
        p.new_environment() as env,
        use_inline("test_accepts_a_standard_environment", env.vs_environment),
    ):
        assert env.vs_environment == vs.get_current_environment()
