#!/usr/bin/env python3
"""Check every configured source and print a receipt.

This exists so nobody has to take the feed list on faith. Run it and you get the
same table the docs claim, generated from your machine, right now:

    python3 scripts/probe_sources.py
    python3 scripts/probe_sources.py --json > receipt.json

It reports what it actually observed. Reachability is not permission to
republish, and this script cannot tell you anything about the latter.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import feedparser  # noqa: E402
import requests  # noqa: E402

from curator.config import load_config  # noqa: E402


def probe_feed(url: str, ua: str, timeout: float) -> dict:
    started = time.time()
    row: dict = {"url": url}
    try:
        resp = requests.get(url, timeout=timeout, headers={"User-Agent": ua}, allow_redirects=True)
        row["status"] = resp.status_code
        row["final_url"] = resp.url
        row["bytes"] = len(resp.content)
        row["content_type"] = resp.headers.get("Content-Type", "")
        if resp.ok:
            parsed = feedparser.parse(resp.content)
            row["entries"] = len(parsed.entries)
            row["bozo"] = bool(getattr(parsed, "bozo", False))
            # A malformed document that happens to yield entries is not "ok".
            # Reporting it as clean is how a slowly rotting feed stays invisible.
            row["ok"] = len(parsed.entries) > 0 and not row["bozo"]
        else:
            row["entries"] = 0
            row["ok"] = False
    except Exception as exc:
        row.update({"status": None, "entries": 0, "ok": False, "error": type(exc).__name__})
    row["elapsed_s"] = round(time.time() - started, 2)
    return row


def probe_hn(ua: str, timeout: float) -> dict:
    url = "https://hn.algolia.com/api/v1/search?query=test&tags=story&hitsPerPage=1"
    started = time.time()
    try:
        resp = requests.get(url, timeout=timeout, headers={"User-Agent": ua})
        hits = resp.json().get("hits", []) if resp.ok else []
        return {
            "url": url, "status": resp.status_code, "entries": len(hits),
            "ok": resp.ok and bool(hits), "elapsed_s": round(time.time() - started, 2),
        }
    except Exception as exc:
        return {"url": url, "status": None, "entries": 0, "ok": False, "error": type(exc).__name__}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--root", type=Path, default=Path(__file__).resolve().parent.parent)
    ap.add_argument("--json", action="store_true", help="emit a machine-readable receipt")
    args = ap.parse_args()

    cfg = load_config(args.root)
    ua, timeout = cfg.user_agent, cfg.timeout

    rows = [{"id": "hackernews", "name": "Hacker News (Algolia)", "category": "-", **probe_hn(ua, timeout)}]
    # Both files: the shared pool in sources.yaml AND every category's curated
    # feeds in topics.yaml. A probe that only checked one of them would report a
    # healthy feed list while half of it was broken.
    for source in cfg.all_feeds:
        rows.append(
            {
                "id": source.id,
                "name": source.name,
                "category": source.category or "shared",
                **probe_feed(source.url, ua, timeout),
            }
        )

    ok = sum(1 for r in rows if r["ok"])
    receipt = {
        "probed_at": datetime.now(timezone.utc).isoformat(),
        "user_agent": ua,
        "total": len(rows),
        "reachable": ok,
        "sources": rows,
    }

    if args.json:
        print(json.dumps(receipt, indent=2))
        return 0 if ok == len(rows) else 1

    print(f"Probed {len(rows)} sources at {receipt['probed_at']}\n")
    print(f"{'id':<16}{'category':<14}{'status':>7}{'entries':>9}{'bytes':>10}  result")
    print("-" * 76)
    for r in rows:
        mark = "ok" if r["ok"] else (r.get("error") or "FAILED")
        print(
            f"{r['id']:<16}{r.get('category', '-'):<14}{str(r.get('status') or '-'):>7}"
            f"{r.get('entries', 0):>9}{r.get('bytes', 0):>10}  {mark}"
        )
    print("-" * 76)
    print(f"{ok}/{len(rows)} reachable")
    print("\nReachable is not the same as licensed to republish. See sources.yaml.")
    return 0 if ok == len(rows) else 1


if __name__ == "__main__":
    sys.exit(main())
