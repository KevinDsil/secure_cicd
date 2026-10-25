"""Redis-backed token bucket. Drop-in replacement for TokenBucketLimiter."""

import redis_limiter

# Runs atomically inside Redis. Uses Redis's own clock (TIME) so that
# app servers with skewed clocks can't disagree about how much has refilled.
LUA_TOKEN_BUCKET = """
local key      = KEYS[1]
local capacity = tonumber(ARGV[1])
local rate     = tonumber(ARGV[2])
local cost     = tonumber(ARGV[3])

local t   = redis.call('TIME')
local now = tonumber(t[1]) + tonumber(t[2]) / 1000000

local data   = redis.call('HMGET', key, 'tokens', 'ts')
local tokens = tonumber(data[1])
local ts     = tonumber(data[2])

if tokens == nil then
  tokens = capacity
  ts = now
end

-- Lazy refill
tokens = math.min(capacity, tokens + math.max(0, now - ts) * rate)

local allowed = 0
local retry   = 0
if cost > capacity then
  retry = -1                      -- can never succeed
elseif tokens >= cost then
  tokens = tokens - cost
  allowed = 1
else
  retry = (cost - tokens) / rate
end

redis.call('HSET', key, 'tokens', tokens, 'ts', now)
-- Once idle long enough to be full, the bucket equals a fresh one: safe to expire.
redis.call('PEXPIRE', key, math.ceil(capacity / rate * 1000) + 1000)

return {allowed, tostring(tokens), tostring(retry)}
"""


class RedisTokenBucketLimiter:
    def __init__(self, url: str = "redis://localhost:6379/0",
                 prefix: str = "rl:", fail_open: bool = True):
        self._r = redis_limiter.Redis.from_url(url, socket_timeout=0.25,
                                       socket_connect_timeout=0.25)
        self._script = self._r.register_script(LUA_TOKEN_BUCKET)
        self._prefix = prefix
        self._fail_open = fail_open

    def check(self, key: str, cost: float, capacity: float, refill_rate: float):
        from main import Decision  # local import avoids a circular import

        try:
            allowed, tokens, retry = self._script(
                keys=[self._prefix + key],
                args=[capacity, refill_rate, cost],
            )
        except redis_limiter.RedisError:
            # Redis is down or slow: choose availability or strictness.
            if self._fail_open:
                return Decision(True, 0.0, 0.0)
            return Decision(False, 0.0, 1.0)

        retry = float(retry)
        return Decision(
            allowed=bool(allowed),
            remaining=float(tokens),
            retry_after=None if retry < 0 else retry,
        )