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
- The plan frozen here is "News Curator Modular Product Scope and Architecture"
  (2026-09-01), which lives in the owner's private planning workspace, not in
  this repository. Sections are cited by name, and criteria by SC id.
