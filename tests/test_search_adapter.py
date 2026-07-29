from competitor_agent.adapters.search import SearchResult, StaticSearchProvider


def test_static_search_provider_returns_isolated_query_results():
    provider = StaticSearchProvider({"agent": [SearchResult("Acme", "https://acme.test", "snippet")]})
    first = provider.search("agent")
    first.clear()
    assert provider.search("agent")[0].title == "Acme"
    assert provider.search("missing") == []
