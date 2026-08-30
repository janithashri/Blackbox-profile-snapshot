# LinkedIn Profile Snapshot API

A cookie-authenticated service that turns a LinkedIn profile URL into
structured JSON — name, headline, location, about, photo, experience,
education, skills, certifications, and languages.

This is a submission for the Tross take-home challenge: build a hosted
API that accepts a LinkedIn profile URL and returns structured profile
data, using the developer's own LinkedIn credentials rather than
end-user OAuth, with no browser automation involved — every LinkedIn
request in this project is a direct HTTP call.

---

## Architecture

```
                         ┌─────────────────────┐
                         │   Browser / Client    │
                         └──────────┬────────────┘
                                    │ HTTP / HTTPS
                                    ▼
                         ┌─────────────────────┐
                         │      FastAPI            │
                         │  GET  /                  │
                         │  GET  /health             │
                         │  POST /profiles            │
                         │  GET  /jobs/{job_id}        │
                         │  GET  /profiles/{public_id}  │
                         │  POST /profiles/normalize      │
                         └──────────┬──────────────────┘
                                    │
                   cache hit        │        cache miss / no in-flight job
              ┌─────────────────────┤
              ▼                     ▼
   ┌───────────────────┐  ┌──────────────────────────┐
   │  In-memory result    │  │  In-memory job store       │
   │  cache (public_id →   │  │  (job_id → status/result)   │
   │  ProfileSnapshot,      │  └──────────────┬──────────────┘
   │  TTL-based)             │                 │
   └───────────────────┘  ┌────────────────▼─────────────────┐
                          │   BackgroundTasks: run_scrape_job     │
                          └────────────────┬─────────────────────┘
                                           │
                          ┌────────────────▼─────────────────────┐
                          │        Two-Account Fallback              │
                          │  1. try PRIMARY li_at/JSESSIONID           │
                          │  2. on session_rejected, try SECONDARY      │
                          │     (if configured), one retry                │
                          └────────────────┬─────────────────────────────┘
                                           │
                          ┌────────────────▼─────────────────────────────┐
                          │           fetch_live_profile                    │
                          │   (throttled, sequential — see below)            │
                          └────────────────┬─────────────────────────────────┘
                                           │
                          ┌────────────────▼─────────────────────────────────┐
                          │   Response Classifier (errors.py)                    │
                          │   999 / login-redirect / thin-challenge → session_death│
                          │   real payload markers → treated as success            │
                          └────────────────┬─────────────────────────────────────┘
                                           │
                          ┌────────────────▼─────────────────────────────────────┐
                          │   Normalize + Merge + Dedupe                             │
                          │   RSC chunks / HTML __como_rehydration__ → ProfileSnapshot│
                          └───────────────────────────────────────────────────────┘
```

No Celery, no Redis, no message broker, no multi-account pool with
health-scored selection. Two accounts with a one-shot fallback and a
response classifier cover the actual failure modes we ran into; a full
queue/worker setup would be solving for scale this project doesn't need.

---

## Why Not Voyager

LinkedIn's officially documented Profile API is scoped to the
authenticated member and can't fetch arbitrary third-party profiles, so
it doesn't fit the requirement. The Voyager REST endpoints most
scraping tutorials reference
(`/voyager/api/identity/profiles/{id}/profileView`) are dead — they
return HTTP 410 now.

The current LinkedIn web client runs on a different system: flagship-web,
serving Server-Driven UI (SDUI) content as React Server Component (RSC)
streams — `application/octet-stream` responses made of numbered,
cross-referencing chunks, or HTML pages carrying the same data inline as
`window.__como_rehydration__`. None of this is documented anywhere. It
was reconstructed by capturing authenticated browser traffic with Burp
Suite and matching request shapes until responses stopped 404ing.

---

## Endpoints We Actually Call

| Data | Endpoint | Method | Notes |
|---|---|---|---|
| Name, headline, location, photo pieces | `POST /flagship-web/in/{public_id}/` | POST (`NavigateToScreen` body) | First-paint "shell." Falls back to `GET /in/{public_id}/` on a session-death response. |
| Experience | `GET /in/{public_id}/details/experience/` | GET | Comes back fully inlined, no further fetch needed. |
| About | `POST /flagship-web/rsc-action/actions/component?componentId=...profileCardsAboveActivity` | POST | Not a details page — `/details/about/` 404s. About is a specific UI component, found by watching what fires when a truncated bio's "…more" link is expanded. |
| Education / Skills / Certifications / Languages | `POST /flagship-web/rsc-action/actions/pagination?sduiid={pagerId}` | POST | Live path posts the pager wrapper directly (see below). A details GET/POST can 200 with an empty list; that first-paint is not used on the job path. |

### Pagination

Requesting education, skills, certifications, or languages through a
details GET or POST returns HTTP 200 with the correct page identity but
an empty list, plus a `nextPageRequest` object naming a pager ID (for
example `com.linkedin.sdui.pagers.profile.details.education`). Sending
that pager ID with a bare `PaginationRequest` body still 404s — the real
contract needs a wrapper:

```json
{
  "pagerId": "com.linkedin.sdui.pagers.profile.details.{section}",
  "clientArguments": {
    "$type": "proto.sdui.actions.requests.RequestedArguments",
    "payload": {
      "vanityName": "{public_id}",
      "profileId": "{dash_profile_id}",
      "start": 0,
      "count": 10
    },
    "screenId": "com.linkedin.sdui.flagshipnav.profile.Profile{Section}Details"
  },
  "paginationRequest": {
    "$type": "proto.sdui.actions.requests.PaginationRequest",
    "pagerId": "com.linkedin.sdui.pagers.profile.details.{section}",
    "trigger": {"$case": "itemDistanceTrigger", "itemDistanceTrigger": {"preloadDistance": 3, "preloadLength": 250}},
    "requestedArguments": { "...same payload..." }
  }
}
```

This shape was only found by watching a real browser's traffic at the
moment a "show all" section expands.

Skills can exceed the 10-item default page. The continuation isn't a
clean `nextPageRequest` key — it's a `start: 10` `PaginationRequest` JSON
string embedded in the response's root chunk, slot 1. A loop walks this
until no continuation is found or a 5-page cap is hit.

Each section's card structure is independent — skills use a typed
`componentKey` (`com.linkedin.sdui.profile.skill(...)`), education
distinguishes school vs. degree by whether a `<p>` node has a `style`
attribute, certifications key off sibling nodes next to (not inside) a
`license-certifications-lockup-view` tracking spec. There's no shared
parser across all four; each was built from an isolated example checked
against a real profile before writing extraction code.

---

## Response Classification

A 200 from LinkedIn doesn't always mean success. We see:
- HTTP 999 — LinkedIn's bot-block signal.
- A redirect to `/uas/login` — session invalidated.
- A "thin" 200 — a challenge/verification page, same status code as a
  real payload.

Every response goes through a classifier (`app/linkedin/errors.py`) that
checks for known success markers (`__como_rehydration__`, `profileId` +
`ACoAA`, `pagers.profile.details`, `NavigateToScreen` +
`profile_view_base`) instead of trusting the status code alone.

Outcomes are logged to `data/session_health.jsonl` — a hashed session
fingerprint, endpoint, outcome, and streak counters. Cookie values are
never written there.

---

## Rate Limiting and Account Handling

Outbound calls are throttled to a minimum interval
(`LINKEDIN_MIN_INTERVAL_SECONDS`, default 2s) and run sequentially, not in
parallel, even though the ~7–8 LinkedIn calls a full fetch needs would go
faster in parallel. A full profile fetch taking 30–60+ seconds is expected.

Successful snapshots are stored in an in-memory dict keyed by `public_id`
with TTL `LINKEDIN_CACHE_TTL_SECONDS` (default 300). Same profile again
within that window returns `status: done`, `cached: true`, and does not
hit LinkedIn. Clients asking for the **same** vanity while a scrape is
still running get the same `job_id` (one scrape).

`LINKEDIN_MAX_INFLIGHT_JOBS` (default 3) is a cap on **simultaneous
LinkedIn scrapes of different profiles**, not a cap on how many people
can use the API. Hundreds of requests are fine if they hit cache or wait
their turn. A fourth *new* vanity while three scrapes are already
`pending`/`running` gets HTTP 429 (`too_many_scrapes`) until one
finishes (typically 30–60s), then that slot is free. LinkedIn HTTP is
still one request at a time behind the 2s throttle even when three jobs
are “running.” Restart clears cache and jobs. Set TTL to `0` to disable
caching.

Two accounts are supported as a one-shot fallback: try primary; if the
classifier reports `session_rejected` and a secondary account is
configured, retry once with secondary; otherwise fail cleanly. No
rotation, no cooldown timers, no health scoring.

---

## API Reference

| Method | Path | Description |
|---|---|---|
| `GET` | `/` | HTML test console |
| `GET` | `/health` | Liveness check — `{"status": "ok"}` |
| `POST` | `/profiles` | Body: `{"linkedin_url_or_id": "<url or vanity id>"}`. Returns a cached result if one exists, attaches to an in-flight job if one is already running for the same profile, or starts a new job. |
| `GET` | `/jobs/{job_id}` | Poll: `pending` / `running` / `done` / `failed`. Also `cached`, `public_id`, `created_at`. On `done`, `result` is the snapshot plus `missing_fields` and `fetched_at`. On `failed`, `error.code` (`session_rejected`, `upstream_error`, `cookies_missing`, `invalid_linkedin_id`, …). |
| `GET` | `/profiles/{public_id}` | Same live fetch, waits on the request. Uses the TTL cache if warm. If `LINKEDIN_CAPTURE_DIR` has files for that id, parses those instead. |
| `POST` | `/profiles/normalize` | Accepts a raw captured RSC/HTML body directly, no live call — used for fixture debugging. |

### Example

```bash
curl -X POST http://localhost:3000/profiles \
  -H "Content-Type: application/json" \
  -d '{"linkedin_url_or_id": "https://www.linkedin.com/in/example/"}'
```

```json
{
  "job_id": "b3f1c2...",
  "status": "pending",
  "linkedin_url_or_id": "https://www.linkedin.com/in/example/",
  "public_id": "example",
  "cached": false,
  "created_at": "2026-08-30T10:00:00+00:00",
  "result": null,
  "error": null
}
```

```bash
curl http://localhost:3000/jobs/b3f1c2...
```

```json
{
  "job_id": "b3f1c2...",
  "status": "done",
  "cached": false,
  "result": {
    "full_name": "...",
    "headline": "...",
    "location": "...",
    "about": "...",
    "photo_url": "...",
    "experience": [{"title": "...", "company": "...", "date_range": "..."}],
    "education": [{"school": "...", "degree": "...", "field_of_study": "..."}],
    "skills": [{"name": "..."}],
    "certifications": [{"name": "...", "issuer": "..."}],
    "languages": [{"name": "..."}],
    "missing_fields": [],
    "fetched_at": "2026-08-30T10:00:00+00:00"
  }
}
```

---

## Setup

```bash
python -m venv .venv
.venv\Scripts\activate          # Windows
pip install -r requirements.txt
copy .env.example .env
```

Fill in `.env` with a currently-authenticated session:

```
LINKEDIN_LI_AT=...
LINKEDIN_JSESSIONID=...
```

Same cookies can go in `LINKEDIN_LI_AT_PRIMARY` / `LINKEDIN_JSESSIONID_PRIMARY`
instead. A secondary fallback account is optional:

```
LINKEDIN_LI_AT_SECONDARY=...
LINKEDIN_JSESSIONID_SECONDARY=...
```

`LINKEDIN_CACHE_TTL_SECONDS` defaults to 300. `0` turns the cache off.

`JSESSIONID` also serves as the CSRF token source — quotes are stripped
for the `csrf-token` header and re-added for the cookie itself. These
values come from a real, manually authenticated LinkedIn browser session;
this service does not automate login.

```bash
uvicorn app.main:app --reload --reload-dir app --port 3000
```

(`--reload-dir app` matters — without it, writes to
`data/session_health.jsonl` can trigger a reload mid-request.)

Open `http://127.0.0.1:3000/`.

Tests (fixture-based, no live LinkedIn calls):
```bash
python -m unittest discover -s tests
```

### Docker

```bash
copy .env.example .env
docker-compose up --build
```

Compose maps **8000**. Local uvicorn in this README is **3000**.

`.dockerignore` excludes `.env`, `.venv`, `data/`, `tests/`, and `scripts/`.

### Railway

New project → deploy from the GitHub repo. Railway injects `PORT`; the
Dockerfile already binds `0.0.0.0` to that port. Health check is `GET /health`
(`railway.toml`).

Set these in the Railway service variables (do not put them in git):

```
ENVIRONMENT=production
LINKEDIN_LI_AT=...
LINKEDIN_JSESSIONID=...
LINKEDIN_LI_AT_SECONDARY=...          # optional
LINKEDIN_JSESSIONID_SECONDARY=...     # optional
LINKEDIN_CACHE_TTL_SECONDS=300
LINKEDIN_MAX_INFLIGHT_JOBS=3
LINKEDIN_MIN_INTERVAL_SECONDS=2
```

`ENVIRONMENT=production` turns off writing RSC dumps under `data/debug/`.
The API has no login of its own — anyone with the public URL can start a
scrape on your cookies. That is fine for a take-home reviewer link; do not
advertise it as an open internet service.

---

## Known Limitations

- This isn't a stable public contract. Pager IDs, component IDs, and RSC
  chunk shapes are undocumented and can shift without notice. A parser
  that works today can return empty lists tomorrow even with a 200
  response.
- Session longevity is outside this code's control. If the hosted
  deployment stops returning live data, it most likely reflects
  LinkedIn-side session restriction rather than a defect in the
  extraction logic.
- Experience entries grouped under one company header sometimes have
  their date range on the header rather than the individual role card.
  The role is still extracted correctly; `date_range` can be `null` in
  that case rather than guessed.
- No field is ever invented — anything not confidently extracted is
  `null`, and `missing_fields` on a completed job lists what came back
  incomplete.
- Skills cap at 5 pager pages (50 skills) as a safety bound.
- Jobs and the result cache live in process memory; a restart clears
  both. TTL is `LINKEDIN_CACHE_TTL_SECONDS` (default 5 minutes).
- At most `LINKEDIN_MAX_INFLIGHT_JOBS` (default 3) **different** profiles
  can be scraped from LinkedIn at the same moment. That is not a
  three-user product limit: cache hits and polls do not use a slot, and
  when a job finishes the slot opens again. Extra *new* vanities during
  that burst get HTTP 429 (`too_many_scrapes`). `GET /profiles/{id}` is
  synchronous and does not go through this job cap.
- There's no authentication on this API. Anyone who can reach a deployed
  instance can trigger a scrape using this project's own LinkedIn
  session — acceptable for this take-home, not for wider use as-is.
- This scrapes LinkedIn using a real authenticated session and
  undocumented internal endpoints, which sits outside LinkedIn's Terms
  of Service. It's built here as a reverse-engineering exercise for this
  specific take-home, not intended for production use.

---

## Project Layout

```
app/
  main.py                   routes: / , /health
  api/profiles.py           job creation, polling, sync fetch, normalize
  linkedin/
    client.py                outbound flagship/HTML/pager/component calls, throttling
    session_health.py         append-only classifier log (hashed session, no cookies)
    normalizer.py             shell + experience RSC/HTML parsing
    pager_normalize.py        education/skills/certifications/languages/about parsing
    merge.py                  cross-source merge + deduplication
    errors.py                 response classification (session-death detection)
    public_id.py              URL → vanity id normalization
  services/jobs.py           in-memory jobs, in-flight coalesce, two-account fallback
  services/cache.py          public_id → snapshot TTL cache
  static/index.html         test console (sectioned view + raw JSON debug panel)
tests/
  fixtures/                  anonymized RSC/HTML captures, no live calls in tests
data/                       gitignored — debug dumps and session health log
```