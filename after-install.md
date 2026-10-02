# Configure resilient-web

1. Set `web.search_backend` and `web.extract_backend` to `resilient-web`.
2. Add provider credentials to the target profile's `.env`.
3. Review `config.example.yaml` and configure plugin settings as needed.
4. Review `DEPENDENCIES.md` and prepare only the Hermes extras used by your routes.
5. Start a new conversation so the `deep_web_search` tool schema is loaded.

Do not place API keys in `config.yaml`. Deep search sends query text to multiple configured providers.
