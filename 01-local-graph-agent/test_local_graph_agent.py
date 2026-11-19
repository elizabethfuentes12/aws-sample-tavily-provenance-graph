"""Same steps as test_local_graph_agent.ipynb, as a script: uv run python test_local_graph_agent.py

A Strands research agent with Tavily web tools and a Neo4j knowledge graph as its memory store.
"""

import os

import certifi
from dotenv import load_dotenv

# macOS's /etc/ssl/cert.pem lacks the root CA AuraDB uses (SSL.com TLS RSA Root CA 2022); certifi has it.
# Verification stays on. Not needed on Linux or on AgentCore.
os.environ.setdefault("SSL_CERT_FILE", certifi.where())
load_dotenv()
TAVILY_KEY = os.getenv("TAVILY_API_KEY") or os.getenv("TAVILY_KEY")
print("OpenAI key:", bool(os.getenv("OPENAI_API_KEY")), "| Tavily key:", bool(TAVILY_KEY), "| Neo4j:", os.getenv("NEO4J_URI"))

from strands.models.openai import OpenAIModel

model = OpenAIModel(model_id="gpt-4o-mini")

# Amazon Bedrock, as the agent runs on AgentCore (Demo 02):
# from strands.models import BedrockModel
# model = BedrockModel(model_id="us.anthropic.claude-sonnet-4-5-20250929-v1:0", region_name="us-east-1")

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

from neo4j import GraphDatabase

from web_facts_store import WebFactsGraph

# Local Neo4j or AuraDB: only the values in .env change (see .env.example).
NEO4J_USER = os.getenv("NEO4J_USERNAME") or os.getenv("NEO4J_USER", "neo4j")   # Aura's credentials file says NEO4J_USERNAME
driver = GraphDatabase.driver(os.environ["NEO4J_URI"], auth=(NEO4J_USER, os.environ["NEO4J_PASSWORD"]))
driver.verify_connectivity()
web_facts = WebFactsGraph(driver, database=os.getenv("NEO4J_DATABASE", "neo4j"))
web_facts.clear()        # start empty; deletes only WebFact, WebEntity and WebSource nodes
print(web_facts.stats())

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

researcher("Which companies make the batteries for the best-selling electric cars?")

researcher("Where are the largest EV battery factories, and which companies run them?")

researcher("Which countries mine and refine most of the lithium used in EV batteries?")

print(web_facts.stats())
for fact in web_facts.facts():
    print(f"{fact['subject']} -[{fact['predicate']}]-> {fact['object']}   {fact['source']}")

import asyncio

for entry in asyncio.run(web_facts.search("Who supplies the batteries in Tesla cars?")):
    print(entry.content)

new_session = Agent(
    model=model,
    system_prompt=SYSTEM_PROMPT,
    tools=web_tools,
    memory_manager=MemoryManager(stores=[web_facts], search_tool_config=RECALL_TOOL),
    hooks=[web_facts],
)

new_session("Who supplies the batteries in Tesla cars?")

new_session("Do the big battery makers build factories in the countries that refine their lithium?")

no_memory = Agent(model=model, system_prompt=SYSTEM_PROMPT, tools=web_tools)
no_memory("Who supplies the batteries in Tesla cars?")

from web_facts_store import VISUALIZE_QUERIES

for name, query in VISUALIZE_QUERIES.items():
    print(f"// {name}\n{query}\n")

web_facts.clear()
driver.close()
