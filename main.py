"""Token bucket rate limiter exposed as a small HTTP API.

Run:   pip install fastapi uvicorn
       uvicorn main:app --port 8000

Try:   curl -X POST localhost:8000/v1/check \
            -H 'content-type: application/json' \
            -d '{"client_id": "alice"}'
"""

import math
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

DEFAULT_CAPACITY = 10        # max burst size (tokens)
DEFAULT_REFILL_RATE = 1.0    # tokens added per second
SWEEP_INTERVAL = 60.0        # seconds between evictions of idle buckets


@dataclass
class Bucket:
    capacity: float
    refill_rate: float
    tokens: float
    last_refill: float


@dataclass
class Decision:
    allowed: bool
    remaining: float
    retry_after: float | None  # seconds; None if the request can never succeed


class TokenBucketLimiter:
    def __init__(self, clock: Callable[[], float] = time.monotonic):
        self._buckets: dict[str, Bucket] = {}
        self._lock = threading.Lock()
        self._clock = clock
        self._last_sweep = clock()

    def check(self, key: str, cost: float, capacity: float, refill_rate: float) -> Decision:
        now = self._clock()
        with self._lock:
            self._maybe_sweep(now)

            b = self._buckets.get(key)
            if b is None:
                b = Bucket(capacity, refill_rate, tokens=capacity, last_refill=now)
                self._buckets[key] = b
            else:
                # Lazy refill using the *old* rate, then adopt any new config.
                self._refill(b, now)
                b.capacity, b.refill_rate = capacity, refill_rate
                b.tokens = min(b.tokens, capacity)

            if cost > b.capacity:
                # Can never be satisfied, no matter how long the caller waits.
                return Decision(False, b.tokens, None)

            if b.tokens >= cost:
                b.tokens -= cost
                return Decision(True, b.tokens, 0.0)

            wait = (cost - b.tokens) / b.refill_rate
            return Decision(False, b.tokens, wait)

    @staticmethod
    def _refill(b: Bucket, now: float) -> None:
        elapsed = max(0.0, now - b.last_refill)
        b.tokens = min(b.capacity, b.tokens + elapsed * b.refill_rate)
        b.last_refill = now

    def _maybe_sweep(self, now: float) -> None:
        """Drop buckets that have fully refilled; they're identical to a fresh one."""
        if now - self._last_sweep < SWEEP_INTERVAL:
            return
        self._last_sweep = now
        for key in list(self._buckets):
            b = self._buckets[key]
            if b.tokens + (now - b.last_refill) * b.refill_rate >= b.capacity:
                del self._buckets[key]


# ---------------------------------------------------------------- HTTP layer

class CheckRequest(BaseModel):
    client_id: str = Field(min_length=1, max_length=256)
    cost: float = Field(1, gt=0, description="Tokens this request consumes")
    capacity: float = Field(DEFAULT_CAPACITY, gt=0, description="Bucket size / max burst")
    refill_rate: float = Field(DEFAULT_REFILL_RATE, gt=0, description="Tokens per second")


class CheckResponse(BaseModel):
    allowed: bool
    remaining: float
    retry_after_seconds: float | None


app = FastAPI(title="Rate Limiter")
import os

if os.environ.get("REDIS_URL"):
    from redis_limiter import RedisTokenBucketLimiter
    limiter = RedisTokenBucketLimiter(
        os.environ["REDIS_URL"],
        fail_open=os.environ.get("FAIL_OPEN", "true").lower() == "true",
    )
else:
    limiter = TokenBucketLimiter()  # in-memory, single instance only


@app.post("/v1/check", response_model=CheckResponse)
def check(req: CheckRequest) -> CheckResponse:
    d = limiter.check(
        req.client_id,
        req.cost,
        req.capacity,
        req.refill_rate,
    )

    response = CheckResponse(
        allowed=d.allowed,
        remaining=math.floor(d.remaining * 1000) / 1000,
        retry_after_seconds=(
            None if d.retry_after is None else round(d.retry_after, 3)
        ),
    )

    if not d.allowed:
        raise HTTPException(
            status_code=429,
            detail=response.model_dump(),
            headers=(
                {"Retry-After": str(max(1, math.ceil(d.retry_after)))}
                if d.retry_after is not None
                else {}
            ),
        )

    return response
@app.get("/health")
def health() -> dict:
    return {"status": "ok"}