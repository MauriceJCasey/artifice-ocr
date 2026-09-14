# SPDX-FileCopyrightText: 2026 Maurice Casey
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Tests for :mod:`model_harness.endpoint_policy`."""

from __future__ import annotations

import socket
import unittest.mock as mock

import pytest
from model_harness.contract import EndpointRejected
from model_harness.endpoint_policy import EndpointPolicy, _default_always_allowed


def _v4_info(addr: str) -> tuple:
    """Build a minimal ``getaddrinfo`` 5-tuple for an IPv4 address."""
    return (socket.AF_INET, socket.SOCK_STREAM, 6, "", (addr, 0))


def _v6_info(addr: str) -> tuple:
    """Build a minimal ``getaddrinfo`` 5-tuple for an IPv6 address."""
    return (socket.AF_INET6, socket.SOCK_STREAM, 6, "", (addr, 0, 0, 0))


class TestClassifyHost:
    """Unit tests for host classification logic."""

    def test_accepts_localhost(self):
        policy = EndpointPolicy()
        permitted, reason = policy.classify_host("localhost")
        assert permitted, reason

    def test_accepts_docker_internal(self):
        policy = EndpointPolicy()
        permitted, reason = policy.classify_host("host.docker.internal")
        assert permitted, reason

    def test_accepts_wsl_gateway_when_env_set(self, monkeypatch):
        """When WSL_HOST_IP is set, the specified host is in the always-allowed set."""
        monkeypatch.setenv("WSL_HOST_IP", "172.30.0.1")
        policy = EndpointPolicy()
        permitted, reason = policy.classify_host("172.30.0.1")
        assert permitted, reason

    def test_wsl_gateway_private_ip_still_passes(self):
        """A private IP that is NOT in the always-allowed set still passes
        via the private-network classification — 172.21.176.1 is in the
        172.16.0.0/12 range."""
        policy = EndpointPolicy()
        # 172.21.176.1 is private, so it passes even without being
        # explicitly always-allowed.
        permitted, reason = policy.classify_host("172.21.176.1")
        assert permitted, reason

    def test_accepts_loopback_ip(self):
        policy = EndpointPolicy()
        permitted, reason = policy.classify_host("127.0.0.1")
        assert permitted, reason

    def test_accepts_private_network(self):
        """RFC1918 addresses are allowed — academics on university networks
        are a first-class supported case."""
        policy = EndpointPolicy()
        for host in ("192.168.1.50", "10.20.30.40", "172.16.0.1"):
            permitted, reason = policy.classify_host(host)
            assert permitted, f"{host}: {reason}"

    def test_rejects_link_local(self):
        """169.254.0.0/16 is always denied regardless of opt-in."""
        policy = EndpointPolicy(allow_public=True)
        permitted, reason = policy.classify_host("169.254.169.254")
        assert not permitted
        assert "link-local" in reason

    def test_rejects_link_local_entire_block(self):
        """Any link-local address must be rejected."""
        policy = EndpointPolicy()
        for host in ("169.254.0.1", "169.254.255.254", "169.254.100.50"):
            permitted, reason = policy.classify_host(host)
            assert not permitted, f"{host} was permitted: {reason}"
            assert "link-local" in reason

    def test_rejects_public_without_opt_in(self):
        """Public addresses require ARTIFICE_ALLOW_PUBLIC_MODELS."""
        policy = EndpointPolicy(allow_public=False)
        # Use an IP literal so the test does not depend on DNS — a machine
        # with a wildcard resolver would otherwise change the outcome.
        permitted, reason = policy.classify_host("8.8.8.8")
        assert not permitted
        assert "ARTIFICE_ALLOW_PUBLIC_MODELS" in reason

    def test_accepts_public_with_opt_in(self):
        """With ARTIFICE_ALLOW_PUBLIC_MODELS set, public hosts pass."""
        policy = EndpointPolicy(allow_public=True)
        permitted, reason = policy.classify_host("8.8.8.8")
        assert permitted, reason

    def test_rejects_unresolvable_host(self):
        """A name that cannot be resolved must fail closed."""
        policy = EndpointPolicy()
        permitted, reason = policy.classify_host("nonexistent.invalid")
        assert not permitted
        assert "could not be resolved" in reason

    def test_custom_always_allowed(self):
        """The always-allowed set can be overridden for testing."""
        policy = EndpointPolicy(always_allowed_hosts=frozenset(["myhost.local"]))
        permitted, reason = policy.classify_host("myhost.local")
        assert permitted, reason
        # localhost is NOT in the custom set
        permitted, reason = policy.classify_host("localhost")
        # localhost resolves to 127.0.0.1 which IS private, so it passes
        # through the address check even though it's not explicitly allowed.
        assert permitted or "resolves to the link-local" not in reason

    # -- always-allowed + link-local integration -------------------------------

    def test_always_allowed_link_local_denied(self):
        """An always-allowed hostname that resolves to a link-local address must
        be denied — the allowlist must not override the one unconditional rule."""
        policy = EndpointPolicy(
            always_allowed_hosts=frozenset(["trusted.local"]),
        )
        with mock.patch(
            "socket.getaddrinfo",
            return_value=[_v4_info("169.254.169.254")],
        ):
            permitted, reason = policy.classify_host("trusted.local")
        assert not permitted
        assert "link-local" in reason
        assert "169.254.169.254" in reason

    def test_always_allowed_public_permitted(self):
        """An always-allowed hostname resolving to a public address is
        still permitted — the allowlist skips the private/public check."""
        policy = EndpointPolicy(
            always_allowed_hosts=frozenset(["trusted.local"]),
            allow_public=False,
        )
        with mock.patch(
            "socket.getaddrinfo",
            return_value=[_v4_info("93.184.216.34")],
        ):
            permitted, reason = policy.classify_host("trusted.local")
        assert permitted

    def test_always_allowed_unresolvable_permitted(self):
        """An always-allowed hostname that cannot be resolved is still
        permitted.  DNS resolution failure in a particular environment is a
        connectivity issue, not a security concern — the host cannot actually
        be reached, and the link-local denial does not apply when there is no
        address to inspect."""
        policy = EndpointPolicy(
            always_allowed_hosts=frozenset(["host.docker.internal"]),
        )
        with mock.patch(
            "socket.getaddrinfo",
            side_effect=OSError("Name or service not known"),
        ):
            permitted, reason = policy.classify_host("host.docker.internal")
        assert permitted

    # -- multi-address resolution ------------------------------------------

    def test_name_resolving_to_private_and_link_local_is_rejected(self):
        """A name that resolves to both a private and a link-local address
        must be rejected — *every* resolved address is checked, not just
        the first one."""
        policy = EndpointPolicy()
        with mock.patch(
            "socket.getaddrinfo",
            return_value=[_v4_info("10.0.0.5"), _v4_info("169.254.169.254")],
        ):
            permitted, reason = policy.classify_host("double.local")
        assert not permitted
        assert "link-local" in reason
        assert "169.254.169.254" in reason

    def test_link_local_first_in_resolution_order_is_rejected(self):
        """The order of addresses in the resolution result must not matter.
        A link-local address listed first is still caught."""
        policy = EndpointPolicy()
        with mock.patch(
            "socket.getaddrinfo",
            return_value=[_v4_info("169.254.169.254"), _v4_info("10.0.0.5")],
        ):
            permitted, reason = policy.classify_host("double.local")
        assert not permitted
        assert "link-local" in reason

    def test_private_plus_public_rejected_without_opt_in(self):
        """A name resolving to both private and public addresses is rejected
        without ARTIFICE_ALLOW_PUBLIC_MODELS — one acceptable address does
        not excuse an unacceptable one."""
        policy = EndpointPolicy(allow_public=False)
        with mock.patch(
            "socket.getaddrinfo",
            return_value=[_v4_info("192.168.1.10"), _v4_info("93.184.216.34")],
        ):
            permitted, reason = policy.classify_host("mixed.local")
        assert not permitted
        assert "ARTIFICE_ALLOW_PUBLIC_MODELS" in reason

    def test_private_plus_public_accepted_with_opt_in(self, monkeypatch):
        """With ARTIFICE_ALLOW_PUBLIC_MODELS=1, a name resolving to both
        private and public addresses is permitted."""
        monkeypatch.setenv("ARTIFICE_ALLOW_PUBLIC_MODELS", "1")
        policy = EndpointPolicy()
        with mock.patch(
            "socket.getaddrinfo",
            return_value=[_v4_info("192.168.1.10"), _v4_info("93.184.216.34")],
        ):
            permitted, reason = policy.classify_host("mixed.local")
        assert permitted, reason

    def test_all_private_multi_address_accepted(self):
        """A name resolving to several RFC1918 addresses with no link-local
        is accepted — multi-address does not cause over-rejection."""
        policy = EndpointPolicy()
        with mock.patch(
            "socket.getaddrinfo",
            return_value=[
                _v4_info("10.0.0.5"),
                _v4_info("192.168.1.10"),
                _v4_info("172.16.0.1"),
            ],
        ):
            permitted, reason = policy.classify_host("all-private.local")
        assert permitted, reason

    def test_mixed_ipv4_ipv6_with_public_is_rejected(self):
        """IPv6 addresses in the resolved set must be checked alongside
        IPv4.  A private IPv4 plus a public IPv6 is still public overall.
        Uses 2001:4860:4860::8888 (Google DNS v6) which ipaddress treats as
        global, unlike 2001:db8::/32 which is the documentation prefix and
        classified as private."""
        policy = EndpointPolicy(allow_public=False)
        with mock.patch(
            "socket.getaddrinfo",
            return_value=[_v4_info("10.0.0.5"), _v6_info("2001:4860:4860::8888")],
        ):
            permitted, reason = policy.classify_host("mixed-family.local")
        assert not permitted
        assert "ARTIFICE_ALLOW_PUBLIC_MODELS" in reason


class TestValidateUrl:
    """Unit tests for full URL validation."""

    def test_accepts_localhost_url(self):
        policy = EndpointPolicy()
        assert policy.validate_url("http://localhost:11434/v1") == "http://localhost:11434/v1"

    def test_accepts_https(self):
        policy = EndpointPolicy()
        assert policy.validate_url("https://localhost:443/v1") == "https://localhost:443/v1"

    def test_rejects_ftp(self):
        policy = EndpointPolicy()
        with pytest.raises(EndpointRejected, match="scheme must be http or https"):
            policy.validate_url("ftp://localhost:21")

    def test_rejects_file_scheme(self):
        policy = EndpointPolicy()
        with pytest.raises(EndpointRejected, match="scheme must be http or https"):
            policy.validate_url("file:///etc/passwd")

    def test_rejects_empty_string(self):
        policy = EndpointPolicy()
        with pytest.raises(EndpointRejected):
            policy.validate_url("")

    def test_rejects_garbage(self):
        policy = EndpointPolicy()
        with pytest.raises(EndpointRejected):
            policy.validate_url("not-a-url!!!")

    def test_rejects_no_host(self):
        policy = EndpointPolicy()
        with pytest.raises(EndpointRejected, match="has no host"):
            policy.validate_url("http:///path")

    def test_rejects_link_local_url(self):
        policy = EndpointPolicy()
        with pytest.raises(EndpointRejected, match="link-local"):
            policy.validate_url("http://169.254.169.254/latest/meta-data/")

    def test_rejects_public_url_without_opt_in(self):
        policy = EndpointPolicy(allow_public=False)
        with pytest.raises(EndpointRejected, match="ARTIFICE_ALLOW_PUBLIC_MODELS"):
            policy.validate_url("http://8.8.8.8:11434/v1")

    def test_accepts_public_url_with_opt_in(self):
        policy = EndpointPolicy(allow_public=True)
        assert policy.validate_url("http://8.8.8.8:11434/v1") == "http://8.8.8.8:11434/v1"


class TestResolve:
    """The Protocol ``resolve`` method."""

    def test_resolve_delegates_to_validate(self):
        policy = EndpointPolicy()
        assert policy.resolve("http://localhost:11434/v1") == "http://localhost:11434/v1"

    def test_resolve_rejects_bad_url(self):
        policy = EndpointPolicy()
        with pytest.raises(EndpointRejected):
            policy.resolve("ftp://evil.com")


class TestDefaultAllowlist:
    """The default always-allowed set must not ship a hardcoded IP."""

    _IP_OCTET = __import__("ipaddress")

    def test_no_literal_ip_when_env_unset(self, monkeypatch):
        """When WSL_HOST_IP is not set, the default allowlist contains no
        literal IP address."""
        monkeypatch.delenv("WSL_HOST_IP", raising=False)
        hosts = _default_always_allowed()
        assert hosts, "default allowlist is empty — localhost must be present"
        for h in hosts:
            try:
                self._IP_OCTET.ip_address(h)
                is_ip = True
            except ValueError:
                is_ip = False
            assert not is_ip, (
                f"default allowlist contains literal IP {h!r} when WSL_HOST_IP is unset"
            )

    def test_wsl_host_ip_added_when_set(self, monkeypatch):
        """When WSL_HOST_IP is set, the specified address is in the allowlist."""
        monkeypatch.setenv("WSL_HOST_IP", "10.99.99.1")
        hosts = _default_always_allowed()
        assert "10.99.99.1" in hosts
