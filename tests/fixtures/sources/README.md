# Source adapter fixtures

`daring-fireball.json` is a sanitized subset captured from
`https://daringfireball.net/feeds/json` on 2026-08-29. It retains the feed
version and two source-published item records. Long `content_html` fields and
author metadata were removed because the adapter does not consume article
bodies or authors.

Existing real captured XML and Hacker News fixtures remain under
`tests/fixtures/feeds/` and are reused by the source contract tests.
