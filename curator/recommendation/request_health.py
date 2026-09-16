"""Bounded aggregate request-health reporting."""
from __future__ import annotations
import json, sys

_OUTCOMES=frozenset(('model','fallback','auth_denied','invalid_request','stale','disabled','server_error','timeout'))

def latency_band(seconds: float) -> str:
    if seconds < 1: return 'lt1s'
    if seconds < 3: return '1to3s'
    if seconds < 6: return '3to6s'
    if seconds < 8: return '6to8s'
    if seconds < 20: return '8to20s'
    return 'gt20s'

class RequestHealthReporter:
    def __init__(self, store): self._store=store
    def record(self, *, endpoint: str, outcome: str, elapsed_seconds: float, latest_input_match: bool=False) -> None:
        if endpoint not in ('rank','page') or outcome not in _OUTCOMES or type(latest_input_match) is not bool:
            raise ValueError('invalid request health event')
        try:
            self._store.record_request_health(endpoint=endpoint,outcome=outcome,
                latency_band=latency_band(elapsed_seconds),latest_input_match=latest_input_match)
        except Exception:
            print(json.dumps({'event':'request_health_write_failed'},separators=(',',':')),file=sys.stderr,flush=True)
