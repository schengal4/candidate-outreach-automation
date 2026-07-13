"""Per-account data isolation as FastAPI dependencies.

Every candidate belongs to the Google account that created it. Routes
declare `candidate: Candidate = Depends(owned_candidate)` (or owned_run) —
a route physically cannot receive the resource without the ownership check
having run, so a newly added route can't forget it, which was the failure
mode when every handler repeated an if-not-owned-return-404 preamble.

A missing resource and someone else's resource raise the same NotFoundError
(rendered as a friendly 404 page by the handler registered in main.py), so
candidate IDs can't be probed for existence.
"""

from typing import Optional, Tuple

from fastapi import Request

from . import config, storage
from .models import Candidate, RunState
from .run_manager import manager


class NotFoundError(Exception):
    """Resource missing or not owned by the session — rendered as a 404 page."""

    def __init__(self, message: str = "Candidate not found."):
        super().__init__(message)
        self.message = message


def session_owner(request: Request) -> Optional[str]:
    """The login email data is scoped to — None in open mode (REQUIRE_LOGIN=0),
    where the app intentionally behaves like the original single-user version."""
    if not config.settings.LOGIN_REQUIRED:
        return None
    return str(request.session.get("user_email", "")).strip().lower()


def owned_candidate(request: Request, candidate_id: str) -> Candidate:
    """The candidate, but only if the logged-in user owns it."""
    candidate = storage.get_candidate(candidate_id)
    if not candidate:
        raise NotFoundError()
    owner = session_owner(request)
    if owner is not None and candidate.owner_email.strip().lower() != owner:
        raise NotFoundError()
    return candidate


def owned_run(request: Request, run_id: str) -> Tuple[RunState, Candidate]:
    """(run, candidate) for a run the logged-in user owns. Runs have no owner
    of their own — they inherit the candidate's."""
    run = manager.get(run_id)
    if not run:
        raise NotFoundError("Run not found.")
    candidate = owned_candidate(request, run.candidate_id)
    return run, candidate
