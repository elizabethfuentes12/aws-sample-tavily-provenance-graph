"""Deterministic tests for the WebFactsGraph memory store (Neo4j only; no model, no web).

They delete the store's own labels (WebFact, WebEntity, WebSource) before and after each test, nothing else,
and skip themselves when Neo4j is not reachable. Strands hook events are built by hand.
"""

import asyncio
import json
import os

import certifi

os.environ.setdefault("SSL_CERT_FILE", certifi.where())   # AuraDB's root CA is missing from macOS /etc/ssl/cert.pem
from types import SimpleNamespace

import pytest
from dotenv import load_dotenv
from neo4j import GraphDatabase
from neo4j.exceptions import AuthError, ServiceUnavailable

from web_facts_store import WebFactsGraph, is_name, urls_in

load_dotenv()

CD = "https://www.caranddriver.com/features/a40991227/electric-car-battery-companies"
EV = "https://www.evemall.eu/selection-guide/top-10-ev-battery-manufacturers-2026-2027"


@pytest.fixture()
def store():
    try:
        driver = GraphDatabase.driver(os.getenv("NEO4J_URI", "neo4j://127.0.0.1:7699"),
                                      auth=(os.getenv("NEO4J_USERNAME") or os.getenv("NEO4J_USER", "neo4j"), os.environ["NEO4J_PASSWORD"]))
        driver.verify_connectivity()
    except (ServiceUnavailable, AuthError, KeyError) as exc:
        pytest.skip(f"Neo4j not reachable or not configured: {exc!r}")
    s = WebFactsGraph(driver)
    s.clear()
    yield s
    s.clear()
    driver.close()


def web_result(name, urls, status="success"):
    body = json.dumps({"results": [{"url": u, "title": "t"} for u in urls]})
    return SimpleNamespace(tool_use={"name": name}, result={"status": status, "content": [{"text": body}]})


def add(store, statement, subject, predicate, obj, url):
    return asyncio.run(store.add(statement, {"subject": subject, "predicate": predicate, "object": obj,
                                             "source_url": url}))


def test_urls_in_reads_tavily_json_and_falls_back_to_bare_urls():
    assert urls_in(json.dumps({"results": [{"url": CD}, {"url": EV}]})) == [CD, EV]
    assert urls_in(f"see {CD}, and {CD} again") == [CD]


def test_hook_records_only_successful_web_tool_results(store):
    store._record_urls(web_result("search_web", [CD]))
    store._record_urls(web_result("tavily___search_web", [EV]))      # AgentCore Gateway tool name
    store._record_urls(web_result("search_web", ["https://failed.example"], status="error"))
    store._record_urls(web_result("recall_web_facts", ["https://memory.example"]))
    assert list(store._seen_urls) == [CD, EV]


def test_a_fact_needs_a_url_a_web_tool_returned(store):
    store._record_urls(web_result("search_web", [CD]))
    assert add(store, "Panasonic supplies batteries to Tesla.", "Panasonic", "supplies batteries to", "Tesla", CD)
    assert add(store, "CATL supplies Tesla.", "CATL", "supplies batteries to", "Tesla", "https://Wikipedia") is None
    assert add(store, "BYD makes batteries.", "BYD", "", "", CD) is None
    assert store.stats() == {"facts": 1, "entities": 2, "sources": 1}


def test_restated_facts_merge_and_entities_are_shared(store):
    store._record_urls(web_result("search_web", [CD, EV]))
    add(store, "Panasonic supplies batteries to Tesla.", "Panasonic", "supplies batteries to", "Tesla", CD)
    add(store, "Panasonic supplies Tesla with batteries.", "panasonic", "Supplies batteries to", "TESLA", CD)
    add(store, "CATL supplies batteries to Tesla.", "CATL", "supplies batteries to", "Tesla", EV)
    assert store.stats() == {"facts": 2, "entities": 3, "sources": 2}


def test_search_returns_the_match_and_its_neighbors(store):
    store._record_urls(web_result("search_web", [CD, EV]))
    add(store, "Panasonic supplies batteries to Tesla.", "Panasonic", "supplies batteries to", "Tesla", CD)
    add(store, "Panasonic has a factory in Nevada.", "Panasonic", "has factory in", "Nevada", EV)
    hits = [e.content for e in asyncio.run(store.search("Who supplies Tesla?"))]
    assert any("supplies batteries to Tesla" in h for h in hits)
    assert any("factory in Nevada" in h for h in hits)          # one entity away, via Panasonic
    assert all("(source: https://" in h for h in hits)


def test_search_with_no_useful_words_returns_nothing(store):
    assert asyncio.run(store.search("what is the")) == []


def test_clear_leaves_other_data_alone(store):
    store._run("CREATE (:Unrelated {name: 'keep'})")
    store.clear()
    assert store._run("MATCH (n:Unrelated) RETURN count(n) AS n")[0]["n"] == 1
    store._run("MATCH (n:Unrelated) DELETE n")


def test_entities_are_names_not_quantities(store):
    assert is_name("CATL") and is_name("us-east-1") and is_name("Model 3")
    assert not is_name("43%") and not is_name("260 GWh") and not is_name("2030")
    store._record_urls(web_result("search_web", [EV]))
    assert add(store, "Australia mines 43% of lithium.", "Australia", "mines lithium share", "43%", EV) is None
