from __future__ import annotations

from typing import Any

from .provider import ResilientWebProvider
from tools.registry import tool_error, tool_result

DEFAULT_SETTINGS = {
    "search_order": ["exa", "parallel-mcp", "parallel", "tavily", "firecrawl", "searxng", "ddgs"],
    "extract_order": ["tavily", "parallel-mcp", "parallel", "firecrawl", "exa"],
    "fallback_on_empty": True,
    "rate_limit_cooldown_seconds": 900,
    "auth_cooldown_seconds": 21600,
    "forbidden_cooldown_seconds": 3600,
    "transient_cooldown_seconds": 120,
    "parallel_mcp_enabled": True,
    "parallel_mcp_url": "https://search.parallel.ai/mcp",
    "parallel_mcp_timeout_seconds": 45,
    "deep_primary_order": ["exa", "parallel-mcp", "tavily"],
    "deep_fallback_order": ["parallel", "firecrawl", "searxng", "ddgs"],
    "deep_min_results": 8,
    "deep_min_successful_providers": 2,
    "deep_fallback_parallelism": 3,
}

DEEP_WEB_SEARCH_SCHEMA = {
    "name": "deep_web_search",
    "description": (
        "Parallel multi-provider discovery for formal research. Runs complementary queries across "
        "multiple search providers, deduplicates URLs, records provider agreement, and falls back "
        "to secondary providers when coverage is thin. Use ordinary web_search for simple lookups."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Research objective."},
            "queries": {"type": "array", "items": {"type": "string"}, "maxItems": 4,
                        "description": "Optional 2-4 complementary search queries assigned across providers."},
            "results_per_provider": {"type": "integer", "minimum": 1, "maximum": 10,
                                     "description": "Results requested from each provider; default 5."},
            "max_results": {"type": "integer", "minimum": 1, "maximum": 50,
                            "description": "Maximum deduplicated results returned; default 20."},
            "parallelism": {"type": "integer", "minimum": 1, "maximum": 4,
                            "description": "Primary providers called concurrently; default 3."},
        },
        "required": ["query"],
    },
}


def _settings_from_context(ctx: Any) -> dict[str, Any]:
    return {key: ctx.get_config(key, default) for key, default in DEFAULT_SETTINGS.items()}


def _handle_deep_web_search(args: dict, provider: ResilientWebProvider) -> str:
    query = str(args.get("query") or "").strip()
    if not query:
        return tool_error("query is required")
    raw_queries = args.get("queries") or []
    if not isinstance(raw_queries, list) or any(not isinstance(value, str) for value in raw_queries):
        return tool_error("queries must be an array of strings")
    if len(raw_queries) > 4:
        return tool_error("queries accepts at most 4 items")
    try:
        result = provider.deep_search(
            query,
            queries=raw_queries,
            results_per_provider=int(args.get("results_per_provider", 5)),
            max_results=int(args.get("max_results", 20)),
            parallelism=int(args.get("parallelism", 3)),
        )
    except (TypeError, ValueError) as exc:
        return tool_error(str(exc))
    return tool_result(result)


def register(ctx) -> None:
    provider = ResilientWebProvider(settings=_settings_from_context(ctx))
    ctx.register_web_search_provider(provider)

    def handle(args: dict, **kwargs: Any) -> str:
        return _handle_deep_web_search(args, provider)

    ctx.register_tool(
        name="deep_web_search", toolset="web", schema=DEEP_WEB_SEARCH_SCHEMA,
        handler=handle, check_fn=provider.is_available,
        description="Parallel multi-provider web discovery for formal research.",
    )
