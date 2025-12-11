from collections.abc import Iterator

import pytest
import vapoursynth as vs

from tests._testutils import forcefully_unregister_policy, use_standalone_policy
from vsengine._helpers import use_inline
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
