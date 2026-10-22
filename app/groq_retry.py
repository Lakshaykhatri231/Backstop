"""One retry for the two Groq errors that are the provider's, not ours (used by the four policy-analysis modules).

  400 "tool_use_failed"  the model's own tool-call JSON came out malformed (a one-off generation slip, seen ~1 in 6) -> try again
  429 rate limit         free-tier tokens-per-minute; Groq says how long to wait ("try again in 11.2s") -> wait that long, once

Anything else, or a second failure, is returned untouched for the caller to raise on, so nothing is ever hidden: an analysis that
still fails shows its `llm_error` and never invents a recommendation.
"""
import re
import time

import httpx

MAX_WAIT_S = 20.0


def post(url: str, **kw) -> httpx.Response:
    resp = httpx.post(url, **kw)
    if resp.status_code == 400 and "tool_use_failed" in resp.text:
        return httpx.post(url, **kw)
    if resp.status_code == 429:
        m = re.search(r"try again in ([\d.]+)s", resp.text)
        wait = float(m.group(1)) + 0.5 if m else None
        if wait is not None and wait <= MAX_WAIT_S:
            time.sleep(wait)
            return httpx.post(url, **kw)
    return resp
