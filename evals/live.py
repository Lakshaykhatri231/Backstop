"""Real-model plumbing for the live suites: a response cache (replays are free and deterministic) and rate-limit patience.

Groq's free tier allows ~8,000 tokens a minute and one turn is ~2.3k, so uncached calls are spaced out and a 429 is waited out
(its message says how long) instead of being counted as a model failure.
"""
import hashlib
import json
import re
import time
from pathlib import Path

import httpx

CACHE = Path(__file__).resolve().parent / ".cache"
MIN_GAP_S = 4.0
_last = [0.0]
stats = {"cached": 0, "live": 0, "waited_s": 0.0, "failed": 0}


def _key(url: str, body: dict) -> str:
    return hashlib.sha256((url + json.dumps(body, sort_keys=True, ensure_ascii=False)).encode()).hexdigest()


def install(module) -> None:
    """Route `module.httpx.post` (voice_chat's call to the model) through the cache."""
    CACHE.mkdir(exist_ok=True)
    real_post = httpx.post

    def cached_post(url, **kw):
        body = kw.get("json") or {}
        path = CACHE / f"{_key(url, body)}.json"
        if path.exists():
            stats["cached"] += 1
            return httpx.Response(200, json=json.loads(path.read_text()), request=httpx.Request("POST", url))
        for attempt in range(5):
            gap = MIN_GAP_S - (time.time() - _last[0])
            if gap > 0:
                time.sleep(gap)
                stats["waited_s"] += gap
            _last[0] = time.time()
            r = real_post(url, **kw)
            if r.status_code == 429:
                m = re.search(r"try again in ([\d.]+)s", r.text)
                wait = float(m.group(1)) + 1.0 if m else 12.0
                time.sleep(wait)
                stats["waited_s"] += wait
                continue
            if r.status_code == 200:
                stats["live"] += 1
                path.write_text(json.dumps(r.json(), ensure_ascii=False))
            else:
                stats["failed"] += 1
            return r
        stats["failed"] += 1
        return r

    class _Shim:                      # voice_chat only uses `httpx.post`; everything else is the real module
        def __getattr__(self, name):
            return getattr(httpx, name)
        post = staticmethod(cached_post)

    module.httpx = _Shim()
