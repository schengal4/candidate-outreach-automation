# Hunter API Documentation (v2)

## Introduction

You can enjoy Hunter's service features with a simple JSON API:

- **Discover** returns companies matching a set of criteria.
- **Domain Search** returns all email addresses found using a given domain name, with sources.
- **Email Finder** finds the most likely email address from a domain name, first name, and last name.
- **Email Verifier** checks the deliverability of a given email address, verifies if it's been found in the database, and returns sources.
- **Enrichment** returns all the information Hunter has about a person or company.

The API also provides a RESTful way to manage Hunter resources. These are the resources you can Create, Read, Update, and Delete:

- Leads
- Custom Attributes
- Leads Lists
- Email Sequences

**API endpoint:** `https://api.hunter.io/v2/`

---

## Structure

The API always uses the same basic structure:

- `data` contains the data you requested.
- `meta` provides information regarding your request.
- `errors` shows errors with insights into what made the request fail.

**Successful response**
```json
{
  "data": { ... },
  "meta": { ... }
}
```

**Error response**
```json
{
  "errors": { ... }
}
```

---

## Authentication

Authentication requires a key added to every API call. This parameter is always required — a missing or invalid key returns an error.

It can be passed as:
- The `api_key` query parameter
- The `X-API-KEY` header
- The `Authorization` header (`Bearer YOUR_API_KEY`)

Your API key identifies your account, so keep it secret. You can retrieve, generate, or delete API keys anytime on your dashboard.

A special test key, `test-api-key`, validates provided parameters but always returns the same dummy response. Available on the three main endpoints: Domain Search, Email Finder, and Email Verifier.

---

## Errors

Hunter's API uses conventional HTTP response codes to indicate success or failure. On error, the API returns an array of errors with details.

### HTTP Status Code Summary

| Code | Meaning | Description |
|---|---|---|
| 200 | OK | The request was successful. |
| 201 | Created | The request was successful and the resource was created. |
| 204 | No content | The request was successful and no additional content was sent. |
| 400 | Bad request | Missing required parameter or invalid supplied parameter. |
| 401 | Unauthorized | No valid API key was provided. |
| 403 | Forbidden | You have reached the rate limit. |
| 404 | Not found | The requested resource does not exist. |
| 422 | Unprocessable entity | Request valid but resource creation failed; check errors. |
| 429 | Too many requests | You've reached your usage limit; upgrade your plan. |
| 451 | Unavailable for legal reasons | Cannot process personal data linked to this person. |
| 5XX | Server errors | Something went wrong on Hunter's end. |

**Error example**
```json
{
  "errors": [
    {
      "id": "wrong_params",
      "code": 400,
      "details": "You are missing the domain parameter"
    }
  ]
}
```

---

## Calls

### Discover

Returns companies matching a set of criteria. **This call is free.**

Each response returns a maximum of 100 companies. Premium users can use `offset` and `limit` to paginate. You can use other endpoints (Domain Search, Company Enrichment) to get more information about returned results.

You can either manually specify filter parameters, or use natural language via `query` and let an AI assistant select filters for you.

**Requirement:** provide either `query` (natural language) or at least one filter parameter.

| Parameter | Description |
|---|---|
| `query` (required unless a filter is set) | Natural language search query, e.g. "Companies in Europe in the Tech Industry." |
| `organization` | List of `domain` and/or `name` values to select companies by. |
| `similar_to` (Premium) | Domain or company name to find similar companies for. Domain takes precedence if both given. |
| `headquarters_location` | `include`/`exclude` lists of locations (continent, business_region, country, state, city). `continent`: Europe, Asia, North America, Africa, Antarctica, South America, Oceania. `business_region`: AMER, EMEA, APAC, LATAM. `country`: ISO 3166-1 alpha-2. `state`: valid US state code (only with country=US). `city`: requires a country too. |
| `industry` | `include`/`exclude` lists of industries (see industries.json). |
| `headcount` | Company sizes: 1-10, 11-50, 51-200, 201-500, 501-1000, 1001-5000, 5001-10000, 10001+ |
| `company_type` | `include`/`exclude`: educational, educational institution, government agency, non profit, partnership, privately held, public company, self employed, self owned, sole proprietorship |
| `year_founded` (Premium) | Years founded — list of years and/or `from`/`to` range. |
| `keywords` | `include`/`exclude` keyword lists; `match` field is `any` or `all` (default `all`). |
| `technology` (Premium) | `include`/`exclude` technology lists (see technologies.json); `match` is `any` or `all` (default `all`). |
| `funding` (Premium) | `series` (pre_seed, seed, pre_series_a, series_a, pre_series_b, series_b, pre_series_c, series_c+, other), `amount` range (`from`/`to`), `date` range (`from`/`to`). |
| `limit` (Premium) | Companies per page. Default/max 100. |
| `offset` (Premium) | Companies to skip. Default 0, max 10,000. |
| `api_key` (required) | Your secret API key. |

Each response returns up to 100 companies max. Use `offset` to paginate, up to a max offset of 10,000 (Premium only).

`filters` in the `meta` section shows the filters actually applied. When paginating AI-assistant (`query`) results, use these returned `filters` on subsequent calls instead of natural language, to keep filters consistent.

**Rate limit:** 5 requests/second, 50 requests/minute.

#### Discover Errors

| Code | ID | Description |
|---|---|---|
| 400 | `invalid_company_type` | Supplied `company_type` value invalid. |
| 400 | `invalid_funding` | Supplied `funding` input invalid. |
| 400 | `invalid_funding_series` | Supplied `funding[series]` value invalid. |
| 400 | `invalid_funding_date_from` | Supplied `funding[date][from]` invalid. |
| 400 | `invalid_funding_date_to` | Supplied `funding[date][to]` invalid. |
| 400 | `invalid_funding_date_range` | `funding[date][from]` must be before `funding[date][to]`. |
| 400 | `invalid_funding_amount_from` | Supplied `funding[amount][from]` invalid. |
| 400 | `invalid_funding_amount_to` | Supplied `funding[amount][to]` invalid. |
| 400 | `invalid_funding_amount_range` | `funding[amount][to]` must be greater than `funding[amount][from]`. |
| 400 | `invalid_headcount` | Supplied `headcount` value invalid. |
| 400 | `invalid_headquarters_location` | Supplied `headquarters_location` input invalid. |
| 400 | `invalid_headquarters_location_include_combination` | Invalid combination in `headquarters_location[include]`. |
| 400 | `invalid_headquarters_location_include_business_region` | Invalid `headquarters_location[include][business_region]`. |
| 400 | `invalid_headquarters_location_include_continent` | Invalid `headquarters_location[include][continent]`. |
| 400 | `invalid_headquarters_location_include_country` | Invalid `headquarters_location[include][country]`. |
| 400 | `invalid_headquarters_location_include_state` | `state` given while country isn't US or isn't a valid US state code. |
| 400 | `invalid_headquarters_location_exclude_combination` | Invalid combination in `headquarters_location[exclude]`. |
| 400 | `invalid_headquarters_location_exclude_business_region` | Invalid `headquarters_location[exclude][business_region]`. |
| 400 | `invalid_headquarters_location_exclude_continent` | Invalid `headquarters_location[exclude][continent]`. |
| 400 | `invalid_headquarters_location_exclude_country` | Invalid `headquarters_location[exclude][country]`. |
| 400 | `invalid_headquarters_location_exclude_state` | `state` given while country isn't US or isn't a valid US state code. |
| 400 | `invalid_industry` | Supplied `industry` value invalid. |
| 400 | `invalid_keywords` | Supplied `keywords` input invalid. |
| 400 | `invalid_keywords_match` | `keywords[match]` must be `any` or `all`. |
| 400 | `invalid_technology` | Supplied `technology` value invalid. |
| 400 | `invalid_technology_match` | `technology[match]` must be `any` or `all`. |
| 400 | `invalid_year_founded_combination` | Invalid combination of fields in `year_founded`. |
| 400 | `invalid_year_founded_include` | Invalid value in `year_founded[include]`. |
| 400 | `invalid_year_founded_from` | Supplied `year_founded[from]` invalid. |
| 400 | `invalid_year_founded_to` | Supplied `year_founded[to]` invalid. |
| 400 | `pagination_error` | Invalid `limit`/`offset`, or changed default values on a Free plan. |
| 403 | `no_discover_access` | Plan doesn't include Discover access (Data Platform users). |

**Request example**
```
POST https://api.hunter.io/v2/discover?api_key=YOUR_KEY
```
```json
{
  "organization": {
    "domain": ["hunter.io"]
  }
}
```

**Response: 200 OK**
```json
{
  "data": [
    {
      "domain": "hunter.io",
      "organization": "Hunter",
      "emails_count": { "personal": 23, "generic": 5, "total": 28 }
    }
  ],
  "meta": {
    "results": 1,
    "limit": 100,
    "offset": 0,
    "params": { "organization": { "domain": ["hunter.io"] } },
    "filters": { "organization": { "domain": ["hunter.io"] } }
  }
}
```

**Request body — AI assistant**
```json
{ "query": "Companies in Europe that specialize in software development" }
```

**Request body — filters**
```json
{
  "headquarters_location": {
    "include": [{ "continent": "Europe" }, { "country": "US" }],
    "exclude": [{ "country": "BE" }]
  },
  "industry": { "exclude": ["Accommodation Services", "Staffing and Recruiting"] },
  "headcount": ["1-10", "11-50", "51-200"],
  "company_type": { "exclude": ["educational", "non profit", "government agency"] },
  "year_founded": { "from": 1980, "to": 2010 },
  "technology": { "match": "any", "include": ["php", "java"] }
}
```

---

### Discover People `Beta`

> This endpoint is in Beta — parameters and response format may still change.

Returns the same companies as Discover, from a people-extraction angle: each company includes personal and generic email counts, and `meta` adds aggregate email totals across all matching companies. Useful to estimate reach before running a Domain Search. **This call is free.**

Accepts the exact same parameters as Discover (`query`, `organization`, `headquarters_location`, `industry`, `headcount`, `company_type`, `year_founded`, `keywords`, `technology`, `funding`, `similar_to`), paginated the same way.

**Requirement:** provide either `query` or at least one filter parameter.

| Parameter | Description |
|---|---|
| `query` (required unless a filter is set) | Natural language search query. |
| `filters` | Every Discover filter is valid here too. |
| `limit` (Premium) | Default/max 100. |
| `offset` (Premium) | Default 0, max 10,000. |
| `api_key` (required) | Your secret API key. |

`meta.total_emails` returns the aggregate personal/generic/total email counts across every matching company, not just the current page. Errors match the Discover endpoint.

**Request example**
```
GET https://api.hunter.io/v2/discover/people?api_key=YOUR_KEY&industry[include][]=Financial Services
```

**Response: 200 OK**
```json
{
  "data": [
    {
      "domain": "stripe.com",
      "organization": "Stripe",
      "emails_count": { "personal": 32, "generic": 8, "total": 40 }
    }
  ],
  "meta": {
    "results": 1247,
    "total_emails": { "personal": 50000, "generic": 8000, "total": 58000 },
    "limit": 100,
    "offset": 0,
    "params": { "industry": { "include": ["Financial Services"] } },
    "filters": { "industry": { "include": ["Financial Services"] } }
  }
}
```

---

### Domain Search

Finds all email addresses corresponding to one website: give a domain and get email addresses found on the internet, with sources. Results are paginated via `limit`/`offset`.

**Requirement:** provide at least one of `domain` or `company`. If both provided, `domain` takes precedence.

| Parameter | Description |
|---|---|
| `domain` (required unless `company`) | e.g. "stripe.com" |
| `company` (required unless `domain`) | e.g. "stripe". Domain gives better results. |
| `limit` | Max email addresses to return. Default 10. |
| `offset` | Email addresses to skip. Default 0. |
| `type` | `personal` or `generic` only. |
| `seniority` | `junior`, `senior`, `executive` (comma-delimited for several). |
| `department` | `executive`, `it`, `finance`, `management`, `sales`, `legal`, `support`, `hr`, `marketing`, `communication`, `education`, `design`, `health`, `operations` (comma-delimited). |
| `required_field` | `full_name`, `position`, `phone_number` (comma-delimited). |
| `verification_status` | `valid`, `accept_all`, `unknown` (comma-delimited). |
| `location` | `include`/`exclude` location lists (continent, business_region, country, state, city — same rules as Discover). Requires a **POST** request. |
| `job_titles` | Comma-delimited job title(s). Matches common executive title equivalents (e.g. CTO ≈ Chief Technology Officer) and word forms (e.g. "engineer" matches "Engineering"). Use `seniority`/`department`/`decision_maker` for whole tiers instead. |
| `api_key` (required) | Your secret API key. |

Returns 10 emails by default, up to 100 via `limit`. Paginate with `limit`+`offset` for more. A new query counts only when it returns at least one result.

Sources per email are limited to 20. `extracted_on` is the first-found date; `last_seen_on` is the last-found date.

`type` is `"personal"` (a person) or `"generic"` (role-based, e.g. contact@hunter.io). `confidence` estimates the probability the email is correct.

**Rate limit:** 15 requests/second, 500 requests/minute.

#### Domain Search Errors

| Code | ID | Description |
|---|---|---|
| 400 | `wrong_params` | `domain` or `company` missing. |
| 400 | `invalid_type` | Supplied `type` invalid. |
| 400 | `invalid_seniority` | Supplied `seniority` invalid. |
| 400 | `invalid_department` | Supplied `department` invalid. |
| 400 | `pagination_error` | Invalid `limit`/`offset`, or `limit + offset > 10` on Free plan. |

**Request example**
```
GET https://api.hunter.io/v2/domain-search?domain=intercom.com&api_key=YOUR_KEY
```

**Response: 200 OK**
```json
{
  "data": {
    "domain": "intercom.com",
    "disposable": false,
    "webmail": false,
    "accept_all": true,
    "pattern": "{first}",
    "organization": "Intercom",
    "linked_domains": [],
    "emails": [
      {
        "value": "ciaran@intercom.com",
        "type": "personal",
        "confidence": 92,
        "sources": [
          { "domain": "github.com", "uri": "http://github.com/ciaranlee", "extracted_on": "2015-07-29", "last_seen_on": "2017-07-01", "still_on_page": true }
        ],
        "first_name": "Ciaran",
        "last_name": "Lee",
        "position": "Support Engineer",
        "position_raw": "Support Engineer",
        "seniority": "senior",
        "department": "it",
        "linkedin": null,
        "twitter": "ciaran_lee",
        "phone_number": null,
        "verification": { "date": "2019-12-06", "status": "valid" }
      }
    ]
  },
  "meta": {
    "results": 35,
    "limit": 10,
    "offset": 0,
    "params": { "domain": "intercom.com", "company": null, "type": null, "seniority": null, "department": null }
  }
}
```

---

### Domain Finder `Beta`

> This endpoint is in Beta.

Returns the most likely domain name(s) for a given company name — the canonical way to resolve a company name into a domain before calling Domain Search or Email Finder.

**Pricing:** free — no credits consumed, does not decrement your monthly search call limit. Still blocked once the monthly search quota is exhausted.

Each result includes `company_name` in addition to `domain`.

| Parameter | Description |
|---|---|
| `company` (required) | Company name to resolve, min 3 characters, e.g. "stripe" or "Y Combinator". |
| `api_key` (required) | Your secret API key. |
| `limit` | Max suggestions, 1–10. Default 5. |
| `perfect_match` | `true` returns only very-high-similarity suggestions. Default `false`. |

#### Errors

| Code | ID | Description |
|---|---|---|
| 400 | `wrong_params` | `company` missing/too short, `limit` out of range, or `perfect_match` not boolean. |

**Request example**
```
GET https://api.hunter.io/v2/domain-finder?company=stripe&api_key=YOUR_KEY
```

**Response: 200 OK**
```json
{
  "data": [
    { "domain": "stripe.com", "company_name": "Stripe", "logo": "https://logos.hunter.io/stripe.com", "email_count": 281 }
  ],
  "meta": {
    "results": 1,
    "params": { "company": "stripe", "limit": 5, "perfect_match": false }
  }
}
```

---

### Email Finder

Finds the most likely email address from a domain name, first name, and last name. An Email Finder request typically triggers an email verification for accuracy; use `max_duration` to allow more time for it.

**Requirement:** provide at least one of `domain`, `company`, or `linkedin_handle`, plus a name (`first_name` + `last_name`, or `full_name`) unless `linkedin_handle` is provided.

| Parameter | Description |
|---|---|
| `domain` (required unless `company`/`linkedin_handle`) | Domain name of the company. |
| `company` (required unless `domain`/`linkedin_handle`) | Company name; domain gives better results. |
| `linkedin_handle` (required unless `domain`/`company`) | LinkedIn profile handle. |
| `first_name` (required unless `full_name`/`linkedin_handle`) | Person's first name. |
| `last_name` (required unless `full_name`/`linkedin_handle`) | Person's last name. |
| `full_name` (required unless `first_name`+`last_name`/`linkedin_handle`) | Person's full name; first+last name gives better results. |
| `max_duration` | Request duration in seconds, 3–20. Default 10. |
| `api_key` (required) | Your secret API key. |

A verification runs on each found email; status shown in `verification`: `valid`, `accept_all`, or `unknown`. For accept-all emails, `score` estimates validity probability. Public source URLs (up to 20) appear in `sources`.

**Rate limit:** 15 requests/second, 500 requests/minute.

#### Email Finder Errors

| Code | ID | Description |
|---|---|---|
| 400 | `wrong_params` | A required parameter is missing. |
| 400 | `invalid_first_name` | Supplied `first_name` invalid. |
| 400 | `invalid_last_name` | Supplied `last_name` invalid. |
| 400 | `invalid_full_name` | Supplied `full_name` invalid. |
| 400 | `invalid_domain` | Domain invalid, no MX record, or owner requested processing stop. |
| 400 | `invalid_max_duration` | Supplied `max_duration` invalid. |
| 451 | `claimed_email` | Owner requested we stop processing this email's data — do not process yourself. |

**Request example**
```
GET https://api.hunter.io/v2/email-finder?domain=reddit.com&first_name=Alexis&last_name=Ohanian&api_key=YOUR_KEY
```

**Response: 200 OK**
```json
{
  "data": {
    "first_name": "Alexis",
    "last_name": "Ohanian",
    "email": "alexis@reddit.com",
    "score": 97,
    "domain": "reddit.com",
    "accept_all": false,
    "position": "Cofounder",
    "twitter": null,
    "linkedin_url": null,
    "phone_number": null,
    "company": "Reddit",
    "sources": [
      { "domain": "redditblog.com", "uri": "http://redditblog.com/2008/10/22/widgets-get-an-upgrade-and-a-firefox-extension-that-will-rock-your-world", "extracted_on": "2018-10-19", "last_seen_on": "2021-05-18", "still_on_page": true }
    ],
    "verification": { "date": "2021-06-14", "status": "valid" }
  },
  "meta": {
    "params": { "first_name": "Alexis", "last_name": "Ohanian", "full_name": null, "domain": "reddit.com", "company": null, "max_duration": null }
  }
}
```

---

### Email Verifier

Verifies the deliverability of an email address. The request runs up to 20 seconds; if it can't respond in time, a 202 is returned and you can poll the same endpoint — requests in this case are counted only once.

| Parameter | Description |
|---|---|
| `email` (required) | Email address to verify. |
| `api_key` (required) | Your secret API key. |

**`status`** — one of:
- `valid` — the email address is valid.
- `invalid` — not valid.
- `accept_all` — valid, but the server accepts any address.
- `webmail` — from a provider like Gmail or Outlook.
- `disposable` — from a disposable email provider.
- `unknown` — verification failed.

**`result`** (deprecated, use `status`) — `deliverable`, `undeliverable`, or `risky`.

Other fields: `score` (deliverability score; webmail/disposable get an arbitrary 50), `regexp` (passes regex), `gibberish` (auto-generated address), `disposable`, `webmail`, `mx_records`, `smtp_server`, `smtp_check` (doesn't bounce), `accept_all` (SMTP accepts all — may cause false positives), `block` (SMTP server blocked the check), `sources` (up to 20, with `extracted_on`/`last_seen_on`).

**Rate limit:** 10 requests/second, 300 requests/minute.

#### Email Verifier Errors

| Code | ID | Description |
|---|---|---|
| 202 | — | Verification still in progress; retry as needed, counted once. |
| 222 | — | Failed due to unexpected remote SMTP response; retry later. |
| 400 | `wrong_params` | `email` parameter missing. |
| 400 | `invalid_email` | Supplied `email` invalid. |
| 451 | `claimed_email` | Owner requested we stop processing this email's data. |

**Request example**
```
GET https://api.hunter.io/v2/email-verifier?email=patrick@stripe.com&api_key=YOUR_KEY
```

**Response: 200 OK**
```json
{
  "data": {
    "status": "valid",
    "score": 100,
    "email": "patrick@stripe.com",
    "regexp": true,
    "gibberish": false,
    "disposable": false,
    "webmail": false,
    "mx_records": true,
    "smtp_server": true,
    "smtp_check": true,
    "accept_all": false,
    "block": false,
    "sources": [
      { "domain": "beta.paganresearch.io", "uri": "http://beta.paganresearch.io/details/stripe", "extracted_on": "2020-06-17", "last_seen_on": "2020-06-17", "still_on_page": true }
    ]
  },
  "meta": { "params": { "email": "patrick@stripe.com" } }
}
```

---

### Enrichment

Retrieves all information Hunter has about a person, a company, or both.

- Email Enrichment
- Company Enrichment
- Combined Enrichment

#### Email Enrichment

Returns all information associated with an email address or LinkedIn handle — name, location, social handles.

**Requirement:** provide `email` or `linkedin_handle`. If both, `linkedin_handle` takes precedence.

| Parameter | Description |
|---|---|
| `email` (required unless `linkedin_handle`) | Email address to look up. |
| `linkedin_handle` (required unless `email`) | LinkedIn profile handle. |
| `api_key` (required) | Your secret API key. |
| `clearbit_format` | Formats the response to match Clearbit's schema. |

Returns `200` with person attributes if found, `404` if not. **Rate limit:** 15 requests/second, 500 requests/minute.

**Request example**
```
GET https://api.hunter.io/v2/people/find?email=matt@hunter.io&api_key=YOUR_KEY
```

**Response: 200 OK**
```json
{
  "data": {
    "id": "b3ae14fb-6725-56d1-ac68-a76f8ce04dec",
    "name": { "fullName": "Matthew Tharp", "givenName": "Matthew", "familyName": "Tharp" },
    "email": "matt@hunter.io",
    "location": "Framingham, Massachusetts, United States",
    "timeZone": "America/New_York",
    "utcOffset": -5,
    "geo": { "city": "Framingham", "state": "Massachusetts", "stateCode": "MA", "country": "United States", "countryCode": "US", "lat": 42.27926, "lng": -71.41617 },
    "employment": { "domain": "hunter.io", "name": "Hunter", "title": "Chief Executive Officer", "role": "executive", "subRole": null, "seniority": "executive" },
    "twitter": { "handle": "matttharp" },
    "linkedin": { "handle": "matttharp" },
    "fuzzy": false,
    "emailProvider": "google.com",
    "indexedAt": "2025-08-30",
    "phone": null,
    "activeAt": "2025-09-03",
    "inactiveAt": null
  },
  "meta": { "email": "matt@hunter.io" }
}
```

#### Company Enrichment

Returns all information associated with a domain — industry, description, HQ location, etc.

| Parameter | Description |
|---|---|
| `domain` (required) | Domain name to look up. |
| `api_key` (required) | Your secret API key. |
| `clearbit_format` | Formats the response to match Clearbit's schema. |

Returns `200` with company attributes if found, `404` if not. **Rate limit:** 15 requests/second, 500 requests/minute.

**Request example**
```
GET https://api.hunter.io/v2/companies/find?domain=hunter.io&api_key=YOUR_KEY
```

**Response: 200 OK** (abbreviated)
```json
{
  "data": {
    "id": "95ca56a8-a019-5c41-881e-293d9ca4741a",
    "name": "Hunter",
    "legalName": "Hunter",
    "domain": "hunter.io",
    "category": { "sector": "Information Technology", "industryGroup": "Software & Services", "industry": "Internet Software & Services", "subIndustry": "Internet" },
    "tags": ["email marketing", "lead generation", "data enrichment", "sales intelligence", "business tools"],
    "description": "Hunter is an email marketing company that specializes in lead generation and data enrichment.",
    "foundedYear": 2015,
    "location": "Wilmington, Delaware, United States",
    "logo": "https://logos.hunter.io/hunter.io",
    "linkedin": { "handle": "company/hunterio" },
    "type": "private",
    "company_type": "privately held",
    "metrics": { "employees": "11-50", "trafficRank": "very_high" },
    "tech": ["cloudflare", "hsts", "http-3", "ruby", "stimulus"],
    "fundingRounds": []
  },
  "meta": { "domain": "hunter.io" }
}
```

#### Combined Enrichment

Returns all information associated with an email address and its domain (person + company together).

| Parameter | Description |
|---|---|
| `email` (required) | Email address to look up. |
| `api_key` (required) | Your secret API key. |
| `clearbit_format` | Formats the response to match Clearbit's schema. |

Returns `200` with person + company attributes if found, `404` if not. **Rate limit:** 15 requests/second, 500 requests/minute.

**Request example**
```
GET https://api.hunter.io/v2/combined/find?email=matt@hunter.io&api_key=YOUR_KEY
```

**Response: 200 OK** returns `data.person` and `data.company`, each shaped like the individual Email Enrichment and Company Enrichment responses above.

---

### Email Count

Returns how many email addresses Hunter has for a domain or company.

**Requirement:** provide `domain` or `company`. `domain` takes precedence if both given.

| Parameter | Description |
|---|---|
| `domain` (required unless `company`) | Domain to count emails for. |
| `company` (required unless `domain`) | Company name, min 3 characters; domain gives better results. |
| `api_key` (required) | Your secret API key. |
| `type` | `personal` or `generic` only. |

**Rate limit:** 15 requests/second.

#### Errors

| Code | ID | Description |
|---|---|---|
| 400 | `wrong_params` | `domain` or `company` missing. |
| 400 | `invalid_type` | Supplied `type` invalid. |

**Request example**
```
GET https://api.hunter.io/v2/email-count?domain=stripe.com&api_key=YOUR_KEY
```

**Response: 200 OK**
```json
{
  "data": {
    "total": 81,
    "personal_emails": 65,
    "generic_emails": 16,
    "department": { "executive": 10, "it": 0, "finance": 8, "management": 0, "sales": 0, "legal": 0, "support": 6, "hr": 0, "marketing": 0, "communication": 2, "education": 0, "design": 0, "health": 0, "operations": 0 },
    "seniority": { "junior": 13, "senior": 5, "executive": 2 }
  },
  "meta": { "params": { "domain": "stripe.com", "company": null, "type": null } }
}
```

---

### Account Information

Get information about your Hunter account. **This call is free.**

| Parameter | Description |
|---|---|
| `api_key` (required) | Your secret API key. |

`calls` (deprecated) sums search and verification requests; use `requests` for a detailed per-type breakdown.

**Request example**
```
GET https://api.hunter.io/v2/account?api_key=YOUR_KEY
```

**Response: 200 OK**
```json
{
  "data": {
    "first_name": "Antoine",
    "last_name": "Finkelstein",
    "email": "antoine@hunter.io",
    "plan_name": "Growth",
    "plan_level": 2,
    "reset_date": "2026-07-11",
    "team_id": 1,
    "requests": {
      "credits": { "used": 550.0, "available": 10000.0 },
      "searches": { "used": 500, "available": 10000 },
      "verifications": { "used": 100, "available": 20000 }
    },
    "calls": { "used": 18526, "available": 20000 }
  }
}
```

---

### Team members `Beta`

Lists team members with user ID, name, and email — used to resolve IDs accepted by the `user_id[]` filter on the leads endpoint (filters leads by creator). **This call is free.**

| Parameter | Description |
|---|---|
| `limit` | 1–100. Default 20. |
| `offset` | Members to skip. |
| `api_key` (required) | Your secret API key. |

**Request example**
```
GET https://api.hunter.io/v2/team/members?api_key=YOUR_KEY
```

**Response: 200 OK**
```json
{
  "data": [
    { "id": 1, "name": "Jane Doe", "email": "jane@example.com" },
    { "id": 2, "name": "John Smith", "email": "john@example.com" }
  ],
  "meta": { "total": 2, "limit": 20, "offset": 0 }
}
```

---

### Usage `Beta`

Returns your team's current usage and remaining requests for the billing period. Useful to check quota before starting a workflow. **Free — does not consume credits.**

| Parameter | Description |
|---|---|
| `api_key` (required) | Your secret API key. |
| `show_overage_requests` | Truthy value includes each type's `over_quota` count (premium plans with overage enabled only). |

`reset_date` is when the period resets. `requests.credits` appears only on a unified credits bucket (includes `remaining`); otherwise per-type `searches`/`verifications` blocks are exposed.

**Request example**
```
GET https://api.hunter.io/v2/usage?api_key=YOUR_KEY
```

**Response: 200 OK**
```json
{
  "data": {
    "reset_date": "2026-07-11",
    "requests": {
      "searches": { "used": 500, "available": 10000 },
      "verifications": { "used": 100, "available": 20000 }
    }
  }
}
```

---

### Company list favorites `Beta`

Mark/unmark a company list as a favorite (pins it in the web app sidebar). Idempotent.

| Parameter | Description |
|---|---|
| `id` (required) | Numeric identifier of the company list. |
| `api_key` (required) | Your secret API key. |

404 if the list doesn't exist or belongs to another team; 401 if the key is missing/invalid.

**Favorite:** `POST https://api.hunter.io/v2/company-lists/42/favorite?api_key=YOUR_KEY` → `201 Created`, `{ "data": { "id": 42, "favorited": true } }`

**Unfavorite:** `DELETE https://api.hunter.io/v2/company-lists/42/favorite?api_key=YOUR_KEY` → `200 OK`, `{ "data": { "id": 42, "favorited": false } }`

---

### Leads list favorites `Beta`

Mark/unmark a leads list as a favorite. Idempotent.

| Parameter | Description |
|---|---|
| `id` (required) | Numeric identifier of the leads list. |
| `api_key` (required) | Your secret API key. |

404 if the list doesn't exist or belongs to another team; 401 if the key is missing/invalid.

**Favorite:** `POST https://api.hunter.io/v2/leads_lists/42/favorite?api_key=YOUR_KEY` → `201 Created`, `{ "data": { "id": 42, "favorited": true } }`

**Unfavorite:** `DELETE https://api.hunter.io/v2/leads_lists/42/favorite?api_key=YOUR_KEY` → `200 OK`, `{ "data": { "id": 42, "favorited": false } }`

---

### Connected Apps `Beta`

Returns third-party apps connected to the team (CRMs, email accounts, automation tools, spreadsheets). Read-only — useful for agents to discover integrations before pushing/syncing data.

| Parameter | Description |
|---|---|
| `api_key` (required) | Your secret API key. |
| `limit` | Page size. Default 20. |
| `offset` | Skip first N apps. Default 0. |

Each app exposes `provider` (slug, e.g. `hubspot`), `name`, `category` (`crm`, `emailing`, `automation`, `spreadsheet`, `connect_as`, `mcp_server`), and connection metadata. The show endpoint adds `attribute_mappings` (Hunter lead ↔ provider field mapping).

**Push leads to a CRM** — triggers an async sync (HubSpot, Salesforce, Pipedrive, Zoho, Zapier). Provide `lead_ids` or `leads_list_id` (which takes precedence). Responds `202 Accepted`; runs in background.

**List connected apps**
```
GET https://api.hunter.io/v2/connected-apps?api_key=YOUR_KEY
```
```json
{
  "data": [
    { "id": 42, "provider": "hubspot", "name": "HubSpot", "category": "crm", "provider_email": "ops@example.com", "connected_at": "2026-01-15T10:00:00Z", "updated_at": "2026-05-07T13:00:00Z" }
  ],
  "meta": { "total": 1, "limit": 20, "offset": 0 }
}
```

**Show one app**
```
GET https://api.hunter.io/v2/connected-apps/42?api_key=YOUR_KEY
```
Adds `attribute_mappings` to the fields above.

**Push leads to a CRM**
```
POST https://api.hunter.io/v2/connected-apps/42/push?api_key=YOUR_KEY
```
```json
{ "lead_ids": [101, 102, 103] }
```
→ `202 Accepted`
```json
{
  "data": { "connected_app_id": 42, "provider": "hubspot", "leads_count": 3, "status": "queued" },
  "meta": { "params": { "lead_ids": [101, 102, 103], "leads_list_id": null } }
}
```

---

### API keys `Beta`

Manage API keys programmatically. Up to 100 keys allowed; you must keep at least one. All calls are free.

Methods: List, Create, Rename, Delete.

Listed keys are masked (last 4 characters only). Full tokens are shown once, only in the create response.

#### List all your API keys

| Parameter | Description |
|---|---|
| `limit` | 1–100. Default 20. |
| `offset` | Keys to skip. |
| `api_key` (required) | Your secret API key. |

```
GET https://api.hunter.io/v2/api-keys?api_key=YOUR_KEY
```
```json
{
  "status": "success",
  "data": [
    { "id": 2, "name": "CI server", "token": "********cdef", "created_at": "2024-01-02T09:00:00Z" },
    { "id": 1, "name": null, "token": "********89ab", "created_at": "2024-01-01T09:00:00Z" }
  ],
  "meta": { "total": 2, "limit": 20, "offset": 0 }
}
```

#### Create an API key

| Parameter | Description |
|---|---|
| `name` | Optional, max 255 chars, unique among your keys. |
| `api_key` (required) | Your secret API key. |

```
POST https://api.hunter.io/v2/api-keys?api_key=YOUR_KEY
```
```json
{ "name": "CI server" }
```
→ `201 Created`
```json
{ "status": "success", "data": { "id": 3, "name": "CI server", "token": "0123456789abcdef0123456789abcdef01234567", "created_at": "2024-01-03T09:00:00Z" } }
```

#### Rename an API key

| Parameter | Description |
|---|---|
| `id` (required) | Identifier of the key. |
| `name` (required) | New name, max 255 chars, unique. |
| `api_key` (required) | Your secret API key. |

```
PUT https://api.hunter.io/v2/api-keys/1?api_key=YOUR_KEY
```
```json
{ "name": "Production server" }
```
→ `200 OK` with the updated (masked) key.

#### Delete an API key

Cannot delete your last key.

| Parameter | Description |
|---|---|
| `id` (required) | Identifier of the key. |
| `api_key` (required) | Your secret API key. |

```
DELETE https://api.hunter.io/v2/api-keys/1?api_key=YOUR_KEY
```
→ `204 No Content`

---

### Webhooks `Beta`

Register a target URL Hunter calls when a supported event occurs. All calls are free.

Supported `event` values: `lead.created`, `message.sent`, `message.read`, `message.clicked`, `message.replied`, `export.completed`, `import.completed`, `sequence.paused`, `sequence.resumed`.

`lead.created` fires only via the Zapier integration once Hunter Leads is connected. The `message.*` events fire for any webhook.

#### List all your webhooks

| Parameter | Description |
|---|---|
| `limit` | 1–100. Default 20. |
| `offset` | Webhooks to skip. |
| `api_key` (required) | Your secret API key. |

```
GET https://api.hunter.io/v2/webhooks?api_key=YOUR_KEY
```
```json
{
  "status": "success",
  "data": [
    { "id": 2, "target_url": "https://example.com/hooks/replies", "event": "message.replied" },
    { "id": 1, "target_url": "https://example.com/hooks/leads", "event": "lead.created" }
  ],
  "meta": { "total": 2, "limit": 20, "offset": 0 }
}
```

#### Get a webhook

| Parameter | Description |
|---|---|
| `id` (required) | Webhook identifier. |
| `api_key` (required) | Your secret API key. |

```
GET https://api.hunter.io/v2/webhooks/1?api_key=YOUR_KEY
```

#### Create a webhook

| Parameter | Description |
|---|---|
| `target_url` (required) | Valid HTTP URL, 12–500 characters. |
| `event` (required) | See supported events above. |
| `filter_on` | Restrict to a single resource (global ID). Omit to fire for every matching event in the team. |
| `api_key` (required) | Your secret API key. |

```
POST https://api.hunter.io/v2/webhooks?api_key=YOUR_KEY
```
```json
{ "target_url": "https://example.com/hooks/messages", "event": "message.sent" }
```
→ `201 Created`

#### Update a webhook

| Parameter | Description |
|---|---|
| `id` (required) | Webhook identifier. |
| `target_url` | New URL. |
| `event` | New triggering event. |
| `api_key` (required) | Your secret API key. |

```
PUT https://api.hunter.io/v2/webhooks/1?api_key=YOUR_KEY
```

#### Delete a webhook

| Parameter | Description |
|---|---|
| `id` (required) | Webhook identifier (query param). |
| `api_key` (required) | Your secret API key. |

```
DELETE https://api.hunter.io/v2/webhooks?id=1&api_key=YOUR_KEY
```
→ `204 No Content`

---

### Email Accounts `Beta`

Returns email accounts the requesting user can act on for sending sequences. Read-only. Sequences cannot start without at least one email account.

On paid plans, returns every team account. On Free, only accounts owned by the requesting user.

| Parameter | Description |
|---|---|
| `api_key` (required) | Your secret API key. |
| `limit` | Default 20. |
| `offset` | Default 0. |

Each account exposes `email`, `first_name`/`last_name` (owner), `sending_status` (`active`, `paused`, `warming`), `daily_limit`, `provider` (e.g. `gmail`, `outlook`, `smtp_imap`).

`sending_status` values:
- `active` — connected and ready to send.
- `warming` — in warmup/ramp-up (takes precedence over connection state).
- `paused` — cannot send now (temporary backoff or hard disconnect).

`meta.total` reflects only accounts visible to the requesting user.

#### Retrieve a single account

`GET /v2/email-accounts/:id` — full config, plan-aware scoping (returns 404 if not visible). No credentials/secrets returned. Adds: `sender_name`, `default_account`, `signature`, `profile_picture_url`, `custom_tracking_domain`, `sending_schedule` (`days`, `start_time`, `end_time`, `time_zone`), `warmup` (`enabled`, `status`, `end_date`).

#### Account health report

`GET /v2/email-accounts/:id/health-report` — deliverability summary.
- `score` (0–100), `status` (`excellent` 90–100, `healthy` 80–89, `at-risk` 60–79, `critical` 0–59)
- `domain`, `domain_score`, `checked_at`
- `issues`: `disconnected`, `no_recent_sending_activity`, `no_inbox_protection`

Requires a paid plan and a completed domain deliverability check; otherwise score fields are `null` and `issues` stays populated.

#### Account usage

`GET /v2/email-accounts/:id/usage` — sending usage/remaining capacity today (bucketed by user's time zone).
- `sent_today`, `sent_last_7_days` (`date`, `count`, `capacity`), `daily_limit`, `scheduled`, `available` (`daily_limit - sent_today - scheduled`, floored at 0)

#### Sending capacities

`GET /v2/email-accounts/capacities` — today's capacity for every account the user can act on, in one call. Per account: `sent`, `daily_limit`, `scheduled`. Unpaginated; `meta.results` is the account count.

#### Warmup report

`GET /v2/email-accounts/:id/warmup-report` — most recent warmup status. `404` if the account never had a warmup mailbox.
- `state`: `pending_oauth`, `scheduled`, `warming_up`, `active`, `limited`, `error`, `aborted`
- `progress`: `percent`, `end_date`, `current_limit`, `target_limit`
- `metrics`: `emails_sent`, `interactions`, `in_spam` (`null` if provider unreachable)
- `chart_data`: per-day `date`, `sent`, `scheduled`
- `billing_cycle`: `monthly`, `yearly`, or `null`

**Examples**

```
GET https://api.hunter.io/v2/email-accounts?api_key=YOUR_KEY
```
```json
{
  "data": [
    { "id": 42, "email": "ops@example.com", "first_name": "Alex", "last_name": "Doe", "sending_status": "active", "daily_limit": 50, "provider": "gmail" }
  ],
  "meta": { "total": 1, "limit": 20, "offset": 0 }
}
```

```
GET https://api.hunter.io/v2/email-accounts/42?api_key=YOUR_KEY
```
```json
{
  "data": {
    "id": 42, "email": "ops@example.com", "first_name": "Alex", "last_name": "Doe",
    "sending_status": "active", "daily_limit": 50, "provider": "gmail",
    "sender_name": "Alex Doe", "default_account": true, "signature": "<p>Best, Alex</p>",
    "profile_picture_url": "https://www.gravatar.com/avatar/...",
    "custom_tracking_domain": "track.example.com",
    "sending_schedule": { "days": [0,1,2,3,4], "start_time": "09:00", "end_time": "16:59", "time_zone": "UTC" },
    "warmup": { "enabled": false, "status": null, "end_date": null }
  }
}
```

```
GET https://api.hunter.io/v2/email-accounts/42/health-report?api_key=YOUR_KEY
```
```json
{
  "data": {
    "score": 98, "status": "excellent", "domain": "example.com", "domain_score": 100,
    "checked_at": "2026-06-30T09:12:00Z",
    "issues": { "disconnected": false, "no_recent_sending_activity": false, "no_inbox_protection": true }
  }
}
```

```
GET https://api.hunter.io/v2/email-accounts/42/usage?api_key=YOUR_KEY
```
```json
{
  "data": {
    "sent_today": 12,
    "sent_last_7_days": [
      { "date": "2026-06-26", "count": 30, "capacity": 50 },
      { "date": "2026-07-02", "count": 12, "capacity": 50 }
    ],
    "daily_limit": 50, "available": 33, "scheduled": 5
  }
}
```

```
GET https://api.hunter.io/v2/email-accounts/capacities?api_key=YOUR_KEY
```
```json
{
  "data": [{ "id": 42, "email": "ops@example.com", "sent": 12, "daily_limit": 50, "scheduled": 5 }],
  "meta": { "results": 1 }
}
```

```
GET https://api.hunter.io/v2/email-accounts/42/warmup-report?api_key=YOUR_KEY
```
```json
{
  "data": {
    "state": "warming_up",
    "progress": { "percent": 40, "end_date": "2026-07-20", "current_limit": 12, "target_limit": 30 },
    "metrics": { "emails_sent": 118, "interactions": 41, "in_spam": 2 },
    "chart_data": [
      { "date": "2026-07-01", "sent": 10, "scheduled": 0 },
      { "date": "2026-07-02", "sent": 6, "scheduled": 6 }
    ],
    "billing_cycle": "monthly"
  }
}
```

---

### Sequences using an email account `Beta`

Returns sequences that send from a given email account. Read-only — useful before adjusting `daily_limit`, pausing, or disconnecting an account.

Account is scoped to those the requesting user can act on; requesting an out-of-team account returns 404.

| Parameter | Description |
|---|---|
| `api_key` (required) | Your secret API key. |
| `include_archived` | `true` to include archived sequences (excluded by default). |
| `limit` | Default 20. |
| `offset` | Default 0. |

Each sequence exposes `id`, `name`, `status` (`draft`, `planned`, `active`, `paused`, `archived`), `recipients_allocated`, `emails_scheduled`.

```
GET https://api.hunter.io/v2/email-accounts/:email_account_id/sequences?api_key=YOUR_KEY
```
```json
{
  "data": [
    { "id": 128, "name": "Q3 outreach", "status": "active", "recipients_allocated": 240, "emails_scheduled": 18 }
  ],
  "meta": { "total": 1, "limit": 20, "offset": 0 }
}
```

---

### Messages `Beta`

Returns messages sent from your team's sequences, with delivery/engagement state, lead, sequence, and sender. Read-only.

Outgoing messages: newest first (by id). Incoming replies: most recent first, by receipt time. Filters are combinable (AND); non-matching values return empty results. By default only outgoing messages are returned — pass `direction` to include incoming replies.

| Parameter | Description |
|---|---|
| `api_key` (required) | Your secret API key. |
| `status` | Comma-separated: `pending`, `error`, `sent`, `bounced`, `opened`, `clicked`, `replied`, `reply`, `unsubscribed`, `canceled`, `successful`. |
| `direction` | `outgoing` (default) or `incoming`. |
| `read` | `true`/`false`. |
| `replied` | `true`/`false`. |
| `bounced` | `true`/`false`. |
| `unsubscribed` | `true`/`false`. |
| `out_of_office` | `true`/`false` (incoming auto-responses). |
| `lead_id` | Restrict to a specific lead (by email match). |
| `sequence_id` (alias `campaign_id`) | Restrict to a specific sequence. |
| `email_account_id` | Restrict to a specific sending account. |
| `sent_after`, `sent_before` | Bound `sent_at` (ISO 8601; unparseable value ignored). |
| `received_after`, `received_before` | Bound receipt date of incoming replies (also filters `sent_at`). |
| `q` | Full-text search on subject/body. |
| `limit` | Default 20, max 100. |
| `offset` | Default 0. |

Each message exposes `id`, `status`, `message_id`, `thread_id`, `to`, `subject`, `body`, `manually_edited`, `opens`, `clicks`, `bounced`, `replied`, `last_activity_at`, `sent_at`, plus embedded `lead` (may be `null`), `campaign` (or `null` for one-off messages), and `owner`.

**Request example**
```
GET https://api.hunter.io/v2/messages?status=sent,opened&sequence_id=42&replied=true&api_key=YOUR_KEY
```

**Response: 200 OK**
```json
{
  "data": {
    "messages": [
      {
        "id": 1001, "status": "opened", "message_id": "<CAKf...@mail.gmail.com>", "thread_id": "1830abc...",
        "to": "marcus@example.com", "subject": "Quick question about Example", "body": "Hi Marcus, ...",
        "manually_edited": false, "opens": 2, "clicks": 0, "bounced": false, "replied": false,
        "last_activity_at": "2026-05-19T15:11:22Z", "sent_at": "2026-05-18T09:00:00Z",
        "lead": { "id": 501, "email": "marcus@example.com", "first_name": "Marcus", "last_name": "Doe" },
        "campaign": { "id": 42, "name": "Spring outreach", "recipients_count": 85, "started": true, "archived": false, "paused": false, "last_email_sent_at": "2026-05-20T08:00:00Z" },
        "owner": { "id": 7, "email": "alex@example.com" }
      }
    ]
  },
  "meta": { "limit": 20, "offset": 0 }
}
```

---

### Leads

Manage leads entirely via the RESTful API: list, retrieve, create, create-or-update, update, delete. All calls are free.

#### List all your leads

Filter values: `*` (any value), `~` (empty), or any string (contains match) — applies to `email`, `first_name`, `last_name`, `position`, `company`, `industry`, `website`, `country_code`, `company_size`, `source`, `twitter`, `linkedin_url`, `phone_number`.

| Parameter | Description |
|---|---|
| `leads_list_id` | Only leads in this list. |
| `email`, `first_name`, `last_name`, `position`, `company`, `industry`, `website`, `country_code`, `company_size`, `source`, `twitter`, `linkedin_url`, `phone_number` | Filter on each attribute (see filter value rules above). |
| `sync_status` | `pending`, `error`, or `success`. |
| `sending_status[]` | `clicked`, `opened`, `sent`, `pending`, `error`, `bounced`, `unsubscribed`, `replied`, or `~`. |
| `verification_status[]` | `accept_all`, `disposable`, `invalid`, `unknown`, `valid`, `webmail`, `pending`. |
| `last_activity_at` | `*` or `~`. |
| `last_contacted_at` | `*` or `~`. |
| `custom_attributes[]` | Key = custom attribute slug; value = `*`, `~`, or a string. |
| `query` | Matches `first_name`, `last_name`, or `email`. |
| `limit` | 1–1,000. Default 20. |
| `offset` | 0–100,000. |
| `api_key` (required) | Your secret API key. |

**Request example**
```
GET https://api.hunter.io/v2/leads?api_key=YOUR_KEY
```
```json
{
  "data": {
    "leads": [
      {
        "id": 1, "email": "hoon@stripe.com", "first_name": "Jeremy", "last_name": "Hoon",
        "company": "Stripe", "website": "stripe.com",
        "verification": { "date": "2021-01-01 12:00:00 UTC", "status": "deliverable" },
        "leads_list": { "id": 1, "name": "My leads list", "leads_count": 2 },
        "created_at": "2021-01-01 12:00:00 UTC"
      }
    ]
  },
  "meta": { "count": 2, "total": 2, "params": { "limit": 20, "offset": 0 } }
}
```

#### Get a lead

| Parameter | Description |
|---|---|
| `id` (required) | Lead identifier. |
| `api_key` (required) | Your secret API key. |

```
GET https://api.hunter.io/v2/leads/1?api_key=YOUR_KEY
```

#### Create a lead

| Parameter | Description |
|---|---|
| `email` (required) | Lead's email address. |
| `first_name`, `last_name` | Name. |
| `position` | Job title. |
| `company` | Employer name. |
| `company_industry` | Recommended values: Animal, Art & Entertainment, Automotive, Beauty & Fitness, Books & Literature, Education & Career, Finance, Food & Drink, Game, Health, Hobby & Leisure, Home & Garden, Industry, Internet & Telecom, Law & Government, Manufacturing, News, Real Estate, Science, Retail, Sport, Technology, Travel. |
| `company_size` | Employer size. |
| `confidence_score` | 0–100. |
| `website` | Employer domain. |
| `country_code` | ISO 3166-1 alpha-2. |
| `linkedin_url`, `phone_number`, `twitter` | Contact details. |
| `notes` | Free-text notes. |
| `source` | Where the lead was found. |
| `leads_list_id` | List to save into; defaults to the last list created. |
| `leads_list_ids` | Multiple lists; defaults to the last list created. |
| `leads_list_name` | Name of a list — reuses an existing list (case-insensitive) or creates one. Cannot combine with `leads_list_id`/`leads_list_ids` (422 if combined). |
| `custom_attributes[slug]` | Value for the custom attribute identified by slug. |
| `api_key` (required) | Your secret API key. |

```
POST https://api.hunter.io/v2/leads?api_key=YOUR_KEY
```
```json
{
  "email": "alexis@reddit.com", "first_name": "Alexis", "last_name": "Ohanian",
  "position": "Cofounder", "company": "Reddit", "company_industry": "Internet & Telecom",
  "company_size": "201-500 employees", "confidence_score": 97, "website": "reddit.com",
  "custom_attributes": { "customer_id": "cus-1234abcd" }
}
```
→ `201 Created`

#### Create or update a lead

Creates the lead if not found by email, otherwise updates it. Same params as create. Custom attributes are partially merged (omitted fields unchanged); send an empty string to clear one.

```
PUT https://api.hunter.io/v2/leads?api_key=YOUR_KEY
```
```json
{ "email": "alexis@reddit.com", "first_name": "Alexis", "last_name": "Ohanian" }
```
→ `201 Created` (new) or `200 OK` (updated)

#### Update a lead

Same params as create, applied to an existing lead. Custom attributes partially merged; empty string clears a field.

```
PUT https://api.hunter.io/v2/leads/1?api_key=YOUR_KEY
```
```json
{ "company": "Facebook" }
```
→ `204 No Content`

#### Delete a lead

| Parameter | Description |
|---|---|
| `id` (required) | Lead identifier. |
| `api_key` (required) | Your secret API key. |

```
DELETE https://api.hunter.io/v2/leads/1?api_key=YOUR_KEY
```
→ `204 No Content`

---

### Lead tags `Beta`

Manage tags used to organize leads and attach/detach them from individual leads. Tags are shared team-wide. All calls are free.

Methods: List, Get, Create, Update, Delete, Attach to lead, Detach from lead.

#### List all your lead tags

| Parameter | Description |
|---|---|
| `limit` | 1–100. Default 25. |
| `offset` | Tags to skip. |
| `api_key` (required) | Your secret API key. |

```
GET https://api.hunter.io/v2/tags?api_key=YOUR_KEY
```
```json
{
  "data": { "tags": [
    { "id": 1, "name": "Prospect", "color": "3489F9", "created_at": "2026-05-26 12:00:00 UTC" },
    { "id": 2, "name": "Important", "color": "10B981", "created_at": "2026-05-26 12:00:01 UTC" }
  ] },
  "meta": { "total": 2, "params": { "limit": 25, "offset": 0 } }
}
```

#### Get a lead tag

```
GET https://api.hunter.io/v2/tags/1?api_key=YOUR_KEY
```

#### Create a lead tag

| Parameter | Description |
|---|---|
| `name` (required) | Must be unique within the team. |
| `color` | Hex color (no `#`); must be a supported palette value. Random if omitted. |
| `api_key` (required) | Your secret API key. |

```
POST https://api.hunter.io/v2/tags?api_key=YOUR_KEY
```
```json
{ "name": "Prospect", "color": "3489F9" }
```
→ `201 Created`

#### Update a lead tag

| Parameter | Description |
|---|---|
| `id` (required) | Tag identifier. |
| `name` | New name. |
| `color` | New hex color. |
| `api_key` (required) | Your secret API key. |

```
PUT https://api.hunter.io/v2/tags/1?api_key=YOUR_KEY
```
```json
{ "name": "Hot prospect", "color": "10B981" }
```
→ `200 OK`

#### Delete a lead tag

Also removes the tag from any lead it was attached to.

```
DELETE https://api.hunter.io/v2/tags/1?api_key=YOUR_KEY
```
→ `204 No Content`

#### Attach a tag to a lead

Idempotent — attaching an already-present tag is a no-op, still returns current tags.

| Parameter | Description |
|---|---|
| `id` (required) | Lead identifier. |
| `tag_id` (required) | Tag identifier (must belong to your team). |
| `api_key` (required) | Your secret API key. |

```
POST https://api.hunter.io/v2/leads/1/tags?api_key=YOUR_KEY
```
→ `201 Created`
```json
{
  "data": { "tags": [{ "id": 42, "name": "Priority", "color": "EF4444" }] },
  "meta": { "params": { "id": "1", "tag_id": "42" } }
}
```

#### Detach a tag from a lead

Tag itself stays available on the team. No-op (still successful) if the lead didn't have the tag.

```
DELETE https://api.hunter.io/v2/leads/1/tags/42?api_key=YOUR_KEY
```
→ `204 No Content`

---

### Lead enrichment `Beta`

Enriches a saved lead in place with Hunter's data for its email address, writing back to the record (unlike the People API, which only looks the data up without saving). All calls are free.

#### Enrich a lead

Fills empty enrichable fields: first name, last name, position, phone number, LinkedIn URL, Twitter, company, industry, website, country. Manually-set fields are never overwritten. If nothing can be enriched (e.g. Free plan, webmail/disposable address, or no data), the lead is returned unchanged.

| Parameter | Description |
|---|---|
| `id` (required) | Lead identifier. |
| `api_key` (required) | Your secret API key. |

```
POST https://api.hunter.io/v2/leads/1/enrich?api_key=YOUR_KEY
```
```json
{
  "data": {
    "id": 1, "email": "alexis@reddit.com", "first_name": "Alexis", "last_name": "Ohanian",
    "position": "Cofounder", "company": "Reddit", "company_industry": "Internet",
    "confidence_score": 97, "website": "reddit.com", "country_code": "US",
    "linkedin_url": "https://www.linkedin.com/in/alexisohanian", "phone_number": null, "twitter": "alexisohanian",
    "leads_list": { "id": 1, "name": "My leads", "leads_count": 1 },
    "created_at": "2026-05-26 12:00:00 UTC"
  }
}
```

---

### Custom Attributes

Manage custom attributes entirely via the API: list, retrieve, create, update, delete. All calls are free.

#### List all your custom attributes

| Parameter | Description |
|---|---|
| `api_key` (required) | Your secret API key. |

```
GET https://api.hunter.io/v2/leads_custom_attributes?api_key=YOUR_KEY
```
```json
{
  "data": { "leads_custom_attributes": [
    { "id": 2, "label": "Customer ID", "slug": "customer_id" },
    { "id": 1, "label": "Campaign ID", "slug": "campaign_id" }
  ] },
  "meta": { "total": 2 }
}
```

#### Get a custom attribute

```
GET https://api.hunter.io/v2/leads_custom_attributes/1?api_key=YOUR_KEY
```

#### Create a custom attribute

| Parameter | Description |
|---|---|
| `label` (required) | Name; must be unique. |
| `api_key` (required) | Your secret API key. |

```
POST https://api.hunter.io/v2/leads_custom_attributes?api_key=YOUR_KEY
```
```json
{ "label": "Campaign ID" }
```
→ `201 Created`

#### Update a custom attribute

```
PUT https://api.hunter.io/v2/leads_custom_attributes/1?api_key=YOUR_KEY
```
```json
{ "label": "Outreach Campaign ID" }
```
→ `204 No Content`

#### Delete a custom attribute

```
DELETE https://api.hunter.io/v2/leads_custom_attributes/1?api_key=YOUR_KEY
```
→ `204 No Content`

---

### Leads Lists

Manage leads lists entirely via the API: list, retrieve, create, update, delete. All calls are free.

#### List all your leads lists

| Parameter | Description |
|---|---|
| `limit` | 1–100. Default 20. |
| `offset` | Lists to skip. |
| `api_key` (required) | Your secret API key. |

```
GET https://api.hunter.io/v2/leads_lists?api_key=YOUR_KEY
```
```json
{
  "data": { "leads_lists": [
    { "id": 1, "name": "My first list", "leads_count": 10, "created_at": "2021-01-01 12:00:00 UTC" },
    { "id": 2, "name": "My second list", "leads_count": 1, "created_at": "2021-01-01 12:00:01 UTC" }
  ] },
  "meta": { "total": 2, "params": { "limit": 20, "offset": 0 } }
}
```

#### Get a leads list

| Parameter | Description |
|---|---|
| `id` (required) | List identifier. |
| `limit` | 1–100. Default 20. |
| `offset` | Leads to skip. |
| `api_key` (required) | Your secret API key. |

```
GET https://api.hunter.io/v2/leads_lists/1?api_key=YOUR_KEY
```

#### Create a leads list

| Parameter | Description |
|---|---|
| `name` (required) | List name. |
| `leads_list_folder_id` | Folder to place the list in (optional). |
| `api_key` (required) | Your secret API key. |

```
POST https://api.hunter.io/v2/leads_lists?api_key=YOUR_KEY
```
```json
{ "name": "My new leads list" }
```
→ `201 Created`

#### Update a leads list

| Parameter | Description |
|---|---|
| `name` (required) | New name. |
| `leads_list_folder_id` | New folder, or `null` to remove from folder. |
| `api_key` (required) | Your secret API key. |

```
PUT https://api.hunter.io/v2/leads_lists/1?api_key=YOUR_KEY
```
```json
{ "name": "New leads list name" }
```
→ `204 No Content`

#### Delete a leads list

```
DELETE https://api.hunter.io/v2/leads_lists/1?api_key=YOUR_KEY
```
→ `204 No Content`

---

### Leads list folders `Beta`

Organize leads lists into folders (name + color). Surfaced in the web app sidebar, referenced by lists via `leads_list_folder_id`. All calls are free.

#### List all your folders

| Parameter | Description |
|---|---|
| `limit` | Default 20, max 100. |
| `offset` | Default 0. |
| `api_key` (required) | Your secret API key. |

Each folder embeds `leads_lists_count`. Sorted by `id` descending.

```
GET https://api.hunter.io/v2/leads_lists/folders?api_key=YOUR_KEY
```

#### Retrieve one of your folders

```
GET https://api.hunter.io/v2/leads_lists/folders/1?api_key=YOUR_KEY
```

#### Create a new folder

| Parameter | Description |
|---|---|
| `name` (required) | Folder name. |
| `color` (required) | One of: `374151`, `3489F9`, `10B981`, `F5BA0B`, `EF4444`, `7C3AED`, `F97316`, `E5E7EB`, `B4D9F7`, `BAE6B0`, `FDE68A`, `FBD0D0`, `D4C4F8`, `FFD79B`. |
| `api_key` (required) | Your secret API key. |

```
POST https://api.hunter.io/v2/leads_lists/folders?api_key=YOUR_KEY
```
→ `201 Created`

#### Update an existing folder

| Parameter | Description |
|---|---|
| `id` (required) | Folder identifier. |
| `name` | New name. |
| `color` | New color (see palette above). |
| `api_key` (required) | Your secret API key. |

```
PUT https://api.hunter.io/v2/leads_lists/folders/1?api_key=YOUR_KEY
```
→ `204 No Content`

#### Delete a folder

Lists in the folder are not deleted; their `leads_list_folder_id` is reset to `null`.

```
DELETE https://api.hunter.io/v2/leads_lists/folders/1?api_key=YOUR_KEY
```
→ `204 No Content`

---

### Bulk lead operations `Beta`

Move or delete many leads at once. All calls are free.

#### Move leads between lists

Both lists must be static and belong to your team. Processed in background.

| Parameter | Description |
|---|---|
| `leads_list_id` (required) | Source list — leads removed from here. |
| `target_leads_list_id` (required) | Destination list — leads added here. |
| `lead_ids` | Optional; restricts the move to these leads within the source list. Omit to move all. |
| `api_key` (required) | Your secret API key. |

```
POST https://api.hunter.io/v2/leads/bulk/move?api_key=YOUR_KEY
```
```json
{ "leads_list_id": 10, "target_leads_list_id": 22, "lead_ids": [1, 2, 3] }
```
→ `202 Accepted`

#### Delete leads in bulk

Selections of ≤10 are deleted immediately; larger selections are processed in the background.

| Parameter | Description |
|---|---|
| `lead_ids` | Required unless `leads_list_id`. |
| `leads_list_id` | Deletes every lead in the list. Required unless `lead_ids`. |
| `api_key` (required) | Your secret API key. |

```
POST https://api.hunter.io/v2/leads/bulk/delete?api_key=YOUR_KEY
```
```json
{ "lead_ids": [1, 2, 3] }
```
→ `200 OK` (immediate) or `202 Accepted` (large selection)

---

### Companies `Beta`

Track companies in Hunter Leads, identified by domain, scoped to your team. All calls are free.

Methods: List, Get, Create, Delete.

#### List all your companies

Filter value conventions: multi-value filters use `|` (pipe) to separate values (e.g. `company_type=privately held|public company`); ranges use `from:to` (e.g. `leads_count=10:100`); `~` matches empty, `*` matches any present value. Funding and founded-year filters require the matching plan entitlement (ignored, not erroring, if not entitled).

| Parameter | Description |
|---|---|
| `limit` | 1–100. Default 20. |
| `offset` | 0–9,999 (served from Elasticsearch). |
| `sort` | `company_name`, `website`, `company_size`, `company_type`, `leads_count`, `created_at`, `updated_at`, `last_exported_at`, `last_funding_date`. |
| `direction` | `asc` (default) or `desc`; only used with `sort`. |
| `query` | Free-text search across name and domain. |
| `company_name` | Contains-match. |
| `website` | Contains-match on domain. |
| `company_size` | One or more: `1-10`, `11-50`, `51-200`, `201-500`, `501-1000`, `1001-5000`, `5001-10000`, `10001+` (URL-encode `+` as `%2B`). |
| `company_type` | One or more types. |
| `industry` | One or more industry category/subcategory IDs. |
| `technology` | One or more technology slugs. |
| `keywords` | Matches description/keywords text. |
| `source` | Contains-match. |
| `tags` | One or more company tag IDs. |
| `company_list_ids` | Scope to one or more company lists (must belong to team). |
| `leads_count` | Range `from:to`. |
| `location` | Matches city/state/country/continent name. For precision use `location_country_included`, `location_state_included`, `location_city_included`, `location_continent_included`, `location_business_region_included` (each with `_excluded` counterpart). |
| `country` | One or more ISO 3166 alpha-2 codes. |
| `founded_year` | Range `from:to` (requires entitlement). |
| `funding_series` | One or more series (requires entitlement). |
| `funding_amount` | Range `from:to` (requires entitlement). |
| `last_funding_date` | Date range (requires entitlement). |
| `api_key` (required) | Your secret API key. |

```
GET https://api.hunter.io/v2/companies?api_key=YOUR_KEY
```
```json
{
  "data": { "companies": [
    { "id": 1, "domain": "stripe.com", "leads_count": 12, "created_at": "2026-05-26 12:00:00 UTC" },
    { "id": 2, "domain": "intercom.com", "leads_count": 4, "created_at": "2026-05-26 12:00:01 UTC" }
  ] },
  "meta": { "total": 2, "params": { "limit": 20, "offset": 0 } }
}
```

#### Get a company

```
GET https://api.hunter.io/v2/companies/1?api_key=YOUR_KEY
```

#### Create a company

If the domain already exists on your team, returns the existing record without creating a new one.

| Parameter | Description |
|---|---|
| `domain` (required) | Must be a valid, already-known domain, e.g. `stripe.com`. |
| `api_key` (required) | Your secret API key. |

```
POST https://api.hunter.io/v2/companies?api_key=YOUR_KEY
```
```json
{ "domain": "stripe.com" }
```
→ `201 Created`

#### Delete a company

Also removes any tag assignments on the company (tags themselves are kept).

```
DELETE https://api.hunter.io/v2/companies/1?api_key=YOUR_KEY
```
→ `204 No Content`

---

### Company Lists `Beta`

Manage company lists (static or dynamic) via the API. All calls are free.

#### List all your company lists

| Parameter | Description |
|---|---|
| `limit` | 1–1000. Default 20. |
| `offset` | Lists to skip. |
| `api_key` (required) | Your secret API key. |

```
GET https://api.hunter.io/v2/company-lists?api_key=YOUR_KEY
```
```json
{
  "data": { "company_lists": [
    { "id": 1, "name": "My static list", "type": "static", "company_list_folder_id": null, "created_at": "2026-03-18T19:00:00Z" },
    { "id": 2, "name": "My dynamic list", "type": "dynamic", "filters": { "company_name": "Acme" }, "company_list_folder_id": 1, "created_at": "2026-03-18T19:00:01Z" }
  ] },
  "meta": { "total": 2, "params": { "limit": 20, "offset": 0 } }
}
```

#### Get a company list

Includes `companies_count`.

```
GET https://api.hunter.io/v2/company-lists/1?api_key=YOUR_KEY
```

#### Create a company list

| Parameter | Description |
|---|---|
| `name` (required) | List name. |
| `type` | `static` (default) or `dynamic`. |
| `filters` | Required if `type` is `dynamic`. |
| `company_list_folder_id` | Optional folder. |
| `api_key` (required) | Your secret API key. |

```
POST https://api.hunter.io/v2/company-lists?api_key=YOUR_KEY
```
Static: `{ "name": "My new company list" }`
Dynamic: `{ "name": "My dynamic list", "type": "dynamic", "filters": { "company_name": "Acme" } }`
→ `201 Created`

#### Update a company list

| Parameter | Description |
|---|---|
| `id` (required) | List identifier. |
| `name` | New name. |
| `filters` | New filters (dynamic lists only; ignored for static). |
| `company_list_folder_id` | New folder. |
| `api_key` (required) | Your secret API key. |

```
PUT https://api.hunter.io/v2/company-lists/1?api_key=YOUR_KEY
```
→ `204 No Content`

#### Delete a company list

Static lists with companies are deleted asynchronously (`202 Accepted`); otherwise `204 No Content`.

```
DELETE https://api.hunter.io/v2/company-lists/1?api_key=YOUR_KEY
```

---

### Company list folders `Beta`

Organize company lists into folders. All calls are free.

#### List all your folders

| Parameter | Description |
|---|---|
| `limit` | Default 20, max 100. |
| `offset` | Default 0. |
| `api_key` (required) | Your secret API key. |

Each folder embeds `company_lists_count`, sorted by `id` descending.

```
GET https://api.hunter.io/v2/company-lists/folders?api_key=YOUR_KEY
```

#### Retrieve one of your folders

```
GET https://api.hunter.io/v2/company-lists/folders/1?api_key=YOUR_KEY
```

#### Create a new folder

| Parameter | Description |
|---|---|
| `name` (required) | Folder name. |
| `color` | Optional color label. |
| `api_key` (required) | Your secret API key. |

```
POST https://api.hunter.io/v2/company-lists/folders?api_key=YOUR_KEY
```
→ `201 Created`

#### Update an existing folder

```
PUT https://api.hunter.io/v2/company-lists/folders/1?api_key=YOUR_KEY
```
→ `204 No Content`

#### Delete a folder

Lists are not deleted; `company_list_folder_id` resets to `null`.

```
DELETE https://api.hunter.io/v2/company-lists/folders/1?api_key=YOUR_KEY
```
→ `204 No Content`

---

### Companies in a company list `Beta`

Add or remove a company in a **static** company list only (dynamic lists auto-populate from filters). All calls are free.

#### Add a company to a list

Company must already exist on your team (typically created from a lead). 404 if list/company missing or belongs to another team; 400 if `company_id` missing.

| Parameter | Description |
|---|---|
| `company_list_id` (required) | Static list to add to. |
| `company_id` (required) | Company to add. |
| `api_key` (required) | Your secret API key. |

```
POST https://api.hunter.io/v2/company-lists/1/companies?api_key=YOUR_KEY
```
→ `201 Created`

#### Remove a company from a list

Company isn't deleted, only its list membership. 404 if not in the list.

```
DELETE https://api.hunter.io/v2/company-lists/1/companies/84?api_key=YOUR_KEY
```
→ `204 No Content`

---

### Company Tags `Beta`

Manage company-level tags and apply/remove them. All calls are free.

Methods: List, Get, Create, Update, Delete, Assign to company, Remove from company.

#### List all your company tags

| Parameter | Description |
|---|---|
| `limit` | 1–100. Default 25. |
| `offset` | Tags to skip. |
| `api_key` (required) | Your secret API key. |

```
GET https://api.hunter.io/v2/company-tags?api_key=YOUR_KEY
```

#### Get a company tag

```
GET https://api.hunter.io/v2/company-tags/1?api_key=YOUR_KEY
```

#### Create a company tag

| Parameter | Description |
|---|---|
| `name` (required) | Unique within team. |
| `color` | Hex color (no `#`), supported palette value. Random if omitted. |
| `api_key` (required) | Your secret API key. |

```
POST https://api.hunter.io/v2/company-tags?api_key=YOUR_KEY
```
→ `201 Created`

#### Update a company tag

```
PUT https://api.hunter.io/v2/company-tags/1?api_key=YOUR_KEY
```
→ `200 OK`

#### Delete a company tag

Also removed from any assigned company.

```
DELETE https://api.hunter.io/v2/company-tags/1?api_key=YOUR_KEY
```
→ `204 No Content`

#### Assign a tag to a company

Pass `tag_id` (existing) or `tag_name` (creates on the fly if missing, random color).

| Parameter | Description |
|---|---|
| `company_id` (required, in URL) | Company identifier. |
| `tag_id` | Required unless `tag_name`. |
| `tag_name` | Required unless `tag_id`. |
| `api_key` (required) | Your secret API key. |

```
POST https://api.hunter.io/v2/companies/1/tags?api_key=YOUR_KEY
```
```json
{ "tag_name": "Partner" }
```
→ `201 Created`

#### Remove a tag from a company

Tag isn't deleted, remains available for other companies. `204` whether or not it was assigned; `404` if `tag_id` doesn't exist in team.

```
DELETE https://api.hunter.io/v2/companies/1/tags/1?api_key=YOUR_KEY
```
→ `204 No Content`

---

### Bulk company operations `Beta`

Move, copy, delete, or tag many companies at once. All calls are free.

#### Move companies between lists

Both lists must be static and belong to your team. Background processing.

| Parameter | Description |
|---|---|
| `company_list_id` (required) | Source list — companies removed from here. |
| `target_company_list_id` (required) | Destination list — companies added here. |
| `company_ids` | Optional; restricts move to these companies within the source list. |
| `api_key` (required) | Your secret API key. |

```
POST https://api.hunter.io/v2/companies/bulk/move?api_key=YOUR_KEY
```
→ `202 Accepted`

#### Copy companies to a list

Leaves source selection untouched. Destination must be static.

| Parameter | Description |
|---|---|
| `target_company_list_id` (required) | Destination list. |
| `company_ids` | Required unless `company_list_id`. |
| `company_list_id` | Required unless `company_ids`. |
| `api_key` (required) | Your secret API key. |

```
POST https://api.hunter.io/v2/companies/bulk/copy?api_key=YOUR_KEY
```
→ `202 Accepted`

#### Delete companies in bulk

Selections of ≤10 deleted immediately; larger selections processed in background.

| Parameter | Description |
|---|---|
| `company_ids` | Required unless `company_list_id`. |
| `company_list_id` | Required unless `company_ids`. |
| `api_key` (required) | Your secret API key. |

```
POST https://api.hunter.io/v2/companies/bulk/delete?api_key=YOUR_KEY
```
→ `200 OK` (immediate) or `202 Accepted` (large selection)

#### Tag companies in bulk

Applies a single tag to every company in the selection. Background processing.

| Parameter | Description |
|---|---|
| `company_ids` | Required unless `company_list_id`. |
| `company_list_id` | Required unless `company_ids`. |
| `tag_id` | Required unless `tag_name`. |
| `tag_name` | Creates tag on the fly if it doesn't exist. Required unless `tag_id`. |
| `api_key` (required) | Your secret API key. |

```
POST https://api.hunter.io/v2/companies/bulk/tag?api_key=YOUR_KEY
```
→ `202 Accepted`

---

### Templates `Beta`

Manage reusable email templates. Can be referenced from sequence follow-ups to pre-fill subject/body/format. All calls are free.

#### List all your templates

| Parameter | Description |
|---|---|
| `limit` | 1–100. Default 25. |
| `offset` | Templates to skip. |
| `api_key` (required) | Your secret API key. |

```
GET https://api.hunter.io/v2/message-templates?api_key=YOUR_KEY
```

#### Get a template

```
GET https://api.hunter.io/v2/message-templates/1?api_key=YOUR_KEY
```

#### Create a template

Merge tags in subject/body must include a fallback, e.g. `{{first_name|fallback:"there"}}`.

| Parameter | Description |
|---|---|
| `name` (required) | Template name. |
| `subject` | Max 255 characters. |
| `body` (required) | Email body; can include merge tags with fallbacks. |
| `message_format` | `html` (default) or `text`. |
| `api_key` (required) | Your secret API key. |

```
POST https://api.hunter.io/v2/message-templates?api_key=YOUR_KEY
```
→ `201 Created`

#### Update a template

```
PUT https://api.hunter.io/v2/message-templates/1?api_key=YOUR_KEY
```
→ `200 OK`

#### Delete a template

```
DELETE https://api.hunter.io/v2/message-templates/1?api_key=YOUR_KEY
```
→ `204 No Content`

---

### Discover saved searches `Beta`

Save/retrieve Discover filter configurations for reuse. Scoped to the API key's user. All calls are free.

#### List all your saved searches

| Parameter | Description |
|---|---|
| `limit` | 1–100. Default 25. |
| `offset` | Saved searches to skip. |
| `api_key` (required) | Your secret API key. |

```
GET https://api.hunter.io/v2/discover/views?api_key=YOUR_KEY
```

#### Get a saved search

```
GET https://api.hunter.io/v2/discover/views/1?api_key=YOUR_KEY
```

#### Save a new search

| Parameter | Description |
|---|---|
| `name` (required) | Unique within team. |
| `filters` | JSON object: filter name → array of selected options. Defaults to `{}`. |
| `api_key` (required) | Your secret API key. |

```
POST https://api.hunter.io/v2/discover/views?api_key=YOUR_KEY
```
```json
{ "name": "Recent US SaaS", "filters": { "location_country_included": ["US"], "industry_included": ["saas"] } }
```
→ `201 Created`

#### Delete a saved search

```
DELETE https://api.hunter.io/v2/discover/views/1?api_key=YOUR_KEY
```
→ `204 No Content`

---

### Email Sequences

Interact with email sequences programmatically for advanced automation. Not all endpoints are publicly available yet — contact Hunter for specific needs. All calls are free.

#### List all your sequences

| Parameter | Description |
|---|---|
| `started` | Only started sequences. |
| `archived` | Only archived sequences. |
| `limit` | 1–100. Default 20. |
| `offset` | Sequences to skip. |
| `api_key` (required) | Your secret API key. |

```
GET https://api.hunter.io/v2/campaigns?api_key=YOUR_KEY
```
```json
{
  "data": { "campaigns": [
    { "id": 2, "name": "January tourism CTO outreach", "recipients_count": 39, "editable": true, "started": true, "archived": false, "paused": false },
    { "id": 1, "name": "Long-term customers upsell", "recipients_count": 85, "editable": true, "started": true, "archived": false, "paused": true }
  ] },
  "meta": { "limit": 20, "offset": 0 }
}
```

#### List the recipients of a sequence

| Parameter | Description |
|---|---|
| `limit` | 1–100. Default 20. |
| `offset` | Recipients to skip. |
| `api_key` (required) | Your secret API key. |

```
GET https://api.hunter.io/v2/campaigns/1/recipients?api_key=YOUR_KEY
```

#### Add a recipient to a sequence

Recipients are matched to an existing lead by email, or a new lead is created in your current list, enabling personalization via attributes.

Response's `skipped_recipients` includes emails not added and why: `duplicate`, `invalid`, `bounced`, `unsubscribed`, or `claimed`.

Adding a recipient to an **active** sequence may result in the email sending shortly after your call, with no time to cancel.

**Requirement:** provide `emails` and/or `lead_ids`.

| Parameter | Description |
|---|---|
| `id` (required) | Sequence identifier. |
| `emails` (required unless `lead_ids`) | String (single) or array (up to 50). Invalid emails cause the whole request to error — no recipients added. |
| `lead_ids` (required unless `emails`) | Array of up to 50 lead IDs. A missing lead causes the whole request to error. |
| `api_key` (required) | Your secret API key. |

```
POST https://api.hunter.io/v2/campaigns/42/recipients?api_key=YOUR_KEY
```
```json
{ "emails": ["marcus@hunter.io", "john@hunter.io"], "lead_ids": [1, 2] }
```
→ `201 Created`
```json
{
  "data": { "recipients_added": 1, "skipped_recipients": [{ "email": "john@hunter.io", "reason": "duplicate" }] },
  "meta": { "params": { "emails": ["marcus@hunter.io", "john@hunter.io"] } }
}
```

#### Cancel scheduled emails to a recipient

Cancels only scheduled messages from the given sequence to the given recipients.

| Parameter | Description |
|---|---|
| `id` (required) | Sequence identifier. |
| `emails` (required) | String or array (up to 50). Invalid email errors the whole request. |
| `api_key` (required) | Your secret API key. |

```
DELETE https://api.hunter.io/v2/campaigns/42/recipients?api_key=YOUR_KEY
```
```json
{ "emails": ["marcus@hunter.io"] }
```
→ `201 Created`

#### Start a sequence

Sequence must be in `draft` state, with recipients and content set.

```
POST https://api.hunter.io/v2/campaigns/42/start?api_key=YOUR_KEY
```
```json
{}
```
→ `200 OK`
```json
{ "data": { "message": "42 emails scheduled for sending.", "recipients_count": 21 } }
```

#### List all your sequences (new path)

Mirrors the legacy `/v2/campaigns` path under `/v2/sequences`. Scoped to the authenticated team; sequences whose owner left the team are excluded here (their `owner` is `null` if fetched individually). Legacy path keeps working with the same data. **Free.**

| Parameter | Description |
|---|---|
| `started` | Only started sequences. |
| `archived` | Only archived sequences. |
| `limit` | 1–100. Default 20. |
| `offset` | Sequences to skip. |
| `api_key` (required) | Your secret API key. |

```
GET https://api.hunter.io/v2/sequences?api_key=YOUR_KEY
```

#### Create, update, and delete a sequence

Full lifecycle management under `/v2/sequences`:

- **`POST /v2/sequences`** — Create a draft. Required: `name`. Optional: `email_account_ids`, `schedule_time_start`, `schedule_time_end`, `schedule_days`, `add_unsubscribe_link`, `tracked`, `tracked_links` (Premium only), `ai_assistant_enabled`, `bcc_recipient`, `start_at`. Returns `201`.
- **`GET /v2/sequences/:id`** — Fetch a sequence.
- **`PUT /v2/sequences/:id`** — Update attributes above. Explicit JSON `null` on `email_account_ids`, `schedule_time_start`, `schedule_time_end`, `schedule_days`, or boolean toggles leaves the value unchanged; `bcc_recipient`/`start_at` accept `null` to clear. Started/archived sequences reject scheduling/sender mutations with `422 sequence_locked`.
- **`DELETE /v2/sequences/:id`** — Soft-deletes a draft (`204`). Started/archived sequences return `422 sequence_not_destroyable` — use archive instead.
- **`POST /v2/sequences/:id/recipients`, `DELETE /v2/sequences/:id/recipients`, `POST /v2/sequences/:id/start`** — Aliases of the legacy `/v2/campaigns/:id/*` endpoints.

**Idempotency-Key header (create only)**

`POST /v2/sequences` supports `Idempotency-Key` to safely retry on network errors without duplicating sequences. Per-user, per-endpoint dedup token, 24-hour TTL (60-second TTL while the original request is in flight).

The server fingerprints the body, normalizing:
- Reordered/duplicated entries in `schedule_days`/`email_account_ids`.
- Case-only differences in `bcc_recipient` (lowercased).
- Explicit `null` on fields with "leave unchanged" semantics (dropped before hashing).

Structured error IDs:
- `idempotency_key_too_long` (422) — header > 255 characters.
- `idempotency_key_in_flight` (409, `Retry-After: 2`) — concurrent request with same key still processing.
- `idempotency_key_mismatch` (422) — same key, different body fingerprint.
- `idempotency_key_consumed` (422) — original sequence no longer accessible.

#### Pause a sequence

Immediately stops scheduled emails until resumed. Idempotent.

```
POST https://api.hunter.io/v2/sequences/42/pause?api_key=YOUR_KEY
```
```json
{}
```
→ `200 OK`
```json
{ "data": { "id": 42, "paused": true } }
```

#### Resume a sequence

Restarts scheduled sending after a pause. Must pass the same validations as starting (connected email account, non-empty schedule, etc.) — if any check fails, returns `422` and stays paused. No-op if not paused.

```
DELETE https://api.hunter.io/v2/sequences/42/pause?api_key=YOUR_KEY
```
```json
{}
```
→ `200 OK`
```json
{ "data": { "message": "Sequence resumed." } }
```

#### Archive a sequence

Only started (active or paused) sequences can be archived. Stops further scheduled emails. Idempotent.

```
POST https://api.hunter.io/v2/sequences/42/archive?api_key=YOUR_KEY
```
```json
{}
```
→ `200 OK`
```json
{ "data": { "id": 42, "archived": true } }
```

#### List the follow-ups of a sequence

Returns every follow-up (step) ordered ascending, including the initial email (step 0). A/B test variants are both returned, ordered by variant within the step. **Free.**

| Parameter | Description |
|---|---|
| `sequence_id` (required) | Sequence identifier. |
| `limit` | 1–100. Default 20. |
| `offset` | Follow-ups to skip. |
| `api_key` (required) | Your secret API key. |

```
GET https://api.hunter.io/v2/sequences/42/follow-ups?api_key=YOUR_KEY
```

#### Get a follow-up of a sequence

| Parameter | Description |
|---|---|
| `sequence_id` (required) | Sequence identifier. |
| `id` (required) | Follow-up identifier. |
| `api_key` (required) | Your secret API key. |

```
GET https://api.hunter.io/v2/sequences/42/follow-ups/1001?api_key=YOUR_KEY
```

#### Add a follow-up to a sequence

Appends a new step. Step 0 is the initial email, step 1 the first follow-up, etc. If `step` omitted, appended after the highest existing step. If `message_template_id` provided, blank `subject`/`body`/`message_format` are filled in from the template. **Free.**

| Parameter | Description |
|---|---|
| `sequence_id` (required) | Also accepts `campaign_id` (equivalent) on the campaign-scoped route. |
| `subject` | Required unless `message_template_id`. |
| `body` | Required unless `message_template_id`. |
| `wait_days` | Days to wait after the previous step. |
| `step` | Position; appended at end if omitted. |
| `message_format` | `text` or `html`; defaults to team preference. |
| `message_template_id` | Fills blank subject/body/format from a saved template. |
| `api_key` (required) | Your secret API key. |

```
POST https://api.hunter.io/v2/sequences/42/follow-ups?api_key=YOUR_KEY
```
```json
{ "subject": "Re: Quick question about {{company}}", "body": "Just checking in ...", "wait_days": 3, "message_format": "text" }
```
→ `201 Created`

#### Update a follow-up in a sequence `Beta`

Updates a single follow-up's content. Send only fields you want to change. Sequence must not be active — updating during an active sequence returns `422`. **Free.**

| Parameter | Description |
|---|---|
| `sequence_id` (required) | Sequence identifier. |
| `id` (required) | Follow-up identifier. |
| `subject` | New subject. |
| `body` | New body. |
| `wait_days` | New wait time. |
| `message_format` | `text` or `html`. |
| `api_key` (required) | Your secret API key. |

```
PUT https://api.hunter.io/v2/sequences/42/follow-ups/1001?api_key=YOUR_KEY
```
→ `200 OK`, or `422` with `{ "id": "sequence_active", "code": 422, ... }` if the sequence is active.

#### Delete a follow-up from a sequence

Only the **last** step can be deleted. Fails if: sequence is active, follow-up isn't the last step, or messages already sent for it.

| Parameter | Description |
|---|---|
| `sequence_id` (required) | Sequence identifier. |
| `id` (required) | Follow-up identifier (must be last step). |
| `api_key` (required) | Your secret API key. |

```
DELETE https://api.hunter.io/v2/sequences/42/follow-ups/5?api_key=YOUR_KEY
```
→ `204 No Content`, or `422` if deletion isn't allowed.

#### Get sequence stats

Returns aggregate stats and per-follow-up breakdown.

**Important:** sequence-level engagement counts/rates are **recipient-based** — each distinct recipient contributes at most once per metric. `sent`, `delivered`, `opened`, `clicked`, `replied` tally distinct recipients; `open_rate`, `click_rate`, `reply_rate` divide by `delivered`; `unsubscribed_recipients_rate` divides unique unsubscribed by all-ever-added recipients. Rates are ratios (0–1), not percentages. Open rate ≥ reply rate by design.

`bounced`/`bounce_rate` are the exception — **message-based** (bounced messages / messages sent), matching the dashboard and the per-follow-up breakdown (which stays message-based since each recipient gets exactly one message per step).

> **Breaking change note (multi-step sequences):** `sent`, `delivered`, `opened`, `clicked`, `replied` previously counted messages; they now count distinct recipients (a recipient with two follow-ups now contributes 1, not 2). Rates changed accordingly to match the dashboard. `bounced`/`bounce_rate` are unchanged.

| Parameter | Description |
|---|---|
| `id` (required) | Sequence identifier. |
| `api_key` (required) | Your secret API key. |

```
GET https://api.hunter.io/v2/sequences/42/stats?api_key=YOUR_KEY
```

#### Get sequence details

Returns full sequence configuration: schedule, tracking settings, attached email accounts, follow-up steps, owner.

`status` (derived): `draft`, `planned`, `active`, `paused`, `completed`, `archived`, `preparing`, `error`.

Schedule times (`time_start`, `time_end`) are `HH:MM` (24-hour). Dates/timestamps are ISO 8601.

| Parameter | Description |
|---|---|
| `id` (required) | Sequence identifier. |
| `api_key` (required) | Your secret API key. |

```
GET https://api.hunter.io/v2/sequences/42?api_key=YOUR_KEY
```
```json
{
  "data": {
    "id": 42, "name": "January tourism CTO outreach", "status": "active", "recipients_count": 100,
    "started": true, "paused": false, "archived": false,
    "schedule": { "start_at": "2026-01-15T00:00:00+00:00", "time_start": "09:00", "time_end": "16:59", "days": [1,2,3,4,5] },
    "settings": { "tracked": true, "tracked_links": true, "add_unsubscribe_link": true, "bcc_recipient": null },
    "email_account_ids": [101, 102],
    "follow_ups": { "unique_steps_count": 3, "steps": [0, 1, 2] },
    "owner": { "id": 7, "email": "owner@example.com" },
    "created_at": "2026-01-10T14:32:11.000Z",
    "updated_at": "2026-01-12T09:05:44.000Z"
  }
}
```

---

## More

### Report API feedback

A structured channel to report friction with the API — missing capabilities, wrong/misleading docs, error responses, bad data, confusing workflows. **Especially recommended for LLMs/agents driving Hunter end to end.** No permission needed, free, never blocks your task.

| Parameter | Description |
|---|---|
| `api_key` (required) | Your secret API key. |
| `feedback_type` (required) | `missing_endpoint`, `incorrect_documentation`, `unexpected_response`, `bug`, `data_quality`, `confusing_behavior`, `feature_request`, `other`. |
| `summary` (required) | One-line title, max 200 characters. |
| `details` (required) | What happened, what you were trying to do. |
| `endpoint` | API path or MCP tool, e.g. `/v2/domain-search`. |
| `expected` | What you expected. |
| `actual` | What actually happened. |
| `request_example` | Sanitized example request. |
| `response_example` | Sanitized example response/error. |
| `severity` | `low` (default), `medium`, `high`, `blocking`. |
| `agent` | Model/agent name, e.g. `gpt-4o`. |

```
POST https://api.hunter.io/v2/feedback?api_key=YOUR_KEY
```
```json
{
  "feedback_type": "unexpected_response",
  "summary": "Domain Search omitted the department field for some emails",
  "details": "Requested emails for stripe.com; half the results had no department even though the docs say it is always present.",
  "endpoint": "/v2/domain-search",
  "expected": "Every email object includes a department string.",
  "actual": "Some email objects had no department key at all.",
  "severity": "medium",
  "agent": "gpt-4o"
}
```
→ `201 Created`

---

### Logos

Get the logo of any company by domain. Returns the image directly (not JSON) — PNG, WEBP, or AVIF. **No authentication required.**

| Parameter | Description |
|---|---|
| `domain` (required) | e.g. `hunter.io` or `stripe.com`. |

**Response:** `200` (image returned) or `404` (logo not currently in database — future requests will retry).

```
GET https://logos.hunter.io/hunter.io
```

---

### API wrappers

Community-built wrappers to get started faster: **Ruby, Node.js, Python, Laravel, Go, R**. Contributions and new wrapper submissions welcome.

Hunter also integrates with **Zapier** for no-code workflows.

---

### MCP Server

Hunter's remote MCP server integrates the API with any LLM supporting the Model Context Protocol (e.g. OpenAI Responses API, Claude for Desktop).

**Endpoint:** `https://mcp.hunter.io/mcp` (Streamable HTTP transport)

Requires a valid Hunter API key via one of:
- `Authorization: Bearer HUNTER_API_KEY`
- `X-API-KEY: HUNTER_API_KEY`

**OpenAI Responses API example**
```bash
curl https://api.openai.com/v1/responses \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer YOUR_OPENAI_API_KEY" \
  -d '{
    "model": "gpt-4.1",
    "tools": [
      {
        "type": "mcp",
        "server_label": "hunter-remote-mcp",
        "server_url": "https://mcp.hunter.io/mcp",
        "require_approval": "never",
        "headers": { "X-API-KEY": "YOUR_HUNTER_API_KEY" }
      }
    ],
    "input": "YOUR_INPUT"
  }'
```

**Claude Desktop configuration**
```json
{
  "mcpServers": {
    "hunter-remote-mcp": {
      "command": "npx",
      "args": [
        "mcp-remote",
        "https://mcp.hunter.io/mcp",
        "--header",
        "X-API-KEY:YOUR_HUNTER_API_KEY"
      ]
    }
  }
}
```
