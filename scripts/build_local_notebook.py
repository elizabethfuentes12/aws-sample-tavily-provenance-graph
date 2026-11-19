"""Builds 01-local-graph-agent/test_local_graph_agent.ipynb and test_local_graph_agent.py from the same cells.

Edit the cells here, then run: python scripts/build_local_notebook.py
The notebook and the script stay identical (the script is the code cells in order, minus %pip).
"""
import os
import pathlib

os.chdir(pathlib.Path(__file__).resolve().parent.parent / "01-local-graph-agent")
import json
C = []   # (kind, text)
def md(t): C.append(("markdown", t.strip("\n")))
def code(t): C.append(("code", t.strip("\n")))

md("""
# A Research Agent That Remembers What It Read on the Web

A research agent with web search answers well, once. Ask a related question in a new conversation and it searches
again: the pages it read are gone. This notebook keeps what the agent reads in a Neo4j knowledge graph, using only
pieces of the [Strands Agents](https://strandsagents.com) harness:

| Harness piece | What it does here |
|---|---|
| `@tool` | `search_web` and `read_web_page`: Tavily's search and extract APIs, with descriptions that tell the model when to use them and when not |
| `MemoryManager` | before each model call, searches the graph and injects the matching facts into the prompt; registers the `recall_web_facts` tool |
| `ModelExtractor` | after each turn, the agent's own model turns the answer into facts with subject, predicate, object and source URL |
| `MemoryStore` + hook | `WebFactsGraph` writes those facts to Neo4j, and drops any fact whose URL no web tool returned |

The same agent runs on Amazon Bedrock AgentCore in Demo 02, with three changes: the model (Bedrock), where Tavily
comes from (AgentCore Gateway) and where Neo4j lives (AuraDB).
""")
md("""
## Prerequisites

- `OPENAI_API_KEY` and a Tavily key (`TAVILY_API_KEY` or `TAVILY_KEY`) exported in your shell
- A Neo4j, configured in `.env`: Neo4j AuraDB, or a local instance (`../scripts/setup_local_neo4j.sh`, bolt 7699, no Docker)
""")
md("## Install dependencies")
code("%pip install -q -r requirements.txt")
md("## Configuration\n\nKeys come from your shell; `.env` only adds the Neo4j connection and never overrides what is exported.")
code("""
import os

import certifi
from dotenv import load_dotenv

# macOS's /etc/ssl/cert.pem lacks the root CA AuraDB uses (SSL.com TLS RSA Root CA 2022); certifi has it.
# Verification stays on. Not needed on Linux or on AgentCore.
os.environ.setdefault("SSL_CERT_FILE", certifi.where())
load_dotenv()
TAVILY_KEY = os.getenv("TAVILY_API_KEY") or os.getenv("TAVILY_KEY")
print("OpenAI key:", bool(os.getenv("OPENAI_API_KEY")), "| Tavily key:", bool(TAVILY_KEY), "| Neo4j:", os.getenv("NEO4J_URI"))
""")
md("""
## The model

OpenAI runs the local demo. On AgentCore (Demo 02) the same agent uses Amazon Bedrock: that is the commented line.
""")
code("""
from strands.models.openai import OpenAIModel

model = OpenAIModel(model_id="gpt-4o-mini")

# Amazon Bedrock, as the agent runs on AgentCore (Demo 02):
# from strands.models import BedrockModel
# model = BedrockModel(model_id="us.anthropic.claude-sonnet-4-5-20250929-v1:0", region_name="us-east-1")
""")
md("""
## The web tools

The model sees each tool's name, description and parameters on every call of the loop, and decides from that text
alone. So each description says what the tool does, **when to use it and when not**, what each parameter means, what
comes back, and its limits. The docstring is the description; `Args:` describes each parameter.

On AgentCore (Demo 02) the same two tools come from AgentCore Gateway, defined in `schemas/tavily-openapi.json` with
the same names, descriptions and parameters (a test checks they match). Only the five parameters the model has to
decide are exposed; each tool returns only the fields the model needs.
""")
code('''
import json
import urllib.request
from typing import Literal

from strands import tool


def call_tavily(path: str, body: dict) -> dict:
    request = urllib.request.Request(f"https://api.tavily.com/{path}", data=json.dumps(body).encode(), method="POST",
                                     headers={"Authorization": f"Bearer {TAVILY_KEY}", "Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=60) as response:
        return json.load(response)


@tool
def search_web(query: str, max_results: int = 5, search_depth: Literal["basic", "advanced"] = "basic",
               topic: Literal["general", "news", "finance"] = "general",
               time_range: Literal["day", "week", "month", "year"] | None = None) -> str:
    """Search the live web and return short excerpts with their source URLs.

    Use it for any fact about companies, products, places, figures or events that neither this conversation nor
    recall_web_facts already holds, and for anything recent. Never answer such facts from your own knowledge. Do not
    use it to look up what was said earlier in this conversation, or to re-check or add sources to facts that
    recall_web_facts already returned.
    Returns JSON with `results`, a list of {title, url, content, score}; `content` is a short excerpt, so cite the
    `url` next to each fact taken from it. An empty list means nothing relevant was found; try other keywords once.

    Args:
        query: Keywords with the names involved, not a full question. Example: "CATL battery plant Hungary".
        max_results: Results to return, 1 to 10. Default 5.
        search_depth: "basic" is faster; use "advanced" only when basic excerpts are too short to answer.
        topic: "news" for recent events, "finance" for markets and company financials, "general" otherwise.
        time_range: Only results published in this window. Omit unless the user asks for recent information.
    """
    body = {"query": query, "max_results": min(max(max_results, 1), 10), "search_depth": search_depth, "topic": topic}
    if time_range:
        body["time_range"] = time_range
    found = call_tavily("search", body)["results"]
    return json.dumps({"results": [{k: r.get(k) for k in ("title", "url", "content", "score")} for r in found]})


@tool
def read_web_page(urls: list[str]) -> str:
    """Read the full text of one or more web pages.

    Use it when a search excerpt is too short to confirm a fact, or when the user gives a URL to read. Do not use
    it on pages that neither a search returned nor the user gave. Returns JSON with `results`, a list of
    {url, raw_content}, and `failed_results` for pages that could not be read. Pages can be long, so read at most 3.

    Args:
        urls: Full URLs (https://...) to read, at most 3.
    """
    found = call_tavily("extract", {"urls": urls[:3]})
    return json.dumps({"results": [{"url": r["url"], "raw_content": r.get("raw_content", "")} for r in found["results"]],
                       "failed_results": [{"url": r.get("url")} for r in found.get("failed_results", [])]})

web_tools = [search_web, read_web_page]
''')
md("""
## The graph: a Strands memory store on Neo4j

`WebFactsGraph` (in `web_facts_store.py`) implements Strands' `MemoryStore` contract (`search`, `add`) and is also a
hook. Its graph:

```
(:WebFact {statement, predicate})-[:SUBJECT]->(:WebEntity)
(:WebFact)-[:OBJECT]->(:WebEntity)
(:WebFact)-[:FROM]->(:WebSource {url})
```

Two facts that mention the same entity share its node. `search` returns the facts that match the question plus the
facts one entity away, so a later question can combine earlier answers.
""")
code("""
from neo4j import GraphDatabase

from web_facts_store import WebFactsGraph

# Local Neo4j or AuraDB: only the values in .env change (see .env.example).
NEO4J_USER = os.getenv("NEO4J_USERNAME") or os.getenv("NEO4J_USER", "neo4j")   # Aura's credentials file says NEO4J_USERNAME
driver = GraphDatabase.driver(os.environ["NEO4J_URI"], auth=(NEO4J_USER, os.environ["NEO4J_PASSWORD"]))
driver.verify_connectivity()
web_facts = WebFactsGraph(driver, database=os.getenv("NEO4J_DATABASE", "neo4j"))
web_facts.clear()        # start empty; deletes only WebFact, WebEntity and WebSource nodes
web_facts.stats()
""")
md("## The agent")
code("""
from strands import Agent
from strands.memory import MemoryManager
from strands.memory.types import MemoryToolConfig

# The role, the answer format and the one rule that spans every tool. When to use each tool lives in the tool.
SYSTEM_PROMPT = (
    "You are a research assistant for questions about companies, products, places and markets. "
    "State only facts that come from tool results or from this conversation, never from your own knowledge, and "
    "cite the full source URL (https://...) next to each fact. Answer in 3-5 sentences."
)

# MemoryManager's default search tool is described for user preferences; this agent's memory holds web facts.
RECALL_TOOL = MemoryToolConfig(
    name="recall_web_facts",
    description=(
        "Look up facts this agent already read on the web in earlier conversations. Each result is one sentence "
        "with its source URL. Use it before searching the web, and when a follow-up refers to something said "
        "earlier: write the query with the actual names (for example 'CATL Panasonic LG Korea'), not "
        "'those companies'. When the results answer the question, answer from them and cite their URLs; search "
        "the web only for the part they do not cover. An empty list means nothing is known yet about that query. "
        "It only holds what was read before, so it can be incomplete or out of date."
    ),
)

researcher = Agent(
    model=model,
    system_prompt=SYSTEM_PROMPT,
    tools=web_tools,
    memory_manager=MemoryManager(stores=[web_facts], search_tool_config=RECALL_TOOL),
    hooks=[web_facts],
)
""")
md("""
## How the agent works, on every turn

![How the agent uses the graph on each turn](images/how-the-agent-works.png)

1. **Look in the graph.** Before the model runs, Strands' `MemoryManager` searches `web_facts` with your question and puts the facts it finds in the prompt.
2. **Search the web** with `search_web` only if those facts, `recall_web_facts` and the conversation are not enough.
3. **Save new facts.** After the answer, the agent's model extracts the facts with their source URL and the store writes them to Neo4j.
""")
md("""
## Session 1: fill the graph

The graph is empty, so step A finds nothing and the agent has to call Tavily. That is expected: this session's job is
to fill the graph. After each answer, step C stores the facts.

Note: `researcher` is one agent, so it also keeps this conversation's history. Session 1 does not prove the graph
works; session 2 does, with a new agent.
""")
code('researcher("Which companies make the batteries for the best-selling electric cars?")')
code('researcher("Where are the largest EV battery factories, and which companies run them?")')
code('researcher("Which countries mine and refine most of the lithium used in EV batteries?")')
md("### What the agent stored")
code("""
print(web_facts.stats())
for fact in web_facts.facts():
    print(f"{fact['subject']} -[{fact['predicate']}]-> {fact['object']}   {fact['source']}")
""")
md("""
## What memory returns for a question

This is step A for the Tesla question: the same `search` the `MemoryManager` runs before the model call. The first
five entries are what the next agent will see in its `<memory>` block.

```cypher
CALL db.index.fulltext.queryNodes('web_facts_fulltext', '"Tesla" OR "supplies" OR "batteries" OR "cars"')
YIELD node                                  // facts and entities whose text matches the question's words
// matched entity -> the facts that mention it; then the facts one shared entity away
MATCH (fact:WebFact)-[:FROM]->(source:WebSource)
RETURN fact.statement, source.url
```
""")
code("""
for entry in await web_facts.search("Who supplies the batteries in Tesla cars?"):
    print(entry.content)
""")
md("""
## Session 2: a new conversation, the same graph

`new_session` is a new `Agent`: empty conversation, nothing from session 1 except the graph. For the Tesla question,
step A injects the facts you just saw, so the model can answer without calling Tavily. The second question combines
answers from session 1 (factories and lithium); the agent searches only if the injected facts are not enough.
""")
code("""
new_session = Agent(
    model=model,
    system_prompt=SYSTEM_PROMPT,
    tools=web_tools,
    memory_manager=MemoryManager(stores=[web_facts], search_tool_config=RECALL_TOOL),
    hooks=[web_facts],
)
""")
code('new_session("Who supplies the batteries in Tesla cars?")')
code('new_session("Do the big battery makers build factories in the countries that refine their lithium?")')
md("""
## The same question without the graph

An agent with the same web tools and no memory store. It has to search again.
""")
code("""
no_memory = Agent(model=model, system_prompt=SYSTEM_PROMPT, tools=web_tools)
no_memory("Who supplies the batteries in Tesla cars?")
""")
md("""
## See the graph in Neo4j Browser

AuraDB: open your instance in the Aura console and choose Query. Local instance: http://127.0.0.1:7499. Paste one of these queries.
""")
code("""
from web_facts_store import VISUALIZE_QUERIES

for name, query in VISUALIZE_QUERIES.items():
    print(f"// {name}\\n{query}\\n")
""")
md("## Cleanup\n\nDeletes only the store's nodes (`WebFact`, `WebEntity`, `WebSource`); other data in the database is untouched. To stop the local Neo4j: `../scripts/setup_local_neo4j.sh stop`.")
code("web_facts.clear()\ndriver.close()")

nb = {"cells": [], "metadata": {"kernelspec": {"display_name": "Python 3", "language": "python", "name": "python3"},
      "language_info": {"name": "python", "pygments_lexer": "ipython3"}}, "nbformat": 4, "nbformat_minor": 5}
for i, (kind, text) in enumerate(C):
    cell = {"cell_type": kind, "id": f"cell-{i:02d}", "metadata": {}, "source": text.splitlines(keepends=True)}
    if kind == "code":
        cell.update(execution_count=None, outputs=[])
    nb["cells"].append(cell)
json.dump(nb, open("test_local_graph_agent.ipynb", "w"), indent=1)

# The script: the same code cells, in order, minus the notebook-only %pip line.
header = ('"""Same steps as test_local_graph_agent.ipynb, as a script: uv run python test_local_graph_agent.py\n\n'
          'A Strands research agent with Tavily web tools and a Neo4j knowledge graph as its memory store.\n"""\n\n')
body = "\n\n".join(t for k, t in C if k == "code" and not t.startswith("%pip"))
body = body.replace("web_facts.stats()\n", "print(web_facts.stats())\n", 1)
body = body.replace("for entry in await web_facts.search(", "import asyncio\n\nfor entry in asyncio.run(web_facts.search(")
body = body.replace('web_facts.search("Who supplies the batteries in Tesla cars?"):', 'web_facts.search("Who supplies the batteries in Tesla cars?")):')
pathlib.Path("test_local_graph_agent.py").write_text(header + body + "\n")
print(len(C), "cells")
