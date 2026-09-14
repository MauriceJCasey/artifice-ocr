# SPDX-FileCopyrightText: 2026 Maurice Casey
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Endpoint allowlist policy — which addresses this suite may send a model request to.

This module is the single owner of the rule. Every app that validates a model
endpoint must call through here rather than carrying its own copy.

The policy exists because "local-first" is a positive guarantee — the software
states what it permits rather than defaulting open and hoping the admin locked
it down. It lives in ``model-harness`` rather than in each app so a working
setup does not break differently in each app.
"""

from __future__ import annotations

import ipaddress
import os
import socket
from urllib.parse import urlparse

from model_harness.contract import EndpointRejected


def _default_always_allowed() -> frozenset[str]:
    """Hosts always permitted regardless of their resolved address."""
    return frozenset(
        h.lower()
        for h in [
            "localhost",
            "host.docker.internal",
            os.environ.get("WSL_HOST_IP", ""),
        ]
        if h
    )


def _read_allow_public_env() -> bool:
    """Check whether the user has opted into public-model endpoints."""
    return os.environ.get("ARTIFICE_ALLOW_PUBLIC_MODELS", "").strip().lower() in (
        "1",
        "true",
        "yes",
    )


class EndpointPolicy:
    """Decides which endpoints this local-first suite may call.

    This is deliberately NOT loopback-only.  Academics on a university network
    reach centrally-served models from a personal machine, so a private-network
    address is a first-class case rather than an escape hatch.  "Local-first"
    means the software never *requires* a remote service, not that it refuses
    one the user chose.

    What is still enforced:

    * the scheme must be ``http`` or ``https``
    * link-local addresses are refused outright, which is what keeps cloud
      metadata endpoints (169.254.169.254) out of reach
    * public addresses require an explicit opt-in, so a mistyped or injected
      hostname cannot quietly send prompts to the open internet

    Hostnames are resolved and *every* returned address is checked, because a
    name that resolves to one permitted and one public address is not permitted.
    Resolution here and connection later is a time-of-check gap; closing it
    needs the connection pinned to the validated address, which belongs in the
    provider adapter rather than in this policy.  Recorded, not solved.
    """

    def __init__(
        self,
        always_allowed_hosts: frozenset[str] | None = None,
        allow_public: bool | None = None,
    ) -> None:
        """Create a policy with optional overrides for testing.

        When *always_allowed_hosts* is ``None`` the default set of localhost,
        ``host.docker.internal`` and ``WSL_HOST_IP`` is used.  When
        *allow_public* is ``None`` the ``ARTIFICE_ALLOW_PUBLIC_MODELS``
        environment variable is read.
        """
        self._always_allowed: frozenset[str] = (
            always_allowed_hosts if always_allowed_hosts is not None else _default_always_allowed()
        )
        self._allow_public: bool = (
            allow_public if allow_public is not None else _read_allow_public_env()
        )

    # -- host classification --------------------------------------------------

    def classify_host(self, host: str) -> tuple[bool, str]:
        """Return ``(permitted, reason)`` for a URL host.

        Split out from :meth:`validate_url` so it can be tested directly
        without constructing URLs.
        """
        # Resolve first — the link-local denial must apply to *every* host
        # regardless of the allowlist.
        try:
            infos = socket.getaddrinfo(host, None)
        except OSError:
            if host in self._always_allowed:
                return True, "explicitly allowed host (unresolvable)"
            return False, f"host {host!r} could not be resolved"

        addresses = {ipaddress.ip_address(info[4][0]) for info in infos}
        if not addresses:
            if host in self._always_allowed:
                return True, "explicitly allowed host (no addresses)"
            return False, f"host {host!r} resolved to no addresses"

        # Link-local denial is unconditional — the allowlist does not
        # override the one absolute rule.
        for addr in sorted(addresses, key=str):
            if addr.is_link_local:
                return False, (
                    f"host {host!r} resolves to the link-local address {addr}, "
                    f"which is never permitted"
                )

        # The allowlist now means "skip the private/public classification"
        # rather than "skip every check".
        if host in self._always_allowed:
            return True, "explicitly allowed host"

        if all(addr.is_loopback or addr.is_private for addr in addresses):
            return True, "loopback or private-network address"

        if self._allow_public:
            return True, "public address, permitted by ARTIFICE_ALLOW_PUBLIC_MODELS"

        public = sorted(str(a) for a in addresses if not (a.is_loopback or a.is_private))
        return False, (
            f"host {host!r} resolves to the public address(es) {public}. "
            f"Set ARTIFICE_ALLOW_PUBLIC_MODELS=1 to permit endpoints outside your "
            f"own network."
        )

    # -- URL validation --------------------------------------------------------

    def validate_url(self, raw: str) -> str:
        """Return *raw* after checking its scheme and host.

        Raises :class:`EndpointRejected` (a harness-level exception, not an
        HTTP one) so this module does not depend on a web framework.  Callers
        that sit behind an HTTP layer wrap this in their own exception type.

        Fails closed, loudly — every rejection is an explicit ``EndpointRejected``.
        """
        try:
            parsed = urlparse(raw)
        except Exception:
            raise EndpointRejected(f"{raw!r} is not a valid URL") from None

        if parsed.scheme not in ("http", "https"):
            raise EndpointRejected(f"scheme must be http or https, got {parsed.scheme!r}")

        host = (parsed.hostname or "").lower()
        if not host:
            raise EndpointRejected(f"{raw!r} has no host")

        permitted, reason = self.classify_host(host)
        if not permitted:
            raise EndpointRejected(reason)
        return raw

    # -- Protocol conformance --------------------------------------------------

    def resolve(self, endpoint: str) -> str:
        """Implement the :class:`~model_harness.contract.EndpointPolicy` Protocol.

        Delegates to :meth:`validate_url`.
        """
        return self.validate_url(endpoint)
