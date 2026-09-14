# SPDX-FileCopyrightText: 2026 Maurice Casey
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Resolve a concrete model name from what the endpoint actually serves.

The suite is BYOM, yet it ships concrete model names as defaults and nothing has
ever checked them against the model list the provider actually serves.
``artifice-ocr`` defaults its ``ocr_model`` to ``allenai/olmocr-2-7b`` — a
Hugging Face repo id, not an Ollama tag — so a default install fails before the
user does anything, and :func:`~model_harness.registry.is_configured` reports an
app "configured" purely from ``base_url``/``api_key`` while pointing at a model
that does not exist.

:func:`resolve_model` is the fix.  It takes the model list the endpoint serves
(obtained elsewhere, via :func:`~model_harness.discovery.probe_endpoint`) plus a
role, and returns the model name to use, preferring the user's explicit choice
and the registry recommendation before falling back to any plausible installed
model.

This module performs **no I/O**.  It takes the installed list as input rather
than probing for it, so it is trivially testable and callers may cache the probe
result and re-run resolution cheaply.

See :func:`resolve_model` for the precedence rules.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from enum import Enum

from model_harness.registry import BadgeRole, HardwareTier, recommendations_for_app

__all__ = ["ModelResolution", "ResolutionSource", "resolve_model"]


class ResolutionSource(Enum):
    """How :func:`resolve_model` arrived at its answer.

    An enum rather than a free string so callers cannot accidentally invent a
    sixth reason and so a UI can render the outcome as a closed set of states.
    """

    USER_CHOICE = "user_choice"
    """The user's explicit ``configured`` choice is installed."""

    CONFIGURED_MISSING = "configured_missing"
    """The user chose a model that is not installed. No substitution made."""

    RECOMMENDED = "recommended"
    """A registry recommendation for ``(app, tier, role)`` is installed."""

    FALLBACK = "fallback"
    """No choice or recommendation applied; the first plausible installed model."""

    NONE_AVAILABLE = "none_available"
    """No installed model fits the role."""


@dataclass(frozen=True, slots=True)
class ModelResolution:
    """The outcome of resolving a role to a concrete installed model."""

    model_name: str | None
    """The model to use — an installed string, or ``None`` when nothing fits."""

    source: ResolutionSource
    """Which precedence step produced this answer."""

    configured_but_missing: bool = False
    """``True`` when the user chose a model and it is not installed.

    Set only when ``source`` is :attr:`ResolutionSource.CONFIGURED_MISSING`.
    Distinct from ``NONE_AVAILABLE`` because a missing *user choice* is a
    different failure from "nothing suitable installed": the first is the
    user's own instruction, the second is an empty shelf.
    """


def _normalise(name: str) -> str:
    """Return a canonical form of *name* for matching — never for use.

    Ollama tags separate the tag with a colon (``llama3.2:3b``) while an
    OpenAI-compatible endpoint exposes the same model with a hyphen
    (``llama3.2-3b``) — see the ``ProbeResult.models`` docstring in
    :mod:`model_harness.discovery`.  Matching must tolerate that difference, so
    both forms are reduced here to the hyphen form.

    This is a matching key only: the returned ``model_name`` is always an
    original installed string, never the normalised form.
    """
    return name.replace(":", "-")


def _looks_like_embedding(name: str) -> bool:
    """Heuristic: does *name* look like an embedding model?

    For an arbitrary installed model the harness cannot know capabilities —
    only the registry carries ``role`` and ``vision``.  The one safe signal in a
    bare name is the conventional ``embed`` substring (``nomic-embed-text``,
    ``mxbai-embed-large``).  Deliberately nothing more: detecting ``bge``,
    ``gte``, or other embedding families by name would be a guess, and a wrong
    guess is worse than no match.
    """
    return "embed" in name.lower()


def resolve_model(
    *,
    role: BadgeRole,
    installed: Sequence[str],
    app: str | None = None,
    tier: HardwareTier | None = None,
    configured: str | None = None,
) -> ModelResolution:
    """Resolve *role* to a concrete model from *installed*.

    Args:
        role: What the model must do — ``"vision"``, ``"chat"``,
            ``"translation"``, or ``"embedding"``.
        installed: Model names the endpoint actually serves, in the order they
            were discovered.  Order is the tie-break at the fallback step, so it
            must be a stable sequence rather than a set.
        app: Registry app key (e.g. ``"artifice-ocr"``) used for
            recommendations.  ``None`` skips the recommendation step.
        tier: Hardware tier used for recommendations.  ``None`` skips it.
        configured: The user's explicit model choice, if any.  ``None`` or an
            empty string means "no choice" and is skipped.

    Returns:
        A :class:`ModelResolution` naming what to use and why.

    Precedence, strictly in order:

    1. *configured* is installed → use it (``USER_CHOICE``).
    2. *configured* is set but not installed → fail with
       ``CONFIGURED_MISSING``.  Falling back here would silently run a different
       model than the user asked for, which is worse than failing.
    3. A registry recommendation for ``(app, tier, role)`` is installed → use it
       (``RECOMMENDED``).
    4. Another installed model plausibly fits *role* → use the first such, in
       *installed* order (``FALLBACK``).
    5. Nothing fits → ``NONE_AVAILABLE``.
    """
    installed_list = list(installed)

    # Normalised name → first installed string that normalises to it.  ``dict``
    # insertion order is deterministic but is never iterated here — lookup only
    # — so the result cannot depend on it.
    by_normalised: dict[str, str] = {}
    for name in installed_list:
        by_normalised.setdefault(_normalise(name), name)

    def _find_installed(candidate: str) -> str | None:
        """Return the installed string matching *candidate*, else ``None``.

        An exact match is preferred; a normalised match (Ollama ``:`` vs
        OpenAI ``-``) is accepted so the same model found under the other naming
        convention is still recognised.
        """
        for name in installed_list:
            if name == candidate:
                return name
        return by_normalised.get(_normalise(candidate))

    # Steps 1–2 — the user's explicit choice wins, or fails loudly.
    if configured:
        match = _find_installed(configured)
        if match is not None:
            return ModelResolution(model_name=match, source=ResolutionSource.USER_CHOICE)
        # Configured but not installed: do not silently substitute a different
        # model.  Returning None with `configured_but_missing=True` is the whole
        # point — the caller must surface the failure, not paper over it.
        return ModelResolution(
            model_name=None,
            source=ResolutionSource.CONFIGURED_MISSING,
            configured_but_missing=True,
        )

    # Step 3 — a registry recommendation for (app, tier, role) that is installed.
    if app is not None and tier is not None:
        try:
            recommendations = recommendations_for_app(app, tier)
        except KeyError:
            recommendations = ()
        for rec in recommendations:
            if rec.role != role:
                continue
            # A vision role may only be satisfied by a registry entry that is
            # actually vision-capable — never by a name match.  The registry is
            # the only place that carries `vision`.
            if role == "vision" and not rec.vision:
                continue
            match = _find_installed(rec.model_name)
            if match is not None:
                return ModelResolution(model_name=match, source=ResolutionSource.RECOMMENDED)

    # Step 4 — fall back to any installed model that plausibly fits the role.

    if role == "vision":
        # Do NOT guess a vision model from its name.  Handing a text model to an
        # OCR call produces confident nonsense rather than an error, which is
        # worse than failing.  Only the registry can certify vision capability,
        # and step 3 already failed, so there is nothing to fall back to.
        return ModelResolution(model_name=None, source=ResolutionSource.NONE_AVAILABLE)

    if role == "embedding":
        # Only a name containing ``embed`` is safe to trust from a bare name;
        # nothing else can be identified as an embedding model by inspection.
        for name in installed_list:
            if _looks_like_embedding(name):
                return ModelResolution(model_name=name, source=ResolutionSource.FALLBACK)
        return ModelResolution(model_name=None, source=ResolutionSource.NONE_AVAILABLE)

    # ``chat`` / ``translation``: any installed non-embedding model is
    # acceptable.  (A vision-language model also chats, so it is not excluded.)
    for name in installed_list:
        if not _looks_like_embedding(name):
            return ModelResolution(model_name=name, source=ResolutionSource.FALLBACK)

    return ModelResolution(model_name=None, source=ResolutionSource.NONE_AVAILABLE)
