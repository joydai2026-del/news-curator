# Translation backend contract

Status: locally implemented, cloud and provider activation unverified.

## Public and private records

`TranslationRecord` is a settled-only immutable projection used to build static localized JSON. Its presence means the private store completed settlement. It contains only story/input identity, language pair, translated title/description, and provider/model provenance.

Lifecycle status, created/finalized timestamps, expiry, reservation counters, and character accounting remain solely in the private durable cache and reservation rows. They are deliberately excluded from the public artifact because copying mutable operational state into a static file would create two authorities, make expiry/accounting stale, and disclose service internals. The artifact loader rejects every field outside its exact settled projection schema.

## Output limits

The checked-in policy sets maximum translated title and description lengths. The Google adapter, translation job, cache record, domain record, artifact loader, and SQL cache hard constraints all reject output above the same configured ceiling before settlement. Runtime policy may lower the limits without code changes, but cannot raise them above the artifact and database hard ceilings.

## Recovery and cost safety

- A stale `leased` reservation is provably never sent and may release its counters.
- A stale `sent` reservation becomes `charge_unknown`, remains fully counted, and blocks automatic retry.
- Identical reconciliation is idempotent. Conflicting reconciliation is rejected.
- If an ambiguous paid outcome cannot be durably recorded, the translation job exits nonzero. The independent ordinary build continues with originals when the artifact is missing.
- Logs expose only fixed, bounded reason counters. They never contain source text, raw provider responses, credentials, or raw exceptions.
