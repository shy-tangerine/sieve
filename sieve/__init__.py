"""Sieve — Web research for AI agents.

$0 forever. Fetch any page with anti-bot bypass plus web search.
"""

__version__ = "13.2.1"

# Lazy imports — server pulls in heavy deps (patchright, playwright, etc.)
# Other modules (cache, security) are lightweight and can be imported directly
# for testing without the full dependency chain.


def __getattr__(name: str):
    """Lazy attribute access for server-level exports."""
    _lazy_exports = {
        "MasterFetchServer",
        "ResponseModel",
        "BulkResponseModel",
        "ArticleModel",
        "SessionInfo",
        "SessionCreatedModel",
        "SessionClosedModel",
        "CacheInfoModel",
        "main",
        "ArcticShiftClient",
        "ArcticShiftResponse",
        "ArcticShiftItem",
        "ArcticShiftPage",
    }
    if name in _lazy_exports:
        if name in {"ArcticShiftClient", "ArcticShiftResponse", "ArcticShiftItem", "ArcticShiftPage"}:
            from sieve.arctic_shift import (  # noqa: E402
                ArcticShiftClient, ArcticShiftResponse, ArcticShiftItem, ArcticShiftPage,
            )
            return locals()[name]
        from sieve.server import (  # noqa: E402
            MasterFetchServer,
            ResponseModel,
            BulkResponseModel,
            ArticleModel,
            SessionInfo,
            SessionCreatedModel,
            SessionClosedModel,
            CacheInfoModel,
            main,
        )
        return locals()[name]
    raise AttributeError(f"module 'sieve' has no attribute '{name}'")


__all__ = [
    "MasterFetchServer",
    "ResponseModel",
    "BulkResponseModel",
    "ArticleModel",
    "SessionInfo",
    "SessionCreatedModel",
    "SessionClosedModel",
    "CacheInfoModel",
    "main",
    "ArcticShiftClient",
    "ArcticShiftResponse",
    "ArcticShiftItem",
    "ArcticShiftPage",
]
