# Output quality benchmark

Date: 2026-10-06. Jira: EMBER-58.

Ember has two jobs that can be done well or badly: loading source data into the graph correctly,
and finding the right item when someone asks a question. This benchmark measures both, has a place
to record a baseline, and lists what it cannot measure yet.

Status: the benchmark code and its unit tests are done. The baseline numbers are **not recorded
yet** (see Baseline). Do not quote a score from this document until that section is filled in.

## How to run

```bash
cd services/pipeline
python -m benchmarks run --tenant brandon-dev --input ../../data/raw/brandon-dev                       # both suites, in memory
python -m benchmarks run --suite loading --tenant brandon-dev --input ../../data/raw/brandon-dev      # no model calls, no key
python -m benchmarks run --suite retrieval --tenant brandon-dev --input ../../data/raw/brandon-dev    # retrieval only
python -m benchmarks run --tenant brandon-dev --input ../../data/raw/brandon-dev --neo4j               # reset and reload the tenant in Neo4j first
python -m benchmarks docs --tenant brandon-dev --input ../../data/raw/brandon-dev                      # list nodes to help write questions
python -m benchmarks docs --tenant brandon-dev --neo4j                                                 # same, from an already loaded tenant
```

What each command does:

- `run` loads the raw connector JSONL with the loader (the same code as `python -m loader`), runs the
  chosen suites, prints a summary and saves one JSON file per suite. `--suite` is `loading`,
  `retrieval` or `all` (the default).
- `run` without `--neo4j` works in memory: no database is needed. With `--neo4j` it **resets that
  tenant's subgraphs and reloads them** through the loader (other tenants are not touched). It reads
  `NEO4J_URI` (default `bolt://localhost:7687`), `NEO4J_USER` (default `neo4j`) and `NEO4J_PASSWORD`
  (required) from the environment or `.env`.
- `docs` prints one line per embeddable node: its ref, a tab, and the first 100 characters of its
  text. It makes no model calls and needs no key. Without `--neo4j` it loads `--input` in memory.
  With `--neo4j` it is **read-only**: it lists the nodes of a tenant that is already loaded, never
  resets or writes anything, and `--input` is optional (it is only required in the other modes).
- `--spec` and `--questions` point at other expectation or question files. The defaults are
  `benchmarks/data/loading_<tenant>.json` and `benchmarks/data/questions_<tenant>.json`.
- `--profile` picks the model profile (default: `MODEL_PROFILE`, which is `local-spark` in
  `.env.example`). `--out` changes where results are saved. `--rejects` changes where rejected
  records are written (default `rejects.jsonl` in the current folder; it is deleted at the start of
  each run).

The retrieval suite makes real embedding calls: it embeds every node of the tenant and then every
question. With the default `local-spark` profile that is free and needs `EMBEDDING_API_KEY` and a route to the
Spark's embedding service (`docs/adr/0005-local-llm-on-the-spark.md`); with `openai-dev` it costs a few cents for
a few hundred nodes and needs `OPENAI_API_KEY` (`docs/adr/0004-cloud-llm-and-embedding.md`). With a missing key the
command stops before loading anything. The loading suite makes no model calls.

Search runs in memory over the nodes' text and the vectors the embed step produced. Neo4j's own
full-text and vector indexes are not used yet (see Gaps).

### Results and exit codes

Each suite writes one file in `services/pipeline/benchmarks/results/`:

- retrieval: `<date>-retrieval-<tenant>-<profile>.json`, for example
  `2026-10-06-retrieval-brandon-dev-openai-dev.json`. The profile is in the name so two profiles run
  on the same day do not overwrite each other.
- loading: `<date>-loading-<tenant>.json`. Loading makes no model calls, so there is no profile.

Exit codes:

| Code | Meaning |
| --- | --- |
| 0 | Both the load and the suites ran clean. |
| 1 | The load was not clean (records were rejected, the schema check found problems, or a loading check failed), or a configuration, model, embedding or benchmark error happened (for example a missing key or a different embedding model already recorded in the graph). When the cause is an unclean load, the result is still saved and printed, and its `meta` records the number of `rejected` records and schema `problems`. |
| 2 | Input error: the tenant name is bad, the input folder is missing or holds no `*.jsonl`, the spec or questions file is missing or malformed, or a question's answer is not a node in the graph. |

Two points about the order of work. The spec and questions files are read and checked **before** any
reset, load or paid API call, so a typo costs nothing. With a retrieval run, the answers are also
checked against the loaded nodes before the graph is embedded, for the same reason.
Any rejected record gives exit code 1 even when it is under the loading suite's reject-rate limit;
the limit only decides whether the "reject rate" check passes.

## Suite 1: loading correctness

Question: does the graph contain what the source data says it should?

Inputs: the raw connector JSONL and an expectations file (`benchmarks/data/loading_<tenant>.json`)
written by hand from the source systems, not from the graph. The file is a JSON object with four
optional parts:

- `max_reject_rate`: the largest share of lines the loader may refuse (default 0).
- `nodes`: expected node counts as `source -> label -> count`.
- `fields`: a list of `{ref, label, props}` entries, where `ref` is `source:key` and `props` are the
  values that node must carry.
- `edges`: a list of `{type, src_label, src, dst_label, dst}` entries for relationships that must exist.

Any other top-level key (a typo such as `edge`) is rejected with exit code 2.
`benchmarks/data/loading_sample.json` is a worked example.

Checks, each pass or fail:

| Check | What it tests |
| --- | --- |
| `records accounted for` | every line read is mapped, skipped or rejected |
| `reject rate` | the share of lines the loader refused is at or under `max_reject_rate` |
| `schema check` | the loader's own check against `docs/graph-schema.md` found no mismatches |
| `nodes <Label>` | the count per source and label equals the count in the spec |
| `field <ref> <name>` | sampled items carry the right values (status, state, branches, names) |
| `edge <type> <from> -> <to>` | sampled relationships exist (a pull request resolves its issue, a reply points at its parent, and so on) |

Score: percent of checks passed, per source, plus a separate run-wide group (`all`) that holds only the
first three checks (`records accounted for`, `reject rate`, `schema check`). There is no overall
percent across the two; report the per-source scores and the run-wide group separately.
Target: 100%. A failure is a bug in the loader or an out-of-date expectation, and the detail says
which check failed and what was found.

## Suite 2: retrieval

Question: given a plain-language question, how high does each search method rank the item a person
says is the right answer?

Inputs: `benchmarks/data/questions_<tenant>.json`, 20 to 30 questions. The file is
`{"dataset": ..., "author": ..., "questions": [{"id", "question", "answers"}]}`; each `id` must be
unique and each question needs text and at least one answer. Rules for writing the questions:

- Written by a person from real project history, never generated from the graph.
- Paraphrase. Do not copy the title, and do not include ticket keys, pull request numbers or file names.
- Each question has one to three right answers, as refs such as `jira:EMBER-39`,
  `github:owner/repo#42` (issue or pull request), `github:owner/repo@<sha>` (commit) or
  `slack:<channel>:<message ts>`. An answer must be an embeddable node of the loaded tenant; run
  `python -m benchmarks docs` to see the exact refs. An answer that is not one stops the run with
  exit code 2 before anything is embedded.
- Cover every source that has data. A second person reads each question and checks the answer.

Systems compared (all over the same text, the node's `embed_text`; one caveat: the vectors see only the
first 24,000 characters of it, while BM25 sees the full text):

| System | How it ranks |
| --- | --- |
| `bm25` | keyword match (BM25, k1 1.5, b 0.75). Only nodes that share a word with the question are listed. |
| `vector` | cosine similarity between the question embedding and each node's embedding |
| `hybrid` | BM25 and vector rankings merged with reciprocal rank fusion (constant 60) |

Metrics: `recall@k` is the share of questions where a right answer is in the top k results
(k = 1, 3, 5, 10). `mrr` is the mean of 1 divided by the rank of the first right answer (0 if it is
not in the top 50). Higher is better for both.

### The sample suite is plumbing only

The repository ships a small sample (`benchmarks/data/loading_sample.json`,
`benchmarks/data/questions_sample.json`) over the loader's test data: 7 documents and 3 questions.
The unit tests run it with a fake word-hashing embedder instead of a model, so no key or network is
needed. It proves the code works from end to end. It says **nothing about quality**: with 7
documents, the vector and hybrid systems rank all of them, so `recall@10` is trivially 1.0, and the
other numbers are decided by a handful of shared words. `recall@10` on the sample is not a quality
signal. Never quote a sample score as a result.

## Recording rules

Every retrieval result file stores:

- `date`, `tenant`, and `dataset` (the name given in the questions file),
- `profile`, `llm_model`, `embedding_model` and `embedding_dimensions`, so the chat and embedding
  models are named in every result,
- `data_version`: a short hash of the node refs and texts plus the questions file, so a change to
  either one changes the version,
- `records`, `rejected` and `problems` from the load behind it, plus `questions` and `documents`
  (counts),
- the development-model note: "Scores come from the cheap development models named here. They are
  not comparable with scores from other models or from other data versions."

Loading results carry the date, tenant, spec name, load counts and a note of their own (they make no
model calls, so no model is named), and the full list of checks with their detail. No key or URL is ever written to a result file.

Compare runs only when the data version and models match, or say what changed.

## Baseline

**Not yet recorded.** No real score exists yet, and none should be invented. (On 2026-10-13 both suites ran clean
on the bundled sample data with the `local-spark` models. That shows the benchmark works on local models; it is a
pipeline check on six invented questions, not a baseline.) Recording one needs
three things that are not in the repository:

1. Access to the Spark models: `EMBEDDING_API_KEY` and `EMBEDDING_BASE_URL` set in `.env` (profile `local-spark`:
   embeddings `qwen3-embedding-0.6b` at 1024 dimensions; the chat model `qwen3-30b-a3b-instruct` is not called by
   the retrieval suite). For `openai-dev` instead, `OPENAI_API_KEY` with a spending limit on the account.
2. The real brandon-dev raw data under `data/raw/brandon-dev/` (the `data/` folder is not in git).
3. The two hand-written files for that tenant: `benchmarks/data/loading_brandon-dev.json` and
   `benchmarks/data/questions_brandon-dev.json` (20 to 30 questions, written by a person under the
   rules above). Writing these is the baseline step below.

Then run, from `services/pipeline`:

```bash
python -m benchmarks run --tenant brandon-dev --input ../../data/raw/brandon-dev
```

and copy the printed tables here, with the date, data version and model names from the result files.
For loading, copy the per-source scores and the run-wide group (`all`) as separate lines; do not
quote `all` as an overall percent.

Also:

- Optionally rerun with `--neo4j` to confirm the numbers match the in-memory run. This is the only
  real test of the Neo4j snapshot query. The benchmark reads the `NEO4J_*` environment variables, so
  a run against the cluster needs them set in the shell (Tailscale up, the password from `.env`).
- Never delete failing checks or questions to reach 100%. Record them as gaps.

| Suite | Result | Date | Data version | Models |
| --- | --- | --- | --- | --- |
| Loading correctness (per source, and run-wide separately) | Not yet recorded | | n/a | none |
| Retrieval: `bm25` | Not yet recorded | | | none |
| Retrieval: `vector` | Not yet recorded | | | `qwen3-embedding-0.6b` (`local-spark`) |
| Retrieval: `hybrid` | Not yet recorded | | | `qwen3-embedding-0.6b` (`local-spark`) |

Whatever is recorded here comes from development models and carries the note above.

## Gaps and next steps

Not measured yet, in rough order of value:

1. **Code-to-ticket linking** (the product's central claim). Test whether turning graph embeddings
   into a semantic space links code to tickets better than the standard BM25-plus-vector hybrid.
   The test masks ticket keys from commits and branch names so keyword search cannot win by
   matching "EMBER-39". It needs a code connector, which does not exist yet, and a list of true
   code-to-ticket links as ground truth. This becomes its own ticket.
2. **Comparison with other tools.** The same questions answered by plain Claude Code, other agent
   harnesses and graphify, one adapter per system. Needs an adapter interface for "answer a
   question, return the items you used" and a scoring rule for free-text answers.
3. **Extraction quality.** Once the semantic layer exists: precision and recall of extracted
   decisions and cross-source links against a hand-labelled sample.
4. **Other models.** Rerun the retrieval suite with stronger LLM and embedding profiles (for
   example `openai-dev`). Scores from different models are shown side by side, never merged.
   Note that a graph holds vectors from one embedding model only, so a different embedding model
   means re-embedding the tenant.
5. **Neo4j-native search.** Rerun hybrid retrieval with Neo4j's full-text and vector indexes and
   compare with the in-memory numbers, to check the product path and not only the method.
6. **A bigger question set.** The first set is small and written by the team. Synthetic data from
   EMBER-43, and a list of true Slack-thread-to-issue links from Ryan, would widen it and add
   cross-source questions.
7. **Answer quality.** Retrieval says whether the right item was found, not whether an agent that
   reads it writes a good answer. That needs an LLM judge or human grading.
