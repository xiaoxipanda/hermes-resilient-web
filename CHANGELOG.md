# Changelog

All notable changes to this project are documented here.

## 0.2.0 - 2026-10-02

- Added a complete Simplified Chinese README.
- Added language navigation and Mermaid architecture diagrams to both READMEs.
- Added integration CI against the latest stable Hermes release and Hermes `main`.
- Replaced the private keyless-registry dependency with public provider capability checks.
- Isolated built-in provider imports behind a compatibility module so missing providers do not prevent plugin loading.

## 0.1.0 - 2026-10-02

- Added ordered search and extraction failover across Hermes web providers.
- Added persistent failure classification and cooldowns.
- Added anonymous Parallel Search MCP support.
- Added `deep_web_search` with bounded concurrency, URL deduplication, provider-agreement ranking, and a fallback wave.
- Preserved Hermes profile and secret context across parallel worker threads.
- Added cross-platform advisory state locking and failure isolation.
- Added offline unit tests, configuration examples, and security documentation.
