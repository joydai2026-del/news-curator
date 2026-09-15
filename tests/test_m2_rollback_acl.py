"""Controlled protocol checks for local M2 ACL rollback command construction."""
from __future__ import annotations
import scripts.verify_m2_rollback_acl as verifier

def test_rollback_sql_revokes_m2_not_m1_and_restores_in_transaction() -> None:
    sql=verifier.rollback_sql()
    revoked=sql.split("revoke execute on function ",1)[1].split(" from authenticated",1)[0]
    assert "begin;" in sql and "rollback;" in sql
    assert "public.m2_owner_export_page(text,text)" in revoked
    assert "public.set_story_state_with_event" in revoked
    assert "public.set_story_state(text,boolean,boolean,bigint,text)" not in revoked
    assert " and " in sql and "m1_fallback_preserved" in sql

def test_acl_fingerprint_captures_every_affected_function() -> None:
    sql=verifier.acl_fingerprint_sql()
    assert "to_regprocedure(signature)" in sql
    assert "resolved_count" in sql and "acl_is_null" in sql
    assert "count(distinct resolved.oid)" in sql and "resolved.oid::text" in sql

def test_acl_fingerprint_rejects_missing_or_unresolved_function_records() -> None:
    # Controlled protocol fingerprint, not a database observation.
    records=[{"signature": signature, "oid": str(index), "acl_is_null": index == 0, "acl": None if index == 0 else "{}"} for index, signature in enumerate(verifier.M2 + verifier.M1, start=1)]
    assert verifier.valid_fingerprint({"expected_count": len(records), "resolved_count": len(records), "functions": records})
    assert not verifier.valid_fingerprint({"expected_count": len(records), "resolved_count": len(records) - 1, "functions": records[:-1]})

def test_target_name_refuses_nonisolated_database() -> None:
    assert verifier.TARGET.fullmatch("nc_m2_combined_123abc")
    assert not verifier.TARGET.fullmatch("postgres")
    assert not verifier.TARGET.fullmatch("production")

def test_transaction_parser_ignores_psql_status_lines() -> None:
    assert verifier.parse_transaction("BEGIN\nREVOKE\nt|t\nROLLBACK\n")
    assert not verifier.parse_transaction("BEGIN\nREVOKE\nt|f\nROLLBACK\n")
