# vs-engine
# Copyright (C) 2022  cid-chan
# Copyright (C) 2025  Jaded-Encoding-Thaumaturgy
# This project is licensed under the EUPL-1.2
# SPDX-License-Identifier: EUPL-1.2
import contextlib
from collections.abc import Iterator

import vapoursynth as vs

from vsengine.policy import ManagedEnvironment


# Automatically set the environment within that block.
@contextlib.contextmanager
def use_inline(function_name: str, env: vs.Environment | ManagedEnvironment | None) -> Iterator[None]:
    if env is None:
        # Ensure there is actually an environment set in this block.
        try:
            vs.get_current_environment()
        except Exception as e:
            raise OSError(
                f"You are currently not running within an environment. "
                f"Pass the environment directly to {function_name}."
            ) from e
        yield

    elif isinstance(env, ManagedEnvironment):
        with env.inline_section():
            yield

    else:
        with env.use():
            yield
