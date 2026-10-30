import hashlib
import threading
import time

from fastapi import Request

from app.config import settings


class RateLimiter:
    """Fixed-window counter keyed by an identity string. Process-local."""

    def __init__(self, window: int = 60) -> None:
        self.window = window
        self._hits: dict[str, tuple[int, int]] = {}
        self._lock = threading.Lock()
        self._pruned_at = 0

    def hit(self, key: str, limit: int) -> tuple[bool, int, int]:
        """Returns (allowed, remaining, seconds_until_reset)."""
        now = time.time()
        bucket = int(now // self.window)
        reset = int((bucket + 1) * self.window - now) + 1
        with self._lock:
            if bucket != self._pruned_at:
                self._hits = {k: v for k, v in self._hits.items() if v[0] == bucket}
                self._pruned_at = bucket
            prev_bucket, count = self._hits.get(key, (bucket, 0))
            count = count + 1 if prev_bucket == bucket else 1
            self._hits[key] = (bucket, count)
        return count <= limit, max(limit - count, 0), reset


limiter = RateLimiter()


def classify(request: Request) -> tuple[str, int]:
    """Pick the rate-limit bucket key and limit for a request."""
    ip = request.client.host if request.client else "-"
    path = request.url.path
    if path in ("/auth/token", "/auth/register"):
        return f"auth:{ip}", settings.auth_rate_limit_per_minute
    if path.startswith("/signing/public/") or path.startswith("/sign/"):
        return f"public:{ip}", settings.public_rate_limit_per_minute
    credential = request.headers.get("x-api-key") or request.headers.get("authorization")
    if credential:
        return f"cred:{hashlib.sha256(credential.encode()).hexdigest()[:24]}", settings.rate_limit_per_minute
    return f"anon:{ip}", settings.rate_limit_per_minute
