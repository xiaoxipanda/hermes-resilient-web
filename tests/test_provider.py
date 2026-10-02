from __future__ import annotations

import asyncio
import contextvars
import importlib.util
import logging
import os
import sys
import tempfile
import threading
import time
import types
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]


class WebSearchProvider:
    pass


def _package(name: str) -> types.ModuleType:
    module = types.ModuleType(name)
    module.__path__ = []
    sys.modules[name] = module
    return module


def _install_hermes_stubs() -> None:
    _package("agent")
    web_search_provider = types.ModuleType("agent.web_search_provider")
    web_search_provider.WebSearchProvider = WebSearchProvider
    web_search_provider.get_provider_env = lambda name: os.getenv(name, "")
    sys.modules["agent.web_search_provider"] = web_search_provider

    constants = types.ModuleType("hermes_constants")
    constants.get_hermes_home = lambda: os.getenv("HERMES_HOME", tempfile.gettempdir())
    sys.modules["hermes_constants"] = constants

    _package("plugins")
    _package("plugins.web")
    providers = {
        "ddgs": "DDGSWebSearchProvider",
        "exa": "ExaWebSearchProvider",
        "firecrawl": "FirecrawlWebSearchProvider",
        "parallel": "ParallelWebSearchProvider",
        "searxng": "SearXNGWebSearchProvider",
        "tavily": "TavilyWebSearchProvider",
    }
    for package_name, class_name in providers.items():
        _package(f"plugins.web.{package_name}")
        module = types.ModuleType(f"plugins.web.{package_name}.provider")
        provider_class = type(
            class_name,
            (WebSearchProvider,),
            {
                "is_available": lambda self: False,
                "supports_search": lambda self: True,
                "supports_extract": lambda self: False,
            },
        )
        setattr(module, class_name, provider_class)
        sys.modules[module.__name__] = module


def _load_provider_module():
    _install_hermes_stubs()
    package = types.ModuleType("resilient_web_plugin")
    package.__path__ = [str(ROOT)]
    sys.modules[package.__name__] = package
    spec = importlib.util.spec_from_file_location(
        "resilient_web_plugin.provider", ROOT / "provider.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


provider_module = _load_provider_module()
compat_module = sys.modules["resilient_web_plugin.compat"]


def row(url: str, title: str, position: int = 1, description: str = "") -> dict:
    return {
        "url": url,
        "title": title,
        "description": description or title,
        "position": position,
    }


class FakeProvider:
    def __init__(
        self,
        *,
        search_result=None,
        search_error: Exception | None = None,
        extract_result=None,
        delay: float = 0,
        tracker: dict | None = None,
    ) -> None:
        self.search_result = search_result
        self.search_error = search_error
        self.extract_result = extract_result
        self.delay = delay
        self.tracker = tracker
        self.search_calls = []
        self.extract_calls = []

    def is_available(self):
        return True

    def supports_search(self):
        return True

    def supports_extract(self):
        return self.extract_result is not None

    def search(self, query, limit=5):
        self.search_calls.append((query, limit))
        if self.tracker is not None:
            with self.tracker["lock"]:
                self.tracker["active"] += 1
                self.tracker["peak"] = max(
                    self.tracker["peak"], self.tracker["active"]
                )
        try:
            time.sleep(self.delay)
            if self.search_error is not None:
                raise self.search_error
            return self.search_result
        finally:
            if self.tracker is not None:
                with self.tracker["lock"]:
                    self.tracker["active"] -= 1

    def extract(self, urls, **kwargs):
        self.extract_calls.append((list(urls), kwargs))
        return self.extract_result


class CompatibilityLayerTests(unittest.TestCase):
    def test_public_keyless_capability_makes_provider_available(self):
        class KeylessOnly:
            def is_available(self):
                return False

            def is_keyless_available(self):
                return True

        self.assertTrue(
            compat_module.provider_is_available(
                "keyless", KeylessOnly(), logging.getLogger("test")
            )
        )

    def test_keyless_check_still_runs_when_direct_check_raises(self):
        class KeylessOnly:
            def is_available(self):
                raise RuntimeError("direct probe failed")

            def is_keyless_available(self):
                return True

        self.assertTrue(
            compat_module.provider_is_available(
                "keyless", KeylessOnly(), logging.getLogger("test")
            )
        )

    def test_missing_builtin_provider_module_is_skipped(self):
        exa_module = types.SimpleNamespace(
            ExaWebSearchProvider=type("ExaWebSearchProvider", (), {})
        )

        def import_module(name):
            if name == "plugins.web.exa.provider":
                return exa_module
            raise ModuleNotFoundError(name)

        marker = object()
        with mock.patch.object(
            compat_module.importlib, "import_module", side_effect=import_module
        ):
            providers = compat_module.load_builtin_providers(
                additional=(("parallel-mcp", marker),),
                logger=logging.getLogger("test"),
            )

        self.assertIs(providers["parallel-mcp"], marker)
        self.assertEqual(type(providers["exa"]).__name__, "ExaWebSearchProvider")
        self.assertEqual(set(providers), {"parallel-mcp", "exa"})

    def test_plugin_does_not_reference_private_keyless_registry_api(self):
        source = (ROOT / "provider.py").read_text() + (ROOT / "compat.py").read_text()
        self.assertNotIn("_keyless_tier_enabled", source)


class ResilientWebProviderTests(unittest.TestCase):
    def router(self, providers: dict, settings: dict):
        directory = Path(tempfile.mkdtemp())
        return provider_module.ResilientWebProvider(
            providers=providers,
            settings=settings,
            state_path=directory / "state.json",
        )

    def test_sequential_search_falls_back_and_cools_down_failure(self):
        first = FakeProvider(search_error=RuntimeError("HTTP 429 rate limit"))
        second = FakeProvider(
            search_result={"success": True, "data": {"web": [row("https://b.test", "B")]}}
        )
        router = self.router(
            {"a": first, "b": second},
            {"search_order": ["a", "b"], "rate_limit_cooldown_seconds": 60},
        )

        result = router.search("query")
        again = router.search("query")

        self.assertTrue(result["success"])
        self.assertEqual(result["data"]["provider_used"], "b")
        self.assertEqual(result["data"]["attempted_providers"], ["a", "b"])
        self.assertEqual(len(first.search_calls), 1)
        self.assertEqual(len(second.search_calls), 2)
        self.assertTrue(again["success"])

    def test_parallel_primary_calls_overlap_and_receive_distinct_queries(self):
        providers = {
            name: FakeProvider(
                search_result={
                    "success": True,
                    "data": {"web": [row(f"https://{name}.test", name)]},
                },
                delay=0.1,
            )
            for name in ("a", "b", "c")
        }
        router = self.router(
            providers,
            {
                "deep_primary_order": ["a", "b", "c"],
                "deep_fallback_order": [],
                "deep_min_results": 1,
                "deep_min_successful_providers": 3,
            },
        )

        started = time.monotonic()
        result = router.deep_search(
            "objective", queries=["q1", "q2", "q3"], parallelism=3
        )
        elapsed = time.monotonic() - started

        self.assertLess(elapsed, 0.25)
        self.assertFalse(result["data"]["fallback_used"])
        self.assertEqual(
            [providers[name].search_calls[0][0] for name in ("a", "b", "c")],
            ["q1", "q2", "q3"],
        )

    def test_parallel_workers_preserve_profile_context(self):
        profile_scope = contextvars.ContextVar("profile_scope", default="missing")

        class ContextAwareProvider(FakeProvider):
            def search(self, query, limit=5):
                if profile_scope.get() != "research":
                    raise RuntimeError("profile context was not propagated")
                return super().search(query, limit)

        provider = ContextAwareProvider(
            search_result={
                "success": True,
                "data": {"web": [row("https://context.test", "Context")]},
            }
        )
        router = self.router(
            {"a": provider},
            {
                "deep_primary_order": ["a"],
                "deep_fallback_order": [],
                "deep_min_results": 1,
                "deep_min_successful_providers": 1,
            },
        )
        token = profile_scope.set("research")
        try:
            result = router.deep_search("objective")
        finally:
            profile_scope.reset(token)

        self.assertTrue(result["success"])

    def test_deduplicates_tracking_urls_and_ranks_provider_agreement_first(self):
        providers = {
            "a": FakeProvider(
                search_result={
                    "success": True,
                    "data": {
                        "web": [
                            row(
                                "https://Example.com/article/?utm_source=test&b=2",
                                "Short",
                                2,
                            )
                        ]
                    },
                }
            ),
            "b": FakeProvider(
                search_result={
                    "success": True,
                    "data": {
                        "web": [
                            row("https://example.com/article?b=2#section", "Shared")
                        ]
                    },
                }
            ),
            "c": FakeProvider(
                search_result={
                    "success": True,
                    "data": {"web": [row("https://unique.test/story", "Unique")]},
                }
            ),
        }
        router = self.router(
            providers,
            {
                "deep_primary_order": ["a", "b", "c"],
                "deep_fallback_order": [],
                "deep_min_results": 1,
                "deep_min_successful_providers": 3,
            },
        )

        result = router.deep_search("objective")

        self.assertEqual(result["data"]["unique_results"], 2)
        self.assertEqual(set(result["data"]["web"][0]["providers"]), {"a", "b"})

    def test_fallback_queue_reaches_fourth_provider_with_parallelism_three(self):
        tracker = {"active": 0, "peak": 0, "lock": threading.Lock()}
        providers = {
            name: FakeProvider(
                search_error=RuntimeError("503 provider unavailable"),
                delay=0.03,
                tracker=tracker,
            )
            for name in ("a", "b", "c", "d", "e", "f")
        }
        providers["g"] = FakeProvider(
            search_result={
                "success": True,
                "data": {"web": [row("https://g.test/result", "Fallback")]},
            },
            delay=0.03,
            tracker=tracker,
        )
        router = self.router(
            providers,
            {
                "deep_primary_order": ["a", "b", "c"],
                "deep_fallback_order": ["d", "e", "f", "g"],
                "deep_min_results": 8,
                "deep_min_successful_providers": 2,
                "deep_fallback_parallelism": 3,
                "transient_cooldown_seconds": 1,
            },
        )

        result = router.deep_search("objective", parallelism=3)

        self.assertTrue(result["success"])
        self.assertTrue(result["data"]["fallback_used"])
        self.assertEqual(len(providers["g"].search_calls), 1)
        self.assertLessEqual(tracker["peak"], 3)

    def test_malformed_provider_response_does_not_abort_fallback(self):
        broken = FakeProvider(search_result={"success": True, "data": None})
        fallback = FakeProvider(
            search_result={
                "success": True,
                "data": {"web": [row("https://fallback.test", "Fallback")]},
            }
        )
        router = self.router(
            {"broken": broken, "fallback": fallback},
            {
                "deep_primary_order": ["broken"],
                "deep_fallback_order": ["fallback"],
                "deep_min_results": 1,
                "deep_min_successful_providers": 1,
            },
        )

        result = router.deep_search("objective")

        self.assertTrue(result["success"])
        reports = {item["provider"]: item for item in result["data"]["provider_reports"]}
        self.assertEqual(reports["broken"]["status"], "failed")
        self.assertEqual(reports["fallback"]["status"], "success")

    def test_extract_uses_next_provider_after_error(self):
        first = FakeProvider(
            extract_result=[
                {
                    "url": "https://source.test",
                    "content": "",
                    "error": "HTTP 403",
                }
            ]
        )
        second = FakeProvider(
            extract_result=[
                {
                    "url": "https://source.test",
                    "title": "Source",
                    "content": "body",
                    "raw_content": "body",
                }
            ]
        )
        router = self.router(
            {"a": first, "b": second},
            {"extract_order": ["a", "b"], "forbidden_cooldown_seconds": 60},
        )

        result = asyncio.run(router.extract(["https://source.test"]))

        self.assertEqual(result[0]["content"], "body")
        self.assertEqual(result[0]["metadata"]["router_provider"], "b")
        self.assertEqual(result[0]["metadata"]["attempted_providers"], ["a", "b"])

    def test_order_can_be_explicitly_empty(self):
        self.assertEqual(provider_module._ordered([], ("a", "b")), ())

    def test_error_redaction_removes_credential_shaped_values(self):
        raw = "authorization: secret-value-123 and tvly-exampleSecret_123456"
        cleaned = provider_module._clean_error(raw)
        self.assertNotIn("secret-value-123", cleaned)
        self.assertNotIn("tvly-exampleSecret_123456", cleaned)
        self.assertIn("[REDACTED]", cleaned)


class PluginRegistrationTests(unittest.TestCase):
    def test_registers_provider_and_deep_search_in_web_toolset(self):
        tools = _package("tools")
        registry = types.ModuleType("tools.registry")
        registry.tool_error = lambda message: {"ok": False, "error": message}
        registry.tool_result = lambda value: {"ok": True, "result": value}
        sys.modules["tools.registry"] = registry
        tools.registry = registry

        spec = importlib.util.spec_from_file_location(
            "resilient_web_plugin",
            ROOT / "__init__.py",
            submodule_search_locations=[str(ROOT)],
        )
        plugin = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = plugin
        assert spec.loader is not None
        spec.loader.exec_module(plugin)

        class Context:
            def __init__(self):
                self.provider = None
                self.tool = None

            def get_config(self, key, default=None):
                if key == "deep_min_results":
                    return 12
                return default

            def register_web_search_provider(self, provider):
                self.provider = provider

            def register_tool(self, **kwargs):
                self.tool = kwargs

        context = Context()
        plugin.register(context)

        self.assertEqual(context.provider.name, "resilient-web")
        self.assertEqual(context.provider._settings["deep_min_results"], 12)
        self.assertEqual(context.tool["name"], "deep_web_search")
        self.assertEqual(context.tool["toolset"], "web")
        error = context.tool["handler"](
            {"query": "q", "queries": ["1", "2", "3", "4", "5"]}
        )
        self.assertEqual(error["error"], "queries accepts at most 4 items")


if __name__ == "__main__":
    unittest.main()
