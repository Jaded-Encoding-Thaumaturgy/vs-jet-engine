# vs-engine
# Copyright (C) 2022  cid-chan
# Copyright (C) 2025  Jaded-Encoding-Thaumaturgy
# This project is licensed under the EUPL-1.2
# SPDX-License-Identifier: EUPL-1.2
"""Tests for the video module."""

from collections.abc import Iterator

import pytest
from vapoursynth import GRAY8, RGB24, PresetVideoFormat, VideoFormat, VideoFrame, VideoNode, core

from tests._testutils import use_standalone_policy
from vsengine.video import frame, frames, planes, render

AnyFormat = PresetVideoFormat | VideoFormat


@pytest.fixture(autouse=True)
def standalone_policy() -> Iterator[None]:
    """Set up a standalone policy for video tests."""
    use_standalone_policy()
    yield


def generate_video(length: int = 3, width: int = 1, height: int = 1, format: AnyFormat = GRAY8) -> VideoNode:
    """Generate a test video clip with frame numbers in props."""
    clip = core.std.BlankClip(length=length, width=width, height=height, format=format, fpsden=1001, fpsnum=24000)

    def _add_frameno(n: int, f: VideoFrame) -> VideoFrame:
        fout = f.copy()
        fout.props["FrameNumber"] = n
        return fout

    clip = core.std.ModifyFrame(clip=clip, clips=clip, selector=_add_frameno)
    return clip


def test_planes() -> None:
    clip_a = core.std.BlankClip(length=1, color=[0, 1, 2], width=1, height=1, format=RGB24)
    clip_b = core.std.BlankClip(length=1, color=[3, 4, 5], width=1, height=1, format=RGB24)

    clip = core.std.Splice([clip_a, clip_b])

    assert list(planes(clip, 0).result()) == [b"\x00", b"\x01", b"\x02"]
    assert list(planes(clip, 0, planes=[0]).result()) == [b"\x00"]
    assert list(planes(clip, 0, planes=[1]).result()) == [b"\x01"]
    assert list(planes(clip, 0, planes=[2]).result()) == [b"\x02"]
    assert list(planes(clip, 0, planes=[2, 1, 0]).result()) == [b"\x02", b"\x01", b"\x00"]

    assert list(planes(clip, 1).result()) == [b"\x03", b"\x04", b"\x05"]
    assert list(planes(clip, 1, planes=[0]).result()) == [b"\x03"]
    assert list(planes(clip, 1, planes=[1]).result()) == [b"\x04"]
    assert list(planes(clip, 1, planes=[2]).result()) == [b"\x05"]
    assert list(planes(clip, 1, planes=[2, 1, 0]).result()) == [b"\x05", b"\x04", b"\x03"]


def test_planes_default_supports_multiformat_clips() -> None:
    clip_a = core.std.BlankClip(length=1, color=[0, 1, 2], width=1, height=1, format=RGB24)
    clip_b = core.std.BlankClip(length=1, color=[3], width=1, height=1, format=GRAY8)

    clip = core.std.Splice([clip_a, clip_b], mismatch=True)
    assert list(planes(clip, 0).result()) == [b"\x00", b"\x01", b"\x02"]
    assert list(planes(clip, 1).result()) == [b"\x03"]


def test_single_frame() -> None:
    clip = generate_video()
    with frame(clip, 0).result(timeout=0.1) as f:
        assert f.props["FrameNumber"] == 0

    with frame(clip, 1).result(timeout=0.1) as f:
        assert f.props["FrameNumber"] == 1

    with frame(clip, 2).result(timeout=0.1) as f:
        assert f.props["FrameNumber"] == 2


def test_multiple_frames() -> None:
    clip = generate_video()
    for nf, f in enumerate(frames(clip)):
        assert f.props["FrameNumber"] == nf


def test_multiple_frames_closes_after_iteration() -> None:
    clip = generate_video()

    it = iter(frames(clip))
    f1 = next(it)

    try:
        f2 = next(it)
    except Exception:
        f1.close()
        raise

    try:
        with pytest.raises(RuntimeError):
            _ = f1.props
    finally:
        f2.close()
        next(it).close()


def test_multiple_frames_without_closing() -> None:
    clip = generate_video()
    for nf, f in enumerate(frames(clip, close=False)):
        assert f.props["FrameNumber"] == nf
        f.close()


def test_render() -> None:
    clip = generate_video()
    data = b"".join(f[1] for f in render(clip))
    assert data == b"\0\0\0"


def test_render_y4m() -> None:
    clip = generate_video()
    data = b"".join(f[1] for f in render(clip, y4m=True))
    assert data == b"YUV4MPEG2 Cmono W1 H1 F24000:1001 Ip A0:0 XLENGTH=3\nFRAME\n\0FRAME\n\0FRAME\n\0"
