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
