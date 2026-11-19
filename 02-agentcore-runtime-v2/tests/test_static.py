"""Offline checks that catch mistakes before a deploy (no AWS, no Neo4j).

- The Tavily OpenAPI schema follows the AgentCore Gateway rules for OpenAPI targets
  (https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/gateway-schema-openapi.html).
- The memory store shared with Demo 01 is an identical copy, and research_agent.py has the same system prompt, recall
  tool and `Agent(...)` call as the Demo 01 notebook, so local and deployed behavior match.
- The Gateway tools (OpenAPI) have the same names, descriptions and parameters as the notebook's @tool functions.
- The three deploy paths (boto3 notebook, CLI notebook, CDK) agree on the entrypoint file and use one schema and one build script.
"""

import json
import pathlib
import re

import nbformat

ROOT = pathlib.Path(__file__).resolve().parent.parent
SCHEMA = json.loads((ROOT / "schemas" / "tavily-openapi.json").read_text())
LOCAL = ROOT.parent / "01-local-graph-agent" / "test_local_graph_agent.py"
AGENT = ROOT / "agent" / "research_agent.py"
SHARED = ("web_facts_store.py",)


def _walk(node):
    if isinstance(node, dict):
        yield node
        for v in node.values():
            yield from _walk(v)
    elif isinstance(node, list):
        for v in node:
            yield from _walk(v)


def test_openapi_version_and_static_server():
    assert SCHEMA["openapi"].startswith(("3.0", "3.1"))
    assert [s["url"] for s in SCHEMA["servers"]] == ["https://api.tavily.com"]
    assert "{" not in SCHEMA["servers"][0]["url"]


def test_operation_ids_are_the_local_tool_names():
    ids = {p: op["operationId"] for p, ops in SCHEMA["paths"].items() for op in ops.values()}
    assert ids == {"/search": "search_web", "/extract": "read_web_page"}


def _local_tool_specs():
    """Run the notebook's web-tools cell (no network: it only defines the tools) and return their tool specs."""
    cell = next(c.source for c in nbformat.read(LOCAL.with_suffix(".ipynb"), as_version=4).cells
                if c.cell_type == "code" and "def search_web(" in c.source)
    scope = {"TAVILY_KEY": "unused"}
    exec(cell, scope)
    return {t.tool_name: t.tool_spec for t in scope["web_tools"]}


def test_gateway_tools_match_the_local_tools():
    """The model must see the same tools locally and on AgentCore: name, description and parameters."""
    norm = lambda text: " ".join(text.split())
    local = _local_tool_specs()
    for ops in SCHEMA["paths"].values():
        op = ops["post"]
        spec = local[op["operationId"]]
        assert norm(op["description"]) == norm(spec["description"]), op["operationId"]
        remote = op["requestBody"]["content"]["application/json"]["schema"]
        params = spec["inputSchema"]["json"]
        assert remote["required"] == params["required"]
        assert list(remote["properties"]) == list(params["properties"])
        for name, prop in params["properties"].items():
            assert norm(remote["properties"][name]["description"]) == norm(prop["description"]), name
            assert remote["properties"][name].get("enum") == prop.get("enum"), name


def test_tool_and_store_descriptions_never_name_the_backend():
    """Descriptions say what the tool is for and how to use it; the model does not need to know about Neo4j."""
    agent = AGENT.read_text()
    recall = agent.split("RECALL_TOOL = MemoryToolConfig(")[1].split("\n)")[0]
    store = (ROOT / "agent" / "web_facts_store.py").read_text().split("    description = ")[1].split("\n")[0]
    texts = [recall, store] + [op["description"] for ops in SCHEMA["paths"].values() for op in ops.values()]
    for text in texts:
        assert not re.search(r"neo4j|graph|cypher|node|index|tavily", text, re.I), text[:80]


def test_no_unsupported_features():
    nodes = list(_walk(SCHEMA))
    assert not any(k in n for n in nodes for k in ("oneOf", "anyOf", "allOf"))
    assert "securitySchemes" not in SCHEMA.get("components", {})
    assert "security" not in SCHEMA
    for ops in SCHEMA["paths"].values():
        for op in ops.values():
            assert set(op.get("requestBody", {}).get("content", {})) <= {"application/json"}


def test_shared_modules_match_demo_01():
    for name in SHARED:
        assert (ROOT / "agent" / name).read_text() == (ROOT.parent / "01-local-graph-agent" / name).read_text(), name


def _source(notebook):
    return "\n".join(c.source for c in nbformat.read(ROOT / notebook, as_version=4).cells if c.cell_type == "code")


def _agent_call(text):
    """The lines of the `researcher = Agent(...)` call, indentation removed."""
    lines = text[text.rindex("researcher = Agent(\n"):].splitlines()
    call = lines[:next(i for i, line in enumerate(lines) if line.strip() == ")") + 1]
    return [line.strip() for line in call]


def test_runtime_agent_matches_the_notebook_agent():
    """Same system prompt and the same Agent(...) call as Demo 01; only model, tools source and Neo4j differ."""
    agent, local = AGENT.read_text(), LOCAL.read_text()
    for block in ("SYSTEM_PROMPT = (", "RECALL_TOOL = MemoryToolConfig("):
        assert agent.split(block)[1].split("\n)")[0] in local, block
    assert _agent_call(agent) == _agent_call(local)
    for piece in ("memory_manager=MemoryManager(stores=[web_facts], search_tool_config=RECALL_TOOL)", "hooks=[web_facts]",
                  "tools=web_tools"):
        assert piece in agent and piece in local, piece


def test_the_three_agentcore_changes_are_marked_and_nothing_else_wraps_the_agent():
    agent = AGENT.read_text()
    assert [f"AgentCore change {n}" in agent for n in (1, 2, 3)] == [True, True, True]
    assert "BedrockModel(" in agent and "aws_iam_streamablehttp_client" in agent and "get_secret_value" in agent
    body = agent.split("def invoke(")[1].split("\napp.run()")[0]
    assert body.count("session_agent(") == 1 and not re.search(r"\b(for|while)\b", body), "no loops around the agent"


def test_v2_startup_and_request_split():
    """runtime-v2-optimize: model, driver and store at startup; Gateway client and Agent built per runtime session
    (on the session's first request, after the snapshot restore), never at startup."""
    agent = AGENT.read_text().split('"""', 2)[2]          # skip the module docstring
    startup, per_session = agent.split("# ── per session")
    for piece in ("BedrockModel(", "GraphDatabase.driver(", "WebFactsGraph("):
        assert piece in startup and piece not in per_session, piece
    for piece in ("MCPClient(", "Agent("):
        assert piece in per_session and piece not in startup, piece
    assert "if session_agent is None:" in per_session.split("@app.entrypoint")[1]
    assert agent.rstrip().endswith("app.run()   # listens on 8080; the V2 snapshot is taken after this starts")


def test_all_deploy_paths_use_the_same_entrypoint_schema_and_build_script():
    entry = AGENT.name
    assert entry == "research_agent.py" and not (ROOT / "agent" / "main.py").exists()
    assert f'entrypoint=[ENTRYPOINT]' in (ROOT / "cdk" / "stack_constructs" / "graph_agent_runtime.py").read_text()
    assert f'ENTRYPOINT = "{entry}"' in (ROOT / "cdk" / "stack_constructs" / "graph_agent_runtime.py").read_text()
    assert f'ENTRYPOINT = "{entry}"' in _source("deploy_with_boto3.ipynb")
    assert f"ENTRYPOINT={entry}" in _source("deploy_with_cli.ipynb")
    for text in (_source("deploy_with_boto3.ipynb"), _source("deploy_with_cli.ipynb"), (ROOT / "cdk" / "deploy.sh").read_text()):
        assert "build_package.sh" in text
    assert "schemas/tavily-openapi.json" in _source("deploy_with_boto3.ipynb")
    assert "schemas/tavily-openapi.json" in _source("deploy_with_cli.ipynb")
    assert not list(ROOT.glob("**/tavily-openapi.json")) or {p.relative_to(ROOT).as_posix() for p in ROOT.glob("**/tavily-openapi.json")
                                                             if "build" not in p.parts and "cdk.out" not in p.parts} == {"schemas/tavily-openapi.json"}
    assert (ROOT / "build_package.sh").exists() and not (ROOT / "cdk" / "build_package.sh").exists()
