#!/usr/bin/env python3
"""Background-only public retained translation and localized projection export."""
from __future__ import annotations
import argparse, json, os, sys, time
from concurrent.futures import ThreadPoolExecutor, as_completed
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from curator.config import load_config
from curator.localization import story_id_for_item
from curator.models import Item
from curator.sources import OriginBoundCredential, SafeHttpPolicy, SafeHttpTransport
from curator.translation import OpenAITranslationAdapter, OpenAITranslationConfig, SupabaseTranslationConfig, SupabaseTranslationStore
from curator.recommendation.supabase_http import validate_https_origin
from scripts.run_translation_job import produce_translation_records

MAX_QUEUE = 1000
MAX_PAGE = 100

def _origin(value: str) -> str:
    return validate_https_origin(value).rstrip("/")

def _queue_policy(policy: Mapping[str, object]) -> tuple[int, int, int]:
    values = (policy.get("queue_limit", 12), policy.get("translation_workers", 1), policy.get("queue_time_budget_seconds", 240))
    if any(isinstance(value, bool) or not isinstance(value, int) for value in values):
        raise ValueError("retained translation queue policy is invalid")
    limit, workers, budget = values
    if not 1 <= limit <= 1000 or not 1 <= workers <= 4 or not 1 <= budget <= 240:
        raise ValueError("retained translation queue policy is out of bounds")
    # One worker preserves the existing store/transport transition order. The queue cap
    # bounds worst-case dispatch below the configured background budget; no retry occurs.
    return limit, workers, budget


def _transport(policy: Mapping[str, object], *, timeout_seconds: float | None = None) -> SafeHttpTransport:
    return SafeHttpTransport(policy=SafeHttpPolicy(total_timeout_seconds=timeout_seconds if timeout_seconds is not None else float(policy.get("request_timeout_seconds",20)), max_wire_bytes=int(policy.get("max_response_bytes",524288)), max_decoded_bytes=int(policy.get("max_response_bytes",524288)), per_host_concurrency=int(policy.get("per_host_concurrency",2))))

def _rpc(transport, origin: str, key: str, name: str, body: dict) -> object:
    credentials=(OriginBoundCredential(origin=origin,header_name="apikey",value=key),) if key.startswith("sb_secret_") else (OriginBoundCredential(origin=origin,header_name="Authorization",value="Bearer "+key),OriginBoundCredential(origin=origin,header_name="apikey",value=key))
    response=transport.request("retained-translation","POST",origin+"/rest/v1/rpc/"+name,headers={"Accept":"application/json","Content-Type":"application/json"},body=json.dumps(body,separators=(",",":")).encode(),credentials=credentials,allowed_mime_types=("application/json",))
    if response.status_code != 200 or len(response.body)>8_000_000: raise ValueError("retained translation RPC unavailable")
    value=json.loads(response.body.decode())
    if not isinstance(value,list): raise ValueError("retained translation RPC response is invalid")
    return value

def _item(row: Mapping[str, object]) -> tuple[Item, tuple[str,...], str]:
    required=("story_id","title","summary","language","source_id","source_name","canonical_url","published_at","category_ids")
    if any(not isinstance(row.get(key),str) for key in required[:-1]) or not isinstance(row.get("category_ids"),list) or not all(isinstance(x,str) for x in row["category_ids"]): raise ValueError("queue row is invalid")
    published=datetime.fromisoformat(str(row["published_at"]).replace("Z","+00:00"))
    if published.tzinfo is None or row["language"] not in {"en", "zh"}: raise ValueError("queue row locale or timestamp invalid")
    item=Item(title=str(row["title"]),description=str(row["summary"]),url=str(row["canonical_url"]),canonical_url=str(row["canonical_url"]),source_id=str(row["source_id"]),source_name=str(row["source_name"]),language=str(row["language"]),published_at=published,native_categories=set(row["category_ids"]))
    if story_id_for_item(item) != row["story_id"]: raise ValueError("retained identity mismatch")
    return item, tuple(row["category_ids"]), str(row["story_id"])

def _provider_store(policy: Mapping[str, object], origin: str, service_key: str, api_key: str):
    provider_transport = _transport(policy, timeout_seconds=float(policy.get("request_timeout_seconds", 20)))
    store_transport = _transport(policy, timeout_seconds=3.0)
    provider = OpenAITranslationAdapter(config=OpenAITranslationConfig(endpoint=str(policy["openai_endpoint"]),model_version=str(policy["openai_model"]),max_output_tokens=int(policy["openai_max_output_tokens"]),input_microusd_per_million_tokens=int(policy["openai_input_microusd_per_million_tokens"]),output_microusd_per_million_tokens=int(policy["openai_output_microusd_per_million_tokens"])),transport=provider_transport,api_key=lambda:api_key)
    return provider, SupabaseTranslationStore(SupabaseTranslationConfig(origin,service_key),transport=store_transport)


def _fair_tasks(rows, categories: Mapping[str, str]):
    buckets: dict[tuple[str, str], list[Item]] = {}
    seen: set[tuple[str, str]] = set()
    for row in rows:
        item, category_ids, story_id = _item(row)
        if item.language not in {"en", "zh"}: continue
        for category_id in (category_ids or ("__all__",)):
            name = categories.get(category_id) or ("All" if category_id == "__all__" else None)
            key = (item.language, name) if name else None
            if key and (item.language, story_id) not in seen:
                buckets.setdefault(key, []).append(item); seen.add((item.language, story_id)); break
    for value in buckets.values(): value.sort(key=lambda item: (item.published_at, item.canonical_url), reverse=True)
    ordered=[]; index=0
    while True:
        added=False
        for language in ("en", "zh"):
            for key in sorted(key for key in buckets if key[0] == language):
                values=buckets[key]
                if index < len(values): ordered.append((language, key[1], values[index])); added=True
        if not added: return ordered
        index += 1


def translate(root: Path, limit: int) -> int:
    cfg=load_config(root); policy=cfg.translation
    if policy.get("enabled") is not True or policy.get("provider") != "openai": return 0
    configured_limit, workers, time_budget = _queue_policy(policy)
    limit = min(limit, configured_limit)
    origin=_origin(os.environ.get(str(policy["supabase_url_env"]),"")); key=os.environ.get(str(policy["supabase_service_role_key_env"]),""); api_key=os.environ.get(str(policy["openai_api_key_env"]),"")
    if not key or not api_key: raise ValueError("retained translation credentials unavailable")
    probe_transport=SafeHttpTransport(policy=SafeHttpPolicy(total_timeout_seconds=5.0, max_wire_bytes=8_000_000, max_decoded_bytes=8_000_000, per_host_concurrency=1))
    rows=_rpc(probe_transport,origin,key,"m2_translation_queue",{"p_limit":MAX_QUEUE,"p_max_age_hours":int(policy.get("queue_max_age_hours",168))})
    tasks=_fair_tasks(rows,{category.id: category.name for category in cfg.categories})[:limit]
    deadline=time.monotonic()+time_budget; run_id="retained:"+datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    def dispatch(task):
        language, category_name, item = task
        if time.monotonic() >= deadline: return {"skipped_deadline": 1}
        local_cfg=deepcopy(cfg); local_cfg.translation["targets"]=["zh" if language == "en" else "en"]
        local_cfg.translation["max_items_per_language"]=1
        provider, store=_provider_store(local_cfg.translation,origin,key,api_key)
        result=produce_translation_records(cfg=local_cfg,ranked_by_language={language:{category_name:[item]}},store=store,provider=provider,now=datetime.now(timezone.utc),run_id=run_id)
        return {"translated":result.counters.get("translated",0),"failed":sum(value for name,value in result.counters.items() if name not in {"translated","cache_hit"}),"fatal":int(result.fatal_persistence_failure)}
    totals={"translated":0,"failed":0,"fatal":0,"skipped_deadline":0}
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures=[]
        for task in tasks:
            if time.monotonic() >= deadline:
                totals["skipped_deadline"] += 1; continue
            futures.append(executor.submit(dispatch,task))
        for future in as_completed(futures):
            result=future.result()
            for field in totals: totals[field]+=int(result.get(field,0))
    print(json.dumps({"queue_rows":len(rows),"dispatched":len(futures),"run_id":run_id,**totals},separators=(",",":")))
    return 1 if totals["fatal"] or (totals["failed"] and not totals["translated"]) else 0

def export(root: Path, output: Path, locale: str) -> int:
    if locale not in {"en","zh"}: raise ValueError("locale is invalid")
    cfg=load_config(root); policy=cfg.translation; origin=_origin(os.environ.get(str(policy["supabase_url_env"]),"")); key=os.environ.get(str(policy["supabase_service_role_key_env"]),"")
    if not key: raise ValueError("localized projection credentials unavailable")
    transport=_transport(policy); categories=[]
    for category in cfg.categories:
        rows=_rpc(transport,origin,key,"m2_localized_candidates",{"p_locale":locale,"p_category_id":category.id,"p_query":None,"p_before_published_at":None,"p_before_story_id":None,"p_limit":MAX_PAGE})
        items=[]
        for row in rows:
            item, _, story_id=_item(row)
            title=row.get("display_title"); summary=row.get("display_summary"); display_language=row.get("display_language"); available=row.get("translation_available")
            if not all(isinstance(x,str) for x in (title,summary,display_language)) or available is not True: raise ValueError("localized projection row is unavailable")
            if display_language != locale or not title.strip(): raise ValueError("mixed or empty locale projection")
            items.append({"story_id":story_id,"title":title,"description":summary,"url":item.url,"canonical_url":item.canonical_url,"source_id":item.source_id,"source_name":item.source_name,"published_at":item.published_at.astimezone(timezone.utc).isoformat(),"original_language":item.language,"display_language":display_language,"translated":item.language != locale,"translation_available":available,"translation_source_language":item.language if available else "","translation_provider":"","translation_model_version":"","image_url":"","is_newsletter":False})
        categories.append({"id":category.id,"name":category.name,"items":items})
    payload={"schema_version":1,"generated_at":datetime.now(timezone.utc).replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ"),"language":locale,"categories":categories}
    output.parent.mkdir(parents=True,exist_ok=True); temp=output.with_suffix(output.suffix+".tmp"); temp.write_text(json.dumps(payload,ensure_ascii=False,separators=(",",":")),encoding="utf-8"); temp.replace(output)
    return 0

def main(argv=None) -> int:
    parser=argparse.ArgumentParser(); subs=parser.add_subparsers(dest="command",required=True)
    q=subs.add_parser("translate"); q.add_argument("--root",type=Path,default=Path.cwd()); q.add_argument("--limit",type=int,default=100)
    e=subs.add_parser("export"); e.add_argument("--root",type=Path,default=Path.cwd()); e.add_argument("--output",type=Path,required=True); e.add_argument("--locale",required=True)
    args=parser.parse_args(argv)
    if args.command == "translate" and not 1 <= args.limit <= MAX_QUEUE: parser.error("limit must be 1..1000")
    return translate(args.root,args.limit) if args.command=="translate" else export(args.root,args.output,args.locale)
if __name__=="__main__":
    try: raise SystemExit(main())
    except Exception: print("retained translation pipeline unavailable",file=sys.stderr); raise SystemExit(2)
