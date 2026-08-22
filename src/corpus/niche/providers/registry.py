"""Construction of the fixed niche provider waterfall."""
from __future__ import annotations

from .epo import EPOProvider
from .firecrawl import FirecrawlProvider
from .google_patents import GooglePatentsProvider
from .local import LocalCorpusProvider
from .marec import MarecProvider
from .scrapingbee import ScrapingBeeProvider
from .self_serp import SelfSerpProvider
from .serpapi import SerpApiProvider
from .uspto import USPTOProvider


def build_default_providers(*, local_connection_factory=None):
    return [
        LocalCorpusProvider(local_connection_factory),
        MarecProvider(),
        USPTOProvider(),
        EPOProvider(),
        GooglePatentsProvider(),
        SelfSerpProvider(),
        FirecrawlProvider(),
        ScrapingBeeProvider(),
        SerpApiProvider(),
    ]
