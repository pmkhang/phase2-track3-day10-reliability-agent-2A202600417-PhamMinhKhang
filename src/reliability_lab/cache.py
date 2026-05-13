from __future__ import annotations

import hashlib
import re
import time
from dataclasses import dataclass
from typing import Any

# ---------------------------------------------------------------------------
# Shared utilities — use these in both ResponseCache and SharedRedisCache
# ---------------------------------------------------------------------------

PRIVACY_PATTERNS = re.compile(
    r"\b(balance|password|credit.card|ssn|social.security|user.\d+|account.\d+)\b",
    re.IGNORECASE,
)


def _is_uncacheable(query: str) -> bool:
    """Return True if query contains privacy-sensitive keywords."""
    return bool(PRIVACY_PATTERNS.search(query))


def _looks_like_false_hit(query: str, cached_key: str) -> bool:
    """Return True if query and cached key contain different 4-digit numbers (years, IDs)."""
    nums_q = set(re.findall(r"\b\d{4}\b", query))
    nums_c = set(re.findall(r"\b\d{4}\b", cached_key))
    return bool(nums_q and nums_c and nums_q != nums_c)


# ---------------------------------------------------------------------------
# In-memory cache (existing)
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class CacheEntry:
    key: str
    value: str
    created_at: float
    metadata: dict[str, str]


class ResponseCache:
    """In-memory response cache with semantic similarity matching and safety guardrails.

    Uses token overlap (Jaccard) with normalization and year-mismatch penalty.
    Privacy-sensitive queries are excluded from caching.
    """

    def __init__(self, ttl_seconds: int, similarity_threshold: float):
        self.ttl_seconds = ttl_seconds
        self.similarity_threshold = similarity_threshold
        self._entries: list[CacheEntry] = []

    def get(self, query: str) -> tuple[str | None, float]:
        if _is_uncacheable(query):
            return None, 0.0
        best_value: str | None = None
        best_key: str | None = None
        best_score = 0.0
        now = time.time()
        self._entries = [e for e in self._entries if now - e.created_at <= self.ttl_seconds]
        for entry in self._entries:
            score = self.similarity(query, entry.key)
            if score > best_score:
                best_score = score
                best_value = entry.value
                best_key = entry.key
        if best_score >= self.similarity_threshold:
            if best_key is not None and _looks_like_false_hit(query, best_key):
                return None, best_score
            return best_value, best_score
        return None, best_score

    def set(self, query: str, value: str, metadata: dict[str, str] | None = None) -> None:
        if _is_uncacheable(query):
            return
        self._entries.append(CacheEntry(query, value, time.time(), metadata or {}))

    @staticmethod
    def similarity(a: str, b: str) -> float:
        """Token overlap (Jaccard) with normalization and year-mismatch penalty."""
        def normalize(text: str) -> str:
            return re.sub(r"[^a-z0-9 ]+", " ", text.lower())

        left = set(normalize(a).split())
        right = set(normalize(b).split())
        if not left or not right:
            return 0.0
        overlap = len(left & right) / len(left | right)
        # Penalize mismatched years/IDs to reduce false semantic hits.
        if _looks_like_false_hit(a, b):
            overlap *= 0.2
        return overlap


# ---------------------------------------------------------------------------
# Redis shared cache (new)
# ---------------------------------------------------------------------------


class SharedRedisCache:
    """Redis-backed shared cache for multi-instance deployments.

    Data model:
        Key    = "{prefix}{query_hash}"   (Redis String namespace)
        Value  = Redis Hash with fields:  "query", "response"
        TTL    = Redis EXPIRE (automatic cleanup)

    For similarity lookup: SCAN all keys with self.prefix, HGET each entry's
    "query" field, compute similarity locally via ResponseCache.similarity().
    """

    def __init__(
        self,
        redis_url: str,
        ttl_seconds: int,
        similarity_threshold: float,
        prefix: str = "rl:cache:",
    ):
        import redis as redis_lib

        self.ttl_seconds = ttl_seconds
        self.similarity_threshold = similarity_threshold
        self.prefix = prefix
        self.false_hit_log: list[dict[str, object]] = []
        self._redis: Any = redis_lib.Redis.from_url(redis_url, decode_responses=True)
        self._redis_errors = (
            ConnectionError,
            TimeoutError,
            redis_lib.exceptions.ConnectionError,
            redis_lib.exceptions.TimeoutError,
            redis_lib.exceptions.RedisError,
        )

    def ping(self) -> bool:
        """Check Redis connectivity."""
        try:
            return bool(self._redis.ping())
        except Exception:
            return False

    def get(self, query: str) -> tuple[str | None, float]:
        """Look up a cached response from Redis with exact match and similarity fallback."""
        if _is_uncacheable(query):
            return None, 0.0

        try:
            exact_key = f"{self.prefix}{self._query_hash(query)}"
            exact_response = self._redis.hget(exact_key, "response")
            if exact_response is not None:
                return exact_response, 1.0

            best_key: str | None = None
            best_query: str | None = None
            best_response: str | None = None
            best_score = 0.0
            for key in self._redis.scan_iter(f"{self.prefix}*"):
                cached_query = self._redis.hget(key, "query")
                cached_response = self._redis.hget(key, "response")
                if not cached_query or cached_response is None:
                    continue
                if _looks_like_false_hit(query, cached_query):
                    self.false_hit_log.append(
                        {
                            "query": query,
                            "cached_query": cached_query,
                            "cached_key": key,
                            "score": ResponseCache.similarity(query, cached_query),
                            "ts": time.time(),
                        }
                    )
                    continue
                score = ResponseCache.similarity(query, cached_query)
                if score > best_score:
                    best_score = score
                    best_key = key
                    best_query = cached_query
                    best_response = cached_response

            if best_score >= self.similarity_threshold and best_response is not None and best_query is not None:
                if _looks_like_false_hit(query, best_query):
                    self.false_hit_log.append(
                        {
                            "query": query,
                            "cached_query": best_query,
                            "cached_key": best_key,
                            "score": best_score,
                            "ts": time.time(),
                        }
                    )
                    return None, best_score
                return best_response, best_score
            return None, best_score
        except self._redis_errors:
            return None, 0.0

    def set(self, query: str, value: str, metadata: dict[str, str] | None = None) -> None:
        """Store a response in Redis with TTL."""
        if _is_uncacheable(query):
            return
        try:
            key = f"{self.prefix}{self._query_hash(query)}"
            payload = {"query": query, "response": value}
            if metadata:
                payload.update({f"meta:{k}": v for k, v in metadata.items()})
            self._redis.hset(key, mapping=payload)
            self._redis.expire(key, self.ttl_seconds)
        except self._redis_errors:
            return

    def flush(self) -> None:
        """Remove all entries with this cache prefix (for testing)."""
        for key in self._redis.scan_iter(f"{self.prefix}*"):
            self._redis.delete(key)

    def close(self) -> None:
        """Close Redis connection."""
        if self._redis is not None:
            self._redis.close()

    @staticmethod
    def _query_hash(query: str) -> str:
        """Deterministic short hash for a query string."""
        return hashlib.md5(query.lower().strip().encode()).hexdigest()[:12]
