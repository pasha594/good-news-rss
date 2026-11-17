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
import re
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


TAG_HEADER = """You tag news articles that a museum curator has already picked for a
good-news wall. Do NOT judge whether an article is good news or belongs on the wall:
every article below is already a pick. Your only job is to describe each one.

For EACH article return one object with: id (exactly as given), entity_type, person,
org, location, region, virtues, topics, as defined below. Fill every field for every
article. Give every article at least one virtue (each pick exemplifies at least one);
topics may be an empty list when none of the topics fits. Use entity_type "NONE" and
empty names only when the story truly has no person or organization at its center,
and location "" only when there is no clear single place.
Return exactly one object per article, in the input order.

"""
# Rules in the month-run entity prompt that leave fields empty for rejected articles.
GATES = ["Fill them only when the article is both impactful and uplifting; .*? region \"OTHER\".",
         "Fill them only when selected is true; return empty lists for every other article."]
TAG_FIELDS = ("entity_type", "person", "org", "location", "region", "virtues", "topics")


def tag_prompt(entity_prompt):
    """Tag-only prompt: the entity prompt's field definitions, minus the deciding filter
    and the rules that tie tagging to the selection verdict."""
    i = entity_prompt.find("The other five fields describe the story.")
    if i < 0:
        sys.exit("entity prompt layout changed: cannot build the tag-only prompt")
    body = entity_prompt[i:].replace("The other five fields describe the story.",
                                     "Five fields describe the story.", 1)
    for g in GATES:
        pat = r"\s+".join(re.escape(w) if w != ".*?" else ".*?" for w in g.split(" "))
        body, n = re.subn(pat, "", body, count=1, flags=re.S)
        if n != 1:
            sys.exit("entity prompt layout changed: a selection rule was not found")
    body = re.sub(r"describe WHY a\s+selected\s+article belongs\.\s+", "describe WHY the article belongs. ", body)
    body = body.replace("describe the story. \n", "describe the story.\n")
    return TAG_HEADER + body.strip() + "\n"


def tag_schema(extras=False):
    props = {"id": {"type": "string"},
             "entity_type": {"type": "string", "enum": mc.ENTITY_TYPES},
             "person": {"type": "string"}, "org": {"type": "string"}, "location": {"type": "string"},
             "region": {"type": "string", "enum": mc.REGIONS},
             "virtues": {"type": "array", "items": {"type": "string", "enum": mc.VIRTUES}},
             "topics": {"type": "array", "items": {"type": "string", "enum": mc.TOPICS}}}
    return {"type": "object", "properties": {"results": {"type": "array", "items": {
        "type": "object", "properties": props, "required": list(props), "additionalProperties": False}}},
        "required": ["results"], "additionalProperties": False}


def tag_parse(text, chunk_ids, extras=False):
    try:
        data = json.loads(text)
    except (TypeError, ValueError):
        return {}
    out = {}
    for row in (data.get("results") if isinstance(data, dict) else None) or []:
        if not isinstance(row, dict) or row.get("id") not in chunk_ids or not all(k in row for k in TAG_FIELDS):
            continue
        got = {"selected": None}          # key marks the row done for model_compare.todo()
        got.update({k: str(row[k] or "").strip()[:200] for k in ("person", "org", "location")})
        got["entity_type"] = row["entity_type"] if row["entity_type"] in mc.ENTITY_TYPES else "NONE"
        got["region"] = row["region"] if row["region"] in mc.REGIONS else "OTHER"
        for k, allowed in (("virtues", mc.VIRTUES), ("topics", mc.TOPICS)):
            vals = row[k] if isinstance(row[k], list) else []
            got[k] = [v for v in allowed if v in vals]
        out[row["id"]] = got
    return out


HEADLINE_ONLY = False     # --headline-only: no description line at all


def article_block(n, aid, r):
    link = r.get("webUrl") or ""
    host = urlparse(link).netloc.lower()
    host = host[4:] if host.startswith("www.") else host
    summary = mc.clean(r.get("summary"))[:SUMMARY_CHARS] or "(none)"
    desc = "" if HEADLINE_ONLY else f"description: {summary}\n"
    return (f"[{n}] id: {aid}\nheadline: {mc.clean(r.get('headline'))}\n"
            f"{desc}source: {r.get('source') or '?'} | host: {host or '?'}")


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
    ap.add_argument("--tag-only", action="store_true",
                    help="tags only, no selection rubric or verdict (results: haiku_bloomberg_tags)")
    ap.add_argument("--headline-only", action="store_true",
                    help="with --tag-only: send the headline without the summary (results: haiku_bloomberg_headline)")
    args = ap.parse_args()

    items = []
    for ln in open(args.input, encoding="utf-8"):
        if ln.strip():
            r = json.loads(ln)
            items.append((r["messageId"], r))
    print(f"input: {len(items)} articles")

    extra = (Path(args.entity_prompt_file).read_text(encoding="utf-8") if args.entity_prompt_file
             else os.environ.get("EVAL_ENTITY_PROMPT", ""))
    if len(extra) < 200:
        sys.exit("entity prompt missing: set EVAL_ENTITY_PROMPT or pass --entity-prompt-file")
    if args.headline_only and not args.tag_only:
        sys.exit("--headline-only needs --tag-only")
    global HEADLINE_ONLY
    HEADLINE_ONLY = args.headline_only
    if args.tag_only:
        prompt = tag_prompt(extra)
    else:
        prompt = mc.load_prompt(args.prompt_file).rstrip() + "\n\n" + extra.strip() + "\n\n" + RUN_NOTE

    mc.OUT = HERE / "out"
    mc.article_block = article_block
    mc.parse = tag_parse if args.tag_only else parse
    if args.tag_only:
        mc.make_schema = tag_schema

    if args.dry_run:
        reqs = [items[k:k + mc.PER_REQUEST] for k in range(0, len(items), mc.PER_REQUEST)]
        user_tok = sum(len(mc.user_message(c)) for c in reqs) / 4
        sys_tok = len(prompt) / 4 * len(reqs)
        out_tok = len(items) * (60 if args.tag_only else 110)
        est = (user_tok + sys_tok) * mc.PRICE["input"] + out_tok * mc.PRICE["output"]
        print(f"requests: {len(reqs)}; ~{(user_tok + sys_tok) / 1e6:.1f}M input tokens, "
              f"~{out_tok / 1e6:.2f}M output; estimate without caching ${est:.2f}")
        return 0
    return mc.run_haiku(items, prompt, args.budget_usd,
                        "haiku_bloomberg_headline" if args.headline_only else
                        "haiku_bloomberg_tags" if args.tag_only else "haiku_bloomberg", extras=True)


if __name__ == "__main__":
    sys.exit(main())
