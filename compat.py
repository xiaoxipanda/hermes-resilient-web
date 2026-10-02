from __future__ import annotations

import importlib
import logging
from typing import Any, Dict, Iterable, Tuple


BUILTIN_PROVIDER_SPECS: Tuple[Tuple[str, str, str], ...] = (
    ("exa", "plugins.web.exa.provider", "ExaWebSearchProvider"),
    ("parallel", "plugins.web.parallel.provider", "ParallelWebSearchProvider"),
    ("tavily", "plugins.web.tavily.provider", "TavilyWebSearchProvider"),
    ("firecrawl", "plugins.web.firecrawl.provider", "FirecrawlWebSearchProvider"),
    ("searxng", "plugins.web.searxng.provider", "SearXNGWebSearchProvider"),
    ("ddgs", "plugins.web.ddgs.provider", "DDGSWebSearchProvider"),
)


def load_builtin_providers(
    *,
    additional: Iterable[tuple[str, Any]] = (),
    logger: logging.Logger,
) -> Dict[str, Any]:
    """Load documented Hermes web-provider modules without making them hard imports."""
    providers: Dict[str, Any] = dict(additional)
    for name, module_name, class_name in BUILTIN_PROVIDER_SPECS:
        try:
            module = importlib.import_module(module_name)
            provider_class = getattr(module, class_name)
            providers[name] = provider_class()
        except (AttributeError, ImportError, ModuleNotFoundError) as exc:
            logger.debug("Hermes provider %s is unavailable: %s", name, exc)
    return providers


def provider_is_available(name: str, provider: Any, logger: logging.Logger) -> bool:
    """Use only the public provider capability methods, with older-version fallback."""
    try:
        if bool(provider.is_available()):
            return True
    except Exception as exc:
        logger.debug("provider %s availability check failed: %s", name, exc)

    keyless_check = getattr(provider, "is_keyless_available", None)
    if not callable(keyless_check):
        return False
    try:
        return bool(keyless_check())
    except Exception as exc:
        logger.debug("provider %s keyless availability check failed: %s", name, exc)
        return False
