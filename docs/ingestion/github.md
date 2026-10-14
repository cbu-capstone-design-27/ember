# GitHub ingestion: rate limits

- Jira: EMBER-44 (floor). The worker itself is EMBER-35.
- GitHub's published limits were checked against docs.github.com on 2026-10-02. Links are in [Sources](#sources).
- Operator steps for the pull live in `services/github-ingestion/README.md`. This page is the limit contract.

The backfill (`services/github-ingestion/pull_test.py`) authenticates as a **GitHub App installation**. It spends two App JWT calls (installation lookup, then an installation access token) and then every repository read uses that installation token. A personal access token is not what this command sends. The numbers below are both documented because a later caller, or a person debugging a 403, needs to know which budget they are on.

Webhook delivery is not covered here. GitHub's rate limits apply to outbound REST calls.

## Rate limits

### Primary limits

Primary limits are requests per hour. They reset on the hour window GitHub reports in `x-ratelimit-reset` (UTC epoch seconds), not on a clock hour we choose.

| Credential | Budget | What shares it |
| --- | --- | --- |
| Unauthenticated | 60 requests / hour / IP | Public reads with no token. This connector does not do that. |
| Personal access token (classic or fine-grained), OAuth user token, or GitHub App **user** access token | 5,000 requests / hour | Every request made as that user, including other apps acting for them. |
| Same user tokens, when a GitHub Enterprise Cloud organization owns or approves the app and the user is a member | 15,000 requests / hour | Requests from that higher limit also consume the 5,000 budget used by a personal access token. Spending 10,000 through the app leaves the PAT with nothing, even if the app still has 5,000 left. |
| GitHub App **installation** access token, installation not on a GitHub Enterprise Cloud organization | 5,000 requests / hour minimum | Scales up: +50 / hour for each repository over 20, and +50 / hour for each user over 20 when the installation is on an organization. Cap **12,500** / hour. |
| GitHub App **installation** access token, installation on a GitHub Enterprise Cloud organization | 15,000 requests / hour | Does not use the per-repo / per-user scale above. |

`pull_test.py` is on the installation-token rows. The token lasts one hour; a backfill that runs longer than that has to be started again with a new token. Refreshing mid-pull is not part of this change.

Search and GraphQL have separate budgets. This pull does not call them.

### Secondary limits

These sit on top of the hourly budget and apply to PATs and installation tokens. GitHub does not publish a status header for them, and it can change them without notice.

- No more than **100 concurrent** requests, shared with GraphQL. Default pull concurrency is 32 (maximum 80).
- No more than **900 points per minute** on REST. Most `GET`, `HEAD`, and `OPTIONS` requests cost 1 point. Most `POST`, `PATCH`, `PUT`, and `DELETE` requests cost 5. Some endpoints cost more and the cost is not published.
- No more than **90 seconds of CPU time per 60 seconds** of real time.
- Content creation: generally no more than 80 requests per minute and 500 per hour. This pull is read-only.

A secondary limit answers **403 or 429** with a message that says `secondary rate limit` or `abuse detection`. There is no way to read how much secondary budget is left.

### Response headers

Sent on REST responses, including successful ones:

| Header | Meaning |
| --- | --- |
| `x-ratelimit-limit` | Requests allowed in the current hour for this resource |
| `x-ratelimit-remaining` | Requests left in that hour |
| `x-ratelimit-used` | Requests already counted |
| `x-ratelimit-reset` | When the window resets, UTC epoch seconds |
| `x-ratelimit-resource` | Which family was charged (`core` for these reads) |
| `retry-after` | Seconds to wait. Present on some secondary limits. |

GitHub tells clients to pace from these headers and not to depend on the count being exact from one response to the next (a later response can show a higher remaining). `GET /rate_limit` does not spend primary quota, but it can spend secondary quota. This connector does not call it. It reads the headers on the requests it was already going to make.

The same parser also accepts `RateLimit-Limit`, `RateLimit-Remaining`, `RateLimit-Reset`, and a structured `RateLimit` policy (`r` remaining, `t` seconds until reset). GitHub's documented headers are the `x-ratelimit-*` set. The aliases are there so a response that uses the other spelling is not ignored.

### When the limit is exceeded

- **Primary:** HTTP 403 or 429, and `x-ratelimit-remaining: 0`. Do not call again until `x-ratelimit-reset`. Calling earlier can get the integration banned.
- **Secondary:** HTTP 403 or 429 and the secondary / abuse message. If `retry-after` is present, wait that many seconds. If remaining is also 0, wait until the reset instead. Otherwise wait at least a minute, then back off exponentially, and stop after a bounded number of tries.

A 403 whose body is a permission error (`Resource not accessible by integration` and the like), with remaining quota above 0 and no `retry-after`, is not a rate limit. The pull fails that request.

### How this connector behaves

Implemented in `services/github-ingestion/pull_test.py`. One gate is shared by every worker in the process, including the two calls that mint the installation token.

| Situation | What the pull does |
| --- | --- |
| Remaining quota at or under the floor, and a reset is present | Every worker pauses until the reset. The floor is 50, or `limit / 10` when that is smaller, so a small window does not pause on every response. Default concurrency is 32, under the floor of 50, so requests already in flight can finish. |
| 403 or 429 with `retry-after` | Wait that many seconds, unless primary remaining is 0 and the reset is later. Then wait until the reset. The goal is to not call again while the hourly window is still closed. |
| 403 or 429 with remaining 0 and `x-ratelimit-reset` / `RateLimit-Reset` | Wait until that timestamp. An epoch value and an ISO-8601 timestamp are both accepted. |
| Structured `RateLimit` / `Beta-RateLimit` policy with `r=0` and `t` | Wait `t` seconds. A policy that still has remaining quota is not a reason to pause the retry. |
| Secondary / abuse body, and the primary quota is not exhausted | Wait 60 seconds, then 120, then stay at 120. |
| 429 with none of the above | Back off 1, 2, 4, … seconds, multiplied by jitter in [0.5, 1]. |
| 500, 502, 503, 504, or a network error (timeout, reset, DNS) | Same jittered backoff. `retry-after` on a 5xx is honored instead. Five attempts, then the pull stops with the HTTP error. A network failure is reported as 503 with a `network error:` body so the same path retries it. |
| 401, 403 permission, 404 | Not retried. |

A delay **the server named** (`retry-after` or a reset) is capped at **3600 seconds**, one primary window. GitHub says not to retry before the reset, and that reset can be most of an hour, so this cap is wider than the 120 second cap used for delays we invent. A 429 that keeps coming is retried until that one call has waited 3600 seconds in total, then it fails. Invented backoff (5xx, network, a 429 with no server delay, secondary backoff) is capped at **120 seconds** per sleep.

The gate is in-process. Two replicas would not share it. That shared limiter is out of scope, as are dashboard metrics.

## Sources

Checked on 2026-10-02:

- [Rate limits for the REST API](https://docs.github.com/en/rest/using-the-rest-api/rate-limits-for-the-rest-api): primary budgets for users, PATs, and GitHub App installation tokens; secondary limits and point costs; `x-ratelimit-*` headers; 403/429 handling; the warning against retrying before the reset
