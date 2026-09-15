# M2 ranking request contract

This contract is the provider-neutral boundary shared by feed and search ranking.

- `AuthenticatedOwner` is supplied by the server authorization boundary. Constructing it does not prove authentication. Tenant, user, and principal IDs are not included in `ModelRankingInput`.
- `query` is optional. When present, the ranking policy gives it priority over inferred history.
- `ordered_history` preserves committed order and distinct repeated actions. A delivery retry keeps the same event ID and revision, while a new user action receives a new pair.
- Every candidate carries immutable title, summary, source, language, publication time, and source-document identity so the model has content to judge.
- Every candidate ID must be a canonical `story:` identity and the candidate set must exactly match the supplied selected eligible-candidate registry snapshot, not the entire corpus.
- `history_revision` names the newest included event. `server_commit_revision` records the latest owner ledger watermark, including when consent produces an empty snapshot. The generation, consent, policy, model, and schema versions bind the decision to reproducible inputs without naming a provider or hardcoding a model.
- History events may include server-resolved story title, summary, and source context from the eligible public corpus. Owner identifiers remain outside model input.
- A response is accepted only when every receipt binding matches the expected request and its ranked IDs are an exact unique permutation of the request candidate IDs.
- Model and fallback responses are distinct modes. A fallback requires a reason and cannot be counted as model-path success.

The boundary validator checks protocol integrity only. Authentication, consent, owner isolation, persistence, provider calls, and policy enforcement remain responsibilities of the later event, index, and reranking paths.
