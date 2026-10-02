# Security Policy

## Supported versions

Security fixes are applied to the latest released version.

## Reporting a vulnerability

Use the repository's private GitHub security-advisory form after publication. Do not include credentials, private search queries, or customer data in a public issue.

Include:

- the affected plugin and Hermes versions;
- the provider and configuration path involved;
- minimal reproduction steps using fake credentials or local test doubles;
- the expected impact and any known mitigations.

## Security boundaries

This plugin runs inside the Hermes process and inherits that profile's filesystem and network permissions. It is routing and reliability logic, not a sandbox.

`deep_web_search` intentionally sends query text to multiple configured providers. Provider privacy, retention, billing, and availability policies remain external to this project.
