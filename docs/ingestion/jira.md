# Jira ingestion: rate limits

- Jira: EMBER-44 (floor). The worker itself is EMBER-31.
- Atlassian's published limits were checked against developer.atlassian.com on 2026-10-02. The rate-limiting page itself says it was last updated **2026-10-01**. Links are in [Sources](#sources).
- Operator steps for the pull live in `services/jira-ingestion/README.md`. This page is the limit contract.

The backfill (`services/jira-ingestion/pull_test.py`) calls Jira Cloud with **HTTP basic auth: email + API token**. That is not a Forge, Connect, or OAuth 2.0 (3LO) app. Which budget applies depends on that choice, so both are written down.

Webhook delivery is not covered here. Jira's rate limits apply to outbound REST calls.

## Rate limits

Jira Cloud runs three limiters at once. Exceeding any of them returns **HTTP 429**.

### What an API token hits

Atlassian says the points-based hourly quotas (enforcement began **2026-03-02**) apply to Forge, Connect, and OAuth 2.0 (3LO) apps. **API token traffic is not in that change.** It stays on the burst limits.

This pull uses an API token, so the limit that matters on a backfill is the **burst** limit: requests per second to one endpoint, per site. It does not grow with the number of users on the site. Draining the burst bucket for `/rest/api/3/issue` does not by itself block `/rest/api/3/search/jql`.

Default steady-state rates:

| Method | Requests / second |
| --- | --- |
| GET | 100 |
| POST | 100 |
| PUT | 50 |
| DELETE | 50 |

Some endpoints override that. The published table lists `GET /api/{version}/issue/{issueIdOrKey}` at 150/s and `GET /api/{version}/issue/{issueIdOrKey}/changelog` at 200/s. The pull's default concurrency is 32, under the GET steady-state rate, but a tight loop can still empty the burst bucket. The bucket allows a short spike above the steady rate; the steady rate is what a backfill should sit on.

A burst 429 looks like:

```http
HTTP/1.1 429 Too Many Requests
Retry-After: 1
X-RateLimit-Limit: 350
X-RateLimit-Remaining: 0
X-RateLimit-Reset: 2026-01-01T01:01:01Z
RateLimit-Reason: jira-burst-based
```

`X-RateLimit-Reset` on these responses is an **ISO-8601 timestamp**, and Atlassian says it is only sent with the 429. `Retry-After` is **seconds**.

Per-issue write limits (20 writes / 2 seconds, 100 writes / 30 seconds, reason `jira-per-issue-on-write`) apply to updates of one issue. This pull does not write issues.

### What an OAuth / Forge / Connect app hits

Documented so the headers are not a surprise if the credential changes. Not what `JIRA_EMAIL` + `JIRA_API_TOKEN` spends today.

Points, per hour, reset at the top of each UTC hour. A read of one issue is about 2 points (1 base + 1 issue). Writes cost 1 point.

| Pool | Quota |
| --- | --- |
| Global (default, one pool for the app across every site) | 65,000 points / hour |
| Per-tenant, Free | 65,000 points / hour |
| Per-tenant, Standard | 100,000 + 10 × users / hour |
| Per-tenant, Premium | 130,000 + 20 × users / hour |
| Per-tenant, Enterprise | 150,000 + 30 × users / hour, capped at 500,000 |

A quota 429 uses the same headers as a burst 429. `RateLimit-Reason` is `jira-quota-global-based` or `jira-quota-tenant-based`. Atlassian's example waits `Retry-After: 1847` seconds, with `X-RateLimit-Remaining: 0`, until the hour resets. There is no partial slowdown: requests are denied until the reset.

### Headers this connector reads

| Header | Meaning |
| --- | --- |
| `Retry-After` | Seconds to wait. Sent on a 429. Also honored on a 5xx when present. |
| `Beta-Retry-After` | Same idea, used when a 429 has no `Retry-After`. |
| `X-RateLimit-Limit` | Budget for the current scope. For burst limits, the allowed requests in the window. |
| `X-RateLimit-Remaining` | How many requests are left in that window. |
| `X-RateLimit-Reset` | When the window resets. ISO-8601 on Jira's 429s. An epoch number is accepted too. |
| `RateLimit-Reason` | Which limiter fired. Logged only as part of the body the caller already has; the wait comes from the headers above. |
| `X-RateLimit-NearLimit` | `true` when under about 20% of an hourly quota. **Not a pause signal.** Pausing at 20% of 65,000 points would stop a backfill that is still inside its budget. |
| `RateLimit` / `Beta-RateLimit` | One or more `"policy";r=<remaining>;t=<seconds until reset>` entries. `r` is omitted when the caller is well inside the quota. |
| `RateLimit-Policy` / `Beta-RateLimit-Policy` | `"policy";q=<quota>;w=<window seconds>`. `q` sizes the pause floor for that policy. |

`RateLimit-Remaining` and `RateLimit-Reset` (no `X-` prefix) are accepted as aliases of the `X-RateLimit-*` headers.

### How this connector behaves

Implemented in `services/jira-ingestion/pull_test.py`. Search pages stay serial. Detail GETs share one in-process gate, so a 429 on comments holds the issue reads too.

| Situation | What the pull does |
| --- | --- |
| Remaining quota at or under the floor, and a reset is known | Every worker pauses until the reset. The floor is 50, or `limit / 10` when that is smaller. A burst bucket of 100 therefore pauses at 10 left, not at 50. Default concurrency is 32. |
| 429 with `Retry-After` | Wait that many seconds. If remaining is 0 and the reset is later, wait until the reset instead, so the pull does not call again while the window is closed. |
| 429 with `X-RateLimit-Reset` / `RateLimit-Reset` and remaining 0 (or no remaining count) | Wait until that time. ISO-8601 and epoch are both accepted. |
| 429 with a `RateLimit` policy whose `r` is 0 | Wait that policy's `t` seconds. If several policies are exhausted, wait the longest. A policy that still has remaining quota is ignored. |
| 429 with none of those | Back off 1, 2, 4, … seconds, multiplied by jitter in [0.5, 1]. |
| 500, 502, 503, 504, or a network error | Same jittered backoff. `Retry-After` on a 5xx is honored instead. Five attempts, then the pull stops. A network failure is reported as 503 with a `network error:` body. |
| 403 or 404 | Not a rate limit. Watchers, votes, and a single issue's changelog still skip on 403/404. Every other GET failure stops the pull. |

A delay **Jira named** is capped at **3600 seconds**. The quota example in Atlassian's docs is already 1847 seconds, and they say not to retry before `Retry-After`. A cap of 120 seconds would call back into a closed hourly window. Invented backoff (5xx, network, a 429 with no server delay) is capped at **120 seconds** per sleep. One call keeps retrying a 429 until it has waited 3600 seconds in total, then it fails.

The gate is in-process. Two replicas would not share it. That shared limiter is out of scope, as are dashboard metrics.

## Sources

Checked on 2026-10-02. The page was last updated 2026-10-01:

- [Rate limiting (Jira Cloud platform)](https://developer.atlassian.com/cloud/jira/platform/rate-limiting/): points quotas and the 2026-03-02 enforcement note for apps; the statement that API tokens stay on burst limits; burst rates and the 429 header example; per-issue write limits; `X-RateLimit-*`, `Retry-After`, `RateLimit-Reason`; `RateLimit` / `Beta-RateLimit` parameters `q`, `w`, `r`, `t`; the quota 429 example with `Retry-After: 1847`
