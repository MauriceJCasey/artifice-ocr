# SPDX-FileCopyrightText: 2026 Maurice Casey
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Unified backend abstraction for LLMs (Ollama, LM Studio, Hugging Face)."""

import logging
import re
from typing import Any

import ollama
from huggingface_hub import InferenceClient
from model_harness.contract import EndpointRejected
from model_harness.discovery import normalise_base_url
from model_harness.endpoint_policy import EndpointPolicy
from openai import NotFoundError, OpenAI

from . import config
from ._resolution import _redact_url

logger = logging.getLogger(__name__)

_endpoint_policy = EndpointPolicy()


def _validate_url(url: str, field_name: str) -> str:
    """Validate a model endpoint URL through the harness policy.

    Raises :class:`~model_harness.contract.EndpointRejected` if the URL
    is not permitted by the local-first endpoint policy.
    """
    try:
        return _endpoint_policy.validate_url(url)
    except EndpointRejected as e:
        raise EndpointRejected(f"{field_name}: {e}") from e


_THINK_UNSUPPORTED_HINTS = (
    "does not support thinking",
    "thinking is not supported",
    "unknown field",
    "unexpected keyword argument",
    "think",
)


def _is_think_unsupported(exc: Exception) -> bool:
    msg = str(exc).lower()
    return any(hint in msg for hint in _THINK_UNSUPPORTED_HINTS)


# Distinct ``(backend, base_url)`` pairs already logged at INFO.  Backends are
# recreated per call (per page) by design, so without this the pipeline would
# emit one INFO line per page; the first construction of a given URL logs at
# INFO and repeats log at DEBUG.
_logged_base_urls: set[tuple[str, str]] = set()


def _log_base_url_once(backend: str, base_url: str) -> None:
    """Log a backend's constructed base URL once per distinct URL per process."""
    key = (backend, base_url)
    if key in _logged_base_urls:
        logger.debug("Reusing %s backend client: base_url=%s", backend, _redact_url(base_url))
        return
    _logged_base_urls.add(key)
    logger.info("Constructing %s backend client: base_url=%s", backend, _redact_url(base_url))


def _provider_404(exc: Exception, *, base_url: str, model: str) -> RuntimeError:
    """Wrap a provider 404 into a RuntimeError naming what was actually called.

    A raw ``openai.NotFoundError`` surfaces as the provider's bare message with
    no indication of which URL was attempted — the exact shape that made a
    doubled ``/v1`` invisible.  The message carries the base URL and model so
    the next such failure names itself.
    """
    return RuntimeError(
        f"Provider returned 404 for model '{model}' at {_redact_url(base_url)}: {exc}"
    )


# Machine-stable error identifiers first — providers change prose, not codes.
# ``exceed_context_size_error`` is LM Studio's ``type``; ``context_length_exceeded``
# is OpenAI's ``code``. Both arrive inside the serialised body in ``str(exc)``.
#
# The two prose entries are kept because a provider may report only a message,
# but they name the *overflow* specifically. An earlier draft also matched the
# bare phrase "maximum context length", which fires on
# "maximum context length not supported for this model" — a capability error,
# not an overflow — and would have replaced it with a misleading one. Rewriting
# an error is only safe when the match is unambiguous, because the rewrite is
# what the user sees.
_CONTEXT_OVERFLOW_MARKERS = (
    "exceed_context_size_error",
    "context_length_exceeded",
    "exceeds the available context size",
    "context length exceeded",
)

# OpenAI's prose form: "This model's maximum context length is 4096 tokens,
# however you requested 5000 tokens". Its body also carries the
# ``context_length_exceeded`` code above, so this is the belt to that braces —
# it catches a provider that sends the sentence without the code.
#
# The trailing clause is required, and is the whole point: "maximum context
# length" alone also appears in "maximum context length not supported for this
# model", which is a capability error. Matching that would replace a real
# failure with advice about a limit the user has not hit.
_CONTEXT_OVERFLOW_PHRASE = re.compile(
    r"maximum context length.{0,80}?(however|you requested|resulted in|but you)",
    re.IGNORECASE | re.DOTALL,
)

# "request (4107 tokens) exceeds the available context size (4096 tokens)"
_CONTEXT_NUMBERS = re.compile(
    r"\((?P<prompt>\d+)\s+tokens?\).{0,40}?\((?P<ctx>\d+)\s+tokens?\)",
    re.IGNORECASE | re.DOTALL,
)


def _configured_context_size() -> int | None:
    """The user's chosen context window in tokens, or ``None`` to leave it alone.

    Returns ``None`` rather than ``0`` for "do not send ``num_ctx``". The config
    value is still ``0`` — that reads well in the UI ("0 uses the model's own
    default") — but ``0`` is a *valid* token count, so a backend author writing
    ``if context_size:`` and one writing ``if context_size is not None:`` would
    get different behaviour from the same value. ``None`` cannot be mistaken for
    a window size.

    Anything non-numeric or non-positive reads as ``None`` rather than raising:
    a malformed setting must not stop a run, it must fall back to the previous
    behaviour of not sending ``num_ctx`` at all.
    """
    try:
        value = int(config.get("context_size") or 0)
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def _context_overflow_hint(backend_name: str) -> str:
    """Where the user actually changes the context window, per backend.

    Naming the wrong place is worse than naming none: LM Studio fixes context
    when it *loads* a model, so telling that user to edit an app setting sends
    them to a control that cannot help them.
    """
    if backend_name in ("ollama", "ollama_openai"):
        return (
            "Raise 'Context size' in Settings (Ollama honours it per request), "
            "or pick a model with a larger context window."
        )
    if backend_name == "lm_studio":
        return (
            "LM Studio fixes the context window when it loads a model, so this "
            "cannot be changed from Artifice. Raise it in LM Studio's model "
            "settings, or run: lms load <model> --context-length 8192"
        )
    return (
        "This backend sets its context window server-side. Use a model with a "
        "larger context window, or a backend you control."
    )


def _context_overflow(exc: BaseException, *, backend_name: str) -> Exception:
    """Turn a provider context-overflow error into an actionable one.

    The raw error arrives as a JSON string nested inside an OpenAI SDK error,
    which renders as a wall of escaped braces that tells a historian nothing.
    The two token counts are the useful part; keep those, drop the rest, and
    say where the limit is actually configured.
    """
    raw = str(exc)
    match = _CONTEXT_NUMBERS.search(raw)
    if match:
        detail = (
            f"The page needs {match.group('prompt')} tokens but the model's "
            f"context window is {match.group('ctx')}."
        )
    else:
        detail = "The request was larger than the model's context window."
    return RuntimeError(f"{detail} {_context_overflow_hint(backend_name)}")


def _is_context_overflow(exc: BaseException) -> bool:
    text = str(exc).lower()
    if any(marker in text for marker in _CONTEXT_OVERFLOW_MARKERS):
        return True
    return bool(_CONTEXT_OVERFLOW_PHRASE.search(text))


def _guarded_chat(call, *, base_url: str, model: str, backend_name: str = "") -> Any:
    """Run *call*, translating a provider 404 into a self-describing error."""
    try:
        return call()
    except NotFoundError as exc:
        raise _provider_404(exc, base_url=base_url, model=model) from exc
    except ollama.ResponseError as exc:
        if getattr(exc, "status_code", None) == 404:
            raise _provider_404(exc, base_url=base_url, model=model) from exc
        if _is_context_overflow(exc):
            raise _context_overflow(exc, backend_name=backend_name) from exc
        raise
    except Exception as exc:
        if _is_context_overflow(exc):
            raise _context_overflow(exc, backend_name=backend_name) from exc
        raise


class _SimpleMessage:
    def __init__(self, content: str):
        self.content = content


class _SimpleResponse:
    def __init__(self, content: str):
        self.message = _SimpleMessage(content)


_DATA_URI_PREFIX = re.compile(r"^data:[^;]+;base64,")


def _to_native_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Convert OpenAI-shaped chat messages to Ollama's native ``/api/chat`` shape.

    OCR builds every vision request in the OpenAI content-block format
    (``[{"type": "text", ...}, {"type": "image_url", ...}]``) because that
    format is shared with LM Studio and the OpenAI-compatible cloud backends.
    Ollama's native API predates that convention: it wants a plain string
    ``content`` and a separate ``images`` list of bare base64 strings. This
    is the seam that lets OCR keep one message-building path while still
    reaching the *native* endpoint — the only one that honours ``num_ctx``
    (see the module docstring on :class:`OllamaOpenAIBackend`).

    A plain string ``content`` (the shape cleanup/translate/language-detection
    already send) passes through completely untouched, including the absence
    of an ``images`` key — those call sites must see byte-identical behaviour
    to before this function existed.
    """
    native: list[dict[str, Any]] = []
    for msg in messages:
        content = msg.get("content")
        if not isinstance(content, list):
            # Plain-string content (or anything else non-block-shaped): pass
            # through unchanged. This is the cleanup/translate path.
            native.append(msg)
            continue

        text_parts: list[str] = []
        images: list[str] = []
        for block in content:
            block_type = block.get("type")
            if block_type == "text":
                text_parts.append(block.get("text", ""))
            elif block_type == "image_url":
                url = block.get("image_url", {}).get("url", "")
                match = _DATA_URI_PREFIX.match(url)
                if not match:
                    raise ValueError(
                        "Ollama's native API only accepts images as base64 data "
                        f"URIs (data:<mime>;base64,...); got {url[:60]!r}. A "
                        "remote image_url cannot be forwarded to the native "
                        "/api/chat endpoint."
                    )
                images.append(url[match.end() :])
            # Unknown block types are dropped silently, matching how an
            # OpenAI-compatible endpoint would ignore a field it does not
            # recognise rather than failing the whole request.

        native_msg: dict[str, Any] = {
            **{k: v for k, v in msg.items() if k != "content"},
            "content": "\n".join(text_parts),
        }
        if images:
            native_msg["images"] = images
        native.append(native_msg)
    return native


class OllamaBackend:
    def chat(
        self,
        *,
        model: str,
        messages: list[dict[str, str]],
        temperature: float = 0.0,
        think: bool | None = None,
        num_predict: int | None = None,
    ) -> Any:
        host = config.get("ollama_url") or "http://localhost:11434"
        normalised = normalise_base_url(host)
        _validate_url(normalised, "ollama_url")
        _log_base_url_once("ollama", normalised)
        client = ollama.Client(host=normalised)
        messages = _to_native_messages(messages)
        options: dict[str, Any] = {"temperature": temperature}
        if num_predict is not None:
            options["num_predict"] = num_predict
        # 0 means "leave it to the model's own default", which is what happened
        # before this setting existed. Only send num_ctx when the user has
        # actually chosen a value.
        context_size = _configured_context_size()
        if context_size is not None:
            options["num_ctx"] = context_size

        kwargs: dict[str, Any] = {"model": model, "messages": messages, "options": options}
        if think is not None:
            kwargs["think"] = think

        if think is None:
            return _guarded_chat(
                lambda: client.chat(**kwargs),
                base_url=normalised,
                model=model,
                backend_name="ollama",
            )

        try:
            return _guarded_chat(
                lambda: client.chat(**kwargs),
                base_url=normalised,
                model=model,
                backend_name="ollama",
            )
        except Exception as exc:
            if _is_think_unsupported(exc):
                logger.debug(
                    "Ollama model '%s' rejected think parameter (%s); retrying without.",
                    model,
                    exc,
                )
                kwargs.pop("think", None)
                return _guarded_chat(
                    lambda: client.chat(**kwargs), base_url=normalised, model=model
                )
            raise


class OllamaOpenAIBackend:
    """Ollama via its OpenAI-compatible ``/v1`` endpoint.

    .. warning::

       **``extra_body={"options": {"num_ctx": N}}`` is NOT honoured here.**
       Measured on live Ollama 0.33.2: requesting ``num_ctx=2048`` for
       ``richardyoung/olmocr2:7b-q8`` through this endpoint loaded a context
       window of 4096 (Ollama's default) per ``/api/ps`` — the field is
       silently dropped. A bare top-level ``num_ctx`` was ignored too. Only
       the native ``/api/chat`` (``OllamaBackend``, with ``options.num_ctx``)
       honoured the requested size exactly (8192 -> 8192, 2048 -> 2048).

       Because of this, OCR's vision call deliberately no longer uses this
       backend for ``ollama`` — see ``stages/ocr.py`` and
       :func:`_to_native_messages`, which teaches ``OllamaBackend`` to accept
       the same OpenAI-shaped ``image_url`` content blocks this class was
       originally built to carry. This class is kept, and still reachable via
       ``get_client("ollama_openai")``, for anything that specifically wants
       the OpenAI-compatible surface — just not for a call whose context size
       must be respected.

    The native Ollama API carries images in an ``images`` field while
    multimodal vision models are sent ``image_url`` content blocks.  This
    backend hits the ``/v1`` endpoint so a caller can use the OpenAI message
    format without a format conversion in the middle — the trade being the
    ignored ``num_ctx`` documented above.
    """

    def _base_url(self) -> str:
        raw = config.get("ollama_url") or "http://localhost:11434"
        return normalise_base_url(raw) + "/v1"

    def _client(self) -> OpenAI:
        base_url = self._base_url()
        _validate_url(base_url, "ollama_url")
        _log_base_url_once("ollama_openai", base_url)
        return OpenAI(base_url=base_url, api_key="ollama")

    def chat(
        self,
        *,
        model: str,
        messages: list[dict[str, str]],
        temperature: float = 0.0,
        think: bool | None = None,
        num_predict: int | None = None,
    ) -> Any:
        base_url = self._base_url()
        client = self._client()
        kwargs: dict[str, Any] = {}
        if num_predict is not None:
            kwargs["max_tokens"] = num_predict

        # `/v1` has no standard field for the context window, so num_ctx rides
        # in `extra_body`, which the OpenAI SDK passes through untouched.
        # Ollama reads `options` from the request body; a server that does not
        # will ignore an unknown key rather than fail. Only sent when the user
        # set a value, so the default path is byte-identical to before.
        #
        # **This is measured NOT to work against live Ollama** — see the
        # class docstring. Kept as-is (rather than dropped) so a future
        # Ollama release that starts honouring it does not require restoring
        # code that was deleted here; do not rely on it today.
        context_size = _configured_context_size()
        if context_size is not None:
            kwargs["extra_body"] = {"options": {"num_ctx": context_size}}

        resp = _guarded_chat(
            lambda: client.chat.completions.create(
                model=model,
                messages=messages,
                temperature=temperature,
                **kwargs,
            ),
            base_url=base_url,
            model=model,
            backend_name="ollama_openai",
        )
        content = resp.choices[0].message.content or ""
        return _SimpleResponse(content)


class LMStudioBackend:
    """OpenAI-compatible LM Studio backend.

    .. note::

       ``LMStudioBackend.health_check`` was removed in the 2b migration.
       The same check now lives in :func:`artifice_ocr.utils.check_lm_studio`,
       which delegates to :func:`model_harness.discovery.probe_endpoint_sync`.
       Nothing is broken; the surface has moved, not disappeared.
    """

    def _base_url(self) -> str:
        raw = config.get("lm_studio_url") or "http://localhost:1234/v1"
        # Normalise the URL to prevent probe/chat divergence: the health probe
        # defensively adds /v1 if missing, while chat uses _base_url() directly.
        # Without normalisation, a stored value of "http://localhost:1234" probes
        # GREEN but fails on chat (404). The normalise_base_url strips trailing
        # slashes and any trailing /v1; we then append /v1 only if absent to
        # ensure both paths agree. See _provider_404 (~line 69) for why doubling
        # /v1 must never happen.
        normalised = normalise_base_url(raw)
        if normalised.endswith("/v1"):
            return normalised
        return normalised + "/v1"

    def _client(self) -> OpenAI:
        base_url = self._base_url()
        _validate_url(base_url, "lm_studio_url")
        _log_base_url_once("lm_studio", base_url)
        return OpenAI(base_url=base_url, api_key="lm-studio")

    def chat(
        self,
        *,
        model: str,
        messages: list[dict[str, str]],
        temperature: float = 0.0,
        think: bool | None = None,
        num_predict: int | None = None,
    ) -> Any:
        base_url = self._base_url()
        client = self._client()
        kwargs: dict[str, Any] = {}
        if num_predict is not None:
            kwargs["max_tokens"] = num_predict

        resp = _guarded_chat(
            lambda: client.chat.completions.create(
                model=model,
                messages=messages,
                temperature=temperature,
                **kwargs,
            ),
            base_url=base_url,
            model=model,
            backend_name="lm_studio",
        )
        content = resp.choices[0].message.content or ""
        return _SimpleResponse(content)


class HuggingFaceBackend:
    """Hugging Face Inference API backend.

    This backend connects to the public Hugging Face Inference API.
    Like :class:`ApiKeyBackend`, it requires
    ``ARTIFICE_ALLOW_PUBLIC_MODELS=1`` in the environment — a deliberate
    opt-in from a user who has already provided a HuggingFace token.

    There is no user-supplied URL here because the SDK always connects
    to HuggingFace's own hosted service.  The validation is a direct
    check against the endpoint policy to confirm public endpoints are
    permitted.  The same policy governs every other backend in this file.
    """

    def chat(
        self,
        *,
        model: str,
        messages: list[dict[str, str]],
        temperature: float = 0.0,
        think: bool | None = None,
        num_predict: int | None = None,
    ) -> Any:
        # HuggingFace Inference API is a public cloud service; the
        # endpoint policy must permit public endpoints first.
        _validate_url(
            "https://api-inference.huggingface.co/models",
            "huggingface",
        )

        token = config.get("huggingface_token") or None
        client = InferenceClient(token=token)
        kwargs: dict[str, Any] = {}
        if num_predict is not None:
            kwargs["max_tokens"] = num_predict

        resp = client.chat_completion(
            model=model,
            messages=messages,
            temperature=temperature,
            **kwargs,
        )
        content = resp.choices[0].message.content or ""
        return _SimpleResponse(content)


class ApiKeyBackend:
    """Any OpenAI-compatible cloud API (OpenAI, Together, Groq, etc.).

    The default endpoint is ``https://api.openai.com/v1``, which is a
    public address.  The endpoint policy denies public endpoints unless
    ``ARTIFICE_ALLOW_PUBLIC_MODELS=1`` is set in the environment —
    a deliberate opt-in that a user who has already entered an API key
    must also make.  See :class:`model_harness.endpoint_policy.EndpointPolicy`.
    """

    def _base_url(self) -> str:
        return config.get("api_base_url") or "https://api.openai.com/v1"

    def _client(self) -> OpenAI:
        base_url = self._base_url()
        api_key = config.get("api_key") or ""
        _validate_url(base_url, "api_base_url")
        _log_base_url_once("api_key", base_url)
        return OpenAI(base_url=base_url, api_key=api_key)

    def health_check(self) -> tuple[bool, str | None]:
        """Return (ok, error_detail)."""
        api_key = config.get("api_key")
        if not api_key:
            return False, "No API key configured"
        base_url = self._base_url()
        _validate_url(base_url, "api_base_url")
        try:
            self._client().models.list()
            return True, None
        except Exception as exc:
            return False, str(exc)

    def chat(
        self,
        *,
        model: str,
        messages: list[dict[str, str]],
        temperature: float = 0.0,
        think: bool | None = None,
        num_predict: int | None = None,
    ) -> Any:
        base_url = self._base_url()
        client = self._client()
        kwargs: dict[str, Any] = {}
        if num_predict is not None:
            kwargs["max_tokens"] = num_predict

        resp = _guarded_chat(
            lambda: client.chat.completions.create(
                model=model,
                messages=messages,
                temperature=temperature,
                **kwargs,
            ),
            base_url=base_url,
            model=model,
            backend_name="api_key",
        )
        content = resp.choices[0].message.content or ""
        return _SimpleResponse(content)


def get_client(backend: str) -> Any:
    b = (backend or "ollama").lower()
    if b == "lm_studio":
        return LMStudioBackend()
    elif b == "ollama_openai":
        return OllamaOpenAIBackend()
    elif b == "huggingface":
        return HuggingFaceBackend()
    elif b == "api_key":
        return ApiKeyBackend()
    else:
        return OllamaBackend()
