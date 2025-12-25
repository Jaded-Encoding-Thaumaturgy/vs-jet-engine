# vs-engine
# Copyright (C) 2022  cid-chan
# Copyright (C) 2025  Jaded-Encoding-Thaumaturgy
# This project is licensed under the EUPL-1.2
# SPDX-License-Identifier: EUPL-1.2
"""
vsengine - A common set of function that bridge vapoursynth with your application.

Parts:
- loops:   Integrate vsengine with your event-loop (be it GUI-based or IO-based).
- policy:  Create new isolated cores as needed.
- video:   Get frames or render the video. Sans-IO and memory safe.
- vpy:     Run .vpy-scripts in your application.
"""

from vsengine.loops import *
from vsengine.policy import *
from vsengine.video import *
from vsengine.vpy import *

__version__: str
__version_tuple__: tuple[int | str, ...]

try:
    from ._version import __version__, __version_tuple__
except ImportError:
    __version__ = "0.0.0+unknown"
    __version_tuple__ = (0, 0, 0, "+unknown")
