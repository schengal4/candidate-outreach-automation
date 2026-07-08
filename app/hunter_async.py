"""Async wrapper over the existing sync HunterClient.

The Hunter client uses `requests`, so calls run in a worker thread. A single
semaphore caps concurrent Hunter calls across ALL company pipelines, per spec.
"""

import asyncio
import sys
from pathlib import Path
from typing import Optional, Tuple

# hunter_client.py lives at the project root, one level above this package.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hunter_client import HunterAPIError, HunterClient  # noqa: E402

from .config import HUNTER_CONCURRENCY, MIN_EMAIL_SCORE  # noqa: E402

_semaphore: Optional[asyncio.Semaphore] = None
_client: Optional[HunterClient] = None


def _get_semaphore() -> asyncio.Semaphore:
    global _semaphore
    if _semaphore is None:
        _semaphore = asyncio.Semaphore(HUNTER_CONCURRENCY)
    return _semaphore


def _get_client() -> HunterClient:
    global _client
    if _client is None:
        _client = HunterClient()  # reads HUNTER_API_KEY from env
    return _client


async def find_email(domain: str, first_name: str, last_name: str) -> Tuple[Optional[str], Optional[int]]:
    """Return (email, score) if Hunter finds a confident match, else (None, None)."""
    async with _get_semaphore():
        try:
            result = await asyncio.to_thread(
                _get_client().email_finder,
                domain=domain,
                first_name=first_name,
                last_name=last_name,
            )
        except HunterAPIError:
            return None, None
    data = (result or {}).get("data") or {}
    email = data.get("email")
    score = data.get("score")
    if email and (score is None or score >= MIN_EMAIL_SCORE):
        return email, score
    return None, None
