"""Async wrapper over the existing sync HunterClient.

The Hunter client uses `requests`, so calls run in a worker thread. A single
semaphore caps concurrent Hunter calls across ALL company pipelines, per spec.
"""

import asyncio
import logging
import sys
from pathlib import Path
from typing import Optional, Tuple

import requests

# hunter_client.py lives at the project root, one level above this package.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hunter_client import HunterAPIError, HunterClient  # noqa: E402

from . import config  # noqa: E402

logger = logging.getLogger("app.hunter")

# Transient network failures (a real run hit an SSL EOF mid-handshake) get one
# retry after this pause before the lookup gives up and reports "no email".
TRANSIENT_RETRY_DELAY_SECONDS = 2.0

_semaphore: Optional[asyncio.Semaphore] = None
_client: Optional[HunterClient] = None


def _get_semaphore() -> asyncio.Semaphore:
    global _semaphore
    if _semaphore is None:
        _semaphore = asyncio.Semaphore(config.settings.HUNTER_CONCURRENCY)
    return _semaphore


def _get_client() -> HunterClient:
    global _client
    if _client is None:
        _client = HunterClient()  # reads HUNTER_API_KEY from env
    return _client


async def find_email(domain: str, first_name: str, last_name: str) -> Tuple[Optional[str], Optional[int], str]:
    """Return (email, score, linkedin_url) if Hunter finds a confident match,
    else (None, None, "").

    linkedin_url is Hunter's own field for the person, sourced from pages it
    observed — free corroboration for the LLM-identified contact's profile
    link, which the model has been known to guess."""
    async with _get_semaphore():
        for attempt in (1, 2):
            try:
                result = await asyncio.to_thread(
                    _get_client().email_finder,
                    domain=domain,
                    first_name=first_name,
                    last_name=last_name,
                )
                break
            except HunterAPIError as exc:
                logger.warning("Hunter lookup failed for %s %s @ %s: %s", first_name, last_name, domain, exc)
                return None, None, ""
            except requests.RequestException as exc:
                # Transient network failure, not a Hunter verdict. One retry;
                # if it persists, report "no email" so the pipeline degrades
                # to the manual-outreach draft instead of dropping a company
                # whose contact is already verified.
                if attempt == 1:
                    logger.warning(
                        "Hunter network error for %s %s @ %s (%s) — retrying once",
                        first_name, last_name, domain, type(exc).__name__,
                    )
                    await asyncio.sleep(TRANSIENT_RETRY_DELAY_SECONDS)
                    continue
                logger.warning(
                    "Hunter network error persisted for %s %s @ %s: %s — treating as no email found",
                    first_name, last_name, domain, exc,
                )
                return None, None, ""
    data = (result or {}).get("data") or {}
    email = data.get("email")
    score = data.get("score")
    linkedin_url = str(data.get("linkedin_url") or "").strip()
    if email and (score is None or score >= config.settings.MIN_EMAIL_SCORE):
        # The address itself stays out of the logs (it's in the run report).
        logger.info(
            "Hunter: email found @ %s (score=%s, linkedin=%s)",
            domain, score, "yes" if linkedin_url else "no",
        )
        return email, score, linkedin_url
    logger.info(
        "Hunter: no confident email for %s %s @ %s (score=%s)",
        first_name, last_name, domain, score,
    )
    return None, None, ""
