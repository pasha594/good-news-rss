#!/usr/bin/env python3
"""Tag accumulated articles with a good-news score and topic categories.

Runs after accumulate.py in the cron. Uses the Gemini free tier (project has no
billing attached, so worst case is 429s, never charges). Hard caps per run keep
usage far inside free quotas: MAX_REQUESTS_PER_RUN requests of BATCH articles.
Articles are classified once, keyed by the same id as the dedupe store; results
append to data/tags.jsonl:

  {"id": sha1, "good": 0-10, "cats": [...], "model": str, "at": UTC ISO}

Never fails the workflow: any API/parse problem logs, skips, and exits 0 —
unclassified articles are retried on later runs.
"""

import hashlib
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent
ARTICLES = ROOT / "data" / "articles.jsonl"
TAGS = ROOT / "data" / "tags.jsonl"

MODEL = "gemini-3.5-flash-lite"
BATCH = 50
MAX_REQUESTS_PER_RUN = 15      # 15 * 48 runs/day = 720 requests/day, inside free RPD
SLEEP_BETWEEN = 4              # seconds; stays inside free RPM
DESC_CLIP = 220

CATS = ["world", "politics", "business", "science", "health", "environment",
        "technology", "sports", "arts", "human-interest", "animals",
        "education", "community", "conflict", "disaster"]

PROMPT = (
    "You are tagging news articles for a good-news dashboard.\n"
    "For EACH numbered article below, return one JSON object with:\n"
    '  "i": the article number\n'
    '  "good": integer 0-10 - how much this is genuinely uplifting, positive,'
    " good news. 0 = tragic/grim/negative, 5 = neutral or mixed, 8+ = clearly"
    " uplifting (progress, rescues, breakthroughs, kindness, recovery,"
    " conservation wins). Judge the substance, not the tone: a peaceful"
    " resolution of something bad IS good news; a cheerful ad or stock rally"
    " is not particularly good news.\n"
    f'  "cats": 1-3 categories from exactly this list: {", ".join(CATS)}\n'
    "Return ONLY a JSON array with one object per article, no other text.\n\n"
)


def gemini_key():
    k = os.environ.get("GEMINI_API_KEY", "").strip()
    if k:
        return k
    f = ROOT / ".gemini_key"
    return f.read_text().strip() if f.exists() else ""


def article_id(source, title, link):
    key = source + "|" + (link or title)
    return hashlib.sha1(key.encode("utf-8", "replace")).hexdigest()


def classify_batch(key, batch):
    lines = []
    for n, (_aid, row) in enumerate(batch, 1):
        desc = (row.get("description") or "")[:DESC_CLIP]
        lines.append(f"{n}. {row['title']} | {desc}")
    body = {
        "contents": [{"parts": [{"text": PROMPT + "\n".join(lines)}]}],
        "generationConfig": {"temperature": 0.1,
                             "responseMimeType": "application/json"},
    }
    r = requests.post(
        f"https://generativelanguage.googleapis.com/v1beta/models/{MODEL}:generateContent",
        params={"key": key}, json=body, timeout=90)
    if r.status_code == 429:
        return "rate_limited"
    r.raise_for_status()
    text = r.json()["candidates"][0]["content"]["parts"][0]["text"]
    results = json.loads(text)

    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    out = []
    for item in results:
        try:
            idx = int(item["i"]) - 1
            good = max(0, min(10, int(item["good"])))
            cats = [c for c in item.get("cats", []) if c in CATS][:3]
        except (KeyError, ValueError, TypeError):
            continue
        if 0 <= idx < len(batch):
            out.append({"id": batch[idx][0], "good": good, "cats": cats,
                        "model": MODEL, "at": now})
    return out


def main():
    key = gemini_key()
    if not key:
        print("classify: no GEMINI_API_KEY; skipping")
        return

    done = set()
    if TAGS.exists():
        for line in TAGS.open(encoding="utf-8"):
            try:
                done.add(json.loads(line)["id"])
            except (json.JSONDecodeError, KeyError):
                pass

    pending = []
    for line in ARTICLES.open(encoding="utf-8"):
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        aid = article_id(row["source"], row["title"], row.get("link", ""))
        if aid not in done:
            pending.append((aid, row))
            done.add(aid)  # guard against duplicate rows in the store

    # newest first so the dashboard fills with current articles before backlog
    pending.sort(key=lambda p: p[1].get("first_seen", ""), reverse=True)
    print(f"classify: {len(pending)} unclassified articles")
    if not pending:
        return

    tagged = 0
    with TAGS.open("a", encoding="utf-8") as out:
        for req_n in range(MAX_REQUESTS_PER_RUN):
            batch = pending[req_n * BATCH:(req_n + 1) * BATCH]
            if not batch:
                break
            try:
                result = classify_batch(key, batch)
            except Exception as exc:
                print(f"classify: request failed ({type(exc).__name__}); "
                      "stopping this run")
                break
            if result == "rate_limited":
                print("classify: 429 rate limited; stopping this run")
                break
            for row in result:
                out.write(json.dumps(row, ensure_ascii=False) + "\n")
            tagged += len(result)
            time.sleep(SLEEP_BETWEEN)

    print(f"classify: +{tagged} tagged this run; "
          f"{len(pending) - tagged} still pending")


if __name__ == "__main__":
    main()
