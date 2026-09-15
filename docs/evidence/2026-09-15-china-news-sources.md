# China News source evidence

**B source verification, 2026-09-15.** China News means reporting about China from Chinese and international publishers. It does not mean every Chinese-language story belongs in the category.

| Candidate | Publisher endpoint | SafeHttpTransport/parser result | Decision |
|---|---|---|---|
| South China Morning Post China | `https://www.scmp.com/rss/91/feed` | Fresh, 50 usable English items under the project RSS parser | Added as the one native China News feed. |
| Sixth Tone | `https://www.sixthtone.com/rss` | HTTP 200 and XML returned, but project parser found zero usable items at the probe time | Not added. |
| China Daily China | `https://www.chinadaily.com.cn/rss/china_rss.xml` | HTTP 404 at the probe time | Not added. |

Existing RFI Chinese, BBC Chinese, CNA International, DW Chinese, and shared Chinese feeds remain configured outside China News. China-specific English and Chinese keyword matches can route their items to this category. Google News feeds remain aggregators and cannot be independent corroboration.
