#!/usr/bin/env python3
"""Verify a built public artifact against an already-isolated local database."""
from __future__ import annotations

import argparse
import copy
import json
import re
import subprocess
from datetime import datetime, timedelta
from pathlib import Path


def literal(value: object) -> str:
    return json.dumps(value, ensure_ascii=False).replace("'", "''")


def text_literal(value: str) -> str:
    return value.replace("'", "''")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", required=True)
    parser.add_argument("--artifact", type=Path, required=True)
    parser.add_argument("--host", default="/tmp")
    parser.add_argument("--category-regression-only", action="store_true")
    args = parser.parse_args()
    rows = json.loads(args.artifact.read_text(encoding="utf-8"))["rows"]
    english = next(re.search(r"[A-Za-z]{4,}", row["title"]).group(0) for row in rows if re.search(r"[A-Za-z]{4,}", row["title"]))
    cjk = next(re.search(r"[\u4e00-\u9fff]{2,}", row["title"]).group(0) for row in rows if re.search(r"[\u4e00-\u9fff]{2,}", row["title"]))
    # Controlled protocol transformations of one real captured publisher row.
    target = next(row for row in rows if row["category_ids"] and not row["source_is_aggregator"])
    original_category = target["category_ids"][0]
    base_observed = datetime.fromisoformat(target["source_observed_at"])
    partial = copy.deepcopy(target)
    partial.update(source_id="controlled-partial-source", source_name="Controlled partial source",
        source_is_aggregator=True, title="Controlled partial title must not replace publisher metadata",
        summary="Controlled partial summary must not replace publisher metadata",
        source_observed_at=(base_observed + timedelta(seconds=3)).isoformat(),
        category_ids=["controlled-independent-source"])
    reclassified = copy.deepcopy(target)
    reclassified.update(source_observed_at=(base_observed + timedelta(seconds=2)).isoformat(),
        category_ids=["controlled-reclassified"])
    stale = copy.deepcopy(target)
    stale.update(source_observed_at=(base_observed + timedelta(seconds=1, microseconds=500000)).isoformat(),
        category_ids=[original_category])
    standard_sql = (
        "set request.jwt.claims = '{\"role\":\"service_role\"}';",
        f"select public.m2_ingest_retained_corpus('{literal(rows)}'::jsonb) > 0 as ingested;",
        "select count(*) > 0 as category_works from public.m2_retained_candidates('us-news',null,null,null,100);",
        f"select count(*) > 0 as english_fts_works from public.m2_retained_candidates(null,'{english}',null,null,100);",
        f"select count(*) > 0 as cjk_substring_works from public.m2_retained_candidates(null,'{cjk}',null,null,100);",
        # M2.1 Phase 1: the reader needs the translation overlay and the
        # language-exclusive corpus to come back from the RPCs, not from a file.
        "select bool_and(value ? 'title_translations' and value ? 'summary_translations' and value ? 'event_group_id') as translation_fields_returned from public.m2_retained_candidates(null,null,null,null,100) as candidates(value);",
        "select coalesce(bool_and((value->>'language') <> 'en'), true) as exclusive_rows_are_other_language from public.m2_retained_candidates_language_exclusive('en',null,null,null,100,'pairing-json-v1') as candidates(value);",
    )
    category_sql = (
        "begin;",
        "set local request.jwt.claims = '{\"role\":\"service_role\"}';",
        f"select public.m2_ingest_retained_corpus('{literal([target])}'::jsonb);",
        f"select public.m2_ingest_retained_corpus('{literal([partial])}'::jsonb) = 1 as partial_observation_accepted;",
        f"select title = '{text_literal(target['title'])}' and source_id = '{text_literal(target['source_id'])}' as publisher_metadata_preserved from public.retained_corpus_observations where story_id = '{text_literal(target['story_id'])}';",
        f"select public.m2_ingest_retained_corpus('{literal([reclassified])}'::jsonb) = 1 as source_local_reclassification_accepted_below_global_watermark;",
        f"select public.m2_ingest_retained_corpus('{literal([stale])}'::jsonb) = 0 as stale_replay_ignored;",
        f"select not exists(select 1 from public.retained_corpus_categories where story_id='{text_literal(target['story_id'])}' and category_id='{text_literal(original_category)}') as old_source_category_removed;",
        f"select exists(select 1 from public.retained_corpus_categories where story_id='{text_literal(target['story_id'])}' and category_id='controlled-reclassified') as new_source_category_present;",
        f"select exists(select 1 from public.retained_corpus_categories where story_id='{text_literal(target['story_id'])}' and category_id='controlled-independent-source') as independent_source_category_preserved;",
        "rollback;",
    )
    sql = "\n".join(category_sql if args.category_regression_only else standard_sql)
    result = subprocess.run(["psql", "-X", "-v", "ON_ERROR_STOP=1", "-h", args.host, args.database], input=sql, text=True, capture_output=True)
    expected = 7 if args.category_regression_only else 6
    if result.returncode or result.stdout.count(" t\n") < expected:
        raise SystemExit("retained corpus PostgreSQL verification failed")
    print("retained corpus PostgreSQL verification passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
