# Contributing

## Development setup

Use Python 3.11 through 3.14. The unit tests stub Hermes modules and require no network access:

```bash
python -m unittest discover -s tests -v
python -m compileall -q .
```

When Hermes is installed locally, also run:

```bash
hermes plugins validate . --json
```

## Change requirements

- Preserve the native Hermes `WebSearchProvider` response contracts.
- Keep provider failures isolated; one provider must not abort another provider's work.
- Add focused tests for routing, cooldown, aggregation, or output-shape changes.
- Never commit API keys, tokens, private URLs, user identifiers, or captured search content.
- Document any new network destination and whether it receives queries or fetched URLs.
- Keep provider dependencies optional and loaded through Hermes where possible.

## Pull requests

Describe the behavioral change, affected providers, test evidence, and compatibility impact. Avoid unrelated formatting or generated-file churn.
