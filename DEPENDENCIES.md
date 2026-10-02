# Dependency policy

`resilient-web` is a Hermes directory plugin, not a standalone Python
distribution. Its mandatory imports come from the Python standard library and
Hermes, so `plugin.yaml` intentionally declares:

```yaml
python_dependencies: []
```

Provider SDKs stay optional. Hermes owns their installation and resolves them
with the rest of its managed environment. This avoids installing every vendor
SDK for users who only enable one or two providers.

## Compatibility

| Component | Supported or verified version |
|---|---|
| Python used by the plugin | `>=3.11,<3.15` |
| Minimum Hermes Agent | `>=0.21.5` |
| Latest stable Hermes verified on 2026-10-03 | `v2026.9.24` (`>=3.11,<3.14`) |
| Hermes development branch verified on 2026-10-03 | `main@54bc5e5` (`>=3.11,<3.15`) |

Hermes chooses the interpreter for its environment. The plugin does not create
or manage a separate virtual environment.

## Optional provider packages

These are the exact direct pins declared by the verified Hermes revisions:

| Plugin route | Hermes extra | Direct package pins |
|---|---|---|
| Exa | `exa` | `exa-py==2.10.2` |
| Firecrawl | `firecrawl` | `firecrawl-py==4.17.0` |
| Parallel API | `parallel-web` | `parallel-web==0.4.2` |
| Parallel MCP | `mcp` | `mcp==2.0.0`, `httpx2==2.7.0`, `starlette==1.3.1` |
| DDGS | `ddgs` | `ddgs==9.16.0` |
| Tavily and SearXNG | none | Use Hermes core HTTP dependencies |

Hermes `v2026.9.24` contains the DDGS provider but does not expose a `ddgs` PM
extra. The extra is present on Hermes `main`; on the stable release, leave DDGS
out of the route or upgrade when a stable release containing that extra is
available.

Install only the extras used by the configured routes:

```bash
hermes pm install \
  --extra exa \
  --extra parallel-web \
  --extra firecrawl \
  --extra mcp
```

On a Hermes revision that declares the DDGS extra:

```bash
hermes pm install --extra ddgs
```

Do not install provider packages directly into a Hermes-managed environment
with `pip`; that bypasses Hermes' shared dependency resolution and can be lost
on the next environment generation.

## Machine-readable source

[`dependencies.toml`](dependencies.toml) is the repository's machine-readable
compatibility manifest. It records the Python and Hermes ranges, the absence of
mandatory third-party requirements, and the exact optional provider pins.

CI runs:

```bash
python scripts/check-hermes-dependencies.py /path/to/hermes-agent/pyproject.toml
```

against the latest stable Hermes release and `main`. A pin change in Hermes
must be reviewed here before compatibility CI passes again.

This repository deliberately has no `pyproject.toml`: Hermes treats any such
file as a managed workspace member and requests dependency admission during
plugin enablement, even when `[project].dependencies` is empty. That would
change installation behavior without adding a required package.
