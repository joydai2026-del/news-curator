# M2 operational health

Add a complete in-process denominator for handled `/rank` and `/page` requests without storing individual requests. `m2_request_health_buckets` holds only five-minute UTC buckets, endpoint, fixed outcome, fixed latency band, request count, and latest-input-match count. No owner, request, query, history, prompt, candidate, token, cost, or provider-attempt data is stored.

`m2_record_request_health` is service-role only and atomically increments one source-bounded dimension. The ASGI boundary records once after choosing the response. Its write has a 500 ms timeout, no retry, no background task, and cannot change the user response. Platform termination can still prevent recording, so the collector reports measurement coverage and never calls absent telemetry a complete denominator.

Operational health uses configurable window rates and minimum volume. It cannot infer consecutive failures, per-owner health, or cold/warm status. Idle and incomplete measurement are neutral, never PASS. Existing reservations and frozen receipts remain authoritative for money and provider usage.

The initial policy uses a 60-minute window and requires at least five handled requests. More than 10% failed deliveries reports FAIL. Lower volume reports insufficient volume, not PASS. These are delivery-alert thresholds, not evidence that recommendations are relevant.

The owner UI is a later change. It may say `Latest activity used` only for model mode, exact used/current revision and generation, current consent, and no pending client writes. Otherwise it says waiting, fallback, or unavailable. It never claims all recorded activity was included.

## QA

- Every handled rank/page response maps to one fixed outcome and latency band.
- RPC rejects unknown dimensions and non-service callers; concurrent increments do not lose counts.
- Telemetry timeout/failure leaves the selected HTTP response unchanged and emits one fixed marker.
- Collector validates bucket shapes, reports coverage, and separates idle, insufficient volume, PASS, and FAIL.
- Quality `insufficient_evidence` never turns operational health red.
