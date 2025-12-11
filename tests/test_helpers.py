from collections.abc import Iterator
import pytest

import vapoursynth as vs
from vapoursynth import core

from vsengine._helpers import use_inline, wrap_variable_size
from vsengine._testutils import forcefully_unregister_policy, use_standalone_policy
from vsengine.policy import GlobalStore, Policy


@pytest.fixture(autouse=True)
def clean_policy() -> Iterator[None]:
    forcefully_unregister_policy()
    yield
    forcefully_unregister_policy()


class TestUseInline:
    def test_with_standalone(self) -> None:
        use_standalone_policy()
        with use_inline("test_with_standalone", None):
            pass

    def test_with_set_environment(self) -> None:
        with (
            Policy(GlobalStore()) as p,
            p.new_environment() as env,
            env.use(),
            use_inline("test_with_set_environment", None),
        ):
            pass

    def test_fails_without_an_environment(self) -> None:
        with (
            Policy(GlobalStore()),
            pytest.raises(OSError),
            use_inline("test_fails_without_an_environment", None),
        ):
            pass

    def test_accepts_a_managed_environment(self) -> None:
        with (
            Policy(GlobalStore()) as p,
            p.new_environment() as env,
            use_inline("test_accepts_a_managed_environment", env),
        ):
            assert env.vs_environment == vs.get_current_environment()

    def test_accepts_a_standard_environment(self) -> None:
        with (
            Policy(GlobalStore()) as p,
            p.new_environment() as env,
            use_inline("test_accepts_a_standard_environment", env.vs_environment),
        ):
            assert env.vs_environment == vs.get_current_environment()


class TestWrapVariable:
    @pytest.fixture(autouse=True)
    def setup_standalone(self) -> None:
        use_standalone_policy()

    def test_wrap_variable_bypasses_on_non_variable(self) -> None:
        bc = core.std.BlankClip()

        def _wrapper(c):
            assert c is bc
            return c

        wrap_variable_size(bc, bc.format, _wrapper)

    def test_wrap_caches_different_formats(self) -> None:
        bc24 = core.std.BlankClip(length=2)
        bc48 = core.std.BlankClip(format=vs.RGB48, length=2)
        sp = core.std.Splice([bc24, bc48, bc24, bc48], mismatch=True)

        counter = 0

        def _wrapper(c):
            nonlocal counter
            counter += 1
            return c.resize.Point(format=vs.RGB24)

        wrapped = wrap_variable_size(sp, vs.RGB24, _wrapper)
        for f in wrapped.frames():
            assert int(f.format) == vs.RGB24

        assert counter == 2
        assert int(wrapped.format) == vs.RGB24

    def test_wrap_caches_different_sizes(self) -> None:
        bc1 = core.std.BlankClip(length=2, width=2, height=2)
        bc2 = core.std.BlankClip(length=2, width=4, height=4)
        sp = core.std.Splice([bc1, bc2, bc1, bc2], mismatch=True)

        counter = 0

        def _wrapper(c: vs.VideoNode) -> vs.VideoNode:
            nonlocal counter
            counter += 1
            return c.resize.Point(format=vs.RGB24)

        wrapped = wrap_variable_size(sp, vs.RGB24, _wrapper)
        for f in wrapped.frames():
            assert int(f.format) == vs.RGB24
        assert counter == 2
        assert int(wrapped.format) == vs.RGB24

    def test_wrap_stops_caching_once_size_exceeded(self) -> None:
        bcs = [core.std.BlankClip(length=1, width=x, height=x) for x in range(1, 102)]
        assert len(bcs) == 101
        sp = core.std.Splice([*bcs, *bcs], mismatch=True)

        counter = 0

        def _wrapper(c: vs.VideoNode) -> vs.VideoNode:
            nonlocal counter
            counter += 1
            return c.resize.Point(format=vs.RGB24)

        wrapped = wrap_variable_size(sp, vs.RGB24, _wrapper)
        for _ in wrapped.frames():
            pass

        assert counter >= 101
