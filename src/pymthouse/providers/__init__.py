"""Cross-cutting providers (the Providers lane in ARCHITECTURE.md).

Providers are sibling to domains, not parent. A domain's `service.py` may
take a provider as an argument; a provider may not import from any domain.
"""
