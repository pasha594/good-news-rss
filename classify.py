#!/usr/bin/env python3
"""Gate-then-enrich classifier (v3) for the good-news pipeline.

Phase GATE (every new article, batched 50/call): impactful, uplifting,
good 0-10, on_mission, cats. Phase ENRICH (only articles passing
impactful AND uplifting, batched 20/call): tolerant virtues, humanitarian
topics, location names, entities.

Quota posture: Gemini free tier, billing never attached ($0 hard cap).
A shared per-run budget of MAX_REQUESTS_PER_RUN calls covers both phases;
gate runs first (newest articles), enrichment consumes what remains.
Steady state is ~150 calls/day against a ~1,000/day free quota.

Rows append to the current week's tags shard; enrichment is a SECOND row
for the same id and readers fold rows by id, so a crash or 429 between
phases never loses work. Forward-only: v2 rows count as gated (never
re-gated, never enriched); only articles <= MAX_AGE_DAYS old are
classified at all - older ones never display.

Never fails the workflow: quota/API/parse problems log, skip, exit 0.
"""

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
from urllib.parse import urlparse

import store

ROOT = Path(__file__).resolve().parent

MODEL = "gemini-3.5-flash-lite"
GATE_BATCH = 50
ENRICH_BATCH = 20
MAX_REQUESTS_PER_RUN = int(os.environ.get("CLASSIFY_MAX_REQUESTS", "15"))
SLEEP_BETWEEN = 4              # seconds; stays inside free RPM
DESC_CLIP = 220
MAX_AGE_DAYS = 8               # never classify articles too old to display

# Rejected in code, not by the LLM: state propaganda outlets whose picks a
# curator could never endorse. Zero quota spent.
STATE_OUTLETS = {"Iran PressTV", "Syrian Arab News Agency (EN)"}

CATS = ["world", "politics", "business", "science", "health", "environment",
        "technology", "sports", "arts", "human-interest", "animals",
        "education", "community", "conflict", "disaster"]

VIRTUES = ["community_engagement", "reconciliation", "environmental_stewardship",
           "diplomacy", "innovation_for_good", "identity", "respect_and_civility",
           "equity_diversity_inclusion", "solidarity"]

TOPICS = ["civil_rights", "anti_discrimination", "holocaust_remembrance",
          "genocide_awareness", "hate_crimes", "religious_freedom", "refugees",
          "women_and_girls", "anti_bullying", "philanthropy", "heroism",
          "education", "health_and_medicine"]

GATE_PROMPT = (
    "You score news articles for a public good-news display. Visitors see"
    " only the HEADLINE on a wall panel, so judge the headline as a"
    " STANDALONE artifact. Each line below starts with [source | domain].\n"
    "For EACH numbered article return one JSON object with:\n"
    '  "i": the article number\n'
    '  "impactful": boolean - the headline is a complete thought about a'
    " real action or event. false for: event/conference announcements and"
    " press releases; business, financial, markets, or earnings news;"
    " fragments too ambiguous to convey a complete thought"
    " ('GreenBiz: Spencer Meyer').\n"
    '  "uplifting": boolean - the BARE headline leaves a reader hopeful or'
    " moved by human goodness (kindness, heroism, communities helped, real"
    " achievements, resilience, solutions). false when the headline carries"
    " a negative anchor even if the story resolves well: 'wins payout after"
    " [wrong]', 'X amid [violence/concerns/crisis]', 'rebuilds/recovers"
    " after [disaster/attack]', 'couldn't afford X', 'trial/investigation"
    " opens', 'spied on/harassed/targeted', institutional names built on"
    " suffering ('Home for War Wounded'). Test: if the bare headline could"
    " put a 5th-grader in a worse mood, uplifting=false.\n"
    '  "good": integer 0-10, overall display-worthiness. 8-10 = clearly'
    " uplifting and substantive; 7 = solid with minor reservations; 4-6 ="
    " neutral, mixed, or positive-but-flawed; 0-3 = negative or excluded."
    " HARD RULE: any story FRAMED by these active conflicts scores 0-2 with"
    " uplifting=false, no matter how humanitarian the angle:"
    " Israel/Palestine/Gaza/West Bank/Lebanon/Syria; Russia-Ukraine war"
    " coverage; Sudan, Myanmar, Yemen, eastern DRC, Tigray; acute"
    " India-Pakistan strike cycles. Aid INTO those regions counts as"
    " conflict-framed. Non-war cultural, scientific, or sports stories from"
    " those countries are NOT excluded. Aggregator/scraper domains,"
    " promotional or affiliate content, and fringe sources (conspiracy,"
    " pseudoscience) cap good at 4 even when the story is real.\n"
    '  "on_mission": boolean - fits a tolerance/civil-rights museum exhibit'
    " (confronting prejudice, civil rights, remembrance, refugees,"
    " interfaith respect, equity) OR is unambiguous uplifting human-goodness"
    " (heroism, philanthropy, concrete environmental wins, science with a"
    " named benefit, personal triumph, education, civic engagement). Sports"
    " recaps, celebrity news, and business news are NOT on_mission.\n"
    f'  "cats": 1-3 categories from exactly this list: {", ".join(CATS)}\n'
    "When in doubt use false and score lower - a false positive erodes"
    " trust in the display more than a false negative.\n"
    "Return ONLY a JSON array, one object per article, no other text.\n\n"
)

ENRICH_PROMPT = (
    "You enrich selected good-news articles for a museum-style display."
    " For EACH numbered article return one JSON object with:\n"
    '  "i": the article number\n'
    '  "virtues": 0-3 that the story exemplifies, from exactly:'
    " community_engagement (communities improving lives or the environment,"
    " volunteering), reconciliation (righting historical wrongs, rising"
    " above past conflict), environmental_stewardship (protecting or"
    " restoring the environment), diplomacy (non-violent solutions,"
    " peace-making), innovation_for_good (discoveries and inventions that"
    " benefit the world), identity (respect for customs, cultures,"
    " traditions), respect_and_civility (courtesy, dignity, supportive"
    " interaction), equity_diversity_inclusion (fairness, representation,"
    " belonging), solidarity (allyship, advocating for marginalized"
    " groups). Empty list if none.\n"
    '  "topics": 0-3 humanitarian subject areas, from exactly:'
    f' {", ".join(TOPICS)}. Empty list if none.\n'
    '  "location": {"present": bool, "city": str, "country": str} - the'
    " story's single clear geographic anchor. present=false with empty"
    " strings when there is no specific place, the place is ambiguous from"
    " headline+description, or it is a multi-country roundup. NEVER guess"
    " from the publisher's country. Do not invent a location.\n"
    '  "entities": up to 4 objects {"name": str, "type": "person" or'
    ' "organization"} for the people/orgs the story is CENTERED on -'
    " caption-ready canonical names, no honorifics or titles. Skip passing"
    " mentions, quoted sources, and the publisher. [] for roundups or"
    " stories without a named subject.\n"
    "Return ONLY a JSON array, one object per article, no other text.\n\n"
)


def gemini_key():
    k = os.environ.get("GEMINI_API_KEY", "").strip()
    if k:
        return k
    f = ROOT / ".gemini_key"
    return f.read_text().strip() if f.exists() else ""


def article_line(n, row):
    desc = (row.get("description") or "")[:DESC_CLIP]
    try:
        domain = urlparse(row.get("link") or "").netloc or "?"
    except ValueError:
        domain = "?"
    return f"{n}. [{row['source']} | {domain}] {row['title']} | {desc}"


def call_model(key, prompt_text):
    """One generateContent call. Returns parsed JSON list or 'rate_limited'."""
    body = {
        "contents": [{"parts": [{"text": prompt_text}]}],
        "generationConfig": {"temperature": 0.1,
                             "responseMimeType": "application/json"},
    }
    r = requests.post(
        f"https://generativelanguage.googleapis.com/v1beta/models/{MODEL}:generateContent",
        params={"key": key}, json=body, timeout=90)
    if r.status_code == 429:
        return "rate_limited"
    r.raise_for_status()
    return json.loads(r.json()["candidates"][0]["content"]["parts"][0]["text"])


def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def gate_batch(key, batch):
    lines = [article_line(n, row) for n, (_aid, row) in enumerate(batch, 1)]
    results = call_model(key, GATE_PROMPT + "\n".join(lines))
    if results == "rate_limited":
        return "rate_limited"
    out, at = [], now_iso()
    for item in results:
        try:
            idx = int(item["i"]) - 1
            good = max(0, min(10, int(item["good"])))
            row = {"id": batch[idx][0], "v": 3,
                   "impactful": bool(item["impactful"]),
                   "uplifting": bool(item["uplifting"]),
                   "on_mission": bool(item.get("on_mission", False)),
                   "good": good,
                   "cats": [c for c in item.get("cats", []) if c in CATS][:3],
                   "model": MODEL, "at": at}
        except (KeyError, ValueError, TypeError, IndexError):
            continue
        if 0 <= idx < len(batch):
            out.append(row)
    return out


def enrich_batch(key, batch):
    lines = [article_line(n, row) for n, (_aid, row) in enumerate(batch, 1)]
    results = call_model(key, ENRICH_PROMPT + "\n".join(lines))
    if results == "rate_limited":
        return "rate_limited"
    out, at = [], now_iso()
    for item in results:
        try:
            idx = int(item["i"]) - 1
            loc = item.get("location") or {}
            ents = []
            for e in (item.get("entities") or [])[:6]:
                name = str(e.get("name", "")).strip()
                etype = e.get("type")
                if name and etype in ("person", "organization"):
                    ents.append({"name": name, "type": etype})
            row = {"id": batch[idx][0],
                   "virtues": [v for v in item.get("virtues", []) if v in VIRTUES][:3],
                   "topics": [t for t in item.get("topics", []) if t in TOPICS][:3],
                   "location": {"present": bool(loc.get("present")),
                                "city": str(loc.get("city", ""))[:80],
                                "country": str(loc.get("country", ""))[:80]},
                   "entities": ents,
                   "enriched_at": at}
        except (KeyError, ValueError, TypeError, IndexError):
            continue
        if 0 <= idx < len(batch):
            out.append(row)
    return out


def main():
    key = gemini_key()
    if not key:
        print("classify: no GEMINI_API_KEY; skipping")
        return

    # Fold all tag rows by id: enrichment rows update gate rows.
    folded = {}
    for t in store.read_jsonl(store.shards("tags")):
        if "id" in t:
            folded.setdefault(t["id"], {}).update(t)

    # Only articles young enough to still display are worth classifying.
    floor = time.time() - MAX_AGE_DAYS * 86400
    recent_shards = [p for p in store.shards("articles")
                     if store.week_start_epoch(store.shard_key(p, "articles"))
                     + 7 * 86400 >= floor]
    articles = {}
    for row in store.read_jsonl(recent_shards):
        if store.parse_iso(row.get("first_seen")) >= floor:
            aid = store.article_id(row["source"], row["title"], row.get("link", ""))
            articles.setdefault(aid, row)

    pending_gate = [(aid, row) for aid, row in articles.items() if aid not in folded]
    pending_gate.sort(key=lambda p: p[1].get("first_seen", ""), reverse=True)

    # Enrichment pending: v3 gate survivors without enrichment fields yet.
    def survivor(f):
        return f.get("v") == 3 and f.get("impactful") and f.get("uplifting")
    pending_enrich = [(aid, articles[aid]) for aid, f in folded.items()
                      if survivor(f) and "virtues" not in f and aid in articles]
    pending_enrich.sort(key=lambda p: p[1].get("first_seen", ""), reverse=True)

    budget = MAX_REQUESTS_PER_RUN
    # Reserve part of the budget for enrichment so a large gate backlog can
    # never starve it; unused reservation is returned to the gate phase.
    enrich_need = -(-len(pending_enrich) // ENRICH_BATCH)  # ceil
    enrich_reserve = min(budget // 3, enrich_need)
    gate_budget = budget - enrich_reserve
    gated = enriched = skipped = failures = 0
    wk_path = store.shard_path("tags", store.week_key())
    with wk_path.open("a", encoding="utf-8") as out:
        # Zero-cost code rejections first.
        for aid, row in list(pending_gate):
            if row["source"] in STATE_OUTLETS:
                out.write(json.dumps(
                    {"id": aid, "v": 3, "impactful": False, "uplifting": False,
                     "on_mission": False, "good": 0, "cats": ["world"],
                     "skip": "state_outlet", "model": "rule", "at": now_iso()},
                    ensure_ascii=False) + "\n")
                pending_gate.remove((aid, row))
                skipped += 1

        # Phase GATE, newest first.
        gi = 0
        while gate_budget > 0 and gi * GATE_BATCH < len(pending_gate):
            batch = pending_gate[gi * GATE_BATCH:(gi + 1) * GATE_BATCH]
            gi += 1
            budget -= 1
            gate_budget -= 1
            try:
                result = gate_batch(key, batch)
            except Exception as exc:
                failures += 1
                print(f"classify: gate batch failed ({type(exc).__name__})")
                if failures >= 3:
                    break
                time.sleep(SLEEP_BETWEEN)
                continue
            if result == "rate_limited":
                print("classify: 429 rate limited; stopping")
                budget = 0
                break
            failures = 0
            by_id = {r["id"]: r for r in result}
            for r in result:
                out.write(json.dumps(r, ensure_ascii=False) + "\n")
            gated += len(result)
            for aid, row in batch:  # survivors join this run's enrich queue
                g = by_id.get(aid)
                if g and g["impactful"] and g["uplifting"]:
                    pending_enrich.append((aid, row))
            time.sleep(SLEEP_BETWEEN)

        # Phase ENRICH with whatever budget remains.
        ei = 0
        while budget > 0 and ei * ENRICH_BATCH < len(pending_enrich):
            batch = pending_enrich[ei * ENRICH_BATCH:(ei + 1) * ENRICH_BATCH]
            ei += 1
            budget -= 1
            try:
                result = enrich_batch(key, batch)
            except Exception as exc:
                failures += 1
                print(f"classify: enrich batch failed ({type(exc).__name__})")
                if failures >= 3:
                    break
                time.sleep(SLEEP_BETWEEN)
                continue
            if result == "rate_limited":
                print("classify: 429 rate limited; stopping")
                break
            failures = 0
            for r in result:
                out.write(json.dumps(r, ensure_ascii=False) + "\n")
            enriched += len(result)
            time.sleep(SLEEP_BETWEEN)

    print(f"classify: gated +{gated} (skipped {skipped} state-outlet), "
          f"enriched +{enriched}; "
          f"pending gate {max(0, len(pending_gate) - gi * GATE_BATCH)}, "
          f"pending enrich {max(0, len(pending_enrich) - ei * ENRICH_BATCH)}")


if __name__ == "__main__":
    main()
