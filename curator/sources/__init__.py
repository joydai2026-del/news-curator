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
from .capabilities import (
    ADAPTER_PLUGIN_VERSION,
    DeclaresCapabilities,
    declared_capabilities,
)
from .errors import SafeTransportError, SafeTransportReason
from .feed import AtomAdapter, FeedAdapter, RssAdapter
from .hackernews import HackerNewsAdapter
from .health_record import HealthFoldOrderError, fold_source_health
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
    "ADAPTER_PLUGIN_VERSION",
    "ConnectedPeer",
    "AtomAdapter",
    "DeclaresCapabilities",
    "FeedAdapter",
    "HackerNewsAdapter",
    "HealthFoldOrderError",
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
    "declared_capabilities",
    "fold_source_health",
)
