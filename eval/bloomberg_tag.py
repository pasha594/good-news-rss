#!/usr/bin/env python3
"""Tag another curator's list of picked articles with Claude Haiku 5.5, using the
same Batch runner, prompt and extra fields as model_compare.py --extras.

The input list is not ours to publish, so it lives in the repo only encrypted
(eval/bloomberg_tag/input.jsonl.enc); the workflow decrypts it with a passphrase
secret and re-encrypts the results before committing them. Logs carry counts only.

Differences from the month run:
  - input is headline + summary + source (+ host); there is no RSS description;
  - a run note tells the model to fill entity/location/region/virtues/topics for
    EVERY article (all are already picks), while still giving its own verdict;
  - its own output dir and spend ledger (eval/bloomberg_tag/out/), so --budget-usd
    caps this run alone.

Usage:
  python eval/bloomberg_tag.py --input F.jsonl [--prompt-file F --entity-prompt-file F]
                               [--budget-usd 2] [--dry-run]
"""

import argparse
import json
import os
import sys
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent))
import model_compare as mc  # noqa: E402

HERE = mc.ROOT / "eval" / "bloomberg_tag"
SUMMARY_CHARS = 1000

RUN_NOTE = """=== THIS RUN: ARTICLES ALREADY PICKED BY ANOTHER CURATOR ===
Every article below was already picked as good news by another curator. Still give
your own honest selected, impactful, uplifting, score, reason and deciding_filter.
But fill entity_type, person, org, location, region, virtues and topics for EVERY
article as if it were selected, impactful and uplifting. This overrides the rules
above that leave those fields empty. The "description" line is the curator's summary.
"""


def article_block(n, aid, r):
    link = r.get("webUrl") or ""
    host = urlparse(link).netloc.lower()
    host = host[4:] if host.startswith("www.") else host
    summary = mc.clean(r.get("summary"))[:SUMMARY_CHARS] or "(none)"
    return (f"[{n}] id: {aid}\nheadline: {mc.clean(r.get('headline'))}\n"
            f"description: {summary}\nsource: {r.get('source') or '?'} | host: {host or '?'}")


_parse = mc.parse


def parse(text, chunk_ids, extras=False):
    """mc.parse drops virtues/topics when selected is false; keep them here."""
    rows = _parse(text, chunk_ids, extras)
    if not extras:
        return rows
    try:
        data = json.loads(text)
        raw = {r["id"]: r for r in data.get("results", []) if isinstance(r, dict) and "id" in r}
    except (TypeError, ValueError, AttributeError):
        return rows
    for aid, got in rows.items():
        src = raw.get(aid, {})
        for k, allowed in (("virtues", mc.VIRTUES), ("topics", mc.TOPICS)):
            vals = src.get(k) if isinstance(src.get(k), list) else []
            got[k] = [v for v in allowed if v in vals]
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", required=True, help="decrypted JSONL: messageId, headline, summary, source, webUrl")
    ap.add_argument("--prompt-file")
    ap.add_argument("--entity-prompt-file")
    ap.add_argument("--budget-usd", type=float, default=2.0)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    items = []
    for ln in open(args.input, encoding="utf-8"):
        if ln.strip():
            r = json.loads(ln)
            items.append((r["messageId"], r))
    print(f"input: {len(items)} articles")

    prompt = mc.load_prompt(args.prompt_file)
    extra = (Path(args.entity_prompt_file).read_text(encoding="utf-8") if args.entity_prompt_file
             else os.environ.get("EVAL_ENTITY_PROMPT", ""))
    if len(extra) < 200:
        sys.exit("entity prompt missing: set EVAL_ENTITY_PROMPT or pass --entity-prompt-file")
    prompt = prompt.rstrip() + "\n\n" + extra.strip() + "\n\n" + RUN_NOTE

    mc.OUT = HERE / "out"
    mc.article_block = article_block
    mc.parse = parse

    if args.dry_run:
        reqs = [items[k:k + mc.PER_REQUEST] for k in range(0, len(items), mc.PER_REQUEST)]
        user_tok = sum(len(mc.user_message(c)) for c in reqs) / 4
        sys_tok = len(prompt) / 4 * len(reqs)
        out_tok = len(items) * 110
        est = (user_tok + sys_tok) * mc.PRICE["input"] + out_tok * mc.PRICE["output"]
        print(f"requests: {len(reqs)}; ~{(user_tok + sys_tok) / 1e6:.1f}M input tokens, "
              f"~{out_tok / 1e6:.2f}M output; estimate without caching ${est:.2f}")
        return 0
    return mc.run_haiku(items, prompt, args.budget_usd, "haiku_bloomberg", extras=True)


if __name__ == "__main__":
    sys.exit(main())
