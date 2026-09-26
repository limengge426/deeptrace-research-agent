"""Knowledge-graph extraction and entity linking (offline), and graph retrieval on a real Neo4j.

The Neo4j tests run when NEO4J_TEST_URI (plus NEO4J_TEST_USER / NEO4J_TEST_PASSWORD) is set; CI starts
a Neo4j service. They reset the graph, so never point them at a database you care about.
"""

import asyncio
import os

import pytest

from deeptrace_agent.graph import CooccurrenceExtractor, link_entities
from deeptrace_agent.search import SearchHit

CHUNKS = [
    SearchHit("pumps.md#0", "Heat pump", "A heat pump moves heat. Carrier and Daikin sell heat pumps in Japan."),
    SearchHit("pumps.md#1", "Heat pump", "Daikin makes heat pumps for Japan and Europe."),
    SearchHit("coal.md#0", "Coal", "Coal plants in Europe emit carbon dioxide. Daikin does not build them."),
    SearchHit("bw.md#0", "Bretton Woods system", "The Bretton Woods system tied currencies to the US dollar."),
]


def test_cooccurrence_extractor_keeps_recurring_names_and_links_them():
    ex = CooccurrenceExtractor(titles=["Heat pump", "Bretton Woods system"], min_count=2).extract(CHUNKS)
    assert {"Heat pump", "Daikin", "Japan", "Europe"} <= ex[0].entities | ex[1].entities
    assert "Carrier" not in ex[0].entities  # mentioned once: below min_count
    assert ("Daikin", "co_occurs", "Japan") in ex[1].relations
    assert "Bretton Woods system" in ex[3].entities


def test_entity_linking_prefers_the_longest_name():
    names = ["Bretton Woods", "Bretton Woods system", "Woods", "Great Depression", "IMF"]
    assert link_entities("How did the Great Depression shape the Bretton Woods system?", names) == [
        "Bretton Woods system", "Great Depression"]
    assert link_entities("nothing relevant", names) == []


NEO4J = os.getenv("NEO4J_TEST_URI")


@pytest.mark.skipif(not NEO4J, reason="NEO4J_TEST_URI not set")
def test_graph_build_and_search_on_neo4j(tmp_path):
    from deeptrace_agent.graph import GraphSearch, build_graph

    corpus = tmp_path / "docs"
    corpus.mkdir()
    (corpus / "pumps.md").write_text("# Heat pump\n\nDaikin makes heat pumps for Japan.\n\nDaikin also sells in Europe.")
    (corpus / "coal.md").write_text("# Coal\n\nCoal plants in Europe emit carbon dioxide.\n\nEurope is phasing out coal.")
    (corpus / "tea.md").write_text("# Tea\n\nJapan grows green tea.\n\nTea in Japan is steamed.")
    creds = dict(uri=NEO4J, user=os.getenv("NEO4J_TEST_USER", "neo4j"), password=os.environ["NEO4J_TEST_PASSWORD"])

    from deeptrace_agent.search import LocalCorpusSearch

    expected_chunks = len(LocalCorpusSearch(corpus, chunk_chars=60).chunks)
    counts = build_graph(corpus, CooccurrenceExtractor(titles=["Heat pump", "Coal", "Tea"], min_count=2),
                         chunk_chars=60, **creds)
    assert counts["chunks"] == expected_chunks and counts["entities"] >= 4 and counts["relations"] >= 1

    graph = GraphSearch(creds["uri"], creds["user"], creds["password"])
    try:
        assert "Daikin" in graph.seeds("Where does Daikin sell heat pumps?")
        hits = asyncio.run(graph.search("What does Daikin make?", 3))
        assert hits and hits[0].url.startswith("pumps.md")
        assert asyncio.run(graph.search("quantum chromodynamics", 3)) == []  # no linked entity, no results
    finally:
        graph.close()
