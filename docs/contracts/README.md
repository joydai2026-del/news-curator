# Frozen contracts (phase 1)

Twelve contracts were frozen at contract freeze, gated by SC-41. Each one has:

- a prose file in this directory: purpose, field table with types and
  constraints, state machine where one exists, invariants, and the plan section
  it freezes;
- a typed definition in `curator/contracts/` (dataclasses, Protocols, and Enums
  only, no behavior and no I/O);
- at least one valid and one invalid fixture in `tests/fixtures/contracts/`.

| Contract | Prose | Typed module |
|---|---|---|
| Tenant | [tenant.md](tenant.md) | `curator/contracts/tenant.py` |
| Authorization | [authorization.md](authorization.md) | `curator/contracts/authorization.py` |
| Source plugin | [source-plugin.md](source-plugin.md) | `curator/contracts/source_plugin.py` |
| Evidence | [evidence.md](evidence.md) | `curator/contracts/evidence.py` |
| Event | [event.md](event.md) | `curator/contracts/event.py` |
| Search | [search.md](search.md) | `curator/contracts/search.py` |
| Candidate | [candidate.md](candidate.md) | `curator/contracts/candidate.py` |
| Artifact | [artifact.md](artifact.md) | `curator/contracts/artifact.py` |
| Mirror | [mirror.md](mirror.md) | `curator/contracts/mirror.py` |
| Output adapter | [output-adapter.md](output-adapter.md) | `curator/contracts/output_adapter.py` |
| Publication | [publication.md](publication.md) | `curator/contracts/publication.py` |
| Receipt | [receipt.md](receipt.md) | `curator/contracts/receipt.py` |

Ranking policy revision 1 is `config/ranking-policy-r1.yaml`. Everything above is
checked by `tests/test_contract_freeze.py`.

Two contracts predate this freeze and are reconciled rather than replaced:

- [personalization.md](personalization.md), the shipped preference schema. The
  field-by-field comparison and its recommendation are in
  [personalization-reconciliation.md](personalization-reconciliation.md).
- [translation.md](translation.md), untouched by this freeze.

## Conventions used by every file here

- **Vendor-neutral, and checked.** No provider, vendor, or product name appears
  in a core contract field name, enum member, or field value. A provider is
  named only inside an adapter's own configuration.
  `test_no_provider_names_in_typed_core_definitions` scans the typed
  definitions and `test_no_absolute_owner_paths_or_provider_names_in_fixtures`
  scans every fixture. The one exception is a field whose documented purpose is
  to NAME an adapter, a configured source route, or a destination: those are
  listed as explicit (file, field) pairs in the test's
  `ADAPTER_IDENTITY_ALLOWLIST`, so a provider name in any other field fails.
- **Every private record inherits `Ownership`.** `tenant_id`, `actor_id`,
  `actor_kind`, and `user_id` are declared once, in
  [tenant.md](tenant.md#ownership), and inherited by every private record, so
  they are never re-declared, never spelled differently, and never optional.
  `test_every_private_record_inherits_the_shared_ownership_shape` fails if a
  contract dataclass carries tenant, actor, or user semantics without either
  inheriting the shape or carrying a written exemption. Membership is decided by
  NAME PATTERN (`*tenant_id`, `*actor_id`, `*user_id`), nested fields included,
  so a renamed or nested guard cannot slip past it.
- **`user_id` follows a TWO-TIER rule, and no record is unclassified.** One
  question decides the tier: must a "delete everything about me" request find
  this row? Yes means SUBJECT-BOUND and `user_id` is required non-blank whatever
  wrote the row (a system writer does not erase the human subject). No means
  SUBJECTLESS and `user_id` may be null, and then only for a `system` actor. The
  tiers are frozen as data in `curator/contracts/__init__.py` with a written
  reason per class, and `test_every_owned_record_is_classified_exactly_once`
  fails if a new owned record lands in no tier or in two. Full table:
  [tenant.md](tenant.md#ownership).
- **CANONICAL is part of every guard, at every layer.** `not null`,
  `non-blank` and `canonical` are three different rules. The fixture
  invariant, the runtime guard in `curator/ownership.py`, and the SQL `check`
  constraints all reject `""`, `"   "`, `" user-1 "`, `"\tuser-1"`,
  `"user\u200b1"` and `"\u3000"` on `tenant_id`, `actor_id`, and a present
  `user_id`. The invisible set is frozen once in
  `curator/contracts/__init__.py` and the SQL text is generated from it.
- **A guard field is REQUIRED, never optional-with-a-check.** An optional
  guard skips its own check when it is omitted, so absence must fail
  validation rather than default. This is the rule the contract-freeze
  re-review produced, and the ownership shape is the last of the four
  bypasses it found.
- **No owner identifiers.** These files ship to the public repository, so an
  absolute home path and the owner's initials are both rejected across the
  whole frozen set by `test_owner_identifiers_are_absent_from_the_whole_frozen_set`.
  The pilot tenant id is `tenant-owner-private`; product-decision comments say
  "the owner" or "the reader" rather than naming a person.
- **Grades.** A = proven in production. B = proven in source at a cited path.
  C = not verified. A frozen contract is a design artifact, so most statements
  about FUTURE behavior are C by construction; statements about what the
  repository contains today are B.
- **Freeze notes.** Where the plan was silent, the smallest choice consistent
  with it was made and recorded in that file's "Freeze notes" section.
- **Canonical tables are derived, never hand-edited.** The ownership
  classification table in `tenant.md` and the receipt kind and wrapper tables
  in `receipt.md` are rendered from the frozen tuples in
  `curator/contracts/__init__.py` by
  `python -m scripts.render_contract_tables`. Their marker names are
  `ownership-classification`, `receipt-kind-tiers`, and
  `receipt-wrapper-kinds`. Refusal cost: authors edit the tuples and re-render;
  any direct table edit fails the freeze test byte for byte.
- **Normative boundary.** Those generated blocks are the only normative
  statement of ownership tiers, receipt kinds, and wrapper bindings. Any other
  mention is informational. The outside-marker guard catches tier statements
  in tables only; it deliberately does not scan general prose.
- **Frozen contracts are exact classes.** They are never subclassed. A new
  record is added to the frozen tuples, with its own reviewed wrapper field and
  kind where applicable. The refusal cost is that extension requires a freeze
  edit instead of inheritance, which keeps runtime classification closed and
  auditable.
- The plan frozen here is "News Curator Modular Product Scope and Architecture"
  (2026-09-01), which lives in the owner's private planning workspace, not in
  this repository. Sections are cited by name, and criteria by SC id.
