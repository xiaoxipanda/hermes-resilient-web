from __future__ import annotations

import asyncio
import concurrent.futures as cf
import contextvars
import importlib.util
import inspect
import json
import logging
import os
import re
import tempfile
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from agent.web_search_provider import WebSearchProvider, get_provider_env
from hermes_constants import get_hermes_home
from plugins.web.ddgs.provider import DDGSWebSearchProvider
from plugins.web.exa.provider import ExaWebSearchProvider
from plugins.web.firecrawl.provider import FirecrawlWebSearchProvider
from plugins.web.parallel.provider import ParallelWebSearchProvider
from plugins.web.searxng.provider import SearXNGWebSearchProvider
from plugins.web.tavily.provider import TavilyWebSearchProvider

logger = logging.getLogger(__name__)

_DEFAULT_SEARCH_ORDER = ("exa", "parallel-mcp", "parallel", "tavily", "firecrawl", "searxng", "ddgs")
_DEFAULT_EXTRACT_ORDER = ("tavily", "parallel-mcp", "parallel", "firecrawl", "exa")
_PARALLEL_MCP_URL = "https://search.parallel.ai/mcp"
_PARALLEL_MCP_SESSION_ID = uuid.uuid4().hex
_TRACKING_QUERY_KEYS = {"fbclid", "gclid", "mc_cid", "mc_eid", "ref", "source"}
_STATE_LOCK = threading.RLock()
_SECRET_PATTERNS = (
    re.compile(r"tvly-[A-Za-z0-9_-]+"),
    re.compile(r"p[a-zA-Z0-9_-]{20,}"),
    re.compile(r"(?i)(api[_-]?key|authorization|bearer)([\s:=]+)[^\s,;}]+"),
)


def _clean_error(value: Any) -> str:
    text = str(value or "unknown error").replace("\n", " ")[:500]
    for pattern in _SECRET_PATTERNS:
        text = pattern.sub("[REDACTED]", text)
    return text


def _ordered(value: Any, default: Iterable[str]) -> tuple[str, ...]:
    if value is None:
        return tuple(default)
    if not isinstance(value, (list, tuple)):
        return tuple(default)
    return tuple(str(item).strip().lower() for item in value if str(item).strip())


def _bounded_int(settings: Mapping[str, Any], key: str, default: int,
                 minimum: int, maximum: Optional[int] = None) -> int:
    try:
        value = int(settings.get(key, default))
    except (TypeError, ValueError):
        value = default
    value = max(minimum, value)
    return min(value, maximum) if maximum is not None else value


class ParallelMCPWebProvider(WebSearchProvider):
    """Anonymous Parallel Search MCP normalized to the native Hermes web-provider contract."""

    name = "parallel-mcp"
    display_name = "Parallel Search MCP (anonymous free)"

    def __init__(self, settings: Optional[Mapping[str, Any]] = None) -> None:
        self._settings = dict(settings or {})

    def is_available(self) -> bool:
        if not self._settings.get("parallel_mcp_enabled", True):
            return False
        try:
            return importlib.util.find_spec("mcp") is not None
        except (ImportError, ValueError):
            return False

    def supports_search(self) -> bool:
        return True

    def supports_extract(self) -> bool:
        return True

    async def _call(self, tool: str, arguments: dict) -> dict:
        from mcp import ClientSession
        from mcp.client.streamable_http import streamable_http_client

        async def invoke() -> dict:
            endpoint = str(self._settings.get("parallel_mcp_url") or _PARALLEL_MCP_URL).strip()
            async with streamable_http_client(endpoint) as streams:
                async with ClientSession(streams[0], streams[1]) as session:
                    await session.initialize()
                    result = await session.call_tool(tool, arguments=arguments)
                    texts = [getattr(item, "text", "") for item in result.content]
                    if result.is_error:
                        raise RuntimeError(next((text for text in texts if text), "Parallel MCP returned an error"))
                    for text in texts:
                        try:
                            data = json.loads(text)
                        except (TypeError, ValueError):
                            continue
                        if isinstance(data, dict):
                            return data
                    raise RuntimeError("Parallel MCP returned no JSON result")

        timeout = _bounded_int(self._settings, "parallel_mcp_timeout_seconds", 45, 5, 300)
        return await asyncio.wait_for(invoke(), timeout=timeout)

    def _run_sync(self, factory):
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(factory())
        timeout = _bounded_int(self._settings, "parallel_mcp_timeout_seconds", 45, 5, 300)
        with cf.ThreadPoolExecutor(max_workers=1, thread_name_prefix="parallel-mcp") as pool:
            return pool.submit(lambda: asyncio.run(factory())).result(timeout=timeout + 5)

    def search(self, query: str, limit: int = 5) -> Dict[str, Any]:
        try:
            data = self._run_sync(lambda: self._call("web_search", {
                "objective": query,
                "search_queries": [query],
                "session_id": _PARALLEL_MCP_SESSION_ID,
            }))
            rows = []
            for index, item in enumerate((data.get("results") or [])[:max(1, int(limit))]):
                excerpts = item.get("excerpts") or []
                description = "\n\n".join(str(value) for value in excerpts) if isinstance(excerpts, list) else str(excerpts)
                rows.append({"url": str(item.get("url") or ""), "title": str(item.get("title") or ""),
                             "description": description, "position": index + 1})
            return {"success": True, "data": {"web": rows, "mcp_search_id": data.get("search_id")}}
        except Exception as exc:
            return {"success": False, "error": "Parallel MCP search failed: " + _clean_error(exc)}

    async def extract(self, urls: List[str], **kwargs: Any) -> List[Dict[str, Any]]:
        try:
            data = await self._call("web_fetch", {
                "urls": [str(url) for url in urls],
                "objective": str(kwargs.get("objective") or "Read the requested source")[:200],
                "full_content": True,
                "session_id": _PARALLEL_MCP_SESSION_ID,
            })
            results = []
            for item in data.get("results") or []:
                excerpts = item.get("excerpts") or []
                excerpt_text = "\n\n".join(str(value) for value in excerpts) if isinstance(excerpts, list) else str(excerpts)
                content = str(item.get("full_content") or excerpt_text)
                url = str(item.get("url") or "")
                results.append({"url": url, "title": str(item.get("title") or ""), "content": content,
                                "raw_content": content, "metadata": {"sourceURL": url, "mcp_extract_id": data.get("extract_id")}})
            for item in data.get("errors") or []:
                results.append({"url": str(item.get("url") or ""), "title": "", "content": "", "raw_content": "",
                                "error": str(item.get("message") or item.get("error") or "Parallel MCP extraction failed")})
            return results
        except Exception as exc:
            return [{"url": str(url), "title": "", "content": "", "raw_content": "",
                     "error": "Parallel MCP extraction failed: " + _clean_error(exc)} for url in urls]


class ResilientWebProvider(WebSearchProvider):
    """One native provider surface backed by ordered built-in Hermes providers."""

    name = "resilient-web"
    display_name = "Resilient Web"

    def __init__(self, *, providers: Optional[Mapping[str, WebSearchProvider]] = None,
                 state_path: Optional[Path] = None,
                 settings: Optional[Mapping[str, Any]] = None) -> None:
        self._injected_providers = dict(providers) if providers is not None else None
        self._settings = dict(settings or {})
        cache = Path(get_hermes_home()) / "cache"
        self._state_path = Path(state_path) if state_path is not None else cache / "resilient-web-state.json"

    def supports_search(self) -> bool:
        return True

    def supports_extract(self) -> bool:
        return True

    def is_available(self) -> bool:
        return any(self._provider_available(name, provider) for name, provider in self._providers().items())

    def _providers(self) -> Dict[str, WebSearchProvider]:
        if self._injected_providers is not None:
            return dict(self._injected_providers)
        return {
            "exa": ExaWebSearchProvider(),
            "parallel-mcp": ParallelMCPWebProvider(self._settings),
            "parallel": ParallelWebSearchProvider(),
            "tavily": TavilyWebSearchProvider(),
            "firecrawl": FirecrawlWebSearchProvider(),
            "searxng": SearXNGWebSearchProvider(),
            "ddgs": DDGSWebSearchProvider(),
        }

    def _provider_available(self, name: str, provider: WebSearchProvider) -> bool:
        try:
            if self._injected_providers is not None:
                return bool(provider.is_available())
            if name == "firecrawl":
                if get_provider_env("FIRECRAWL_API_KEY") or get_provider_env("FIRECRAWL_API_URL"):
                    return True
                from agent.web_search_registry import _keyless_tier_enabled
                return bool(_keyless_tier_enabled() and provider.is_keyless_available())
            return bool(provider.is_available())
        except Exception as exc:
            logger.debug("provider %s availability failed: %s", name, _clean_error(exc))
            return False

    @contextmanager
    def _state_lock(self):
        # Hermes serves a profile from one process. A process-local lock plus
        # atomic replace keeps this advisory cooldown cache portable.
        with _STATE_LOCK:
            yield

    def _load_state_unlocked(self) -> dict:
        try:
            data = json.loads(self._state_path.read_text())
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def _save_state_unlocked(self, state: dict) -> None:
        self._state_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd, temp = tempfile.mkstemp(prefix=".resilient-web-state.", dir=self._state_path.parent)
        try:
            with os.fdopen(fd, "w") as stream:
                json.dump(state, stream, ensure_ascii=False, indent=2, sort_keys=True)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.chmod(temp, 0o600)
            os.replace(temp, self._state_path)
        finally:
            if os.path.exists(temp):
                os.unlink(temp)

    def _suspended(self, provider: str) -> Optional[str]:
        now = time.time()
        try:
            with self._state_lock():
                state = self._load_state_unlocked()
                item = state.get(provider)
                if not isinstance(item, dict):
                    return None
                until = float(item.get("until", 0) or 0)
                if until <= now:
                    state.pop(provider, None)
                    self._save_state_unlocked(state)
                    return None
                return str(item.get("reason") or "cooldown")
        except (OSError, TypeError, ValueError) as exc:
            logger.debug("cooldown read failed for %s: %s", provider, _clean_error(exc))
            return None

    def _clear_failure(self, provider: str) -> None:
        try:
            with self._state_lock():
                state = self._load_state_unlocked()
                if provider in state:
                    state.pop(provider, None)
                    self._save_state_unlocked(state)
        except OSError as exc:
            logger.debug("cooldown clear failed for %s: %s", provider, _clean_error(exc))

    @staticmethod
    def _search_rows(result: Any) -> list[dict]:
        if not isinstance(result, dict):
            return []
        data = result.get("data")
        if not isinstance(data, dict):
            return []
        rows = data.get("web")
        if not isinstance(rows, list):
            return []
        return [row for row in rows if isinstance(row, dict)]

    @staticmethod
    def _position(value: Any) -> int:
        try:
            return max(1, int(value))
        except (TypeError, ValueError):
            return 999

    @classmethod
    def _row_key(cls, row: Mapping[str, Any]) -> str:
        url = cls._canonical_url(row.get("url"))
        if url:
            return url
        title = str(row.get("title") or "").strip().lower()
        return f"title:{title}" if title else ""

    def _store_failure(self, provider: str, reason: str, until: float, now: float) -> None:
        try:
            with self._state_lock():
                state = self._load_state_unlocked()
                state[provider] = {"until": until, "reason": reason, "updated_at": now}
                self._save_state_unlocked(state)
        except OSError as exc:
            logger.debug("cooldown write failed for %s: %s", provider, _clean_error(exc))

    @staticmethod
    def _next_month_epoch() -> float:
        now = datetime.now(timezone.utc)
        year, month = (now.year + 1, 1) if now.month == 12 else (now.year, now.month + 1)
        return datetime(year, month, 1, 0, 5, tzinfo=timezone.utc).timestamp()

    def _record_failure(self, provider: str, error: Any) -> str:
        message = _clean_error(error)
        lower = message.lower()
        settings = self._settings
        now = time.time()
        if any(marker in lower for marker in ("402", "quota exceeded", "credits exhausted", "credit balance", "monthly limit")):
            reason, until = "quota", self._next_month_epoch()
        elif any(marker in lower for marker in ("429", "rate limit", "too many requests", "slow down")):
            reason = "rate_limit"
            until = now + _bounded_int(settings, "rate_limit_cooldown_seconds", 900, 1)
        elif any(marker in lower for marker in ("401", "unauthorized", "invalid api key", "authentication")):
            reason = "authentication"
            until = now + _bounded_int(settings, "auth_cooldown_seconds", 21600, 1)
        elif any(marker in lower for marker in ("403", "forbidden")):
            reason = "forbidden"
            until = now + _bounded_int(settings, "forbidden_cooldown_seconds", 3600, 1)
        elif any(marker in lower for marker in ("timeout", "timed out", "connection", "502", "503", "504", "server error")):
            reason = "transient"
            until = now + _bounded_int(settings, "transient_cooldown_seconds", 120, 1)
        else:
            return "request_error"
        self._store_failure(provider, reason, until, now)
        return reason

    def search(self, query: str, limit: int = 5) -> Dict[str, Any]:
        settings = self._settings
        order = _ordered(settings.get("search_order"), _DEFAULT_SEARCH_ORDER)
        providers = self._providers()
        attempted: list[str] = []
        failures: list[str] = []
        for name in order:
            provider = providers.get(name)
            if provider is None or not provider.supports_search() or not self._provider_available(name, provider):
                continue
            if self._suspended(name):
                continue
            attempted.append(name)
            try:
                result = provider.search(query, limit)
            except Exception as exc:
                reason = self._record_failure(name, exc)
                failures.append(f"{name}:{reason}")
                logger.warning("search provider %s failed; trying next: %s", name, _clean_error(exc))
                continue
            rows = self._search_rows(result)
            if isinstance(result, dict) and result.get("success") and (rows or not settings.get("fallback_on_empty", True)):
                self._clear_failure(name)
                routed = dict(result)
                routed_data = dict(result.get("data") or {})
                routed_data["provider_used"] = name
                routed_data["attempted_providers"] = list(attempted)
                routed["data"] = routed_data
                return routed
            error = result.get("error", "empty results") if isinstance(result, dict) else "invalid response"
            reason = self._record_failure(name, error) if error != "empty results" else "empty_results"
            failures.append(f"{name}:{reason}")
            logger.warning("search provider %s returned no usable result; trying next: %s", name, _clean_error(error))
        return {"success": False, "error": "All configured search providers failed or were unavailable.",
                "data": {"attempted_providers": attempted, "failure_categories": failures}}

    @staticmethod
    def _canonical_url(raw: Any) -> str:
        value = str(raw or "").strip()
        if not value:
            return ""
        try:
            parsed = urlsplit(value)
            query = [(key, val) for key, val in parse_qsl(parsed.query, keep_blank_values=True)
                     if not key.lower().startswith("utm_") and key.lower() not in _TRACKING_QUERY_KEYS]
            path = parsed.path.rstrip("/") or "/"
            return urlunsplit((parsed.scheme.lower(), parsed.netloc.lower(), path, urlencode(sorted(query)), ""))
        except Exception:
            return value

    def _parallel_search_one(self, name: str, provider: WebSearchProvider, query: str, limit: int) -> dict:
        try:
            result = provider.search(query, limit)
            rows = self._search_rows(result)
            if isinstance(result, dict) and result.get("success") and rows:
                self._clear_failure(name)
                return {"provider": name, "query": query, "status": "success", "rows": rows}
            error = result.get("error", "empty results") if isinstance(result, dict) else "invalid response"
            reason = self._record_failure(name, error) if error != "empty results" else "empty_results"
            logger.warning("deep search provider %s returned no usable result: %s", name, _clean_error(error))
            return {"provider": name, "query": query, "status": "failed", "reason": reason, "rows": []}
        except Exception as exc:
            reason = self._record_failure(name, exc)
            logger.warning("deep search provider %s failed: %s", name, _clean_error(exc))
            return {"provider": name, "query": query, "status": "failed", "reason": reason, "rows": []}

    def deep_search(self, query: str, *, queries: Optional[List[str]] = None,
                    results_per_provider: int = 5, max_results: int = 20,
                    parallelism: int = 3) -> Dict[str, Any]:
        settings = self._settings
        providers = self._providers()
        primary = _ordered(settings.get("deep_primary_order"), ("exa", "parallel-mcp", "tavily"))
        fallback = _ordered(settings.get("deep_fallback_order"), ("parallel", "firecrawl", "searxng", "ddgs"))
        query_variants = [str(value).strip() for value in (queries or []) if str(value).strip()][:4]
        if not query_variants:
            query_variants = [query]
        per_provider = max(1, min(int(results_per_provider), 10))
        total_limit = max(1, min(int(max_results), 50))
        workers = max(1, min(int(parallelism), 4))
        reports: list[dict] = []
        collected: list[tuple[str, str, dict]] = []

        def run_group(names: tuple[str, ...], cap: int) -> None:
            jobs: list[tuple[str, str, WebSearchProvider]] = []
            for index, name in enumerate(names):
                provider = providers.get(name)
                if provider is None or not provider.supports_search() or not self._provider_available(name, provider):
                    reports.append({"provider": name, "status": "unavailable"})
                    continue
                suspended = self._suspended(name)
                if suspended:
                    reports.append({"provider": name, "status": "cooldown", "reason": suspended})
                    continue
                assigned = query_variants[index % len(query_variants)]
                jobs.append((name, assigned, provider))
            if not jobs:
                return
            with cf.ThreadPoolExecutor(max_workers=min(cap, len(jobs)), thread_name_prefix="deep-web-search") as pool:
                futures = {
                    pool.submit(
                        contextvars.copy_context().run,
                        self._parallel_search_one,
                        name,
                        provider,
                        assigned,
                        per_provider,
                    ): (name, assigned)
                    for name, assigned, provider in jobs
                }
                for future in cf.as_completed(futures):
                    name, assigned = futures[future]
                    try:
                        report = future.result()
                    except Exception as exc:
                        reason = self._record_failure(name, exc)
                        logger.warning("deep search worker %s failed: %s", name, _clean_error(exc))
                        report = {"provider": name, "query": assigned, "status": "failed",
                                  "reason": reason, "rows": []}
                    rows = report.pop("rows")
                    report["result_count"] = len(rows)
                    reports.append(report)
                    collected.extend((report["provider"], report["query"], row) for row in rows if isinstance(row, dict))

        run_group(primary, workers)

        def unique_count() -> int:
            return len({key for _provider, _query, row in collected if (key := self._row_key(row))})

        successful_primary = sum(1 for report in reports if report.get("provider") in primary and report.get("status") == "success")
        min_results = _bounded_int(settings, "deep_min_results", 8, 1, 100)
        min_providers = _bounded_int(settings, "deep_min_successful_providers", 2, 1, 20)
        fallback_used = unique_count() < min_results or successful_primary < min_providers
        if fallback_used:
            run_group(fallback, _bounded_int(settings, "deep_fallback_parallelism", 3, 1, 4))

        provider_rank = {name: index for index, name in enumerate((*primary, *fallback))}
        merged: dict[str, dict] = {}
        for provider_name, used_query, row in collected:
            key = self._row_key(row)
            if not key:
                continue
            description = str(row.get("description") or "")
            current = merged.get(key)
            if current is None:
                current = {
                    "title": str(row.get("title") or ""), "url": str(row.get("url") or ""),
                    "description": description, "providers": [], "queries": [],
                    "_best_provider_rank": provider_rank.get(provider_name, 999),
                    "_best_position": self._position(row.get("position")),
                }
                merged[key] = current
            elif len(description) > len(current["description"]):
                current["description"] = description
            if provider_name not in current["providers"]:
                current["providers"].append(provider_name)
            if used_query not in current["queries"]:
                current["queries"].append(used_query)
            current["_best_provider_rank"] = min(current["_best_provider_rank"], provider_rank.get(provider_name, 999))
            current["_best_position"] = min(current["_best_position"], self._position(row.get("position")))

        ranked = sorted(merged.values(), key=lambda row: (
            -len(row["providers"]), row["_best_provider_rank"], row["_best_position"], row["title"].lower()))[:total_limit]
        for index, row in enumerate(ranked, 1):
            row["position"] = index
            row.pop("_best_provider_rank", None)
            row.pop("_best_position", None)
        return {
            "success": bool(ranked),
            "data": {
                "web": ranked, "mode": "parallel_aggregate", "objective": query,
                "query_variants": query_variants, "provider_reports": reports,
                "fallback_used": fallback_used, "unique_results": len(merged),
                "returned_results": len(ranked),
            },
            **({} if ranked else {"error": "No configured deep-search provider returned usable results."}),
        }

    async def extract(self, urls: List[str], **kwargs: Any) -> List[Dict[str, Any]]:
        settings = self._settings
        order = _ordered(settings.get("extract_order"), _DEFAULT_EXTRACT_ORDER)
        providers = self._providers()
        attempted: list[str] = []
        failures: list[str] = []
        for name in order:
            provider = providers.get(name)
            if provider is None or not provider.supports_extract() or not self._provider_available(name, provider):
                continue
            if self._suspended(name):
                continue
            attempted.append(name)
            try:
                result = provider.extract(urls, **kwargs)
                if inspect.isawaitable(result):
                    result = await result
            except Exception as exc:
                reason = self._record_failure(name, exc)
                failures.append(f"{name}:{reason}")
                logger.warning("extract provider %s failed; trying next: %s", name, _clean_error(exc))
                continue
            usable = [row for row in (result or []) if isinstance(row, dict) and not row.get("error") and row.get("content")]
            if usable:
                self._clear_failure(name)
                routed = []
                for row in result:
                    if isinstance(row, dict):
                        item = dict(row)
                        metadata = dict(row.get("metadata") or {}) if isinstance(row.get("metadata"), dict) else {}
                        metadata["router_provider"] = name
                        metadata["attempted_providers"] = list(attempted)
                        item["metadata"] = metadata
                        routed.append(item)
                    else:
                        routed.append(row)
                return routed
            errors = [row.get("error") for row in (result or []) if isinstance(row, dict) and row.get("error")]
            reason = self._record_failure(name, errors[0] if errors else "empty extraction") if errors else "empty_results"
            failures.append(f"{name}:{reason}")
            logger.warning("extract provider %s returned no usable content; trying next", name)
        return [{"url": str(url), "title": "", "content": "", "raw_content": "",
                 "error": "All configured extraction providers failed or were unavailable.",
                 "metadata": {"sourceURL": str(url), "attempted_providers": attempted,
                              "failure_categories": failures}} for url in urls]
