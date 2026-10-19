# vs-engine
# Copyright (C) 2022  cid-chan
# Copyright (C) 2025  Jaded-Encoding-Thaumaturgy
# This project is licensed under the EUPL-1.2
# SPDX-License-Identifier: EUPL-1.2
"""Tests for the video module."""

from collections.abc import Generator
from concurrent.futures import Future

import pytest
import pytest_mock
import vapoursynth as vs

from tests._testutils import use_standalone_policy
from vsengine import set_loop
from vsengine._nodes import buffer_futures, close_when_needed
from vsengine.adapters.asyncio import AsyncIOLoop
from vsengine.loops import NO_LOOP
from vsengine.video import frame, frames, planes, render

AnyFormat = vs.PresetVideoFormat | vs.VideoFormat
core = vs.core


@pytest.fixture(autouse=True)
def standalone_policy() -> None:
    """Set up a standalone policy for video tests."""
    use_standalone_policy()


@pytest.fixture
def asyncio_loop() -> Generator[None]:
    set_loop(AsyncIOLoop())
    try:
        yield
    finally:
        set_loop(NO_LOOP)


def generate_video(length: int = 3, width: int = 1, height: int = 1, format: AnyFormat = vs.GRAY8) -> vs.VideoNode:
    """Generate a test video clip with frame numbers in props."""
    clip = core.std.BlankClip(length=length, width=width, height=height, format=format, fpsden=1001, fpsnum=24000)

    def add_frameno(n: int, f: vs.VideoFrame) -> vs.VideoFrame:
        fout = f.copy()
        fout.props["FrameNumber"] = n
        return fout

    return core.std.ModifyFrame(clip=clip, clips=clip, selector=add_frameno)


def test_planes() -> None:
    clip_a = core.std.BlankClip(length=1, color=[0, 1, 2], width=1, height=1, format=vs.RGB24)
    clip_b = core.std.BlankClip(length=1, color=[3, 4, 5], width=1, height=1, format=vs.RGB24)

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
    clip_a = core.std.BlankClip(length=1, color=[0, 1, 2], width=1, height=1, format=vs.RGB24)
    clip_b = core.std.BlankClip(length=1, color=[3], width=1, height=1, format=vs.GRAY8)

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


def test_render_frame_numbers_async_futures() -> None:
    clip = generate_video(length=5)
    r = render(clip)
    futs = list(r.futures)
    assert [f.result()[0] for f in futs] == [1, 2, 3, 4, 5]


def test_render_y4m() -> None:
    clip = generate_video()
    data = b"".join(f[1] for f in render(clip, y4m=True))
    assert data == b"YUV4MPEG2 Cmono W1 H1 F24000:1001 Ip A0:0 XLENGTH=3\nFRAME\n\0FRAME\n\0FRAME\n\0"


@pytest.mark.parametrize(
    ("clip_format", "csp_tag"),
    [
        (vs.YUV420P8, b"C420"),
        (vs.YUV422P8, b"C422"),
        (vs.YUV444P8, b"C444"),
        (vs.YUV410P8, b"C410"),
        (vs.YUV411P8, b"C411"),
        (vs.YUV440P8, b"C440"),
        (vs.YUV420P10, b"C420p10"),
        (vs.YUV420P16, b"C420p16"),
        (vs.GRAY10, b"Cmonop10"),
        (vs.GRAY16, b"Cmonop16"),
    ],
)
def test_render_y4m_headers(clip_format: vs.PresetVideoFormat, csp_tag: bytes) -> None:
    clip = core.std.BlankClip(length=1, width=4, height=4, format=clip_format)
    data = b"".join(f[1] for f in render(clip, y4m=True))
    header = b"YUV4MPEG2 " + csp_tag + b" W4 H4 F24:1 Ip A0:0 XLENGTH=1\n"
    assert data.startswith(header)


def test_render_y4m_rgb_raises() -> None:
    clip = core.std.BlankClip(length=1, width=2, height=2, format=vs.RGB24)
    with pytest.raises(ValueError, match="Can only use GRAY and YUV"):
        list(render(clip, y4m=True))


def test_render_frame_numbers() -> None:
    clip = generate_video(length=5)
    frame_numbers = [f[0] for f in render(clip)]
    assert frame_numbers == [1, 2, 3, 4, 5]


def test_render_frame_numbers_y4m() -> None:
    """Test that render with Y4M returns correct frame numbers (0 for header, then 1-based)."""
    clip = generate_video(length=3)
    frame_numbers = [f[0] for f in render(clip, y4m=True)]
    # First tuple is the header (frame 0), then frame 1, 2, 3
    assert frame_numbers == [0, 1, 2, 3]


def test_render_with_prefetch() -> None:
    clip = generate_video(length=5)
    data = b"".join(f[1] for f in render(clip, prefetch=2))
    assert data == b"\0\0\0\0\0"


def test_render_with_backlog() -> None:
    clip = generate_video(length=5)
    # backlog=None means no limit on queued frames
    data = b"".join(f[1] for f in render(clip, backlog=None))
    assert data == b"\0\0\0\0\0"


def test_render_with_prefetch_and_backlog() -> None:
    clip = generate_video(length=5)
    data = b"".join(f[1] for f in render(clip, prefetch=3, backlog=5))
    assert data == b"\0\0\0\0\0"


def test_render_single_frame() -> None:
    clip = core.std.BlankClip(length=1, width=1, height=1, format=vs.GRAY8)
    data = list(render(clip))
    assert len(data) == 1
    assert data[0][0] == 1
    assert data[0][1] == b"\0"


def test_render_single_frame_y4m() -> None:
    clip = core.std.BlankClip(length=1, width=1, height=1, format=vs.GRAY8)
    data = list(render(clip, y4m=True))
    # Should have header at index 0, frame at index 1
    assert len(data) == 2
    assert data[0][0] == 0  # Header is frame 0
    assert data[0][1].startswith(b"YUV4MPEG2")
    assert data[1][0] == 1  # First frame is 1
    assert b"FRAME\n" in data[1][1]


def test_render_exception_handling() -> None:
    clip = generate_video(length=3)

    # Modify frame 1 to raise an exception
    def add_frameno_with_error(n: int, f: vs.VideoFrame) -> vs.VideoFrame:
        if n == 1:
            raise RuntimeError("Intentional error for testing")
        fout = f.copy()
        fout.props["FrameNumber"] = n
        return fout

    clip_err = core.std.ModifyFrame(clip=clip, clips=clip, selector=add_frameno_with_error)

    # Test close_when_needed exception propagation
    # We set prefetch/backlog to 0/None to test close_when_needed without buffer_futures
    with pytest.raises(vs.Error, match="Intentional error for testing"):
        list(frames(clip_err, prefetch=0, backlog=0))

    # Test buffer_futures exception handling
    # Render uses buffer_futures, and should raise the exception.
    with pytest.raises(vs.Error, match="Intentional error for testing"):
        list(render(clip_err, prefetch=2, backlog=4))


def test_render_backlog_less_than_prefetch() -> None:
    clip = generate_video(length=5)
    # This will trigger backlog = prefetch line in buffer_futures
    data = b"".join(f[1] for f in render(clip, prefetch=4, backlog=2))
    assert data == b"\0\0\0\0\0"


def test_buffer_futures_resolved_and_cancelled() -> None:
    # Test already resolved futures
    f1 = Future[vs.VideoFrame]()
    clip = generate_video(length=2)
    frame0 = clip.get_frame(0)
    f1.set_result(frame0)

    f2 = Future[vs.VideoFrame]()
    f2.set_result(clip.get_frame(1))

    res = list(buffer_futures([f1, f2], prefetch=2))
    assert len(res) == 2
    assert res[0].result().props["FrameNumber"] == 0
    assert res[1].result().props["FrameNumber"] == 1

    # Test cancelled future in buffer_futures
    fc1 = Future[vs.VideoFrame]()
    fc1.cancel()
    fc2 = Future[vs.VideoFrame]()
    fc2.set_result(frame0)

    res_cancel = list(buffer_futures([fc1, fc2], prefetch=2))
    assert res_cancel[0].cancelled()

    # Test close_when_needed with cancelled future
    fc3 = Future[vs.VideoFrame]()
    fc3.cancel()
    res_close = list(close_when_needed([fc3]))
    assert len(res_close) == 1
    assert res_close[0].cancelled()


def test_buffer_futures_early_exit() -> None:
    futs = [Future[vs.VideoFrame]() for _ in range(10)]
    gen = buffer_futures(futs, prefetch=4, backlog=8)
    first = next(gen)
    clip = generate_video(length=1)
    first.set_result(clip.get_frame(0))
    assert first.result().props["FrameNumber"] == 0

    # Closing generator early should cancel queued futures in backlog/reorder
    gen.close()
    cancelled_count = sum(1 for f in futs if f.cancelled())
    assert cancelled_count > 0


def test_render_y4m_float_raises() -> None:
    clip = core.std.BlankClip(length=2, width=16, height=16, format=vs.GRAYS)
    with pytest.raises(ValueError, match="Can only use integer formats for Y4M-Streams"):
        list(render(clip, y4m=True))


def test_render_y4m_variable_resolution_raises() -> None:
    clip_a = core.std.BlankClip(length=1, width=16, height=16, format=vs.GRAY8)
    clip_b = core.std.BlankClip(length=1, width=32, height=32, format=vs.GRAY8)
    var_clip = core.std.Splice([clip_a, clip_b], mismatch=True)

    with pytest.raises(ValueError, match="Cannot render variable format or variable resolution clip to Y4M"):
        list(render(var_clip, y4m=True))


@pytest.mark.asyncio
async def test_render_as_completed_does_not_release_frames_early(asyncio_loop: None) -> None:
    clip = generate_video(length=6)
    results = list[tuple[int, bytes]]()

    results = [fut async for fut in render(clip)]

    assert len(results) == 6
    assert {idx for idx, _ in results} == {1, 2, 3, 4, 5, 6}


def test_close_when_needed_early_break_closes_frame() -> None:
    clip = generate_video(length=4)
    it = iter(close_when_needed([frame(clip, n) for n in range(4)]))

    f = next(it)
    frame_obj = f.result()
    it.close()

    # Closing iterator should have registered the exit callback and closed the frame
    with pytest.raises(RuntimeError):
        _ = frame_obj.props


def test_buffer_futures_source_exception() -> None:
    def broken_generator() -> Generator[Future[vs.VideoFrame]]:
        yield Future[vs.VideoFrame]()
        raise RuntimeError("source generator failed")

    with pytest.raises(RuntimeError, match="source generator failed"):
        buffer_futures(broken_generator(), prefetch=2)


def test_buffer_futures_throw() -> None:
    futs = [Future[vs.VideoFrame]() for _ in range(5)]
    gen = buffer_futures(futs, prefetch=2, backlog=4)

    with pytest.raises(ValueError, match="throw test"):
        gen.throw(ValueError("throw test"))

    assert gen.finished
    assert any(f.cancelled() for f in futs)


def test_close_when_needed_result_exception(mocker: pytest_mock.MockerFixture) -> None:
    mock_frame = mocker.MagicMock()
    fut = Future[vs.VideoFrame]()

    mocker.patch.object(fut, "exception", return_value=None)
    mocker.patch.object(fut, "result", side_effect=[RuntimeError("result error"), mock_frame])

    gen = close_when_needed([fut])
    wrapped_fut = next(iter(gen))
    fut.set_result(mock_frame)

    with pytest.raises(RuntimeError, match="result error"):
        wrapped_fut.result()
