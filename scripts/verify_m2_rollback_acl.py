#!/usr/bin/env python3
"""Verify M2 ACL rollback in an existing isolated local `nc_m2_*` database only."""
from __future__ import annotations
import argparse, json, re, shutil, subprocess, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from scripts.verify_import_rehearsal import validate_local_socket

TARGET = re.compile(r"^nc_m2_[a-z0-9_]+$")
M2 = (
    "public.m2_history_snapshot(integer)", "public.m2_owner_story_states(text[])",
    "public.m2_owner_export_page(text,text)",
    "public.append_behavior_event(text,text,jsonb,timestamptz,bigint,integer)",
    "public.set_behavior_consent(boolean,boolean,text)", "public.clear_behavior_history()",
    "public.set_story_state_with_event(text,boolean,boolean,bigint,text,text,text,text,timestamptz,bigint)",
    "public.set_story_interest_with_event(text,text,text,bigint,text,text,text,timestamptz,bigint)",
)
M1 = ("public.set_story_state(text,boolean,boolean,bigint,text)", "public.set_story_interest(text,text,text,bigint,text)")

def _array(values: tuple[str, ...]) -> str:
    return "array[" + ",".join("'" + value + "'" for value in values) + "]"

def acl_fingerprint_sql() -> str:
    values = ",".join("('" + function + "')" for function in M2 + M1)
    return f"""with targets(signature) as (values {values}), resolved as (
select signature,to_regprocedure(signature) as oid from targets
) select jsonb_build_object('expected_count',{len(M2 + M1)},'resolved_count',count(distinct resolved.oid),
'functions',coalesce(jsonb_agg(jsonb_build_object('signature',resolved.signature,'oid',resolved.oid::text,
'acl_is_null',p.proacl is null,'acl',case when p.proacl is null then null else p.proacl::text end)
order by signature),'[]'::jsonb))::text from resolved left join pg_proc p on p.oid=resolved.oid;"""

def valid_fingerprint(value: object) -> bool:
    if not isinstance(value, dict) or value.get("expected_count") != len(M2 + M1) or value.get("resolved_count") != len(M2 + M1):
        return False
    functions = value.get("functions")
    return isinstance(functions, list) and len(functions) == len(M2 + M1) and {
        row.get("signature") for row in functions if isinstance(row, dict) and isinstance(row.get("oid"), str) and row["oid"]
    } == set(M2 + M1)

def rollback_sql() -> str:
    revokes = ", ".join(M2)
    m2_checks = " and ".join(f"not has_function_privilege('authenticated','{function}','execute')" for function in M2)
    m1_checks = " and ".join(f"has_function_privilege('authenticated','{function}','execute')" for function in M1)
    return f"begin; revoke execute on function {revokes} from authenticated; select ({m2_checks}) as m2_private_paths_denied, ({m1_checks}) as m1_fallback_preserved; rollback;"

def _psql(psql: str, database: str, statement: str, host: Path, port: int) -> str:
    result = subprocess.run([psql,"-X","-At","-v","ON_ERROR_STOP=1","-h",str(host),"-p",str(port),database], input=statement, text=True, capture_output=True, timeout=30)
    if result.returncode: raise RuntimeError("local PostgreSQL ACL rollback command failed")
    return result.stdout.strip()

def parse_transaction(output: str) -> bool:
    return [line for line in output.splitlines() if line in {"t|t","t|f","f|t","f|f"}] == ["t|t"]

def main() -> int:
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database",required=True); parser.add_argument("--receipt",type=Path,required=True); parser.add_argument("--host",default="/tmp"); parser.add_argument("--port",type=int,default=5432)
    args=parser.parse_args(); receipt={"environment":"local isolated PostgreSQL only","database":args.database,"status":"blocked_before_execution","checks":[]}
    try:
        if not TARGET.fullmatch(args.database): raise ValueError("database must be an existing nc_m2_* isolated database")
        host=validate_local_socket(args.host,args.port); psql=shutil.which("psql")
        if not psql: raise RuntimeError("psql unavailable")
        before=json.loads(_psql(psql,args.database,acl_fingerprint_sql(),host,args.port))
        if not valid_fingerprint(before): raise RuntimeError("incomplete ACL fingerprint")
        denied=parse_transaction(_psql(psql,args.database,rollback_sql(),host,args.port))
        after=json.loads(_psql(psql,args.database,acl_fingerprint_sql(),host,args.port))
        if not valid_fingerprint(after): raise RuntimeError("incomplete ACL fingerprint")
        checks=[("M2 owner/private paths denied while M1 fallback stays available",denied), ("rollback restores exact pre-drill ACL fingerprint",before==after)]
        receipt.update(status="pass" if all(value for _,value in checks) else "fail",checks=[{"name":name,"passed":value} for name,value in checks],acl_fingerprint_before=before,acl_fingerprint_after=after)
    except Exception as exc:
        receipt.update(status="fail",error_class=type(exc).__name__)
    finally:
        args.receipt.write_text(json.dumps(receipt,indent=2,sort_keys=True)+"\n")
    return 0 if receipt["status"]=="pass" else 1
if __name__=="__main__": raise SystemExit(main())
