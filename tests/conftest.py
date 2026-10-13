# vs-engine
# Copyright (C) 2022  cid-chan
# Copyright (C) 2025  Jaded-Encoding-Thaumaturgy
# This project is licensed under the EUPL-1.2
# SPDX-License-Identifier: EUPL-1.2
"""
Global pytest configuration and fixtures for vs-engine tests.
"""

from collections.abc import Generator

import pytest

from tests._testutils import forcefully_unregister_policy
from vsengine import _hospice as hospice
from vsengine.loops import NO_LOOP, set_loop


@pytest.fixture(autouse=True)
def clean_policy() -> Generator[None]:
    """
    Global fixture that runs before and after every test.

    Ensures clean policy state by:
    - Unregistering any existing policy before the test
    - Unregistering any policy after the test
    - Resetting the event loop to NO_LOOP after the test
    """
    forcefully_unregister_policy()
    try:
        yield
    finally:
        forcefully_unregister_policy()
        set_loop(NO_LOOP)


@pytest.fixture(autouse=True)
def reset_hospice_state() -> Generator[None]:
    """Reset hospice module state before each test to ensure isolation."""
    hospice.stage1.clear()
    hospice.stage2.clear()
    hospice.stage2_to_add.clear()
    hospice.hold.clear()
    hospice.cores.clear()
    hospice.refnanny.clear()

    try:
        yield
    finally:
        hospice.stage1.clear()
        hospice.stage2.clear()
        hospice.stage2_to_add.clear()
        hospice.hold.clear()
        hospice.cores.clear()
        hospice.refnanny.clear()
