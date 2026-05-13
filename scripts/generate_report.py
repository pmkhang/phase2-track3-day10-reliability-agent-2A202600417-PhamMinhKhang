from __future__ import annotations

import argparse
import json
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--metrics", default="reports/metrics.json")
    parser.add_argument("--out", default="reports/final_report.md")
    args = parser.parse_args()

    metrics = json.loads(Path(args.metrics).read_text())

    # Load comparison if available
    comparison_path = Path(args.metrics).with_name("metrics.comparison.json")
    comparison = json.loads(comparison_path.read_text()) if comparison_path.exists() else None

    lines = [
        "# Day 10 Reliability Final Report",
        "",
        "## 1. Architecture Summary",
        "",
        "The gateway implements a multi-layer reliability pattern:",
        "",
        "```",
        "User Request",
        "    |",
        "    v",
        "[Gateway] ---> [Cache check] ---> HIT? return cached (cost=0)",
        "    |                                 |",
        "    v                                 v MISS",
        "[Circuit Breaker: Primary] -------> Provider A (primary)",
        "    |  (OPEN? skip to next)",
        "    v",
        "[Circuit Breaker: Backup] --------> Provider B (backup)",
        "    |  (OPEN? skip)",
        "    v",
        "[Static fallback message]",
        "```",
        "",
        "- **Cache layer**: Checks in-memory or Redis cache first. Privacy-sensitive queries bypass cache.",
        "- **Circuit breaker**: 3-state machine (CLOSED/OPEN/HALF_OPEN) per provider. Fails fast when OPEN.",
        "- **Fallback chain**: Iterates providers in order; skips those with open circuits.",
        "- **Static fallback**: Returns degraded message when all providers unavailable.",
        "",
        "## 2. Configuration",
        "",
        "| Setting | Value | Reason |",
        "|---|---:|---|",
        "| failure_threshold | 3 | Low enough to detect failures fast, high enough to avoid false opens from jitter |",
        "| reset_timeout_seconds | 2 | Matches expected provider recovery time (~2s) |",
        "| success_threshold | 1 | Single successful probe sufficient to restore confidence |",
        "| cache TTL | 300 | 5-min freshness for FAQ-type queries; balances hit rate vs staleness |",
        "| similarity_threshold | 0.92 | Tested: lower values (0.85) caused false hits on date-sensitive queries |",
        "| load_test requests | 100 | Per-scenario; 400 total across 4 scenarios |",
        "",
        "## 3. SLO Definitions",
        "",
        "| SLI | SLO Target | Actual Value | Met? |",
        "|---|---|---:|---|",
        f"| Availability | >= 95% | {metrics.get('availability', 0) * 100:.1f}% | {'✅' if metrics.get('availability', 0) >= 0.95 else '❌'} |",
        f"| Latency P95 | < 2500 ms | {metrics.get('latency_p95_ms', 0):.1f} ms | {'✅' if metrics.get('latency_p95_ms', 0) < 2500 else '❌'} |",
        f"| Fallback success rate | >= 90% | {metrics.get('fallback_success_rate', 0) * 100:.1f}% | {'✅' if metrics.get('fallback_success_rate', 0) >= 0.9 else '❌'} |",
        f"| Cache hit rate | >= 5% | {metrics.get('cache_hit_rate', 0) * 100:.1f}% | {'✅' if metrics.get('cache_hit_rate', 0) >= 0.05 else '❌'} |",
        f"| Recovery time | < 5000 ms | {metrics.get('recovery_time_ms') or 'N/A'} ms | {'✅' if metrics.get('recovery_time_ms') is not None and metrics['recovery_time_ms'] < 5000 else '❌' if metrics.get('recovery_time_ms') is not None else 'N/A'} |",
        "",
        "## 4. Metrics",
        "",
        "| Metric | Value |",
        "|---|---:|",
    ]
    for key, value in metrics.items():
        if key == "scenarios":
            continue
        lines.append(f"| {key} | {value} |")

    lines += [
        "",
        "## 5. Cache Comparison",
        "",
    ]

    if comparison:
        nc = comparison["no_cache"]
        wc = comparison["with_cache"]
        lines += [
            "| Metric | Without Cache | With Cache | Delta |",
            "|---|---:|---:|---|",
            f"| latency_p50_ms | {nc['latency_p50_ms']} | {wc['latency_p50_ms']} | {((wc['latency_p50_ms'] - nc['latency_p50_ms']) / nc['latency_p50_ms'] * 100):.1f}% |" if nc['latency_p50_ms'] else f"| latency_p50_ms | {nc['latency_p50_ms']} | {wc['latency_p50_ms']} | N/A |",
            f"| latency_p95_ms | {nc['latency_p95_ms']} | {wc['latency_p95_ms']} | {((wc['latency_p95_ms'] - nc['latency_p95_ms']) / nc['latency_p95_ms'] * 100):.1f}% |" if nc['latency_p95_ms'] else f"| latency_p95_ms | {nc['latency_p95_ms']} | {wc['latency_p95_ms']} | N/A |",
            f"| estimated_cost | {nc['estimated_cost']} | {wc['estimated_cost']} | {((wc['estimated_cost'] - nc['estimated_cost']) / nc['estimated_cost'] * 100):.1f}% |" if nc['estimated_cost'] else f"| estimated_cost | {nc['estimated_cost']} | {wc['estimated_cost']} | N/A |",
            f"| cache_hit_rate | {nc['cache_hit_rate']} | {wc['cache_hit_rate']} | +{wc['cache_hit_rate']:.4f} |",
        ]
    else:
        lines.append("*Comparison data not available. Run `make run-chaos` to generate.*")

    lines += [
        "",
        "## 6. Redis Shared Cache",
        "",
        "### Why shared cache matters",
        "",
        "- **In-memory cache limitation**: Each gateway instance maintains its own cache. In a horizontally-scaled deployment, cache hits are inconsistent — a query cached on instance A is a miss on instance B.",
        "- **SharedRedisCache solution**: All instances share one Redis-backed cache. A response cached by any instance is immediately available to all others, improving hit rate proportionally to instance count.",
        "",
        "### Evidence of shared state",
        "",
        "Two `SharedRedisCache` instances on the same Redis see identical data:",
        "",
        "```python",
        "c1 = SharedRedisCache('redis://localhost:6379/0', ttl_seconds=60, similarity_threshold=0.5, prefix='rl:test:')",
        "c2 = SharedRedisCache('redis://localhost:6379/0', ttl_seconds=60, similarity_threshold=0.5, prefix='rl:test:')",
        "c1.set('shared query', 'shared response')",
        "cached, score = c2.get('shared query')  # Returns ('shared response', 1.0)",
        "```",
        "",
        "This is verified by `test_redis_cache.py::test_shared_state_across_instances`.",
        "",
        "### Graceful degradation",
        "",
        "If Redis is unreachable, `get()` returns `(None, 0.0)` and `set()` is a no-op — the gateway continues serving via providers without crashing.",
        "",
        "## 7. Chaos Scenarios",
        "",
        "| Scenario | Expected Behavior | Pass/Fail |",
        "|---|---|---|",
    ]

    scenarios = metrics.get("scenarios", {})
    scenario_descriptions = {
        "primary_timeout_100": "All traffic fallback to backup, circuit opens, fallback_success_rate > 0.9",
        "primary_flaky_50": "Circuit oscillates, mix of primary/fallback, availability >= 0.8",
        "all_healthy": "All requests via primary, error_rate < 0.1",
        "cache_stale_candidate": "False-hit guardrails active, availability >= 0.8",
    }
    for name, status in scenarios.items():
        desc = scenario_descriptions.get(name, "Custom scenario")
        lines.append(f"| {name} | {desc} | {status} |")

    lines += [
        "",
        "## 8. Failure Analysis",
        "",
        "### Remaining weakness: Cache SCAN performance under high cardinality",
        "",
        "- **What could go wrong**: The similarity-based cache lookup uses `SCAN` to iterate all cached keys in Redis. With thousands of cached entries, this becomes O(n) per request — adding latency that defeats the purpose of caching.",
        "- **Mitigation**: For production, use Redis Search module (RediSearch) with vector similarity indexing, or limit SCAN to a fixed window of recent entries. Alternatively, rely solely on exact-match (hash-based) lookups and accept lower hit rate.",
        "",
        "### Remaining weakness: Circuit breaker state is per-instance",
        "",
        "- **What could go wrong**: In a multi-instance deployment, each gateway has its own circuit breaker. Instance A may have an open circuit while instance B still sends traffic to a failing provider.",
        "- **Mitigation**: Store circuit breaker counters in Redis (INCR + EXPIRE) so all instances share failure/success state.",
        "",
        "## 9. Next Steps",
        "",
        "1. **Redis-backed circuit state**: Move failure counters to Redis so circuit breaker state is shared across instances.",
        "2. **Cost-aware routing**: Track cumulative cost per billing period; when budget hits 80%, route to cheaper model or cache-only mode.",
        "3. **Concurrent load testing**: Use `ThreadPoolExecutor` to simulate realistic concurrent traffic and measure behavior under contention.",
    ]

    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text("\n".join(lines) + "\n")
    print(f"wrote {args.out}")


if __name__ == "__main__":
    main()
