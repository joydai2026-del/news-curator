# M2 passing capture provenance

This fixture is a compact public-source subset of the supplied real captures. It contains no owner profile, account identifier, credential, or private history. The test declares `first_local_edition` with an explicitly empty local history.

Source captures and clocks:

- Current capture: `2026-09-10T02:31:33.824945Z`, SHA-256 `89649e6189a3892789d599d1f4e2f21cca96dc30e8eebb42f15895b0580528d7`.
- Previous capture: `2026-09-10T01:18:50.021985Z`, SHA-256 `6de18b6268bcedd5dcd0f4180f4add433e39c04c691377fd6e7a41000953e69c`.
- Configuration digest: `8f8214578dfd2cdea69bff9bff811c122ecfa575feeff155e2875e9cc8e8d450`.

Subset method: retain every current candidate whose primary lane is Updates, Hot, or Surprise, plus the top 40 Interested candidates by the receipt's existing final-score/story-ID order. For each retained canonical story ID, retain every original source observation in both captures. Retain source-health rows for source IDs represented by those observations. The existing `write_source_snapshot` writer preserved field values, provenance, and clocks.

The subset contains 71 current items and 19 current health rows (72,372 bytes), plus 52 previous items and 18 previous health rows (54,143 bytes). Generated fixture hashes are recorded here after writing:

- `discovery-passing-current.json`: `acab220797e67ec053d522eed2e7d19954705ec74e8decbe734b5d593bf1ce34`.
- `discovery-passing-previous.json`: `8c3a9f981bfcf98a5cf53b3652af7db30465b981ecb79cede1a7caf74b2a7490`.

The companion replay test recomputes the receipt from the two fixtures, checks all seven active bands and all four primary lanes, and verifies deterministic replay. This fixture is evidence for local replay only, not publication or production acceptance.
