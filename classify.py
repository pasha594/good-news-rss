#!/usr/bin/env python3
"""Gate-then-enrich classifier (v3) for the good-news pipeline.

Phase GATE (every new article, batched 50/call): impactful, uplifting,
good 0-10, on_mission, cats. Phase ENRICH (only articles passing
impactful AND uplifting, batched 20/call): tolerant virtues, humanitarian
topics, location names, entities.

Quota posture: Gemini free tier, billing never attached ($0 hard cap).
The budget self-paces against the daily quota: each run estimates the
calls already made today (distinct batch timestamps in the tag shards;
"today" = the Pacific calendar day, Gemini's quota reset) and spends at
most MAX_REQUESTS_PER_RUN of what remains under DAILY_QUOTA. Runs are
scheduled every 30 min but GitHub throttles cron unpredictably (observed:
48/day scheduled, 2-8/day delivered), so a fixed per-run budget is wrong
in both directions - self-pacing keeps daily spend at ~DAILY_QUOTA no
matter how many runs actually fire. Within a run, gate goes first
(newest articles), enrichment consumes what remains past its reserve.

Model fallback: the free tier sheds load with HTTP 503 "high demand" on
the primary model for hours at a time (Nov 2026: 18% of calls one day,
100% the next), while other free models stay up. Each batch therefore
walks MODELS in order; free quotas are per model, so every model is paced
against its own daily cap. A model that fails twice in a row cools down
and drops to the back of the chain for a few minutes; a 429 retires it
for the run. Gate rows record the model that produced them in "model",
enrichment rows in "enrich_model", and fail markers in "model".

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
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
from urllib.parse import urlparse

import store

ROOT = Path(__file__).resolve().parent

MODEL = "gemini-3.5-flash-lite"   # primary; earlier rows all carry it
# Fallbacks in order. gemini-3.1-flash-lite agreed with the primary on 93%
# of good>=7 calls in a 100-article spot check (2026-11-05).
MODELS = [m.strip() for m in os.environ.get(
    "CLASSIFY_MODELS", f"{MODEL},gemini-3.1-flash-lite").split(",") if m.strip()]
GATE_BATCH = 50
ENRICH_BATCH = 20
MAX_REQUESTS_PER_RUN = int(os.environ.get("CLASSIFY_MAX_REQUESTS", "100"))
DAILY_QUOTA = int(os.environ.get("CLASSIFY_DAILY_QUOTA", "900"))   # primary
FALLBACK_DAILY_QUOTA = int(os.environ.get("CLASSIFY_FALLBACK_DAILY_QUOTA", "500"))
SLEEP_BETWEEN = 4              # seconds; stays inside free RPM
FAIL_SLEEP = 15                # back off harder after a batch failed on
                               # every model; 503/timeout storms at peak
MAX_FAIL_STREAK = 4            # consecutive batches that failed everywhere
MODEL_FAIL_STREAK = 2          # consecutive failures before a model cools
MODEL_COOLDOWN_S = 180
DEADLINE_S = int(os.environ.get("CLASSIFY_DEADLINE_S", "1200"))
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


def call_model(key, prompt_text, model=MODEL):
    """One generateContent call. Returns parsed JSON list or 'rate_limited'."""
    body = {
        "contents": [{"parts": [{"text": prompt_text}]}],
        "generationConfig": {"temperature": 0.1,
                             "responseMimeType": "application/json"},
    }
    r = requests.post(
        f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
        params={"key": key}, json=body, timeout=75)
    if r.status_code == 429:
        return "rate_limited"
    r.raise_for_status()
    return json.loads(r.json()["candidates"][0]["content"]["parts"][0]["text"])


def describe(exc):
    """Short failure label. Never str(exc): requests embeds the URL, and the
    URL carries the API key."""
    resp = getattr(exc, "response", None)
    if resp is not None:
        return f"HTTP {resp.status_code}"
    return type(exc).__name__


class AllModelsFailed(Exception):
    pass


class ModelPool:
    """Walks MODELS per batch, pacing each against its own daily cap."""

    def __init__(self, models, remaining, budget, out):
        self.models = models
        self.remaining = remaining        # calls left today, per model
        self.budget = budget              # calls left this run, all models
        self.calls = 0
        self.out = out
        self.streak = {m: 0 for m in models}
        self.cool_until = {m: 0.0 for m in models}
        self.retired = set()              # 429: quota gone for this run
        self.used = {}

    def usable(self):
        return [m for m in self.models
                if m not in self.retired and self.remaining.get(m, 0) > 0]

    def call(self, key, prompt, phase, limit=None):
        """Returns (model, parsed result). Warm models first, cooling ones
        only if every warm one fails. Attempts stop once the run has made
        `limit` calls (the gate phase's share). Raises AllModelsFailed."""
        cap = self.budget if limit is None else min(self.budget, limit)
        now = time.monotonic()
        usable = self.usable()
        order = ([m for m in usable if self.cool_until[m] <= now]
                 + [m for m in usable if self.cool_until[m] > now])
        errors = []
        for m in order:
            if self.calls >= cap:
                break
            self.calls += 1
            self.remaining[m] -= 1
            try:
                result = call_model(key, prompt, m)
            except Exception as exc:
                # A failed attempt may still consume server-side quota, and
                # writes no batch rows the pacer could count - record it as
                # an id-less marker row (invisible to every fold/reader).
                self.out.write(json.dumps(
                    {"spend": "fail", "model": m, "at": now_iso()},
                    ensure_ascii=False) + "\n")
                errors.append(f"{m}: {describe(exc)}")
                self.streak[m] += 1
                if self.streak[m] >= MODEL_FAIL_STREAK:
                    self.streak[m] = 0
                    self.cool_until[m] = time.monotonic() + MODEL_COOLDOWN_S
                continue
            if result == "rate_limited":
                errors.append(f"{m}: HTTP 429")
                self.retired.add(m)
                continue
            self.streak[m] = 0
            self.cool_until[m] = 0.0
            self.used[(phase, m)] = self.used.get((phase, m), 0) + 1
            return m, result
        raise AllModelsFailed("; ".join(errors) or "no model left")


def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def quota_day_start():
    """Epoch of the start of the current Pacific calendar day - Gemini's
    free-tier daily quota resets at midnight PT."""
    try:
        from zoneinfo import ZoneInfo
        tz = ZoneInfo("America/Los_Angeles")
    except Exception:              # no tzdata; fixed PST is close enough
        tz = timezone(timedelta(hours=-8))
    now = datetime.now(tz)
    return datetime(now.year, now.month, now.day, tzinfo=tz).timestamp()


def gate_batch(pool, key, batch, limit):
    lines = [article_line(n, row) for n, (_aid, row) in enumerate(batch, 1)]
    model, results = pool.call(key, GATE_PROMPT + "\n".join(lines), "gate", limit)
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
                   "model": model, "at": at}
        except (KeyError, ValueError, TypeError, IndexError, AttributeError):
            continue
        if 0 <= idx < len(batch):
            out.append(row)
    return out


def enrich_batch(pool, key, batch):
    lines = [article_line(n, row) for n, (_aid, row) in enumerate(batch, 1)]
    model, results = pool.call(key, ENRICH_PROMPT + "\n".join(lines), "enrich")
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
                   "enrich_model": model,
                   "enriched_at": at}
        except (KeyError, ValueError, TypeError, IndexError, AttributeError):
            continue
        if 0 <= idx < len(batch):
            out.append(row)
    return out


def main():
    key = gemini_key()
    if not key:
        print("classify: no GEMINI_API_KEY; skipping")
        return

    # Only articles young enough to still display are worth classifying.
    floor = time.time() - MAX_AGE_DAYS * 86400

    # Fold tag rows by id (enrichment rows update gate rows). A tag is always
    # written at-or-after its article's first_seen, so tags for in-window
    # articles only live in shards whose week overlaps the window - the fold
    # stays bounded as history grows.
    folded = {}
    # Every successful API call writes one batch of rows sharing one
    # timestamp (calls are SLEEP_BETWEEN apart, so timestamps are distinct
    # per call). Counting today's distinct batch timestamps therefore
    # estimates today's quota spend - no extra state file needed. Skip rows
    # (model="rule") cost nothing and are excluded; failed calls write no
    # rows, which undercounts - DAILY_QUOTA sits below the real quota to
    # absorb that. Spend is tracked per model (free quotas are per model);
    # rows from before the fallback chain carry no enrich_model / fail
    # model and count against the primary.
    day_start = quota_day_start()
    spent = {m: set() for m in MODELS}
    recent_tag_shards = [p for p in store.shards("tags")
                         if store.week_start_epoch(store.shard_key(p, "tags"))
                         + 7 * 86400 >= floor]
    for t in store.read_jsonl(recent_tag_shards):
        if "id" in t:
            folded.setdefault(t["id"], {}).update(t)
        if t.get("spend") == "fail":
            m = t.get("model", MODEL)
            if m in spent and store.parse_iso(t.get("at")) >= day_start:
                spent[m].add(("fail", t["at"]))
            continue
        if t.get("model") in spent and store.parse_iso(t.get("at")) >= day_start:
            spent[t["model"]].add(("gate", t["at"]))
        if store.parse_iso(t.get("enriched_at")) >= day_start:
            m = t.get("enrich_model", MODEL)
            if m in spent:
                spent[m].add(("enrich", t["enriched_at"]))
    recent_shards = [p for p in store.shards("articles")
                     if store.week_start_epoch(store.shard_key(p, "articles"))
                     + 7 * 86400 >= floor]
    articles = {}
    for row in store.read_jsonl(recent_shards):
        if store.parse_iso(row.get("first_seen")) >= floor:
            aid = store.article_id(row["source"], row["title"], row.get("link", ""))
            articles.setdefault(aid, row)

    # An article counts as gated only if a GATE row exists ("good" present) -
    # an orphaned enrichment row alone must not block re-gating.
    pending_gate = [(aid, row) for aid, row in articles.items()
                    if "good" not in folded.get(aid, {})]
    pending_gate.sort(key=lambda p: p[1].get("first_seen", ""), reverse=True)

    # Enrichment pending: v3 gate survivors without enrichment fields yet.
    def survivor(f):
        return f.get("v") == 3 and f.get("impactful") and f.get("uplifting")
    pending_enrich = [(aid, articles[aid]) for aid, f in folded.items()
                      if survivor(f) and "virtues" not in f and aid in articles]
    pending_enrich.sort(key=lambda p: p[1].get("first_seen", ""), reverse=True)

    caps = {m: DAILY_QUOTA if m == MODEL else FALLBACK_DAILY_QUOTA
            for m in MODELS}
    remaining = {m: max(0, caps[m] - len(spent[m])) for m in MODELS}
    spend_note = ", ".join(f"{m} ~{len(spent[m])}/{caps[m]}" for m in MODELS)
    budget = min(MAX_REQUESTS_PER_RUN, sum(remaining.values()))
    if budget == 0:
        print(f"classify: daily caps reached ({spend_note}); skipping run")
        return
    print(f"classify: budget {budget} calls (spent this quota day: {spend_note})")
    started = time.monotonic()
    # Reserve part of the budget for enrichment so a large gate backlog can
    # never starve it; unused reservation is returned to the gate phase.
    enrich_need = -(-len(pending_enrich) // ENRICH_BATCH)  # ceil
    enrich_reserve = min(budget // 3, enrich_need)
    gate_budget = budget - enrich_reserve
    # Reserve wall-clock for enrichment, not just calls: a backlogged gate
    # phase otherwise eats the whole deadline and enrichment never runs
    # (seen live: 52 gate calls, enriched +0). ~24s covers a call + sleep.
    gate_deadline = DEADLINE_S - min(DEADLINE_S // 3, 24 * enrich_reserve)
    gated = enriched = skipped = failures = 0
    gate_done = enrich_done = 0    # articles in batches that came back
    wk_path = store.shard_path("tags", store.week_key())
    with wk_path.open("a", encoding="utf-8") as out:
        pool = ModelPool(MODELS, remaining, budget, out)
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
        while (pool.calls < gate_budget and pool.usable()
               and gi * GATE_BATCH < len(pending_gate)
               and time.monotonic() - started < gate_deadline):
            batch = pending_gate[gi * GATE_BATCH:(gi + 1) * GATE_BATCH]
            gi += 1
            try:
                result = gate_batch(pool, key, batch, gate_budget)
            except AllModelsFailed as exc:
                failures += 1
                print(f"classify: gate batch failed on every model ({exc})")
                if failures >= MAX_FAIL_STREAK:
                    break
                time.sleep(FAIL_SLEEP)
                continue
            except Exception as exc:     # parse error etc.; the call succeeded
                failures += 1
                print(f"classify: gate batch unusable ({describe(exc)})")
                if failures >= MAX_FAIL_STREAK:
                    break
                time.sleep(FAIL_SLEEP)
                continue
            failures = 0
            by_id = {r["id"]: r for r in result}
            for r in result:
                out.write(json.dumps(r, ensure_ascii=False) + "\n")
            gated += len(result)
            gate_done += len(batch)
            for aid, row in batch:  # survivors join this run's enrich queue
                g = by_id.get(aid)
                if g and g["impactful"] and g["uplifting"]:
                    pending_enrich.append((aid, row))
            time.sleep(SLEEP_BETWEEN)

        # Phase ENRICH with whatever budget remains - its own failure streak.
        failures = 0
        ei = 0
        while (pool.calls < budget and pool.usable()
               and ei * ENRICH_BATCH < len(pending_enrich)
               and time.monotonic() - started < DEADLINE_S):
            batch = pending_enrich[ei * ENRICH_BATCH:(ei + 1) * ENRICH_BATCH]
            ei += 1
            try:
                result = enrich_batch(pool, key, batch)
            except AllModelsFailed as exc:
                failures += 1
                print(f"classify: enrich batch failed on every model ({exc})")
                if failures >= MAX_FAIL_STREAK:
                    break
                time.sleep(FAIL_SLEEP)
                continue
            except Exception as exc:
                failures += 1
                print(f"classify: enrich batch unusable ({describe(exc)})")
                if failures >= MAX_FAIL_STREAK:
                    break
                time.sleep(FAIL_SLEEP)
                continue
            failures = 0
            for r in result:
                out.write(json.dumps(r, ensure_ascii=False) + "\n")
            enriched += len(result)
            enrich_done += len(batch)
            time.sleep(SLEEP_BETWEEN)

    # Failed batches stay pending (retried next run), so count only the
    # batches that came back.
    left_gate = max(0, len(pending_gate) - gate_done)
    left_enrich = max(0, len(pending_enrich) - enrich_done)
    print(f"classify: gated +{gated} (skipped {skipped} state-outlet), "
          f"enriched +{enriched}; pending gate {left_gate}, "
          f"pending enrich {left_enrich}")
    if pool.used or pool.calls:
        used = ", ".join(f"{p} {m} x{n}" for (p, m), n in sorted(pool.used.items()))
        print(f"classify: {pool.calls} calls made; succeeded: {used or 'none'}")
    if any(m != MODEL for (_p, m) in pool.used):
        print("::warning::classify fell back from "
              f"{MODEL} this run ({used})")
    if left_gate > 5000:
        print(f"::warning::classify gate backlog at {left_gate} - budget may "
              "be saturating; articles age out of classification at "
              f"{MAX_AGE_DAYS} days")


if __name__ == "__main__":
    main()
