#!/usr/bin/env python3
"""Create the web app's test accounts on a running web app, or check they are there (EMBER-112).

Three accounts in one organization, one per role:

  test-owner@example.com   owner of "Ember Test" (slug ember-test)
  test-admin@example.com   admin
  test-member@example.com  member

All three share one password, read from WEB_TEST_PASSWORD. WEB_URL is the app's address, e.g.
https://ember-web.tail470a31.ts.net. Called by playbooks/web.yml (make web-test-accounts), which
reads the password from the vault.

Safe to re-run: it signs in where an account exists and signs up where it doesn't, creates the
organization only if the owner has none by that slug, and invites only accounts that aren't members.
After the cluster's Postgres is wiped, running it again recreates everything. It prints only emails,
roles and what it did, never the password or a session token.
"""

from __future__ import annotations

import http.cookiejar
import json
import os
import sys
import time
import urllib.error
import urllib.request

ORG_NAME = "Ember Test"
ORG_SLUG = "ember-test"
ACCOUNTS = [
    {"role": "owner", "name": "Test Owner", "email": "test-owner@example.com"},
    {"role": "admin", "name": "Test Admin", "email": "test-admin@example.com"},
    {"role": "member", "name": "Test Member", "email": "test-member@example.com"},
]
# The sources the preview workspace has data for, so the dashboard isn't empty
SOURCES = ["github", "jira", "slack"]


class Session:
    """One signed-in browser: a cookie jar, and the Origin header Better Auth checks on POSTs."""

    def __init__(self, base: str):
        self.base = base
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))

    def call(self, path: str, body: dict | None = None) -> tuple[int, object]:
        """GET (no body) or POST JSON to /api/auth<path>. Waits out the sign-in rate limit (429)."""
        for _ in range(10):
            request = urllib.request.Request(
                f"{self.base}/api/auth{path}",
                data=None if body is None else json.dumps(body).encode(),
                headers={"origin": self.base, "content-type": "application/json"},
                method="GET" if body is None else "POST",
            )
            try:
                with self.opener.open(request, timeout=30) as response:
                    status, text = response.status, response.read().decode()
            except urllib.error.HTTPError as error:
                status, text = error.code, error.read().decode()
                if status == 429:
                    # Sign-in and sign-up allow 3 requests per 10 s per client IP. Not X-Retry-After:
                    # on Postgres Better Auth sends a nonsense value there
                    time.sleep(11)
                    continue
            return status, json.loads(text) if text else None
        raise SystemExit(f"{path}: still rate limited after 10 tries")


def fail(what: str, status: int, body: object) -> None:
    message = body.get("message") if isinstance(body, dict) else body
    raise SystemExit(f"{what}: HTTP {status}: {message}")


def sign_in_or_up(base: str, password: str, account: dict) -> Session:
    session = Session(base)
    status, body = session.call("/sign-in/email", {"email": account["email"], "password": password})
    if status == 200:
        print(f"{account['email']}: signed in")
        return session
    if status != 401:
        fail(f"sign in {account['email']}", status, body)
    status, body = session.call(
        "/sign-up/email",
        {"name": account["name"], "email": account["email"], "password": password, "sources": SOURCES},
    )
    if status == 422:
        raise SystemExit(
            f"{account['email']} exists, but not with this password: vault_web_test_password changed after the "
            "accounts were made. Put the old one back, or change the account's password in Settings"
        )
    if status != 200:
        fail(f"sign up {account['email']}", status, body)
    print(f"{account['email']}: created")
    return session


def main() -> None:
    base = os.environ["WEB_URL"].rstrip("/")
    password = os.environ["WEB_TEST_PASSWORD"]
    sessions = {a["role"]: sign_in_or_up(base, password, a) for a in ACCOUNTS}
    owner = sessions["owner"]

    status, orgs = owner.call("/organization/list")
    if status != 200:
        fail("list the owner's organizations", status, orgs)
    org = next((o for o in orgs if o["slug"] == ORG_SLUG), None)
    if org is None:
        status, org = owner.call("/organization/create", {"name": ORG_NAME, "slug": ORG_SLUG})
        if status != 200:
            # "That slug is taken" means another account owns ember-test
            fail(f"create organization {ORG_SLUG}", status, org)
        print(f"{ORG_SLUG}: created, owned by {ACCOUNTS[0]['email']}")
    else:
        print(f"{ORG_SLUG}: exists")

    status, full = owner.call(f"/organization/get-full-organization?organizationId={org['id']}")
    if status != 200:
        fail(f"read organization {ORG_SLUG}", status, full)
    roles = {m["user"]["email"]: m["role"] for m in full["members"]}

    for account in ACCOUNTS[1:]:
        email, role = account["email"], account["role"]
        if email in roles:
            if roles[email] != role:
                raise SystemExit(f"{email} is {roles[email]} in {ORG_SLUG}, not {role}. Change it in Settings")
            print(f"{email}: already {role}")
            continue
        status, invite = owner.call(
            "/organization/invite-member", {"email": email, "role": role, "organizationId": org["id"]}
        )
        if status != 200:
            fail(f"invite {email}", status, invite)
        status, body = sessions[role].call("/organization/accept-invitation", {"invitationId": invite["id"]})
        if status != 200:
            fail(f"accept the invite for {email}", status, body)
        print(f"{email}: joined as {role}")

    print(f"Test accounts are ready at {base}/login")


if __name__ == "__main__":
    try:
        main()
    except urllib.error.URLError as error:
        sys.exit(f"Can't reach the web app: {error.reason}")
