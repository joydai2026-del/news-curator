"""Source-platform contracts."""

from .base import (
    SourceAdapter,
    SourceContext,
    SourceParseError,
    SourceQuery,
    SourceResult,
    SourceSpec,
    SourceValidationError,
)
from .errors import SafeTransportError, SafeTransportReason
from .feed import AtomAdapter, FeedAdapter, RssAdapter
from .hackernews import HackerNewsAdapter
from .json_feed import JsonFeedAdapter
from .news_sitemap import NewsSitemapAdapter
from .registry import SourceRegistry, build_builtin_registry, collect_sources
from .transport import (
    ConnectedPeer,
    OriginBoundCredential,
    SafeHttpPolicy,
    SafeHttpResponse,
    SafeHttpTransport,
)

__all__ = (
    "ConnectedPeer",
    "AtomAdapter",
    "FeedAdapter",
    "HackerNewsAdapter",
    "JsonFeedAdapter",
    "NewsSitemapAdapter",
    "OriginBoundCredential",
    "RssAdapter",
    "SafeHttpPolicy",
    "SafeHttpResponse",
    "SafeHttpTransport",
    "SafeTransportError",
    "SafeTransportReason",
    "SourceAdapter",
    "SourceContext",
    "SourceParseError",
    "SourceQuery",
    "SourceRegistry",
    "SourceResult",
    "SourceSpec",
    "SourceValidationError",
    "build_builtin_registry",
    "collect_sources",
)
