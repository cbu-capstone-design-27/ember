"""Result metadata, saving and printing."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import date
from pathlib import Path

DEV_NOTE = ("Scores come from the cheap development models named here. They are not comparable with "
            "scores from other models or from other data versions.")
LOADING_NOTE = ("Loading checks compare the loaded graph with expectations written by hand from the source data. "
                "They make no model calls, so no model is named.")


class BenchmarkError(RuntimeError):
    pass


def data_version(docs, extra: str = "") -> str:
    """Twelve hex characters that change when any embeddable node's ref or text changes (order does not matter).

    `extra` folds in anything else that makes scores incomparable, such as a hash of the questions file.
    """
    digest = hashlib.sha256()
    for line in sorted(f"{d.ref}|{hashlib.sha256(d.text.encode('utf-8')).hexdigest()}" for d in docs):
        digest.update(line.encode("utf-8"))
    if extra:
        digest.update(b"\0" + extra.encode("utf-8"))
    return digest.hexdigest()[:12]


def build_meta(tenant: str, dataset: str, profile=None, docs=None, today: date | None = None,
               extra: str = "") -> dict:
    """Everything a reader needs to interpret a score: when, which data, which models, and the disclaimer.

    Only model names and settings go in; never a key or a URL that could carry credentials.
    """
    meta = {"date": (today or date.today()).isoformat(), "tenant": tenant, "dataset": dataset,
            "note": DEV_NOTE if profile is not None else LOADING_NOTE}
    if profile is not None:
        meta.update({"profile": profile.name, "llm_model": profile.llm_model,
                     "embedding_model": profile.embedding_model,
                     "embedding_dimensions": profile.embedding_dimensions})
    if docs is not None:
        meta["data_version"] = data_version(docs, extra)
    return meta


def save_result(result: dict, out_dir: Path) -> Path:
    """Write <date>-<suite>-<tenant>[-<profile>].json; a result that names a model profile carries it in the
    file name, so two profiles run on the same day do not overwrite each other."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    meta = result["meta"]
    profile = re.sub(r"[^A-Za-z0-9._-]", "-", meta.get("profile") or "")
    path = out_dir / f"{meta['date']}-{result['suite']}-{meta['tenant']}{'-' + profile if profile else ''}.json"
    path.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def render_summary(result: dict) -> str:
    meta = result["meta"]
    lines = [f"{result['suite']} ({meta['date']}, tenant {meta['tenant']}, data {meta.get('data_version', 'n/a')})"]
    if "embedding_model" in meta:
        lines.append(f"  models: llm {meta['llm_model']}, embedding {meta['embedding_model']}")
    if meta.get("rejected") or meta.get("problems"):
        lines.append(f"  load was not clean: {meta.get('rejected', 0)} records rejected, "
                     f"{meta.get('problems', 0)} schema problems")
    if result["suite"] == "loading":
        for group, score in result["scores"].items():
            lines.append(f"  {group:<8} {score['passed']}/{score['total']} checks passed ({score['percent']}%)")
        lines += [f"  FAILED {c['group']}: {c['name']}: {c['detail']}" for c in result.get("checks", []) if not c["ok"]]
    else:
        columns = list(next(iter(result["scores"].values())))
        lines.append("  " + f"{'system':<8}" + "".join(f"{c:>10}" for c in columns))
        for system, row in result["scores"].items():
            lines.append("  " + f"{system:<8}" + "".join(f"{row[c]:>10}" for c in columns))
    lines.append(f"  note: {meta['note']}")
    return "\n".join(lines)
