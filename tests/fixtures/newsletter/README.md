# Newsletter fixtures — ALL SYNTHETIC

Every file in this folder is **written by hand for the test suite**. None of it
is a real newsletter, a real subscriber token, or a real email address.

- Destination hosts use the reserved `.example` TLD, so they can never resolve.
- Reader addresses use `.invalid`, likewise reserved.
- Subscriber tokens are the literal strings listed in `mime.py` under
  `FAKE_TOKENS`. The privacy test asserts none of them reaches an item URL, the
  state file, or a log record.
- Tracker HOSTNAMES are real (`tracking.tldrnewsletter.com`,
  `link.mail.beehiiv.com`, `click.convertkit-mail2.com`) because the sanitizer's
  host matching is the thing under test. Nothing is ever fetched from them: the
  suite blocks the network (`tests/conftest.py`).

The HTML structures are modelled on the publicly visible shape of each
newsletter (TLDR's bold headline plus "(N minute read)" suffix; beehiiv's
heading-and-paragraph content blocks; Ben's Bites' link list). They are a
plausible reconstruction, **not** a captured sample, so a passing adapter test
proves the parser handles this shape. It does not measure a real hit rate. That
number only exists after OAuth is wired and a real mailbox is read.
