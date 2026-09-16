from pathlib import Path
SQL=(Path(__file__).parents[1]/'supabase/migrations/202609160001_m2_request_health.sql').read_text()

def test_health_schema_is_aggregate_service_only_and_atomic():
    assert 'user_id' not in SQL and 'request_id' not in SQL and 'query' not in SQL
    assert 'on conflict(bucket_start,endpoint,outcome,latency_band) do update' in SQL
    assert 'revoke all on public.m2_request_health_buckets from public,anon,authenticated' in SQL
    assert 'revoke execute on function public.m2_record_request_health(text,text,text,boolean) from public,anon,authenticated' in SQL
