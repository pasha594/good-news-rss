#!/usr/bin/env python3
"""Shared storage helpers: the weekly-sharded JSONL data store.

The three growing files are sharded by ISO week so no single file ever
approaches GitHub's 100MB per-file push limit (~4MB/day of articles would
cross it in under a month as a single file):

  data/articles-<YYYY-Wnn>.jsonl  article rows, sharded by first_seen
  data/tags-<YYYY-Wnn>.jsonl      classifier tags, sharded by classification time
  data/seen_ids-<YYYY-Wnn>.txt    dedupe ids, sharded alongside their articles

A shard freezes once its week passes; only the current week's files grow.
Article ids are content hashes, so no reader cares which shard a row is in.
"""

import calendar
import hashlib
import json
import time
from datetime import datetime, timezone
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent / "data"


def article_id(source, title, link):
    key = source + "|" + (link or title)
    return hashlib.sha1(key.encode("utf-8", "replace")).hexdigest()


def parse_iso(s):
    if not s:
        return 0
    try:
        return calendar.timegm(time.strptime(s, "%Y-%m-%dT%H:%M:%SZ"))
    except Exception:
        return 0


def week_key(epoch=None):
    """ISO week label like '2026-W34' for a UTC epoch (default: now)."""
    dt = (datetime.fromtimestamp(epoch, tz=timezone.utc) if epoch
          else datetime.now(timezone.utc))
    y, w, _ = dt.isocalendar()
    return f"{y}-W{w:02d}"


def week_start_epoch(key):
    """UTC epoch of Monday 00:00 for a 'YYYY-Wnn' shard key."""
    y, w = key.split("-W")
    return calendar.timegm(datetime.fromisocalendar(int(y), int(w), 1).timetuple())


def shards(prefix, suffix=".jsonl"):
    """All existing shard paths for a prefix, oldest first."""
    return sorted(DATA_DIR.glob(f"{prefix}-*-W*{suffix}"))


def shard_key(path, prefix):
    """'2026-W34' from a shard path."""
    return path.name[len(prefix) + 1:].rsplit(".", 1)[0]


def shard_path(prefix, key, suffix=".jsonl"):
    return DATA_DIR / f"{prefix}-{key}{suffix}"


def read_jsonl(paths):
    """Yield parsed rows from JSONL files, skipping malformed lines."""
    for p in paths:
        with p.open(encoding="utf-8") as f:
            for line in f:
                try:
                    yield json.loads(line)
                except json.JSONDecodeError:
                    pass
