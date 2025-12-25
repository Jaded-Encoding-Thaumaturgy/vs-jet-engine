# vs-engine
# Copyright (C) 2022  cid-chan
# Copyright (C) 2025  Jaded-Encoding-Thaumaturgy
# This project is licensed under the EUPL-1.2
# SPDX-License-Identifier: EUPL-1.2
"""
This allows test-cases to forcefully unregister a policy,
e.g. in case of test-failures.

This should ensure that failing tests can safely clean up the current policy.

It works by implementing a proxy policy
and monkey-patching vapoursynth.register_policy.

This policy is transparent to subsequent policies registering themselves.

To unregister a policy, run forcefully_unregister_policy.

As an addition,
it prevents VapourSynth from creating a vapoursynth.StandalonePolicy.
This ensures that no misbehaving test can accidentally prevent policy-based
tests from registering their own policies.

For policy-unrelated tests, use the function use_standalone_policy.
This function will build a policy which only ever uses one environment.
"""

from typing import Any

import vapoursynth as vs
from vapoursynth import Core, EnvironmentData, EnvironmentPolicy, EnvironmentPolicyAPI, core

from vsengine import policy as vsengine_policy
from vsengine._hospice import admit_environment

__all__ = ["BLACKBOARD", "forcefully_unregister_policy", "use_standalone_policy"]


BLACKBOARD = dict[Any, Any]()


class ProxyPolicy(EnvironmentPolicy):
    _api: EnvironmentPolicyAPI | None
    _policy: EnvironmentPolicy | None

    __slots__ = ("_api", "_policy")

    def __init__(self) -> None:
        self._api = None
        self._policy = None

    def attach_policy_to_proxy(self, policy: EnvironmentPolicy) -> None:
        if self._api is None:
            raise RuntimeError("This proxy is not active")

        if self._policy is not None:
            orig_register_policy(policy)
            raise SystemError("Unreachable code")

        self._policy = policy
        try:
            policy.on_policy_registered(EnvironmentPolicyAPIWrapper(self._api, self))  # type: ignore[arg-type]
        except:
            self._policy = None
            raise

    def forcefully_unregister_policy(self) -> None:
        if self._policy is None:
            return
        if self._api is None:
            return

        self._policy.on_policy_cleared()
        self._policy = None

        self._api.unregister_policy()
        orig_register_policy(self)

    def on_policy_registered(self, special_api: EnvironmentPolicyAPI) -> None:
        self._api = special_api
        # Patch both vapoursynth.register_policy and vsengine.policy.register_policy
        vs.register_policy = self.attach_policy_to_proxy
        vsengine_policy.register_policy = self.attach_policy_to_proxy  # type: ignore[attr-defined]

    def on_policy_cleared(self) -> None:
        try:
            if self._policy is not None:
                self._policy.on_policy_cleared()
        finally:
            self._policy = None
            self._api = None
            vs.register_policy = orig_register_policy
            vsengine_policy.register_policy = orig_register_policy  # type: ignore[attr-defined]

    def get_current_environment(self) -> EnvironmentData | None:
        if self._policy is None:
            raise RuntimeError("This proxy is not attached to a policy.")
        return self._policy.get_current_environment()

    def set_environment(self, environment: EnvironmentData | None) -> EnvironmentData | None:
        if self._policy is None:
            raise RuntimeError("This proxy is not attached to a policy.")
        return self._policy.set_environment(environment)

    def is_alive(self, environment: EnvironmentData) -> bool:
        if self._policy is None:
            raise RuntimeError("This proxy is not attached to a policy.")
        return self._policy.is_alive(environment)


class StandalonePolicy(EnvironmentPolicy):
    """A simple standalone policy that uses a single environment."""

    _current: EnvironmentData
    _api: EnvironmentPolicyAPI
    _core: Core
    __slots__ = ("_api", "_core", "_current")

    def on_policy_registered(self, special_api: EnvironmentPolicyAPI) -> None:
        self._api = special_api
        self._current = special_api.create_environment()
        self._core = core.core

    def on_policy_cleared(self) -> None:
        admit_environment(self._current, self._core)

        del self._current
        del self._core

    def get_current_environment(self) -> EnvironmentData:
        return self._current

    def set_environment(self, environment: EnvironmentData | None) -> EnvironmentData | None:
        if environment is not None and environment is not self._current:
            raise RuntimeError("No other environments should exist.")
        return None

    def is_alive(self, environment: EnvironmentData) -> bool:
        return self._current is environment


orig_register_policy = vs.register_policy


class EnvironmentPolicyAPIWrapper:
    _api: EnvironmentPolicyAPI
    _proxy: ProxyPolicy

    __slots__ = ("_api", "_proxy")

    def __init__(self, api: EnvironmentPolicyAPI, proxy: ProxyPolicy) -> None:
        self._api = api
        self._proxy = proxy

    def __getattr__(self, name: str) -> Any:
        return getattr(self._api, name)

    def unregister_policy(self) -> None:
        self._proxy.forcefully_unregister_policy()


_policy = ProxyPolicy()
orig_register_policy(_policy)

forcefully_unregister_policy = _policy.forcefully_unregister_policy


def use_standalone_policy() -> None:
    """Register a standalone policy for tests that don't need custom policies."""
    _policy.attach_policy_to_proxy(StandalonePolicy())
