"""A Strands MemoryStore that keeps what the agent learns from the web as a Neo4j knowledge graph.

Plug it into the agent's harness and Strands does the rest:

    MemoryManager(stores=[WebFactsGraph(driver)])

- Recall: before each model call the MemoryManager calls `search()` with the user's message and injects the
  matching facts into the prompt (a `<memory>` block). It also registers a search tool (the agents name it
  `recall_web_facts` with `MemoryToolConfig`).
- Write: after each turn, Strands' `ModelExtractor` (using the agent's own model) reads the transcript and calls
  `add()` once per fact, with subject, predicate, object and source URL in `metadata`.

Graph:

    (:WebFact {statement, predicate})-[:SUBJECT]->(:WebEntity {name})
    (:WebFact)-[:OBJECT]->(:WebEntity)
    (:WebFact)-[:FROM]->(:WebSource {url})

Labels are prefixed with `Web` so the store can share a database (for example an AuraDB instance that also
holds neo4j-agent-memory's `Entity`/`Fact` nodes) without mixing data; `clear()` deletes only these three labels.

Two facts that mention the same entity share its node, so recall returns the fact that matched the question
plus the facts one entity away. That is what lets a later question combine earlier answers.

Rules: an entity is a name, not a number; and no source URL, no fact. The store is also a Strands hook: after every web tool call it records the URLs
the tool returned, and `add()` drops any fact whose source URL is not one of them. A citation the model invented
("https://Wikipedia") never reaches the graph. Attach it with `Agent(..., hooks=[store])`.

Strands contract: https://strandsagents.com/docs/user-guide/sdk/memory/managing-memory/#custom-stores
Works with a local Neo4j or Neo4j AuraDB: only the driver's URI changes.
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from collections import OrderedDict
from datetime import datetime, timezone

from strands.hooks import AfterToolCallEvent, HookProvider, HookRegistry
from strands.memory.extraction.model_extractor import ModelExtractor
from strands.memory.extraction.triggers import InvocationTrigger
from strands.memory.types import MemoryEntry, SearchOptions

log = logging.getLogger(__name__)
# AuraDB (Cypher 25) flags db.index.fulltext.queryNodes as deprecated in favor of SEARCH; the procedure still works
# on AuraDB and on self-managed Neo4j, so keep it and keep that warning out of the demo output.
logging.getLogger("neo4j.notifications").setLevel(logging.ERROR)

LABELS = ("WebFact", "WebEntity", "WebSource")
FULLTEXT_INDEX = "web_facts_fulltext"
_WORD = re.compile(r"[A-Za-z0-9][A-Za-z0-9\-]{2,}")
# Local @tool names; on AgentCore Gateway the same tools are named tavily___search_web and tavily___read_web_page.
WEB_TOOL_SUFFIXES = ("search_web", "read_web_page")
_URL = re.compile(r"https?://[^\s\"'<>)\]]+")
_STOP = {"the", "and", "for", "are", "which", "what", "who", "how", "does", "did", "that", "this", "with",
         "from", "same", "most", "into", "their", "there", "they", "where", "when", "used", "use", "make"}

EXTRACTION_PROMPT = (
    "You read a research conversation and extract the facts the assistant stated that came from web pages.\n"
    "Each fact connects two named entities: companies, countries, cities, people, products or organizations. "
    "Both `subject` and `object` must be such a proper name (for example 'CATL', 'Tesla', 'Chile', 'Nevada'), "
    "never a number, a percentage or a description. Put numbers and dates in the sentence instead.\n"
    "`predicate` is a short verb phrase, for example 'supplies batteries to', 'has factory in', 'mines lithium in'. "
    "`source_url` is the full URL (https://...) the assistant cited next to that fact, copied verbatim from the transcript. Skip facts with no full URL.\n"
    'Each array item: {"content": "<one self-contained sentence>", "metadata": {"subject": "...", '
    '"predicate": "...", "object": "...", "source_url": "https://..."}}'
)


def entity_key(name: str) -> str:
    return " ".join(name.lower().split())


def is_name(value: str) -> bool:
    """Entities are names ('CATL', 'Chile'), not quantities ('43%', '260 GWh', '2030')."""
    return "%" not in value and not re.fullmatch(r"[\d\s.,]+(gwh|mwh|kwh|gw|mw|tons?|million|billion)?", value.lower())


def predicate_key(predicate: str) -> str:
    return re.sub(r"[^A-Z0-9]+", "_", predicate.upper()).strip("_")[:64] or "RELATED_TO"


class WebFactsGraph(HookProvider):
    """Strands MemoryStore over Neo4j, and a hook that records which URLs the web tools returned.

    `name`, `description`, `max_search_results`, `writable`, `extraction` are the MemoryStore protocol fields.
    """

    # The model reads this next to the recall tool: say what is inside and how to use it, never the backend.
    description = "Facts read on the web in earlier conversations, each one sentence with its source URL."
    writable = True

    def __init__(self, driver, *, database: str = "neo4j", name: str = "web_facts",
                 max_search_results: int = 10, extraction_model=None):
        self.driver = driver
        self.database = database
        self.name = name
        self.max_search_results = max_search_results
        # After every turn, distill facts from the transcript with the agent's model (or `extraction_model`).
        self.extraction = {
            "trigger": InvocationTrigger(),
            "extractor": ModelExtractor(model=extraction_model, system_prompt=EXTRACTION_PROMPT),
        }
        self._seen_urls: OrderedDict[str, None] = OrderedDict()   # bounded: the last 5000 URLs
        self._init_schema()

    # ── Neo4j plumbing ───────────────────────────────────────────────────────
    def _run(self, query: str, **params) -> list[dict]:
        records, _, _ = self.driver.execute_query(query, params, database_=self.database)
        return [r.data() for r in records]

    def _init_schema(self) -> None:
        self._run("CREATE CONSTRAINT web_entity_key IF NOT EXISTS FOR (e:WebEntity) REQUIRE e.key IS UNIQUE")
        self._run("CREATE CONSTRAINT web_source_url IF NOT EXISTS FOR (s:WebSource) REQUIRE s.url IS UNIQUE")
        self._run("CREATE CONSTRAINT web_fact_key IF NOT EXISTS FOR (f:WebFact) REQUIRE f.key IS UNIQUE")
        self._run(f"CREATE FULLTEXT INDEX {FULLTEXT_INDEX} IF NOT EXISTS FOR (n:WebFact|WebEntity) ON EACH [n.statement, n.name]")

    def clear(self) -> None:
        """Delete only this store's labels. Other data in the database is never touched."""
        for label in LABELS:
            self._run(f"MATCH (n:{label}) DETACH DELETE n")

    # ── Hook: remember the URLs the web tools returned ───────────────────────
    def register_hooks(self, registry: HookRegistry, **kwargs) -> None:
        registry.add_callback(AfterToolCallEvent, self._record_urls)

    def _record_urls(self, event: AfterToolCallEvent) -> None:
        if not event.tool_use["name"].endswith(WEB_TOOL_SUFFIXES) or event.result.get("status") != "success":
            return
        for url in urls_in("".join(b.get("text", "") for b in event.result.get("content", []) if isinstance(b, dict))):
            self._seen_urls[url] = None
            self._seen_urls.move_to_end(url)
        while len(self._seen_urls) > 5000:
            self._seen_urls.popitem(last=False)

    # ── MemoryStore: write ───────────────────────────────────────────────────
    async def add(self, content: str, metadata: dict | None = None) -> dict | None:
        meta = metadata or {}
        subject, predicate, obj = (str(meta.get(k, "")).strip() for k in ("subject", "predicate", "object"))
        url = str(meta.get("source_url", "")).strip()
        if url not in self._seen_urls:
            log.info("web_facts: dropped a fact whose source no web tool returned (%s): %s", url or "none", content[:80])
            return None
        if not (subject and predicate and obj):
            log.info("web_facts: dropped a fact without subject/predicate/object: %s", content[:80])
            return None
        if not (is_name(subject) and is_name(obj)):
            log.info("web_facts: dropped a fact whose subject or object is a number, not a name: %s", content[:80])
            return None
        key = f"{entity_key(subject)}|{predicate_key(predicate)}|{entity_key(obj)}"
        # MERGE on the triple: extraction is at-least-once and later turns restate known facts.
        self._run(
            """
            MERGE (s:WebEntity {key: $s_key}) ON CREATE SET s.name = $s_name
            MERGE (o:WebEntity {key: $o_key}) ON CREATE SET o.name = $o_name
            MERGE (src:WebSource {url: $url})
            MERGE (f:WebFact {key: $key})
              ON CREATE SET f.id = $id, f.statement = $statement, f.predicate = $predicate, f.at = $at
            MERGE (f)-[:SUBJECT]->(s)
            MERGE (f)-[:OBJECT]->(o)
            MERGE (f)-[:FROM]->(src)
            """,
            s_key=entity_key(subject), s_name=subject, o_key=entity_key(obj), o_name=obj, url=url, key=key,
            id=str(uuid.uuid4()), statement=content.strip(), predicate=predicate_key(predicate),
            at=datetime.now(timezone.utc).isoformat(),
        )
        return {"kind": "fact", "key": key}

    # ── MemoryStore: recall ──────────────────────────────────────────────────
    async def search(self, query: str, options: SearchOptions | None = None) -> list[MemoryEntry]:
        """Facts that match the question's words, plus the facts one shared entity away."""
        limit = (options or {}).get("max_search_results") or self.max_search_results
        terms = [w for w in _WORD.findall(query or "") if w.lower() not in _STOP]
        if not terms:
            return []
        rows = self._run(
            f"""
            CALL db.index.fulltext.queryNodes('{FULLTEXT_INDEX}', $lucene) YIELD node, score
            WITH node ORDER BY score DESC LIMIT 10
            CALL (node) {{
              MATCH (node:WebFact) RETURN node AS hit
              UNION
              MATCH (node:WebEntity)<-[:SUBJECT|OBJECT]-(f:WebFact) RETURN f AS hit
            }}
            WITH collect(DISTINCT hit) AS hits
            UNWIND hits AS hit
            OPTIONAL MATCH (hit)-[:SUBJECT|OBJECT]->(:WebEntity)<-[:SUBJECT|OBJECT]-(neighbor:WebFact)
            WITH hits, collect(DISTINCT neighbor) AS neighbors
            UNWIND hits + neighbors AS f
            WITH DISTINCT f
            MATCH (f)-[:FROM]->(src:WebSource)
            RETURN f.statement AS statement, src.url AS url
            LIMIT $limit
            """,
            lucene=" OR ".join(f'"{t}"' for t in terms), limit=limit,
        )
        return [MemoryEntry(content=f"{r['statement']} (source: {r['url']})", store_name=self.name,
                            metadata={"source_url": r["url"]}) for r in rows]

    # ── for showing the graph in the demo ────────────────────────────────────
    def facts(self) -> list[dict]:
        return self._run(
            "MATCH (s:WebEntity)<-[:SUBJECT]-(f:WebFact)-[:OBJECT]->(o:WebEntity), (f)-[:FROM]->(src:WebSource) "
            "RETURN s.name AS subject, f.predicate AS predicate, o.name AS object, src.url AS source ORDER BY f.at")

    def stats(self) -> dict:
        return self._run(
            "OPTIONAL MATCH (f:WebFact) WITH count(f) AS facts OPTIONAL MATCH (e:WebEntity) WITH facts, count(e) AS entities "
            "OPTIONAL MATCH (s:WebSource) RETURN facts, entities, count(s) AS sources")[0]


def urls_in(text: str) -> list[str]:
    """URLs in a web tool result: the `results[].url` of Tavily's JSON, else any bare URL."""
    try:
        data = json.loads(text)
        if isinstance(data, dict) and isinstance(data.get("results"), list):
            found = [r["url"] for r in data["results"] if isinstance(r, dict) and r.get("url")]
            if found:
                return found
    except (json.JSONDecodeError, TypeError):
        pass
    return list(dict.fromkeys(u.rstrip(".,") for u in _URL.findall(text)))


VISUALIZE_QUERIES = {
    "whole_graph": "MATCH p = (:WebEntity)<-[:SUBJECT]-(:WebFact)-[:OBJECT]->(:WebEntity) RETURN p",
    "with_sources": "MATCH p = (:WebFact)-[:FROM]->(:WebSource) RETURN p",
}
