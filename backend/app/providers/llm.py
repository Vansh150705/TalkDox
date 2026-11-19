"""LLM provider abstraction.

* `GeminiLLM` is the primary provider (model id comes from config - never hard-coded in callers).
* `GroqLLM` is an optional OpenAI-compatible alternative.
* `FakeLLM` is a scriptable test double (tests only; never selected automatically).

All providers share retry/backoff, a concurrency cap and a small circuit breaker so a provider
outage degrades into a fast, honest error instead of a pile of hanging requests.
"""
from __future__ import annotations

import json
import logging
import random
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional, Type

import httpx
from pydantic import BaseModel, ValidationError

from ..config import Settings
from ..errors import NotConfigured, ProviderError, ProviderRateLimited, ProviderUnavailable

log = logging.getLogger("talkdox.llm")


@dataclass
class LLMResult:
    text: str
    provider: str
    model: str
    latency_ms: int = 0
    usage: dict = field(default_factory=dict)
    parsed: Any = None


class _Breaker:
    """Opens after N consecutive failures for `cooldown` seconds."""

    def __init__(self, threshold: int = 5, cooldown: float = 20.0):
        self.threshold, self.cooldown = threshold, cooldown
        self.fails = 0
        self.open_until = 0.0
        self.lock = threading.Lock()

    def check(self) -> None:
        if time.time() < self.open_until:
            raise ProviderUnavailable("The AI provider is temporarily unavailable. Please retry in a few seconds.",
                                      code="provider_circuit_open")

    def ok(self) -> None:
        with self.lock:
            self.fails = 0

    def fail(self) -> None:
        with self.lock:
            self.fails += 1
            if self.fails >= self.threshold:
                self.open_until = time.time() + self.cooldown
                self.fails = 0


class BaseLLM:
    provider = "base"
    model = ""

    def __init__(self, settings: Settings):
        self.s = settings
        self._sem = threading.BoundedSemaphore(6)
        self._breaker = _Breaker()
        self._health: tuple[float, dict] | None = None

    # subclasses implement a single attempt
    def _call(self, prompt: str, system: Optional[str], schema: Any, temperature: float,
              max_tokens: int, thinking: bool) -> tuple[str, dict, str]:
        raise NotImplementedError

    def generate(self, prompt: str, *, system: Optional[str] = None, schema: Any = None,
                 temperature: float = 0.2, max_tokens: Optional[int] = None, thinking: bool = True) -> LLMResult:
        max_tokens = max_tokens or self.s.llm_max_output_tokens
        self._breaker.check()
        last: Exception | None = None
        t0 = time.time()
        for attempt in range(self.s.llm_max_retries + 1):
            try:
                with self._sem:
                    text, usage, model = self._call(prompt, system, schema, temperature, max_tokens, thinking)
                self._breaker.ok()
                return LLMResult(text=text, provider=self.provider, model=model, usage=usage,
                                 latency_ms=int((time.time() - t0) * 1000))
            except (ProviderRateLimited, ProviderUnavailable) as e:
                last = e
                self._breaker.fail()
                if attempt >= self.s.llm_max_retries:
                    break
                delay = min(2 ** attempt + random.random(), 20)
                log.warning("LLM transient failure (%s); retry %d in %.1fs", e.code, attempt + 1, delay)
                time.sleep(delay)
            except ProviderError:
                raise
        assert last is not None
        raise last

    def health(self) -> dict:
        now = time.time()
        if self._health and now - self._health[0] < 300:
            return self._health[1]
        try:
            info = self._check()
        except Exception as e:  # health must never raise
            info = {"ok": False, "provider": self.provider, "model": self.model, "detail": str(e)[:200]}
        self._health = (now, info)
        return info

    def _check(self) -> dict:
        return {"ok": True, "provider": self.provider, "model": self.model}


def _status_to_error(status: int | None, msg: str) -> ProviderError:
    if status == 429:
        return ProviderRateLimited("The AI provider's rate limit or quota was reached. Please retry shortly.",
                                   code="provider_rate_limited", detail={"upstream": msg[:200]})
    if status in (500, 502, 503, 504) or status is None:
        return ProviderUnavailable("The AI provider is temporarily unavailable.", code="provider_unavailable",
                                   detail={"upstream": msg[:200]})
    if status in (401, 403):
        return ProviderError("The AI provider rejected the server's credentials. Check the API key configuration.",
                             code="provider_auth", status=502)
    if status == 402:
        return ProviderError("The AI provider account is out of credits or has a billing problem. "
                             "Add credits or enable billing for the API key, then try again.",
                             code="provider_billing", status=502, detail={"upstream": msg[:200]})
    if status == 404:
        return ProviderError("The configured AI model is not available. Check GEMINI_MODEL.",
                             code="model_not_found", status=502, detail={"upstream": msg[:200]})
    return ProviderError("The AI provider rejected the request.", code="provider_bad_request",
                         detail={"upstream": msg[:200]})


class GeminiLLM(BaseLLM):
    provider = "gemini"

    def __init__(self, settings: Settings):
        super().__init__(settings)
        if not settings.gemini_api_key:
            raise NotConfigured("GEMINI_API_KEY is not set.")
        from google import genai
        from google.genai import types
        self._types = types
        self._client = genai.Client(
            api_key=settings.gemini_api_key,
            http_options=types.HttpOptions(timeout=int(settings.llm_timeout_s * 1000),
                                           **({"base_url": settings.gemini_base_url} if settings.gemini_base_url else {})),
        )
        self.model = settings.gemini_model

    def _call(self, prompt, system, schema, temperature, max_tokens, thinking):
        from google.genai import errors as gerr
        t = self._types
        cfg_kwargs: dict[str, Any] = dict(temperature=temperature, max_output_tokens=max_tokens)
        if system:
            cfg_kwargs["system_instruction"] = system
        if schema is not None:
            cfg_kwargs["response_mime_type"] = "application/json"
        budget = self.s.gemini_thinking_budget if thinking else 0
        if budget >= 0 and "2.5" in self.model:
            cfg_kwargs["thinking_config"] = t.ThinkingConfig(thinking_budget=budget)
        fb = self.s.gemini_fallback_model
        models = [self.model] + ([fb] if fb and fb != self.model else [])
        last_err: Exception | None = None
        for idx, model in enumerate(models):
            has_next = idx + 1 < len(models)
            try:
                resp = self._client.models.generate_content(
                    model=model, contents=prompt, config=t.GenerateContentConfig(**cfg_kwargs))
            except gerr.APIError as e:
                status = getattr(e, "code", None)
                # The fallback covers "this model can't serve us right now": retired (404), quota (429) or overloaded
                # (5xx). Auth, billing and malformed-request errors would fail identically on any model.
                if has_next and (status in (404, 429) or (status or 0) >= 500):
                    log.warning("Model %s failed (HTTP %s); using configured fallback %s", model, status, models[idx + 1])
                    last_err = e
                    continue
                raise _status_to_error(status, str(e)) from e
            except (httpx.TimeoutException, httpx.TransportError) as e:
                if has_next:
                    log.warning("Model %s timed out; using configured fallback %s", model, models[idx + 1])
                    last_err = e
                    continue
                raise ProviderUnavailable("The AI provider timed out.", code="provider_timeout") from e
            text = getattr(resp, "text", None) or ""
            if not text:
                reason = None
                try:
                    reason = resp.candidates[0].finish_reason
                except Exception:
                    pass
                raise ProviderError(f"The AI model returned no content (finish reason: {reason}).",
                                    code="empty_response")
            um = getattr(resp, "usage_metadata", None)
            usage = {"prompt_tokens": getattr(um, "prompt_token_count", None),
                     "output_tokens": getattr(um, "candidates_token_count", None),
                     "total_tokens": getattr(um, "total_token_count", None)} if um else {}
            return text, usage, model
        raise _status_to_error(404, str(last_err))

    def _check(self) -> dict:
        try:
            m = self._client.models.get(model=self.model)
            return {"ok": True, "provider": "gemini", "model": self.model,
                    "display_name": getattr(m, "display_name", None)}
        except Exception as e:
            msg = str(e)
            return {"ok": False, "provider": "gemini", "model": self.model,
                    "detail": "Configured model is not available or the key is invalid: " + msg[:160],
                    "fallback_model": self.s.gemini_fallback_model or None}


class GroqLLM(BaseLLM):
    provider = "groq"

    def __init__(self, settings: Settings):
        super().__init__(settings)
        if not settings.groq_api_key:
            raise NotConfigured("GROQ_API_KEY is not set.")
        self.model = settings.groq_model

    def _call(self, prompt, system, schema, temperature, max_tokens, thinking):
        msgs = ([{"role": "system", "content": system}] if system else []) + [{"role": "user", "content": prompt}]
        body: dict[str, Any] = {"model": self.model, "messages": msgs, "temperature": temperature,
                                "max_tokens": min(max_tokens, 8000)}
        if schema is not None:
            body["response_format"] = {"type": "json_object"}
        try:
            r = httpx.post("https://api.groq.com/openai/v1/chat/completions", json=body,
                           headers={"Authorization": f"Bearer {self.s.groq_api_key}"},
                           timeout=self.s.llm_timeout_s)
        except (httpx.TimeoutException, httpx.TransportError) as e:
            raise ProviderUnavailable("The AI provider timed out.", code="provider_timeout") from e
        if r.status_code != 200:
            raise _status_to_error(r.status_code, r.text)
        data = r.json()
        text = data["choices"][0]["message"]["content"] or ""
        u = data.get("usage", {})
        return text, {"prompt_tokens": u.get("prompt_tokens"), "output_tokens": u.get("completion_tokens"),
                      "total_tokens": u.get("total_tokens")}, self.model


class FakeLLM(BaseLLM):
    """Scriptable test double. `handler(prompt, system, schema) -> str`."""
    provider = "fake"
    model = "fake-llm"

    def __init__(self, settings: Settings, handler: Callable[[str, Optional[str], Any], str] | None = None):
        super().__init__(settings)
        self.handler = handler or (lambda p, s, sc: "{}")
        self.calls: list[dict] = []

    def _call(self, prompt, system, schema, temperature, max_tokens, thinking):
        self.calls.append({"prompt": prompt, "system": system, "schema": schema})
        out = self.handler(prompt, system, schema)
        if isinstance(out, Exception):
            raise out
        return out, {"prompt_tokens": len(prompt) // 4, "output_tokens": len(out) // 4}, self.model


def build_llm(settings: Settings) -> Optional[BaseLLM]:
    p = settings.resolved_llm_provider
    if p == "gemini":
        return GeminiLLM(settings)
    if p == "groq":
        return GroqLLM(settings)
    if p == "fake":
        return FakeLLM(settings)
    return None


# ---------------------------------------------------------------- structured output helpers
_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$", re.I)


def extract_json(text: str) -> Any:
    """Parse JSON from model output, tolerating code fences and leading/trailing prose."""
    t = _FENCE.sub("", text.strip()).strip()
    try:
        return json.loads(t)
    except json.JSONDecodeError:
        pass
    for open_c, close_c in (("{", "}"), ("[", "]")):
        start = t.find(open_c)
        if start == -1:
            continue
        depth, in_str, esc = 0, False, False
        for i in range(start, len(t)):
            ch = t[i]
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch == open_c:
                depth += 1
            elif ch == close_c:
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(t[start:i + 1])
                    except json.JSONDecodeError:
                        break
    raise ValueError("no valid JSON found in model output")


def generate_structured(llm: BaseLLM, prompt: str, model_cls: Type[BaseModel], *, system: str | None = None,
                        temperature: float = 0.1, max_tokens: int | None = None, thinking: bool = False,
                        repair_attempts: int = 1) -> tuple[BaseModel, LLMResult]:
    """Call the LLM and validate the output against a pydantic model, with one repair attempt."""
    schema_hint = json.dumps(model_cls.model_json_schema(), separators=(",", ":"))[:3000]
    full_prompt = f"{prompt}\n\nReturn ONLY JSON matching this JSON schema:\n{schema_hint}"
    err: Exception | None = None
    res: LLMResult | None = None
    for attempt in range(repair_attempts + 1):
        p = full_prompt if attempt == 0 else (
            f"{full_prompt}\n\nYour previous reply was invalid ({str(err)[:300]}). Reply again with ONLY valid JSON.")
        res = llm.generate(p, system=system, schema=model_cls, temperature=temperature, max_tokens=max_tokens,
                           thinking=thinking)
        try:
            data = extract_json(res.text)
            obj = model_cls.model_validate(data)
            res.parsed = obj
            return obj, res
        except (ValueError, ValidationError) as e:
            err = e
    raise ProviderError("The AI model returned malformed structured output.", code="malformed_output",
                        detail={"validation": str(err)[:300]})
