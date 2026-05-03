"""No-op replacement for Chroma's product-telemetry client.

Chroma 0.6.x's built-in `Posthog` telemetry client calls
`posthog.capture(user_id, event_name, properties)` with three positional
args. PostHog SDK 7.x changed `capture()` to take only `event` as a
positional, so every call raises `TypeError` and Chroma logs it at ERROR.
Setting `anonymized_telemetry=False` doesn't help — that flag is checked
*inside* posthog's `capture()` body, but the TypeError fires during
argument binding before the body runs.

Pointing Chroma at this class via `chroma_product_telemetry_impl`
prevents any posthog call from being attempted at all.
"""
from __future__ import annotations

from chromadb.telemetry.product import ProductTelemetryClient, ProductTelemetryEvent
from overrides import override


class NoopTelemetry(ProductTelemetryClient):
    @override
    def capture(self, event: ProductTelemetryEvent) -> None:
        pass
