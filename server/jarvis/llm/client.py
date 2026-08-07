"""LLM client: an OpenAI-compatible caller over a pool of endpoints.

Failure handling is the substance of this file. Providers fail in several
distinct ways and each needs a different response — treating them all as
"retry the same thing" is what produced a downgrade loop into a model that could
never have served the request:

  429 rate limited   -> this endpoint is busy; move to the next one now
  413 too large      -> this *model* can't take it; try a roomier one, never a smaller
  400 bad tool call  -> the model emitted a malformed call; nudge it once, then move on
  5xx / network      -> provider trouble; cool it briefly and move on
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any

import httpx

from ..config import Settings, get_settings
from .pool import Endpoint, Pool, build_pool

log = logging.getLogger(__name__)


class LLMError(RuntimeError):
    pass


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]

    @classmethod
    def parse(cls, raw: dict) -> ToolCall:
        fn = raw.get("function", {})
        name = (fn.get("name") or "").strip()
        text = fn.get("arguments") or "{}"

        # Weaker models sometimes cram the arguments into the function name,
        # e.g. 'calendar_list {"start": "today"}'. Providers reject that as an
        # unknown tool, so recover the real name and arguments rather than
        # letting a malformed call take down the turn.
        if name and (" " in name or "{" in name):
            head, _, tail = name.partition("{")
            name = head.strip().rstrip("(,:").strip()
            if tail and (not text or text == "{}"):
                text = "{" + tail

        try:
            args = json.loads(text) if isinstance(text, str) else dict(text)
        except json.JSONDecodeError:
            log.warning("Non-JSON tool arguments for %s: %s", name, str(text)[:200])
            args = {}
        if not isinstance(args, dict):
            args = {}
        return cls(id=raw.get("id", ""), name=name, arguments=args)


@dataclass
class LLMResponse:
    content: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    finish_reason: str = "stop"
    usage: dict = field(default_factory=dict)
    model: str = ""

    @property
    def wants_tools(self) -> bool:
        return bool(self.tool_calls)


class _RateLimited(Exception):
    def __init__(self, retry_after: float | None = None) -> None:
        self.retry_after = retry_after
        super().__init__("rate limited")


class _TooLarge(Exception):
    def __init__(self, detail: str) -> None:
        self.detail = detail
        super().__init__(detail)


class _BadToolCall(Exception):
    def __init__(self, detail: str) -> None:
        self.detail = detail
        super().__init__(detail)


class _Upstream(Exception):
    def __init__(self, detail: str) -> None:
        self.detail = detail
        super().__init__(detail)


class GroqClient:
    """Named for its default provider, but any OpenAI-compatible endpoint works."""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self.pool: Pool = build_pool(self.settings)
        self._client: httpx.AsyncClient | None = None

    def reload_pool(self) -> None:
        self.pool = build_pool(self.settings)

    async def _http(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(timeout=httpx.Timeout(120.0, connect=15.0))
        return self._client

    async def aclose(self) -> None:
        if self._client and not self._client.is_closed:
            await self._client.aclose()

    # ---------------------------------------------------------------- public

    async def complete(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        *,
        model: str | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        force_json: bool = False,
    ) -> LLMResponse:
        if not len(self.pool):
            raise LLMError(
                "No LLM endpoints configured. Set GROQ_API_KEY (or GROQ_API_KEYS) in .env."
            )

        # An explicit model request bypasses the ladder but still uses the pool's
        # keys, so a caller asking for a specific model keeps the failover.
        candidates = self._candidates(model)
        errors: list[str] = []
        nudged = False

        for endpoint in candidates:
            try:
                response = await self._request(
                    endpoint, messages, tools, temperature, max_tokens, force_json
                )
                endpoint.succeeded()
                return response

            except _RateLimited as exc:
                endpoint.rest(exc.retry_after)
                errors.append(f"{endpoint.label}: rate limited")
                log.info("Rate limited on %s, moving on", endpoint.label)

            except _TooLarge as exc:
                # Never retried on a smaller model — that is a guaranteed second
                # failure. Only a roomier endpoint is worth trying.
                endpoint.rest(5.0)
                errors.append(f"{endpoint.label}: request too large")
                log.info("Too large for %s: %s", endpoint.label, exc.detail)

            except _BadToolCall as exc:
                errors.append(f"{endpoint.label}: malformed tool call")
                log.warning("Malformed tool call from %s: %s", endpoint.label, exc.detail)
                if not nudged:
                    # One corrective shot, because a formatting slip is sometimes
                    # a one-off. But it is often not: some models simply cannot
                    # do tool calling reliably and fail this way on every request.
                    # Measured on a real account, llama-3.3-70b-versatile returns
                    # 400 "Failed to call a function" on even a single-tool
                    # prompt, so the nudge is a courtesy, not an expectation.
                    nudged = True
                    messages = messages + [
                        {
                            "role": "user",
                            "content": (
                                "Your last tool call was malformed. Put only the tool "
                                "name in the name field and all parameters in the "
                                "arguments object. Try again."
                            ),
                        }
                    ]
                    try:
                        response = await self._request(
                            endpoint, messages, tools, temperature, max_tokens, force_json
                        )
                        endpoint.succeeded()
                        return response
                    except Exception:
                        # Escalating backoff rather than a fixed pause: an
                        # endpoint that fails this way twice in a row is very
                        # likely incapable, not unlucky, and a fixed rest would
                        # have it retried every few seconds indefinitely — two
                        # wasted round-trips on every turn. Backing off further
                        # each time lets a broken model fall out of rotation on
                        # its own, without needing to be identified by name.
                        endpoint.rest()

            except _Upstream as exc:
                endpoint.rest()
                errors.append(f"{endpoint.label}: {exc.detail[:80]}")
                log.warning("Upstream error on %s: %s", endpoint.label, exc.detail[:200])

        raise LLMError(self._exhausted_message(errors))

    def _candidates(self, model: str | None) -> list[Endpoint]:
        ready = self.pool.ready()
        if model:
            preferred = [e for e in ready if e.model == model]
            return preferred + [e for e in ready if e.model != model]
        return ready

    def _exhausted_message(self, errors: list[str]) -> str:
        wait = self.pool.wait_hint()
        detail = "; ".join(errors[:4]) or "no endpoints available"

        if len(self.pool) == 1:
            advice = (
                "You're running on a single API key, so one busy minute stops everything. "
                "Adding a second provider (Cerebras and OpenRouter both have free tiers) "
                "gives independent capacity — see docs/model-capacity.md."
            )
        else:
            advice = f"All {len(self.pool)} endpoints are busy or failing."

        timing = f" Try again in about {int(wait)}s." if wait > 1 else ""
        return f"{advice} ({detail}).{timing}"

    # ---------------------------------------------------------------- request

    async def _request(
        self,
        endpoint: Endpoint,
        messages: list[dict],
        tools: list[dict] | None,
        temperature: float | None,
        max_tokens: int | None,
        force_json: bool,
    ) -> LLMResponse:
        payload: dict[str, Any] = {
            "model": endpoint.model,
            "messages": messages,
            "temperature": self.settings.llm_temperature if temperature is None else temperature,
            "max_tokens": max_tokens or self.settings.llm_max_tokens,
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"
        if force_json:
            payload["response_format"] = {"type": "json_object"}

        client = await self._http()
        try:
            response = await client.post(
                f"{endpoint.base_url.rstrip('/')}/chat/completions",
                json=payload,
                headers={"Authorization": f"Bearer {endpoint.api_key}"},
            )
        except httpx.HTTPError as exc:
            raise _Upstream(f"{type(exc).__name__}: {exc}") from exc

        if response.status_code == 429:
            raise _RateLimited(_retry_after(response))
        if response.status_code == 413:
            raise _TooLarge(_error_message(response))
        if response.status_code == 400:
            message = _error_message(response)
            lowered = message.lower()
            if "tool" in lowered and ("not in request.tools" in lowered or "validation" in lowered):
                raise _BadToolCall(message)
            # Some providers signal an oversized request as 400 rather than 413.
            if "too large" in lowered or "context length" in lowered:
                raise _TooLarge(message)
            raise _Upstream(message)
        if response.status_code >= 400:
            raise _Upstream(f"HTTP {response.status_code}: {_error_message(response)}")

        try:
            data = response.json()
        except ValueError as exc:
            raise _Upstream("provider returned a non-JSON body") from exc

        choice = (data.get("choices") or [{}])[0]
        message = choice.get("message") or {}
        return LLMResponse(
            content=message.get("content") or "",
            tool_calls=[ToolCall.parse(t) for t in (message.get("tool_calls") or [])],
            finish_reason=choice.get("finish_reason", "stop"),
            usage=data.get("usage", {}),
            model=data.get("model", endpoint.model),
        )

    # ---------------------------------------------------------------- models

    async def list_available_models(self) -> list[dict]:
        """Ask each configured provider what its key can actually reach.

        Rate limits and model names shift, and guessing at either produces
        confident-sounding nonsense. Better to read it from the provider.
        """
        seen: dict[str, dict] = {}
        client = await self._http()

        for base_url, api_key in {(e.base_url, e.api_key) for e in self.pool.endpoints}:
            try:
                response = await client.get(
                    f"{base_url.rstrip('/')}/models",
                    headers={"Authorization": f"Bearer {api_key}"},
                    timeout=20.0,
                )
                if response.status_code >= 400:
                    continue
                for item in response.json().get("data", []):
                    model_id = item.get("id")
                    if model_id and model_id not in seen:
                        seen[model_id] = {
                            "id": model_id,
                            "provider": base_url.split("//")[-1].split("/")[0],
                            "context_window": item.get("context_window"),
                            "owned_by": item.get("owned_by"),
                        }
            except (httpx.HTTPError, ValueError):
                continue

        return sorted(seen.values(), key=lambda m: m["id"])


def _retry_after(response: httpx.Response) -> float | None:
    raw = response.headers.get("retry-after")
    if not raw:
        return None
    try:
        return max(0.0, min(float(raw), 120.0))
    except ValueError:
        return None


def error_payload(response: httpx.Response) -> dict:
    """Normalise a provider error body to a dict, whatever shape it arrived in.

    There is no agreed format. OpenAI and Groq send {"error": {...}}; Google's
    OpenAI-compatible endpoint sends that wrapped in a *list*; some send a bare
    string, or FastAPI-style {"detail": ...}. Assuming a dict and calling .get()
    on it crashes outright on the list form, which is exactly what happened the
    first time a Gemini key was added.
    """
    try:
        body = response.json()
    except ValueError:
        return {}

    # Gemini wraps its error object in a single-element array.
    if isinstance(body, list):
        body = next((item for item in body if isinstance(item, dict)), {})
    if not isinstance(body, dict):
        return {}

    error = body.get("error", body)
    if isinstance(error, str):
        return {"message": error}
    if not isinstance(error, dict):
        return {}
    return error


def _error_message(response: httpx.Response) -> str:
    """Pull the human sentence out of a provider error.

    Providers wrap errors in JSON and append marketing ("Upgrade to Dev Tier"),
    organisation IDs and service-tier names. None of that helps the person
    reading it in a chat window, so it's stripped down to the actionable part.
    """
    error = error_payload(response)
    message = error.get("message") or error.get("detail") or ""
    if not isinstance(message, str):
        message = str(message)
    if not message:
        return response.text[:200]

    for marker in ("Need more tokens?", "Upgrade to", "Visit ", "please reduce"):
        message = message.split(marker)[0]

    # Drop the organisation id and service tier — noise to the reader.
    import re

    message = re.sub(r"\s*in organization org_\w+", "", message)
    message = re.sub(r"\s*service tier \w+", "", message)
    return " ".join(message.split()).strip(" .,") or response.text[:200]


_client: GroqClient | None = None


def get_llm() -> GroqClient:
    global _client
    if _client is None:
        _client = GroqClient()
    return _client
