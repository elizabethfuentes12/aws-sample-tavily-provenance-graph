"""The Demo 01 research agent on Amazon Bedrock AgentCore Runtime (platform version V2).

This file reads like test_local_graph_agent.ipynb: model, web tools, Neo4j graph store, system prompt, recall tool,
and the same `researcher = Agent(...)` call. Three things change for AgentCore, marked "AgentCore change":
  1. model:  Amazon Bedrock (BedrockModel) instead of OpenAI
  2. tools:  search_web and read_web_page from AgentCore Gateway (MCPClient, SigV4) instead of local @tool functions
  3. graph:  Neo4j AuraDB, credentials from AWS Secrets Manager, instead of a local Neo4j
plus the BedrockAgentCoreApp entrypoint.

V2 starts the agent from a snapshot taken after startup
(https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-v2-optimize.html):
the model, the Neo4j driver and the store are built at module scope (startup); the Gateway connection and the
Agent are built per request, so the Gateway tool list is never frozen in the snapshot.
"""

import json
import os
import re

import boto3
from bedrock_agentcore.runtime import BedrockAgentCoreApp
from mcp_proxy_for_aws.client import aws_iam_streamablehttp_client
from neo4j import GraphDatabase
from strands import Agent
from strands.memory import MemoryManager
from strands.memory.types import MemoryToolConfig
from strands.models import BedrockModel
from strands.tools.mcp import MCPClient

from web_facts_store import WebFactsGraph

REGION = os.environ.get("AWS_REGION", "us-east-1")

# ── startup (captured in the V2 snapshot) ────────────────────────────────────

# AgentCore change 1: Amazon Bedrock instead of OpenAI
model = BedrockModel(model_id=os.environ.get("BEDROCK_MODEL_ID", "us.anthropic.claude-sonnet-4-5-20250929-v1:0"),
                     region_name=REGION)

# AgentCore change 3: AuraDB, credentials {"uri","username","password","database"} from Secrets Manager
aura = json.loads(boto3.client("secretsmanager", region_name=REGION)
                  .get_secret_value(SecretId=os.environ["NEO4J_SECRET_ID"])["SecretString"])
driver = GraphDatabase.driver(aura["uri"], auth=(aura["username"], aura["password"]))
web_facts = WebFactsGraph(driver, database=aura.get("database", "neo4j"))

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

app = BedrockAgentCoreApp()


# ── per session ──────────────────────────────────────────────────────────────
# One runtimeSessionId = one microVM that keeps its context between invocations
# (https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-sessions.html).
# So one Agent per microVM, created on the session's first request: the same runtimeSessionId continues the
# conversation (like `researcher` in the notebook); a new runtimeSessionId is a new Agent with the same graph
# (like `new_session`). Created after the V2 snapshot restore, so the Gateway tool list is never frozen in it.
session_agent = None


def new_researcher() -> Agent:
    # AgentCore change 2: the same two web tools (search_web, read_web_page) come from AgentCore Gateway
    # (IAM inbound, SigV4), defined in schemas/tavily-openapi.json; Gateway names them tavily___<operationId>.
    web_tools = [MCPClient(
        lambda: aws_iam_streamablehttp_client(endpoint=os.environ["TAVILY_GATEWAY_URL"],
                                              aws_service="bedrock-agentcore", aws_region=REGION),
        tool_filters={"allowed": [re.compile(r"^tavily___")]},
    )]
    researcher = Agent(
        model=model,
        system_prompt=SYSTEM_PROMPT,
        tools=web_tools,
        memory_manager=MemoryManager(stores=[web_facts], search_tool_config=RECALL_TOOL),
        hooks=[web_facts],
    )
    return researcher


def user_turns(agent: Agent) -> int:
    """User questions in this session's conversation (tool results are also 'user' messages; skip them)."""
    return sum(1 for m in agent.messages if m["role"] == "user" and any("text" in b for b in m["content"]))


def tools_this_turn(agent: Agent) -> list[str]:
    """Tools the model called since the last user question, in order. (result.metrics.tool_metrics accumulates over
    the whole session, because one Agent serves every request of a runtime session.)"""
    called = []
    for message in reversed(agent.messages):
        if message["role"] == "user" and any("text" in b for b in message["content"]):
            break
        called += [b["toolUse"]["name"] for b in reversed(message["content"]) if "toolUse" in b]
    return called[::-1]


@app.entrypoint
def invoke(payload, context=None):
    global session_agent
    if session_agent is None:                     # first request of this runtime session (microVM)
        session_agent = new_researcher()
    result = session_agent(payload["prompt"])
    return {
        "answer": str(result),
        "tools_used": tools_this_turn(session_agent),
        "session_id": getattr(context, "session_id", None),
        "turns_in_session": user_turns(session_agent),
    }


app.run()   # listens on 8080; the V2 snapshot is taken after this starts
