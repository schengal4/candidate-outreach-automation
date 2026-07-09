"""Data structures shared across the pipeline and UI."""

from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


# ------------------------------------------------------------------ #
# Candidate
# ------------------------------------------------------------------ #
@dataclass
class Candidate:
    id: str
    name: str
    email: str
    current_employer: str
    resume_text: str
    # Google account (login email) this candidate belongs to. Every list and
    # candidate-scoped route filters on it when login is enabled. Empty only
    # in open mode (REQUIRE_LOGIN=0), where no filtering applies.
    owner_email: str = ""
    resume_filename: str = ""
    linkedin_url: str = ""
    career_goals: str = ""
    culture_prefs: str = ""
    target_industry_role: str = ""
    # Optional freeform guidance for the drafted outreach emails — style
    # preferences, phrases to use/avoid, or a template to follow. Editable
    # from the candidate page; fed into every draft call's prompt.
    draft_instructions: str = ""
    red_flag_detection: bool = False
    retention_months: int = 12
    max_companies: int = 10

    @staticmethod
    def new_id() -> str:
        return uuid.uuid4().hex[:10]

    def to_dict(self) -> Dict[str, Any]:
        return dict(self.__dict__)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "Candidate":
        return cls(**{k: d[k] for k in cls.__dataclass_fields__ if k in d})


# ------------------------------------------------------------------ #
# Contacts
# ------------------------------------------------------------------ #
@dataclass
class Contact:
    first_name: str = ""
    last_name: str = ""
    title: str = ""
    linkedin_url: str = ""
    employment_verified: bool = False
    evidence: str = ""

    @property
    def full_name(self) -> str:
        return f"{self.first_name} {self.last_name}".strip()

    def to_dict(self) -> Dict[str, Any]:
        return dict(self.__dict__)

    @classmethod
    def from_dict(cls, d: Optional[Dict[str, Any]]) -> Optional["Contact"]:
        if not d:
            return None
        return cls(
            first_name=str(d.get("first_name", "") or ""),
            last_name=str(d.get("last_name", "") or ""),
            title=str(d.get("title", "") or ""),
            linkedin_url=str(d.get("linkedin_url", "") or ""),
            employment_verified=bool(d.get("employment_verified", False)),
            evidence=str(d.get("evidence", "") or ""),
        )


# ------------------------------------------------------------------ #
# Per-company pipeline state
# ------------------------------------------------------------------ #
class CompanyStatus:
    PENDING = "pending"
    CONTACTS = "identifying contacts"
    EMAIL = "looking up email"
    RESEARCH = "researching"
    DRAFTING = "drafting"
    DONE = "done"
    DROPPED = "dropped"


@dataclass
class CompanyState:
    name: str
    domain: str
    reason: str = ""                      # why the LLM shortlisted it
    status: str = CompanyStatus.PENDING
    activity: str = ""                    # live progress text for the current status
    drop_reason: str = ""
    primary: Optional[Contact] = None
    backup: Optional[Contact] = None
    contact_used: Optional[Contact] = None
    email: str = ""
    email_score: Optional[int] = None
    research_summary: str = ""
    research_items: List[str] = field(default_factory=list)
    red_flags: List[str] = field(default_factory=list)
    draft_subject: str = ""
    draft_body: str = ""
    gmail_draft_created: bool = False
    gmail_error: str = ""

    def to_dict(self) -> Dict[str, Any]:
        d = dict(self.__dict__)
        for key in ("primary", "backup", "contact_used"):
            contact = d[key]
            d[key] = contact.to_dict() if contact else None
        return d

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "CompanyState":
        obj = cls(name=str(d.get("name", "")), domain=str(d.get("domain", "")))
        for key, value in d.items():
            if key in ("primary", "backup", "contact_used"):
                setattr(obj, key, Contact.from_dict(value))
            elif hasattr(obj, key):
                setattr(obj, key, value)
        return obj


# ------------------------------------------------------------------ #
# Run state
# ------------------------------------------------------------------ #
class RunPhase:
    DISCOVERING = "discovering"
    REVIEW = "review"          # candidate review gate
    RUNNING = "running"
    DONE = "done"
    ERROR = "error"


@dataclass
class RunState:
    id: str
    candidate_id: str
    phase: str = RunPhase.DISCOVERING
    error: str = ""
    activity: str = ""  # live progress text during discovery
    # discovery output (shown at the review gate)
    discovered: List[Dict[str, str]] = field(default_factory=list)
    # per-company pipeline states (populated after approval)
    companies: List[CompanyState] = field(default_factory=list)
    # set when phase -> RUNNING; drives the timeout-button / hard-timeout logic
    started_running_at: Optional[float] = None
    # set when the run reaches DONE or ERROR; freezes the elapsed-time display
    finished_at: Optional[float] = None
    created_at: float = field(default_factory=time.time)
    # set by the "retrieve what's done" button to cut a RUNNING run short.
    # In-memory only — a persisted run that gets reloaded starts with a
    # fresh (unset) event, which is correct: its tasks are gone anyway.
    stop_event: asyncio.Event = field(default_factory=asyncio.Event)

    @staticmethod
    def new_id() -> str:
        return uuid.uuid4().hex[:12]

    # ---- persistence (see app/run_store.py) ----
    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "candidate_id": self.candidate_id,
            "phase": self.phase,
            "error": self.error,
            "activity": self.activity,
            "discovered": self.discovered,
            "companies": [c.to_dict() for c in self.companies],
            "started_running_at": self.started_running_at,
            "finished_at": self.finished_at,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "RunState":
        run = cls(id=str(d["id"]), candidate_id=str(d.get("candidate_id", "")))
        run.phase = str(d.get("phase", RunPhase.DISCOVERING))
        run.error = str(d.get("error", ""))
        run.activity = str(d.get("activity", ""))
        run.discovered = list(d.get("discovered", []))
        run.companies = [CompanyState.from_dict(cd) for cd in d.get("companies", [])]
        run.started_running_at = d.get("started_running_at")
        run.finished_at = d.get("finished_at")  # absent in pre-feature run files
        run.created_at = float(d.get("created_at", time.time()))
        return run

    # ---- run report helpers ----
    def summary(self) -> Dict[str, Any]:
        done = [c for c in self.companies if c.status == CompanyStatus.DONE]
        dropped = [c for c in self.companies if c.status == CompanyStatus.DROPPED]
        return {
            "attempted": len(self.companies),
            "drafts": len(done),
            "dropped": dropped,
            "contacts_found": sum(1 for c in self.companies if c.primary or c.backup),
            "excluded_at_review": max(0, len(self.discovered) - len(self.companies))
            if self.discovered
            else 0,
        }
