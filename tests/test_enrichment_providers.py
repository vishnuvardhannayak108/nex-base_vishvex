"""Paid providers never report spend they did not make."""
from __future__ import annotations

from nexbase.enrichment.apollo import ApolloProvider
from nexbase.enrichment.zoominfo import ZoomInfoProvider


def test_unconfigured_providers_are_not_billable(settings):
    contacts = [{"name": "Jane Doe", "title": "Owner", "email": "jane@acme.com"}]
    for provider in (ZoomInfoProvider(settings), ApolloProvider(settings)):
        result = provider.enrich("Acme", "acme.com", contacts)
        assert result.ok is False
        assert result.error == "NOT_CONFIGURED"
        assert result.billable is False
        assert result.credit_cost == 0
