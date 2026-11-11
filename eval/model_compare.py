#!/usr/bin/env python3
"""Model comparison: classify a fixed sample of RSS articles with Claude Haiku 5.5
(Message Batches API, hard USD cap) and with Gemini (free tier), both given the
same system prompt (the MOTLA good-news rubric), for a side-by-side evaluation.

The sample is a list of our own article ids (eval/model_compare/sample_ids.txt);
the articles themselves come from data/articles-*.jsonl. The system prompt comes
from the EVAL_SYSTEM_PROMPT env var (an Actions secret) or --prompt-file and is
never printed. Logs carry counts only: on a public repo, Actions logs are public.

Results append to eval/model_compare/out/<model>.jsonl, one row per article. Runs
are resumable: done ids are skipped, and Haiku's batch ids and spend live in
out/haiku_state.json, so a retry collects a pending batch instead of paying twice.

Exit codes: 0 done, 3 stopped on purpose (budget or daily quota), other = retry.

The --budget-usd cap is cumulative across runs (the ledger is in haiku_state.json),
so a whole-month run after the sample run reuses the sample's results and spend.

Usage:
  python eval/model_compare.py dry-run [--prompt-file F] [--data DIR] [--ids FILE]
  python eval/model_compare.py haiku --budget-usd 1.0
  python eval/model_compare.py haiku --ids eval/model_compare/month_ids.txt \
      --out-name haiku_month --extras --budget-usd 12    # needs EVAL_ENTITY_PROMPT
  python eval/model_compare.py gemini [--max-requests N]
"""

import argparse
import glob
import json
import os
import re
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import store  # noqa: E402

HERE = ROOT / "eval" / "model_compare"
OUT = HERE / "out"
PER_REQUEST = 25          # articles per model call
DESC_CHARS = 400
MAX_TRIES = 2             # per article, before it is recorded as an error

HAIKU = "claude-haiku-5-5"
MAX_BATCH_REQUESTS = 2000  # ~35 KB per request keeps each batch far under the API's 256 MB cap
# Batch API bills 50% of list: $0.10 / $0.50 per M tokens (prompts under 100K).
PRICE = {"input": 0.05e-6, "output": 0.25e-6, "cache_write": 0.0625e-6, "cache_read": 0.005e-6}
HAIKU_CONFIGS = [         # tried in order; the second only if the first is rejected
    {"thinking": {"type": "disabled"}},
    {"thinking": {"type": "adaptive"}, "effort": "low"},
]
GEMINI = os.environ.get("EVAL_GEMINI_MODEL", "gemini-3.5-flash-lite")   # the live classifier's model
GEMINI_PAUSE_S = 4.5

FIELDS = ("selected", "impactful", "uplifting", "score", "reason")
# --extras adds these (their instructions come from EVAL_ENTITY_PROMPT).
EXTRA_FIELDS = ("deciding_filter", "entity_type", "person", "org", "location", "region")
DECIDING = ["pass", "conflict", "headline", "tone", "scope", "source"]
ENTITY_TYPES = ["PER", "ORG", "NONE"]
REGIONS = ["NORTH_AMERICA", "LATAM", "EUROPE", "MIDDLE_EAST", "AFRICA", "ASIA", "OTHER"]


def make_schema(extras=False):
    props = {"id": {"type": "string"}, "selected": {"type": "boolean"},
             "impactful": {"type": "boolean"}, "uplifting": {"type": "boolean"},
             "score": {"type": "integer"}, "reason": {"type": "string"}}
    if extras:
        props.update({"deciding_filter": {"type": "string", "enum": DECIDING},
                      "entity_type": {"type": "string", "enum": ENTITY_TYPES},
                      "person": {"type": "string"}, "org": {"type": "string"},
                      "location": {"type": "string"},
                      "region": {"type": "string", "enum": REGIONS}})
    return {
        "type": "object",
        "properties": {"results": {"type": "array", "items": {
            "type": "object", "properties": props,
            "required": list(props), "additionalProperties": False}}},
        "required": ["results"], "additionalProperties": False,
    }


SCHEMA = make_schema()


def gemini_schema(s):
    """Gemini's responseSchema: OpenAPI-style upper-case types, no additionalProperties."""
    out = {k: v for k, v in s.items() if k != "additionalProperties"}
    out["type"] = s["type"].upper()
    if "properties" in s:
        out["properties"] = {k: gemini_schema(v) for k, v in s["properties"].items()}
    if "items" in s:
        out["items"] = gemini_schema(s["items"])
    return out


# ---------------------------------------------------------------- inputs

def load_sample(data_dir, ids_file):
    ids = [ln.strip() for ln in open(ids_file) if ln.strip()]
    want, found = set(ids), {}
    for p in sorted(glob.glob(os.path.join(data_dir, "articles-*.jsonl"))):
        for r in store.read_jsonl([Path(p)]):
            aid = store.article_id(r["source"], r["title"], r.get("link", ""))
            if aid in want and aid not in found:
                found[aid] = r
    return [(i, found[i]) for i in ids if i in found], [i for i in ids if i not in found]


def load_prompt(path):
    text = Path(path).read_text(encoding="utf-8") if path else os.environ.get("EVAL_SYSTEM_PROMPT", "")
    if len(text) < 1000:
        sys.exit("system prompt missing: set EVAL_SYSTEM_PROMPT or pass --prompt-file")
    return text


def clean(s):
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", s or "")).strip()


def article_block(n, aid, r):
    link = r.get("link") or ""
    host = urlparse(link).netloc.lower()
    host = host[4:] if host.startswith("www.") else host
    return (f"[{n}] id: {aid}\nheadline: {clean(r.get('title'))}\n"
            f"description: {clean(r.get('description'))[:DESC_CHARS] or '(none)'}\n"
            f"source: {r['source']} | host: {host or '?'}\nurl: {link or '?'}")


def user_message(chunk):
    return "Classify each article below.\n\n" + "\n\n".join(
        article_block(n, aid, r) for n, (aid, r) in enumerate(chunk, 1))


def parse(text, chunk_ids, extras=False):
    """{id: row} for the well-formed rows the model returned for this chunk."""
    try:
        data = json.loads(text)
    except (TypeError, ValueError):
        return {}
    rows = data.get("results") if isinstance(data, dict) else data
    out = {}
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict) or row.get("id") not in chunk_ids:
            continue
        if not all(k in row for k in FIELDS):
            continue
        try:
            score = max(0, min(10, int(row["score"])))
        except (TypeError, ValueError):
            continue
        got = {"selected": bool(row["selected"]), "impactful": bool(row["impactful"]),
               "uplifting": bool(row["uplifting"]), "score": score,
               "reason": str(row["reason"])[:300]}
        if extras:
            if not all(k in row for k in EXTRA_FIELDS):
                continue
            got["deciding_filter"] = row["deciding_filter"] if row["deciding_filter"] in DECIDING else "scope"
            got.update({k: str(row[k] or "").strip()[:200] for k in ("person", "org", "location")})
            got["entity_type"] = row["entity_type"] if row["entity_type"] in ENTITY_TYPES else "NONE"
            got["region"] = row["region"] if row["region"] in REGIONS else "OTHER"
        out[row["id"]] = got
    return out


# ---------------------------------------------------------------- results

def read_results(path):
    done, tries = set(), {}
    if path.exists():
        for ln in path.read_text().splitlines():
            r = json.loads(ln)
            if "selected" in r or r.get("final"):
                done.add(r["id"])
            tries[r["id"]] = tries.get(r["id"], 0) + (1 if "miss" in r else 0)
    return done, tries


def append(path, rows):
    OUT.mkdir(parents=True, exist_ok=True)
    with open(path, "a") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


def record(path, chunk_ids, got, model, tries, why):
    """Write parsed rows; a missing id is retried once, then recorded as final."""
    rows = [{"id": i, "model": model, **got[i]} for i in chunk_ids if i in got]
    for i in chunk_ids:
        if i not in got:
            tries[i] = tries.get(i, 0) + 1
            rows.append({"id": i, "model": model, "miss": why, "final": tries[i] >= MAX_TRIES})
    append(path, rows)
    return len([i for i in chunk_ids if i in got])


def todo(items, path):
    done, tries = read_results(path)
    return [it for it in items if it[0] not in done and tries.get(it[0], 0) < MAX_TRIES], tries


# ---------------------------------------------------------------- Claude Haiku (batch)

def run_haiku(items, prompt, budget, name="haiku", extras=False):
    """Results go to out/<name>.jsonl; the spend ledger in haiku_state.json is shared by
    every run, so --budget-usd caps them together."""
    import anthropic
    client = anthropic.Anthropic(max_retries=5)
    res = OUT / f"{name}.jsonl"
    state_path = OUT / "haiku_state.json"
    state = json.loads(state_path.read_text()) if state_path.exists() else {
        "spent_usd": 0.0, "requests_billed": 0, "articles_billed": 0, "config": 0, "batches": []}

    def save():
        OUT.mkdir(parents=True, exist_ok=True)
        state_path.write_text(json.dumps(state, indent=1))

    def collect(b):
        """Poll one batch to the end, record its rows, add its cost to the ledger."""
        while True:
            info = client.messages.batches.retrieve(b["id"])
            if info.processing_status == "ended":
                break
            c = info.request_counts
            print(f"batch {b['n']}: processing {c.processing}, done {c.succeeded + c.errored}")
            time.sleep(30)
        b_res = OUT / f"{b.get('res', 'haiku')}.jsonl"   # each batch writes to its own run's file
        b_ext = b.get("extras", False)
        _, tries = read_results(b_res)
        invalid, ok = 0, 0
        for r in client.messages.batches.results(b["id"]):
            chunk_ids = b["requests"][r.custom_id]
            kind = r.result.type
            if kind == "succeeded":
                msg = r.result.message
                u = msg.usage
                cost = (u.input_tokens * PRICE["input"] + u.output_tokens * PRICE["output"]
                        + (u.cache_creation_input_tokens or 0) * PRICE["cache_write"]
                        + (u.cache_read_input_tokens or 0) * PRICE["cache_read"])
                state["spent_usd"] += cost
                state["requests_billed"] += 1
                state["articles_billed"] += len(chunk_ids)
                if msg.stop_reason == "refusal":
                    got, why = {}, "refusal"
                else:
                    text = next((blk.text for blk in msg.content if blk.type == "text"), "")
                    got, why = parse(text, set(chunk_ids), b_ext), f"unparsed ({msg.stop_reason})"
                ok += record(b_res, chunk_ids, got, HAIKU, tries, why)
            else:
                err = str(getattr(r.result, "error", ""))
                invalid += kind == "errored" and "invalid_request" in err
                record(b_res, chunk_ids, {}, HAIKU, tries, f"{kind}: {err[:200]}")
        b["status"] = "collected"
        save()
        print(f"batch {b['n']}: {ok} articles classified; spent so far ${state['spent_usd']:.4f}")
        return invalid == len(b["requests"])

    def submit(chunk):
        cfg = HAIKU_CONFIGS[state["config"]]
        output_config = {"format": {"type": "json_schema", "schema": make_schema(extras)}}
        if "effort" in cfg:
            output_config["effort"] = cfg["effort"]
        n_batch = len(state["batches"])
        reqs, mapping = [], {}
        for k in range(0, len(chunk), PER_REQUEST):
            part = chunk[k:k + PER_REQUEST]
            cid = f"b{n_batch}-r{k // PER_REQUEST}"
            mapping[cid] = [aid for aid, _ in part]
            reqs.append({"custom_id": cid, "params": {
                "model": HAIKU, "max_tokens": 8000, "thinking": cfg["thinking"],
                "system": [{"type": "text", "text": prompt, "cache_control": {"type": "ephemeral"}}],
                "messages": [{"role": "user", "content": user_message(part)}],
                "output_config": output_config,
            }})
        batch = client.messages.batches.create(requests=reqs)
        b = {"n": n_batch, "id": batch.id, "status": "submitted", "res": name,
             "extras": extras, "requests": mapping}
        state["batches"].append(b)
        save()
        print(f"batch {n_batch}: submitted {len(reqs)} requests ({len(chunk)} articles)")
        return b

    for b in state["batches"]:                    # a retry first finishes what was submitted
        if b["status"] != "collected":
            collect(b)

    while True:
        pending, _ = todo(items, res)
        if not pending:
            print(f"haiku: all done, spent ${state['spent_usd']:.4f}")
            return 0
        room = budget - state["spent_usd"]
        per_article = state["spent_usd"] / state["articles_billed"] if state["articles_billed"] else 0
        if per_article > 0:
            n = int(room / (per_article * 1.3))   # 30% margin under the cap
        else:
            n = 8 * PER_REQUEST                   # small first batch to measure real cost
        if n < PER_REQUEST:
            print(f"haiku: budget reached (${state['spent_usd']:.4f} of ${budget:.2f}); "
                  f"{len(pending)} articles left unclassified")
            return 3
        chunk = pending[:n]
        step = MAX_BATCH_REQUESTS * PER_REQUEST
        new = [submit(chunk[k:k + step]) for k in range(0, len(chunk), step)]
        rejected = [collect(b) for b in new]
        if all(rejected) and not state["articles_billed"]:   # every request rejected: next config
            if state["config"] + 1 >= len(HAIKU_CONFIGS):
                print("haiku: every request was rejected as invalid; stopping")
                return 2
            state["config"] += 1
            save()
            # Rejected rows count as a try; clear them so the articles are re-asked.
            asked = {i for b in new for ids in b["requests"].values() for i in ids}
            keep = [ln for ln in res.read_text().splitlines() if json.loads(ln)["id"] not in asked]
            res.write_text("".join(ln + "\n" for ln in keep))
            print(f"haiku: switching to config {state['config']}")


# ---------------------------------------------------------------- Gemini (free tier)

def run_gemini(items, prompt, key, max_requests):
    import requests
    res = OUT / "gemini.jsonl"
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI}:generateContent"
    schema = gemini_schema(SCHEMA)
    made, fails = 0, 0
    while made < max_requests:
        pending, tries = todo(items, res)
        if not pending:
            print("gemini: all done")
            return 0
        chunk = pending[:PER_REQUEST]
        chunk_ids = [aid for aid, _ in chunk]
        body = {"systemInstruction": {"parts": [{"text": prompt}]},
                "contents": [{"role": "user", "parts": [{"text": user_message(chunk)}]}],
                "generationConfig": {"temperature": 0.1, "responseMimeType": "application/json",
                                     "responseSchema": schema}}
        try:
            r = requests.post(url, params={"key": key}, json=body, timeout=180)
        except requests.RequestException as exc:   # never print exc: the URL carries the key
            r, err = None, type(exc).__name__
        made += 1
        if r is not None and r.status_code == 200:
            fails = 0
            data = r.json()
            cand = (data.get("candidates") or [{}])[0]
            parts = (cand.get("content") or {}).get("parts") or []
            text = "".join(p.get("text", "") for p in parts if not p.get("thought"))
            got = parse(text, set(chunk_ids))
            record(res, chunk_ids, got, GEMINI, tries, f"unparsed ({cand.get('finishReason')})")
            if made % 20 == 0:
                print(f"gemini: {made} requests, {len(pending) - len(got)} articles left")
            time.sleep(GEMINI_PAUSE_S)
            continue
        status = r.status_code if r is not None else err
        detail = r.text if r is not None else ""
        if status == 429 and re.search(r"per ?day|PerDay", detail, re.I):
            print("gemini: daily free quota reached; stopping until it resets")
            return 3
        if isinstance(status, int) and 400 <= status < 500 and status != 429:
            msg = (r.json().get("error") or {}).get("message", "") if r is not None else ""
            print(f"gemini: request rejected (HTTP {status}): {msg[:300]}")
            return 2
        fails += 1
        if fails >= 8:
            print(f"gemini: {fails} failures in a row (last: {status}); giving up this attempt")
            return 1
        m = re.search(r'"retryDelay":\s*"(\d+)', detail)
        wait = int(m.group(1)) + 1 if m else min(180, 10 * 2 ** fails)
        print(f"gemini: {status}, waiting {wait}s")
        time.sleep(wait)
    print(f"gemini: request cap ({max_requests}) reached")
    return 3


# ---------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("mode", choices=["dry-run", "haiku", "gemini"])
    ap.add_argument("--data", default=str(ROOT / "data"))
    ap.add_argument("--ids", default=str(HERE / "sample_ids.txt"),
                    help="article ids to classify (month_ids.txt for the whole window)")
    ap.add_argument("--prompt-file")
    ap.add_argument("--budget-usd", type=float, default=1.0)
    ap.add_argument("--out-name", default="haiku", help="results file stem for the haiku run")
    ap.add_argument("--extras", action="store_true",
                    help="also return deciding_filter, entity type, person, org, location, region")
    ap.add_argument("--entity-prompt-file")
    ap.add_argument("--max-requests", type=int, default=2000)
    args = ap.parse_args()

    items, missing = load_sample(args.data, args.ids)
    print(f"sample: {len(items)} articles found, {len(missing)} missing")
    if not items:
        return 2
    prompt = load_prompt(args.prompt_file)
    if args.extras:
        extra = (Path(args.entity_prompt_file).read_text(encoding="utf-8") if args.entity_prompt_file
                 else os.environ.get("EVAL_ENTITY_PROMPT", ""))
        if len(extra) < 200:
            sys.exit("entity prompt missing: set EVAL_ENTITY_PROMPT or pass --entity-prompt-file")
        prompt = prompt.rstrip() + "\n\n" + extra.strip() + "\n"

    if args.mode == "dry-run":
        reqs = [items[k:k + PER_REQUEST] for k in range(0, len(items), PER_REQUEST)]
        user_tok = sum(len(user_message(c)) for c in reqs) / 4
        sys_tok = len(prompt) / 4 * len(reqs)
        out_tok = len(items) * 70
        est = user_tok * PRICE["input"] + sys_tok * PRICE["input"] + out_tok * PRICE["output"]
        print(f"requests: {len(reqs)} of {PER_REQUEST}; ~{(user_tok + sys_tok) / 1e6:.1f}M input tokens "
              f"(~{sys_tok / 1e6:.1f}M of it system prompt), ~{out_tok / 1e6:.2f}M output")
        print(f"haiku batch cost estimate without caching: ${est:.2f}")
        print("first article block:\n" + article_block(1, *items[0]))
        json.dumps(make_schema(args.extras)), json.dumps(gemini_schema(make_schema(args.extras)))
        return 0
    if args.mode == "haiku":
        return run_haiku(items, prompt, args.budget_usd, args.out_name, args.extras)
    key = os.environ.get("GEMINI_API_KEY", "")
    if not key:
        sys.exit("GEMINI_API_KEY missing")
    return run_gemini(items, prompt, key, args.max_requests)


if __name__ == "__main__":
    sys.exit(main())
