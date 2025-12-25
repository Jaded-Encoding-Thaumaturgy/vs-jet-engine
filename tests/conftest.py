# vs-engine
# Copyright (C) 2022  cid-chan
# Copyright (C) 2025  Jaded-Encoding-Thaumaturgy
# This project is licensed under the EUPL-1.2
# SPDX-License-Identifier: EUPL-1.2
"""
Global pytest configuration and fixtures for vs-engine tests.
"""

from collections.abc import Iterator

import pytest

from tests._testutils import forcefully_unregister_policy
from vsengine.loops import NO_LOOP, set_loop


@pytest.fixture(autouse=True)
def clean_policy() -> Iterator[None]:
    """
    Global fixture that runs before and after every test.

    Ensures clean policy state by:
    - Unregistering any existing policy before the test
    - Unregistering any policy after the test
    - Resetting the event loop to NO_LOOP after the test
    """
    forcefully_unregister_policy()
    yield
    forcefully_unregister_policy()
    set_loop(NO_LOOP)
