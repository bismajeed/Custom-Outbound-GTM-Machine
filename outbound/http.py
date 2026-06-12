"""Shared HTTP helpers: a retrying requests wrapper for third-party APIs.

Apollo / Smartlead calls go through ``request_with_retry`` so 429/5xx responses
get exponential backoff without each call site re-implementing it.
"""

from __future__ import annotations

from typing import Any, Optional

import requests
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)


class TransientHTTPError(Exception):
    """Marks a response/exception as worth retrying (429 or 5xx)."""


_RETRYABLE_STATUS = {429, 500, 502, 503, 504}


@retry(
    retry=retry_if_exception_type((TransientHTTPError, requests.ConnectionError,
                                   requests.Timeout)),
    stop=stop_after_attempt(4),  # 1 try + 3 retries
    wait=wait_exponential(multiplier=2, min=2, max=30),
    reraise=True,
)
def request_with_retry(
    method: str,
    url: str,
    *,
    headers: Optional[dict] = None,
    json: Optional[Any] = None,
    params: Optional[dict] = None,
    timeout: int = 30,
) -> requests.Response:
    """Issue an HTTP request, retrying on 429/5xx with exponential backoff.

    Raises requests.HTTPError on non-retryable 4xx so callers can surface it.
    """
    resp = requests.request(
        method, url, headers=headers, json=json, params=params, timeout=timeout
    )
    if resp.status_code in _RETRYABLE_STATUS:
        raise TransientHTTPError(f"{resp.status_code} from {url}: {resp.text[:200]}")
    resp.raise_for_status()
    return resp
