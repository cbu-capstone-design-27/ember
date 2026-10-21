"""embed (EMBER-77): embeds every source_records row, one row = one chunk, for RAG (EMBER-78).

    DATABASE_URL=postgresql://ember:change-me-locally@localhost:5432/ember?sslmode=disable \
    EMBEDDING_URL=http://<spark>:8000 [EMBEDDING_API_KEY=...] python3 embed.py

It claims rows like the enrich workers (docs/ingestion/architecture.md, "Interim: claiming without
the job queue"): LISTEN on SOURCE_RECORDS_CHANNEL (default source_records), and claim once at start,
on any notification (the payload is ignored) and on a 30 s poll. A claim takes up to EMBED_BATCH
(default 32) rows with embedded_at null, sends them in one request to the embedding service, and
writes embedding, embed_text and embedded_at in the same transaction, each row in its own savepoint.
429, 5xx and network errors are retried with backoff. SIGINT or SIGTERM stops claiming, lets the
batch in flight finish, and exits 0.

ADR 0001: documents are embedded with no prefix (the query template is EMBER-78's), and the stored
embed_text is exactly what was embedded. At start the worker refuses to run (exit 2) unless the
endpoint returns DIMENSION-long vectors.
"""

from __future__ import annotations

import asyncio
import json
import os
import random
import signal
import sys
import time
import urllib.error
import urllib.request

from sqlalchemy import func, select, update
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from models import DIMENSION, SourceRecord

CHANNEL = os.environ.get("SOURCE_RECORDS_CHANNEL", "source_records")
DEFAULT_MODEL = "qwen3-embedding-0.6b"
POLL_SECONDS = 30.0
BATCH = 32
# The Spark serves --max-model-len 8192 tokens (infra/docker-compose.yml), not the model's 32k. 8000
# characters stays under that even at one token per character (CJK, emoji); English is ~4 per token.
MAX_CHARS = 8000
MAX_ATTEMPTS = 8


# --- embed_text: a readable text per record. Fields that are missing or empty are left out. ---


def _join(*parts, sep: str = "\n\n") -> str:
    return sep.join(part.strip() for part in parts if isinstance(part, str) and part.strip())


def adf_text(node) -> str:
    """Jira rich text (Atlassian Document Format) as plain text: text nodes, one line per block.
    Jira's v2 API sends a plain string instead, which is returned as it is."""
    if isinstance(node, str):
        return node
    if not isinstance(node, dict):
        return ""
    kind = node.get("type")
    if kind == "text":
        return node.get("text", "")
    if kind == "hardBreak":
        return "\n"
    if kind in ("mention", "emoji"):
        return (node.get("attrs") or {}).get("text", "")
    children = [adf_text(child) for child in node.get("content") or []]
    return ("" if kind in ("paragraph", "heading") else "\n").join(child for child in children if child)


def _file_paths(commit: dict) -> str:
    """Webhook commits list added/modified/removed; REST commits (GitHub compare) list files."""
    paths = [*commit.get("added", []), *commit.get("modified", []), *commit.get("removed", [])]
    paths += [f["filename"] for f in commit.get("files") or [] if isinstance(f, dict) and "filename" in f]
    return "\n".join(paths)


def _github(kind: str, payload: dict, record: dict) -> str:
    if kind == "commit":
        message = payload.get("message") or (payload.get("commit") or {}).get("message")
        return _join(message, _file_paths(payload))
    parent = payload.get("pull_request") or payload.get("issue") or {}
    if kind in ("issue", "pull_request"):
        files = "\n".join(f["filename"] for f in (record.get("fetched") or {}).get("files", []))
        return _join(parent.get("title"), parent.get("body"), files)
    if kind.endswith("comment"):
        return _join(parent.get("title"), (payload.get("comment") or {}).get("body"))
    if kind == "pull_request_review":
        return _join(parent.get("title"), (payload.get("review") or {}).get("body"))
    if kind == "release":
        release = payload.get("release") or {}
        return _join(release.get("name") or release.get("tag_name"), release.get("body"))
    if kind == "milestone":
        milestone = payload.get("milestone") or {}
        return _join(milestone.get("title"), milestone.get("description"))
    return ""


def _jira(kind: str, payload: dict, record: dict) -> str:
    issue = payload.get("issue") or {}
    fields = issue.get("fields") or {}
    if kind == "issue":
        return _join(_join(issue.get("key"), fields.get("summary"), sep=": "), adf_text(fields.get("description")))
    if kind == "comment":
        return _join(_join(issue.get("key"), fields.get("summary"), sep=": "),
                     adf_text((payload.get("comment") or {}).get("body")))
    return ""


def _gitlab(kind: str, payload: dict, record: dict) -> str:
    if kind == "commit":
        return _join(payload.get("message"), _file_paths(payload))
    attrs = payload.get("object_attributes") or payload  # webhook, or a REST object from reconcile
    if kind in ("merge_request", "issue"):
        return _join(attrs.get("title"), attrs.get("description"))
    if kind == "note":
        parent = payload.get("merge_request") or payload.get("issue") or {}
        return _join(parent.get("title"), attrs.get("note") or attrs.get("body"))
    if kind == "project":
        return _join(payload.get("path_with_namespace"), payload.get("description"))
    return ""


def _slack(kind: str, payload: dict, record: dict) -> str:
    return _join((payload.get("event") or payload).get("text"))


BUILDERS = {"github": _github, "jira": _jira, "gitlab": _gitlab, "slack": _slack}


def embed_text(source: str, kind: str, record: dict) -> str:
    """The text one source_records row is embedded as: the readable fields for its source and kind,
    else the record as compact JSON. Cut to MAX_CHARS."""
    build = BUILDERS.get(source)
    text = build(kind, record.get("payload") or {}, record) if build else ""
    if not text:
        text = json.dumps(record, separators=(",", ":"), ensure_ascii=False, sort_keys=True)
    return text[:MAX_CHARS]


# --- The embedding service: OpenAI-compatible POST {EMBEDDING_URL}/v1/embeddings ---


class Rejected(Exception):
    """A 4xx other than 429: the input is at fault, so retrying the same request can't help."""


class IdentityMismatch(Exception):
    """The endpoint isn't ADR 0001's model (wrong dimension). The worker refuses to run."""


class EmbeddingClient:
    """Synchronous, standard library only; the worker calls it in a thread. With no api_key, no
    Authorization header is sent."""

    def __init__(self, url: str, api_key: str | None, model: str = DEFAULT_MODEL, *, base_delay: float = 1.0) -> None:
        self.endpoint = url.rstrip("/") + "/v1/embeddings"
        self.api_key = api_key
        self.model = model
        self.base_delay = base_delay

    def embed(self, texts: list[str]) -> list[list[float]]:
        body = json.dumps({"model": self.model, "input": texts}).encode()
        headers = {"Content-Type": "application/json"} | ({"Authorization": f"Bearer {self.api_key}"} if self.api_key else {})
        for attempt in range(MAX_ATTEMPTS):
            request = urllib.request.Request(self.endpoint, data=body, headers=headers, method="POST")
            try:
                with urllib.request.urlopen(request, timeout=120) as response:
                    data = json.load(response)["data"]
                if len(data) != len(texts):
                    raise ValueError(f"asked for {len(texts)} embeddings, got {len(data)}")
                return [item["embedding"] for item in sorted(data, key=lambda item: item["index"])]
            except urllib.error.HTTPError as exc:
                with exc:
                    if exc.code != 429 and exc.code < 500:
                        raise Rejected(f"{exc.code}: {exc.read()[:500]!r}") from exc
                retry_after = exc.headers.get("Retry-After", "")
                wait = float(retry_after) if retry_after.isdigit() else None
                error: Exception = exc
            except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
                wait, error = None, exc
            if attempt == MAX_ATTEMPTS - 1:
                raise error
            # 1, 2, 4 … 60 s plus jitter, like Transient in architecture.md; Retry-After wins on a 429
            wait = wait if wait is not None else min(self.base_delay * 2**attempt, 60.0) * random.uniform(1, 1.25)
            print(f"embedding service: {error}, retry {attempt + 1} in {wait:.1f} s", file=sys.stderr, flush=True)
            time.sleep(wait)
        raise AssertionError("unreachable")


# --- The worker ---


class Worker:
    """One embed process. `client` is the embedding-service boundary tests stub."""

    def __init__(
        self, database_url: str, client: EmbeddingClient, *, batch: int = BATCH, poll_seconds: float = POLL_SECONDS
    ) -> None:
        url = make_url(database_url).set(drivername="postgresql+asyncpg")
        if "sslmode" in url.query:  # libpq's name; asyncpg takes the same values as `ssl`
            url = url.difference_update_query(["sslmode"]).update_query_dict({"ssl": url.query["sslmode"]})
        self.engine = create_async_engine(url, pool_size=2)  # the listener and the batch in flight
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)
        self.client = client
        self.batch = batch
        self.poll_seconds = poll_seconds
        self.wake = asyncio.Event()
        self.stopping = asyncio.Event()
        # ponytail: failed rows are skipped until the next poll, in memory. The job queue's retry and dead letter replace it.
        self.failed: set[int] = set()

    def stop(self) -> None:
        self.stopping.set()
        self.wake.set()

    def _notified(self, _conn, _pid, _channel, _payload: str) -> None:
        self.wake.set()

    async def check_identity(self) -> None:
        """ADR 0001's startup check: the endpoint must return DIMENSION-long vectors."""
        [vector] = await asyncio.to_thread(self.client.embed, ["ember embedding identity check"])
        if len(vector) != DIMENSION:
            raise IdentityMismatch(
                f"{self.client.endpoint} returned {len(vector)} dimensions for {self.client.model}, "
                f"source_records.embedding is vector({DIMENSION}) (ADR 0001)"
            )

    async def run(self) -> None:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, self.stop)
        try:
            await self.check_identity()
            async with self.engine.connect() as conn:
                listener = (await conn.get_raw_connection()).driver_connection
                await listener.add_listener(CHANNEL, self._notified)
                print(f"embed listening on {CHANNEL}", flush=True)
                while not self.stopping.is_set():
                    self.wake.clear()
                    await self._drain()
                    try:
                        await asyncio.wait_for(self.wake.wait(), self.poll_seconds)
                    except asyncio.TimeoutError:
                        self.failed.clear()
                print("embed stopped", flush=True)
        finally:
            for sig in (signal.SIGINT, signal.SIGTERM):
                loop.remove_signal_handler(sig)
            await self.engine.dispose()

    async def _drain(self) -> None:
        """Embed batches until a claim finds no row. One batch at a time: the GPU endpoint is the limit."""
        while not self.stopping.is_set():
            try:
                if not await self.embed_batch():
                    return
            except Exception as exc:  # noqa: BLE001 an outage must not stop the worker; the rows wait in Postgres
                print(f"batch failed, retried on the next wake or poll: {exc!r}", file=sys.stderr, flush=True)
                return

    async def embed_batch(self) -> int:
        """Claim up to `batch` unembedded rows, embed them in one request and write them. Returns rows claimed."""
        async with self.sessions.begin() as session:
            claim = (
                select(SourceRecord.id, SourceRecord.source, SourceRecord.kind, SourceRecord.record)
                .where(SourceRecord.embedded_at.is_(None))
                .order_by(SourceRecord.id)
                .limit(self.batch)
                .with_for_update(skip_locked=True)
            )
            if self.failed:
                claim = claim.where(SourceRecord.id.not_in(self.failed))
            rows = (await session.execute(claim)).all()
            if not rows:
                return 0
            texts: dict[int, str] = {}
            for row in rows:
                try:
                    texts[row.id] = embed_text(row.source, row.kind, row.record)
                except Exception as exc:  # noqa: BLE001 a record shape we don't expect fails only itself
                    self._fail(row.id, exc)
            written = 0
            for row_id, vector in (await self._embed(texts)).items():
                try:
                    async with session.begin_nested():
                        await session.execute(
                            update(SourceRecord)
                            .where(SourceRecord.id == row_id, SourceRecord.embedded_at.is_(None))
                            # clock_timestamp(), not now(): now() is the claim, before the request
                            .values(embedding=vector, embed_text=texts[row_id], embedded_at=func.clock_timestamp())
                        )
                    written += 1
                except Exception as exc:  # noqa: BLE001 e.g. a wrong-length or NaN vector; the savepoint keeps the rest
                    self._fail(row_id, exc)
        print(f"embedded {written} of {len(rows)} source_records", flush=True)
        return len(rows)

    async def _embed(self, texts: dict[int, str]) -> dict[int, list[float]]:
        """One request for the batch. If the service rejects it, one per row, so only the bad row fails."""
        if not texts:
            return {}
        try:
            return dict(zip(texts, await asyncio.to_thread(self.client.embed, list(texts.values()))))
        except Rejected as exc:
            if len(texts) == 1:
                self._fail(next(iter(texts)), exc)
                return {}
            vectors: dict[int, list[float]] = {}
            for row_id, text in texts.items():
                vectors |= await self._embed({row_id: text})
            return vectors

    def _fail(self, row_id: int, exc: Exception) -> None:
        self.failed.add(row_id)
        print(f"source_records {row_id} failed, retried after the next poll: {exc!r}", file=sys.stderr, flush=True)


def main() -> int:
    env = {name: os.environ.get(name, "").strip() for name in ("DATABASE_URL", "EMBEDDING_URL")}
    missing = [name for name, value in env.items() if not value]
    if missing:
        print(f"Set {', '.join(missing)} (see .env.example)", file=sys.stderr)
        return 2
    client = EmbeddingClient(
        env["EMBEDDING_URL"], os.environ.get("EMBEDDING_API_KEY", "").strip() or None, os.environ.get("EMBEDDING_MODEL", "").strip() or DEFAULT_MODEL
    )
    try:
        asyncio.run(Worker(env["DATABASE_URL"], client, batch=int(os.environ.get("EMBED_BATCH", BATCH))).run())
    except IdentityMismatch as exc:
        print(f"refusing to run: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
