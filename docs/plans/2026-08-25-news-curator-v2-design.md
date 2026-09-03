# Historical design note: News Curator v2 (2026-08-25)

> **Historical record only. This is not the current roadmap, build order, or
> definition of done.** The current plan, including milestones, dates, done
> tests, explainer gates, priorities, and product decisions, lives in
> [Notion](https://app.notion.com/p/Fall-2026-AI-Sprint-3c6442f52cf7801db1c2fe2e54d777f2).

This note preserves technical observations from a design session on
2026-08-25. It has no scheduling or prioritization authority.

## Historical product shape

The design explored a Discover-card news site with a responsive card grid,
category tabs, client-side filtering, images when available, and cards that
unfold in place. The existing static-site architecture rebuilt the output on
GitHub Actions and served it through GitHub Pages.

The design treated `topics.yaml` as the configuration boundary for category
names, keywords, and curated sources. Category changes made through GitHub's
editor would be picked up by a subsequent build. Those facts describe the
implementation considered at the time, not the current product sequence.

## Historical newsletter-ingestion constraints

- Gmail access required read-only OAuth with the client id, client secret, and
  refresh token stored only as protected GitHub Actions secrets.
- A durable cursor needed an overlap window and privacy-safe hashes so a failed
  run could not silently skip mail.
- Generic newsletter parsing was considered unreliable. Per-sender adapters
  with MIME fixtures and measured extraction results were the proposed narrow
  technical shape.
- Newsletter HTML was untrusted input. Only escaped extracted text and validated
  URLs could reach the rendered site.
- Subscriber-specific links and tracking redirects created a privacy risk.
  Newsletter-derived URLs needed sanitization before rendering or image lookup;
  an unrecoverable clean publisher URL meant omitting the link.
- A newsletter item could be cross-tagged into categories by its extracted
  story title while remaining one item for deduplication.
- The newsletter integration needed a fail-soft boundary so an unavailable or
  revoked OAuth grant could not fail the rest of the news build.

## Historical card and rendering constraints

- Descriptions came from the source and were not generated.
- Images came from feed media or publisher metadata, with a typographic fallback.
- Expanded cards retained source, publication time, matched evidence, alternate
  links from clustering, and the outbound URL.
- Image failure, unsafe URL schemes, and newsletter-derived identifiers needed
  output-side tests because a successful build alone did not prove a safe page.

## Historical operational risks

- No account address, token, message identifier, subject, or personal data could
  enter the public repository, committed cache, rendered page, or logs.
- Third-party actions handling credentials needed immutable pinning and minimum
  permissions.
- Custom-domain and HTTPS propagation were external deployment concerns, not
  proof that the product path was complete.
- The accuracy claim was deliberately narrow: the source supplied a title and
  link at build time. The software did not verify the linked article's truth.

The original session-specific build sequence and deferred-feature ordering were
removed because they competed with the current Notion plan.
