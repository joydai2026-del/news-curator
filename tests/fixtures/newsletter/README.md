# Newsletter fixtures

Four of these are **real messages with every identifier scrubbed out**. Two are
hand-written. Nothing here is a real subscriber token or a real email address,
and no host in any of these files can resolve.

## Why four of them changed

They used to all be hand-written reconstructions of "the publicly visible shape
of each newsletter". The first live run showed what that was worth: the TLDR
adapter read 15 real messages and produced **0 stories**, and the three beehiiv
senders extracted stories but dropped **100% of their links**, while every test
in this suite was green.

Both failures were formats nobody had looked at:

- Real TLDR mail writes `<meta ...>` with no closing slash. The reconstruction
  wrote `<meta ... />`. `meta` was in the parser's skip-tag set, so each real
  one opened a skip scope that could never close and the whole body was thrown
  away. The reconstruction and the parser agreed with each other about a format
  neither had seen.
- beehiiv's hrefs are `link.mail.beehiiv.com/ss/c/u001.<blob>/...` where the
  blob is 240 to 411 characters of **encrypted** payload, not encoded. There is
  nothing in it to recover offline. The reconstruction had written a plausible
  wrapper with the destination inside.

So `tldr.html`, `therundown.html`, `theneuron.html` and `milkroad.html` (plus
their `.txt` halves) are now captures from the mailbox on 2026-08-28, scrubbed.
`bensbites.html` stays hand-written because that sender had no mail to capture,
and it is labelled as the one adapter still carrying that risk.

## The `.txt` files are not decoration

They are the plain-text half of the same `multipart/alternative` message, and
they are where the three beehiiv senders' links actually come from: beehiiv
renders that half as markdown, and its `[label](url)` links carry the real
publisher URL rather than the tracked one. Reading it is offline decoding of
mail we already have, so the sanitizer's no-network rule is intact.

## Scrub contract (enforced by `test_newsletter_fixtures.py`)

- **No `@` anywhere** except in `fixture-reader@example.invalid`. Absolute on
  purpose: real newsletter copy is full of `@handles` and `plugin@package`
  strings, and deciding case by case which `@` is safe is exactly the judgement
  call that lets a real address through.
- **No path or query token over 20 characters** unless it starts with
  `SYNTHETIC-`. Two narrow escapes: a percent-encoded nested destination (TLDR's
  tracker carries the article that way) is allowed if it points at a reserved
  test host, and the fake reader address is allowed as a path segment because
  that is a shape the sanitizer must refuse.
- **Destination hosts** are rewritten under the reserved `.example` TLD, keeping
  the recognisable label so a human can still read the file.
- **Subjects** come from `SUBJECTS` in the test module and are generic; no real
  subject line is stored here.
- **Headlines and blurbs are real.** They are published newsletter copy, they
  carry no identifier, and they are the reason these fixtures are worth having:
  the parser is measured against sentences a person actually wrote.
- **Tracker HOSTNAMES are real** (`tracking.tldrnewsletter.com`,
  `link.mail.beehiiv.com`, `click.convertkit-mail2.com`) because the sanitizer's
  host matching is the thing under test. Nothing is ever fetched from them: the
  suite blocks the network (`tests/conftest.py`).

## `leakshapes.html`

The odd one out, and excluded from the hygiene tests by name. It is not modelled
on a sender: it is a TLDR-shaped carrier for the five link shapes review round 1
proved leaked (reader address in a query, reader address in a path segment,
`?subid=`, `?token=`, `?ref=`) plus one unresolvable tracker link and one clean
publisher link. The end-to-end privacy test asserts the file really contains
them BEFORE asserting they are gone from the rendered page, so the test cannot
pass by having nothing to remove. Every string in it is synthetic.

## What a passing test still does not prove

The counts in `EXPECTED_STORIES` and `EXPECTED_LINKED` are what these four
issues yielded, from one week. Senders redesign. A green suite means the parser
handles the format captured on 2026-08-28; the lane's per-run report remains the
only honest measurement of the live hit rate.
