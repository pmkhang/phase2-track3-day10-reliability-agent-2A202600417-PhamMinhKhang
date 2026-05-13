from __future__ import annotations

import time
from dataclasses import dataclass

from reliability_lab.cache import ResponseCache, SharedRedisCache
from reliability_lab.circuit_breaker import CircuitBreaker, CircuitOpenError
from reliability_lab.providers import FakeLLMProvider, ProviderError, ProviderResponse


@dataclass(slots=True)
class GatewayResponse:
    text: str
    route: str
    provider: str | None
    cache_hit: bool
    latency_ms: float
    estimated_cost: float
    error: str | None = None


class ReliabilityGateway:
    """Routes requests through cache, circuit breakers, and fallback providers."""

    def __init__(
        self,
        providers: list[FakeLLMProvider],
        breakers: dict[str, CircuitBreaker],
        cache: ResponseCache | SharedRedisCache | None = None,
        max_request_budget: float | None = None,
    ):
        self.providers = providers
        self.breakers = breakers
        self.cache = cache
        self.max_request_budget = max_request_budget

    def complete(self, prompt: str) -> GatewayResponse:
        """Route request through cache → circuit breakers → fallback chain → static fallback."""
        start = time.monotonic()
        estimated_spent = 0.0
        if self.cache is not None:
            cached, score = self.cache.get(prompt)
            if cached is not None:
                return GatewayResponse(
                    cached,
                    f"cache_hit:{score:.2f}",
                    None,
                    True,
                    (time.monotonic() - start) * 1000,
                    0.0,
                )

        last_error: str | None = None
        fallback_reason: str | None = None
        cheapest_cost = min((p.cost_per_1k_tokens for p in self.providers), default=0.0)
        for provider in self.providers:
            projected_cost = self._estimate_provider_cost(prompt, provider)
            if (
                self.max_request_budget is not None
                and estimated_spent + projected_cost > self.max_request_budget
                and provider.cost_per_1k_tokens > cheapest_cost
            ):
                fallback_reason = "budget_exceeded"
                continue
            breaker = self.breakers[provider.name]
            try:
                response: ProviderResponse = breaker.call(provider.complete, prompt)
                estimated_spent += response.estimated_cost
                if self.cache is not None:
                    self.cache.set(prompt, response.text, {"provider": provider.name})
                if provider == self.providers[0]:
                    route = f"primary:{provider.name}"
                else:
                    route = f"fallback:{provider.name}:{fallback_reason or 'provider_error'}"
                return GatewayResponse(
                    text=response.text,
                    route=route,
                    provider=provider.name,
                    cache_hit=False,
                    latency_ms=(time.monotonic() - start) * 1000,
                    estimated_cost=response.estimated_cost,
                )
            except CircuitOpenError as exc:
                last_error = str(exc)
                fallback_reason = "circuit_open"
                if provider != self.providers[0]:
                    continue
                # Keep explicit reason in the route of the next successful provider.
                continue
            except ProviderError as exc:
                last_error = str(exc)
                fallback_reason = "provider_error"
                continue

        return GatewayResponse(
            text="The service is temporarily degraded. Please try again soon.",
            route=f"static_fallback:{last_error or 'all_providers_failed'}",
            provider=None,
            cache_hit=False,
            latency_ms=(time.monotonic() - start) * 1000,
            estimated_cost=0.0,
            error=last_error,
        )

    @staticmethod
    def _estimate_provider_cost(prompt: str, provider: FakeLLMProvider) -> float:
        input_tokens = max(1, len(prompt.split()))
        # Constant output token estimate for budget gating.
        estimated_output_tokens = 40
        return (input_tokens + estimated_output_tokens) / 1000.0 * provider.cost_per_1k_tokens
