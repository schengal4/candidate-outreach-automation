"""
Hunter API (v2) client.
========================

This file is a Python "wrapper" around Hunter's email-finding API
(https://api.hunter.io/v2/). Instead of writing raw web requests every time
you want to look something up, you import the HunterClient class from this
file and call simple methods like client.email_finder(...).

It reads your secret API key from the HUNTER_API_KEY environment variable,
so the key never has to be typed into your code.

Endpoints covered in this file:
  - Account Information / Usage        : is my key valid? how many credits left?
  - Domain Search                      : list every email Hunter has for a domain
  - Domain Finder                      : turn a company name into a domain
  - Email Finder                       : guess one person's email from name + domain
  - Email Verifier                     : check whether an email address is real/deliverable
  - Email Count                        : how many emails does Hunter have for a domain?
  - Email / Company / Combined Enrichment : look up everything Hunter knows about a person/company
  - Discover / Discover People          : search for companies matching criteria
  - Leads (list, get, create, create-or-update, update, delete): Hunter's CRM-style lead storage

Setup
-----
    pip install requests
    export HUNTER_API_KEY="your_api_key_here"   (macOS/Linux)
    setx HUNTER_API_KEY "your_api_key_here"      (Windows, only affects NEW terminals)

Basic usage
-----------
    from hunter_client import HunterClient

    client = HunterClient()
    print(client.account_information())
    print(client.domain_search(domain="intercom.com"))
    print(client.email_finder(domain="reddit.com", first_name="Alexis", last_name="Ohanian"))
    print(client.email_verifier(email="patrick@stripe.com"))

Run this file directly to sanity-check your API key against the free
Account Information endpoint (and try a live email_finder lookup):

    python hunter_client.py
"""

from __future__ import annotations  # lets us write type hints without import headaches

import os
from typing import Any, Dict, List, Optional, Union

import requests  # third-party library that does the actual HTTP calls (pip install requests)


class HunterAPIError(Exception):
    """
    Custom exception raised whenever Hunter's API responds with an error
    (e.g. bad API key, missing parameter, rate limit hit).

    Instead of just crashing with a generic error, this carries the HTTP
    status code and Hunter's own error details so you can decide what to do
    (retry, tell the user, log it, etc.).
    """

    def __init__(self, status_code: int, errors: Any):
        self.status_code = status_code  # e.g. 400, 401, 403, 404, 429
        self.errors = errors            # the "errors" block Hunter sent back
        super().__init__(f"Hunter API error ({status_code}): {errors}")


class HunterClient:
    """
    Thin wrapper around the Hunter API (https://api.hunter.io/v2/).

    Every public method below (domain_search, email_finder, etc.) corresponds
    to one Hunter API endpoint. They all funnel through the private
    `_request` helper, which handles the URL, the API key, and error checking
    in one place so we don't repeat that logic in every method.
    """

    # Every endpoint URL is built by sticking a path onto this base.
    BASE_URL = "https://api.hunter.io/v2"

    def __init__(self, api_key: Optional[str] = None, timeout: int = 20):
        """
        Set up the client once, then reuse it for as many API calls as you like.

        Args:
            api_key: Your Hunter API key. If you don't pass one explicitly,
                it's read from the HUNTER_API_KEY environment variable.
            timeout: How many seconds to wait for a response before giving up
                on a single request.
        """
        # Prefer an explicitly-passed key; otherwise fall back to the
        # environment variable set up on your machine.
        self.api_key = api_key or os.environ.get("HUNTER_API_KEY")

        if not self.api_key:
            # Fail fast and clearly, rather than letting every API call error
            # out later with a confusing "unauthorized" message.
            raise ValueError(
                "No Hunter API key found. Pass api_key=... or set the "
                "HUNTER_API_KEY environment variable."
            )

        self.timeout = timeout

        # A requests.Session reuses the same underlying HTTP connection
        # across multiple calls, which is faster than opening a new
        # connection every time.
        self.session = requests.Session()

        # Hunter accepts the API key three different ways (query param,
        # X-API-KEY header, or Authorization header). We use the
        # Authorization header here so the key never appears in URLs/logs.
        self.session.headers.update({"Authorization": "Bearer " + self.api_key})

    # ------------------------------------------------------------------ #
    # Internal request helper
    # ------------------------------------------------------------------ #
    def _request(
        self,
        method: str,
        path: str,
        params: Optional[Dict[str, Any]] = None,
        json_body: Optional[Dict[str, Any]] = None,
    ) -> Any:
        """
        Send one HTTP request to Hunter and hand back the parsed JSON.

        Every public method in this class calls this helper instead of
        talking to `requests` directly. That keeps things like error
        handling and URL-building consistent in exactly one place.

        Args:
            method: HTTP verb, e.g. "GET", "POST", "PUT", "DELETE".
            path: The bit of the URL after the base, e.g. "/domain-search".
            params: Query-string parameters (?key=value&...). Any value that
                is None gets dropped, so callers can pass optional args
                freely without worrying about sending "None" as text.
            json_body: For POST/PUT requests, the JSON payload to send.

        Returns:
            The parsed JSON response as a Python dict (or None for empty
            "204 No Content" responses).

        Raises:
            HunterAPIError: if Hunter responds with an error status code.
        """
        url = self.BASE_URL + path

        # Strip out any parameters the caller left as None so they aren't
        # sent as literal "None" strings in the request.
        params = {k: v for k, v in (params or {}).items() if v is not None}

        response = self.session.request(
            method=method,
            url=url,
            params=params,
            json=json_body,
            timeout=self.timeout,
        )

        # A 204 means "success, but there's nothing to return" (used by
        # some update/delete endpoints) -- there's no JSON body to parse.
        if response.status_code == 204:
            return None

        try:
            payload = response.json()
        except ValueError:
            # Response wasn't JSON at all (shouldn't normally happen with
            # this API) -- fall back to raising based on the HTTP status.
            response.raise_for_status()
            return None

        if not response.ok:
            # Hunter's error responses look like {"errors": [...]}. Surface
            # that in our own exception so callers can inspect it.
            raise HunterAPIError(response.status_code, payload.get("errors", payload))

        return payload

    @staticmethod
    def _csv(value: Optional[Union[str, List[str]]]) -> Optional[str]:
        """
        Small helper for parameters where Hunter wants a comma-separated
        string (e.g. seniority="junior,senior") but it's nicer for callers
        of this client to just pass a Python list, e.g. ["junior", "senior"].
        """
        if value is None:
            return None
        if isinstance(value, str):
            return value  # already a plain string, nothing to do
        return ",".join(value)

    # ------------------------------------------------------------------ #
    # Account / usage
    # ------------------------------------------------------------------ #
    def account_information(self) -> Dict[str, Any]:
        """
        GET /account -- free.
        Returns your Hunter account details: name, plan, and how many
        search/verification credits you've used vs. how many you have.
        Handy as a first call to confirm your API key actually works.
        """
        return self._request("GET", "/account")

    def usage(self, show_overage_requests: Optional[bool] = None) -> Dict[str, Any]:
        """
        GET /usage -- free, does not consume credits.
        Returns how much of your monthly quota you've used and when it resets.
        """
        return self._request(
            "GET", "/usage", params={"show_overage_requests": show_overage_requests}
        )

    # ------------------------------------------------------------------ #
    # Domain / email discovery
    # ------------------------------------------------------------------ #
    def domain_search(
        self,
        domain: Optional[str] = None,
        company: Optional[str] = None,
        limit: Optional[int] = None,
        offset: Optional[int] = None,
        type: Optional[str] = None,  # "personal" | "generic"
        seniority: Optional[Union[str, List[str]]] = None,
        department: Optional[Union[str, List[str]]] = None,
        required_field: Optional[Union[str, List[str]]] = None,
        verification_status: Optional[Union[str, List[str]]] = None,
        job_titles: Optional[Union[str, List[str]]] = None,
    ) -> Dict[str, Any]:
        """
        GET /domain-search -- find every email address Hunter has for a domain.

        You must supply either `domain` (preferred, e.g. "stripe.com") or
        `company` (a company name, less accurate). Everything else is an
        optional filter to narrow the results (job seniority, department,
        job titles, etc.). `limit`/`offset` control pagination.
        """
        if not domain and not company:
            raise ValueError("domain_search requires 'domain' or 'company'.")

        params = {
            "domain": domain,
            "company": company,
            "limit": limit,
            "offset": offset,
            "type": type,
            "seniority": self._csv(seniority),
            "department": self._csv(department),
            "required_field": self._csv(required_field),
            "verification_status": self._csv(verification_status),
            "job_titles": self._csv(job_titles),
        }
        return self._request("GET", "/domain-search", params=params)

    def domain_finder(
        self, company: str, limit: Optional[int] = None, perfect_match: Optional[bool] = None
    ) -> Dict[str, Any]:
        """
        GET /domain-finder -- free, doesn't use credits.
        Turns a company name (e.g. "stripe") into its likely domain
        (e.g. "stripe.com"). Useful as a first step before domain_search or
        email_finder when you only know the company's name.
        """
        params = {"company": company, "limit": limit, "perfect_match": perfect_match}
        return self._request("GET", "/domain-finder", params=params)

    def email_finder(
        self,
        domain: Optional[str] = None,
        company: Optional[str] = None,
        linkedin_handle: Optional[str] = None,
        first_name: Optional[str] = None,
        last_name: Optional[str] = None,
        full_name: Optional[str] = None,
        max_duration: Optional[int] = None,
    ) -> Dict[str, Any]:
        """
        GET /email-finder -- guess ONE specific person's email address.

        You need:
          1. A way to identify the company: `domain` (best), `company`
             (name), or `linkedin_handle`.
          2. A way to identify the person: `first_name` + `last_name`, or
             `full_name` (not needed if you provided `linkedin_handle`,
             which is enough on its own).

        Example: email_finder(domain="anterior.com", first_name="Zahid", last_name="Mahmood")
        """
        if not any([domain, company, linkedin_handle]):
            raise ValueError("email_finder requires 'domain', 'company', or 'linkedin_handle'.")
        if not linkedin_handle and not full_name and not (first_name and last_name):
            raise ValueError(
                "email_finder requires 'first_name' and 'last_name' (or 'full_name'), "
                "unless 'linkedin_handle' is provided."
            )
        params = {
            "domain": domain,
            "company": company,
            "linkedin_handle": linkedin_handle,
            "first_name": first_name,
            "last_name": last_name,
            "full_name": full_name,
            "max_duration": max_duration,
        }
        return self._request("GET", "/email-finder", params=params)

    def email_verifier(self, email: str) -> Dict[str, Any]:
        """
        GET /email-verifier -- check whether an email address is real and
        likely to actually receive mail (as opposed to bouncing).
        Note: Hunter may take a few seconds to verify; a 202 response means
        "still checking, ask again shortly."
        """
        return self._request("GET", "/email-verifier", params={"email": email})

    def email_count(
        self, domain: Optional[str] = None, company: Optional[str] = None, type: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        GET /email-count -- free.
        Quick "how many emails does Hunter have for this domain/company?"
        check -- useful before running a full domain_search.
        """
        if not domain and not company:
            raise ValueError("email_count requires 'domain' or 'company'.")
        return self._request(
            "GET", "/email-count", params={"domain": domain, "company": company, "type": type}
        )

    # ------------------------------------------------------------------ #
    # Enrichment -- "tell me everything you know about ___"
    # ------------------------------------------------------------------ #
    def email_enrichment(
        self,
        email: Optional[str] = None,
        linkedin_handle: Optional[str] = None,
        clearbit_format: Optional[bool] = None,
    ) -> Dict[str, Any]:
        """
        GET /people/find -- look up everything Hunter knows about a PERSON
        (name, location, job title, social handles) given their email or
        LinkedIn handle.
        """
        if not email and not linkedin_handle:
            raise ValueError("email_enrichment requires 'email' or 'linkedin_handle'.")
        params = {"email": email, "linkedin_handle": linkedin_handle, "clearbit_format": clearbit_format}
        return self._request("GET", "/people/find", params=params)

    def company_enrichment(
        self, domain: str, clearbit_format: Optional[bool] = None
    ) -> Dict[str, Any]:
        """
        GET /companies/find -- look up everything Hunter knows about a
        COMPANY (industry, headquarters, size, tech stack) given its domain.
        """
        return self._request(
            "GET", "/companies/find", params={"domain": domain, "clearbit_format": clearbit_format}
        )

    def combined_enrichment(
        self, email: str, clearbit_format: Optional[bool] = None
    ) -> Dict[str, Any]:
        """
        GET /combined/find -- person + company info together in one call,
        given just an email address.
        """
        return self._request(
            "GET", "/combined/find", params={"email": email, "clearbit_format": clearbit_format}
        )

    # ------------------------------------------------------------------ #
    # Discover -- search for companies matching criteria
    # ------------------------------------------------------------------ #
    def discover(
        self, query: Optional[str] = None, filters: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """
        POST /discover -- free.
        Search for companies matching criteria. Either describe what you
        want in plain English via `query` (e.g. "SaaS companies in Europe
        with under 50 employees"), or pass a `filters` dict with specific
        fields (industry, headcount, headquarters_location, etc.) -- see
        Hunter_API_Documentation.md for the full filter list.
        """
        if not query and not filters:
            raise ValueError("discover requires 'query' or at least one filter.")
        body: Dict[str, Any] = {}
        if query:
            body["query"] = query
        if filters:
            body.update(filters)
        return self._request("POST", "/discover", json_body=body)

    def discover_people(
        self, query: Optional[str] = None, filters: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """
        POST /discover/people -- free.
        Same as discover(), but each company result also includes an email
        count, so you can estimate how many contacts you'd find before
        running a full domain_search on each company.
        """
        if not query and not filters:
            raise ValueError("discover_people requires 'query' or at least one filter.")
        body: Dict[str, Any] = {}
        if query:
            body["query"] = query
        if filters:
            body.update(filters)
        return self._request("POST", "/discover/people", json_body=body)

    # ------------------------------------------------------------------ #
    # Leads -- Hunter's built-in lightweight CRM
    # ------------------------------------------------------------------ #
    # These let you save contacts you've found into Hunter itself (as
    # opposed to just looking them up). All Leads calls are free.
    def list_leads(self, **filters: Any) -> Dict[str, Any]:
        """
        GET /leads -- list leads you've already saved in Hunter.
        Pass any supported filter as a keyword argument, e.g.
        list_leads(company="Stripe") to only see leads from Stripe.
        """
        return self._request("GET", "/leads", params=filters)

    def get_lead(self, lead_id: int) -> Dict[str, Any]:
        """GET /leads/:id -- fetch one saved lead by its numeric ID."""
        return self._request("GET", "/leads/" + str(lead_id))

    def create_lead(self, email: str, **fields: Any) -> Dict[str, Any]:
        """
        POST /leads -- save a brand-new lead.
        `email` is required; everything else (first_name, last_name,
        company, position, etc.) is optional -- pass as keyword arguments.
        """
        return self._request("POST", "/leads", json_body={"email": email, **fields})

    def create_or_update_lead(self, email: str, **fields: Any) -> Dict[str, Any]:
        """
        PUT /leads -- "upsert" a lead: creates it if this email doesn't exist
        yet, otherwise updates the existing lead's fields.
        """
        return self._request("PUT", "/leads", json_body={"email": email, **fields})

    def update_lead(self, lead_id: int, **fields: Any) -> None:
        """PUT /leads/:id -- update fields on an existing lead by ID."""
        return self._request("PUT", "/leads/" + str(lead_id), json_body=fields)

    def delete_lead(self, lead_id: int) -> None:
        """DELETE /leads/:id -- permanently remove a saved lead."""
        return self._request("DELETE", "/leads/" + str(lead_id))


# ---------------------------------------------------------------------- #
# This block only runs when you execute the file directly
# (`python hunter_client.py`), NOT when it's imported by another script
# like find_email.py. It's a quick way to confirm your API key works and
# to try out a live email_finder lookup.
# ---------------------------------------------------------------------- #
if __name__ == "__main__":
    client = HunterClient()

    # Step 1: confirm the API key works by hitting the free Account
    # Information endpoint.
    try:
        info = client.account_information()["data"]
        print("Connected to Hunter API successfully.")
        print(f"Account: {info['first_name']} {info['last_name']} <{info['email']}>")
        print(f"Plan: {info['plan_name']} (level {info['plan_level']})")
        print(f"Requests used: {info['requests']}")
    except HunterAPIError as exc:
        print(f"Hunter API returned an error: {exc}")

    # Step 2: example email_finder lookup -- swap in whatever
    # first_name / last_name / domain you're looking for. The full
    # response is nested under the "data" key, same as every other
    # Hunter endpoint.
    try:
        result = client.email_finder(first_name="Richard", last_name="Yoo", domain="lunit.io")
        email = result["data"]
        print(email)
        print(f"Email finder result: {email['email']}, score {email['score']}")
    except Exception as exc:
        print(f"Email finder failed: {exc}")
