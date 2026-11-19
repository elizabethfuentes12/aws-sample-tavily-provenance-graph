# aws-sample-tavily-provenance-graph

Demos for the talk "Your Agent Searches the Web and Forgets What It Read" (Graphs for Engineering, Part 2,
2026-11-19). A Strands research agent keeps what it reads with Tavily in a Neo4j knowledge graph, using only harness
pieces: Tavily as an MCP tool, the graph as a MemoryManager store (injection before each model call, ModelExtractor
after each turn) that is also a hook (records the URLs Tavily returned). Demo 01 runs locally in a notebook; Demo 02
deploys the same Agent to AgentCore Runtime V2 with Tavily through AgentCore Gateway and the graph in AuraDB.

## Structure
- `01-local-graph-agent/`: `test_local_graph_agent.ipynb` (the demo), `test_local_graph_agent.py` (same code cells as a
  script), `images/how-the-agent-works.{d2,svg,png}` (per-turn diagram; regenerate: `d2 --pad 30 how-the-agent-works.d2
  how-the-agent-works.svg && rsvg-convert -w 1600 how-the-agent-works.svg -o how-the-agent-works.png`), `web_facts_store.py` (`WebFactsGraph`: Strands MemoryStore `search`/`add` + hook, extraction prompt), `tests/` (8 tests, Neo4j only).
- `02-agentcore-runtime-v2/`: `agent/research_agent.py` (the same Agent as the Demo 01 notebook; `# AgentCore change 1/2/3`:
  BedrockModel, MCPClient on the Gateway with SigV4, AuraDB from Secrets Manager; plus BedrockAgentCoreApp) + identical
  `agent/web_facts_store.py` + `agent/requirements.txt`; shared by all deploy paths: `build_package.sh` (arm64 .zip, no Docker,
  writes `build/package/` and `build/agent-code.zip`) and `schemas/tavily-openapi.json`; three ways to deploy the same
  resources: `deploy_with_boto3.ipynb` (prefix `tavily-graph-boto3`), `deploy_with_cli.ipynb` (`%%bash`, prefix
  `tavily-graph-cli`, state in `build/cli/state.env`) and `cdk/` (CDK app: `app.py`, `infra/graph_agent_stack.py`,
  `stack_constructs/tavily_gateway.py`, `stack_constructs/graph_agent_runtime.py`, `deploy.sh`, `destroy.sh`, `write_env.py`;
  names `tavily-graph-agent`); `invoke_agent.py`; `tests/` (`test_static.py`, `test_notebooks.py`, `notebook_offline.py`).
- `scripts/setup_local_neo4j.sh`: dedicated local Neo4j Community (no Docker), bolt 7699 / http 7499.

## Commands
```bash
./scripts/setup_local_neo4j.sh [start|stop|status]
cd 01-local-graph-agent && uv venv && uv pip install -r requirements.txt && cp .env.example .env
uv run pytest && uv run jupyter lab test_local_graph_agent.ipynb   # or uv run python test_local_graph_agent.py
cd 02-agentcore-runtime-v2 && set -a && source ../01-local-graph-agent/.env && set +a   # TAVILY_API_KEY exported in the shell
uv venv && uv pip install -r requirements.txt jupyter
uv run jupyter lab deploy_with_boto3.ipynb      # or deploy_with_cli.ipynb; cleanup is the last section (CONFIRM_DELETE)
./cdk/deploy.sh && uv run python invoke_agent.py --series     # CDK path; ./cdk/destroy.sh removes it
uv run pytest                                   # offline: static checks + notebooks vs botocore model / stub aws CLI
# CDK synth only: ./build_package.sh && cd cdk && source .venv/bin/activate && CDK_DEFAULT_ACCOUNT=111111111111 CDK_DEFAULT_REGION=us-east-1 cdk synth
```

## Dependencies and configuration
Python 3.10+ (package built for 3.12 arm64), uv. Demo 01: `strands-agents[openai]>=1.65` (MemoryManager, ModelExtractor),
`neo4j>=5.28`, `python-dotenv`, `pytest`, `jupyter`. Runtime adds `bedrock-agentcore`, `mcp-proxy-for-aws>=1.8`, `boto3`. Deploy side: boto3 `>=1.43.95` (first with `platformVersion` in `CreateAgentRuntime`), AWS CLI v2 `>=2.36.46` (first with `--platform-version`), `uv`, `zip`, `nbformat` (tests).
CDK: `aws-cdk-lib==2.273.0` (first with Runtime `platform_version`, schema 54, works with CDK CLI 2.1126.0).
Keys come from the shell: `OPENAI_API_KEY`, `TAVILY_API_KEY` or `TAVILY_KEY` (Eli's shell exports `TAVILY_KEY`).
Demo 01 `.env`: only `NEO4J_URI`, `NEO4J_USERNAME` (or `NEO4J_USER`), `NEO4J_PASSWORD`, `NEO4J_DATABASE`; AuraDB block
active and local block commented in `.env.example`; switching is only `.env` values. `load_dotenv()` without override.
Demo 02 `.env`: `AWS_REGION`, `AGENT_RUNTIME_ARN`. CDK path secrets (`cdk/deploy.sh`, outside CDK): `tavily-graph-agent/tavily-api-key`,
`tavily-graph-agent/neo4j-aura` (JSON uri/username/password/database). Notebook paths read the same env vars and create their own secret
(`<prefix>/neo4j-aura`) and an AgentCore Identity credential provider (the Tavily key is not kept in a Secrets Manager secret of ours).

## Conventions and gotchas
- Eli's rule: build the HARNESS, not Python around the LLM. The notebook shows `Agent(...)` and calls it directly
  (`researcher("...")`). No `build_agent()` helpers, no loops or counters around agent calls, no hand-seeded facts.
- Behavior lives in Strands pieces: web tools `search_web`/`read_web_page` (local `@tool` on Tavily's API; on AgentCore the
  same tools from the Gateway), `MemoryManager(stores=[web_facts], search_tool_config=RECALL_TOOL)` (injection +
  `recall_web_facts`), store `extraction` = `InvocationTrigger` + `ModelExtractor` (agent's own model, facts carry
  subject/predicate/object/source_url in metadata), `hooks=[web_facts]` (records Tavily URLs).
- Local model: OpenAI `gpt-4o-mini`, Bedrock line commented in the notebook. Bedrock only on AgentCore.
- `web_facts_store.py` and the `Agent(...)` wiring must stay identical in both demos (`tests/test_static.py`).
- Store rules in `add()`: drop facts whose URL no web tool returned; drop facts whose subject/object is a number.
- Not `neo4j-agent-memory`'s Neo4jMemoryStore: its LLM extraction supports OpenAI/Anthropic only, not Bedrock, so the
  same agent could not run on AgentCore with Bedrock. Strands documents custom stores; that is the path used.
- Sources of truth: Strands docs (strandsagents.com) and official AWS docs, not blogs.
- Tools follow the `agent-tool-design` skill: each description says what, when, when not, parameters, result, limits, and
  never names the backend (a test bans Neo4j/graph/Tavily words in tool and store descriptions). The system prompt holds
  only the role, the no-own-knowledge + cite-URL rule and the length.
- The notebook's web-tools cell is the source of truth. After editing it: `python scripts/build_local_notebook.py &&
  python scripts/sync_gateway_schema.py` (writes `schemas/tavily-openapi.json`); `test_gateway_tools_match_the_local_tools`
  fails if they drift. Gateway turns `operationId` + `description` into the tool; names are `tavily___search_web` and
  `tavily___read_web_page`, filtered with `^tavily___`. Same server/auth as the console-only "Tavily Search" template.
- Runtime V2: model, driver and store at startup, then `app.run()` at module level; Gateway MCPClient and Agent per request
  (a test enforces the split). `entryPoint` is an array: any file name works (`main.py` is only the docs' example).
- One build script and one schema for the three deploy paths; never copy them. Notebooks print no account IDs (`mask()`), take
  secrets only from env vars, pass them to the CLI through mode-600 files, and clean up only with `CONFIRM_DELETE`.
- Notebook tests run the boto3 notebook with `BaseClient._make_api_call` replaced (botocore validation + enum/pattern checks, no
  network) and the CLI notebook's bash against a stub `aws`; the CLI test also checks every command/option exists in the installed CLI.
- Gateway trust condition uses `gateway/<name>-*` (the gateway ID is `<name>-<suffix>`). Runtime names: letters, digits, `_`.
- AuraDB on macOS: set `SSL_CERT_FILE` to `certifi.where()` before connecting (macOS `/etc/ssl/cert.pem` lacks
  SSL.com TLS RSA Root CA 2022). Never use `neo4j+ssc` or disable verification.
- `WebFactsGraph.clear()` deletes only `WebFact`, `WebEntity`, `WebSource` (prefixed so a shared AuraDB with neo4j-agent-memory's `Entity`/`Fact` is never touched). No Docker on this machine; do not touch the Neo4j
  Desktop DBMS on 7474/7687/7688. Constructs folder is `stack_constructs`.

## Runtime sessions
- `research_agent.py` keeps ONE Agent per runtime session (module global `session_agent`, created on the first request
  after the V2 restore): same `runtimeSessionId` = same conversation (like `researcher`), new id = new Agent, same graph
  (like `new_session`). `test_deployed_agent.ipynb` (built by `scripts/build_test_deployed_notebook.py`) exercises it.
- Local notebook is built by `scripts/build_local_notebook.py` (edit cells there, not in the .ipynb).
- NOT deployed or tested against AWS yet (credentials expired); deploy needs Eli's choice of profile/account.

## Current state (2026-11-17)
- Rebuilt on the Strands harness after Eli's review (previous version had a hidden `build_agent`, hand-seeded facts,
  Python loops counting calls, closed questions about AgentCore, and missed the `TAVILY_KEY` env var).
- Verified: notebook executed end to end (OpenAI gpt-4o-mini, Tavily remote MCP, local Neo4j): session 1, 3 questions,
  3 Tavily searches, 8 facts from 2 sources; session 2 (new Agent, same graph) answered 2 questions with 0 searches;
  agent without the graph made 2 searches for the same question. 8 store tests and 5 static tests pass; `cdk synth`
  works; arm64 package builds (117 MB).
- AuraDB (one instance shared with other demos: hotel GraphRAG graph and neo4j-agent-memory schema on
  `Entity`/`Fact`): store labels renamed to `WebFact`/`WebEntity`/`WebSource`, full-text index `web_facts_fulltext`.
  8 store tests and the full notebook pass against it (run 2: 3/14 facts/4 sources; session 2: 0 and 1 searches; no graph: 1).
- Notebook explains each turn with the diagram and shows `await web_facts.search(question)`: exactly what
  MemoryManager injects (query = last user message, top 5). Session 1 = fill the graph (same agent keeps history);
  session 2 = new Agent, proves the graph.
- Known: facts are dropped when the model answers without full URLs (the factory question stored none in that run).
- Not verified: deploy, Gateway accepting the schema and returning Tavily JSON, the AgentCore run end to end.

## Demo 02 restructure (2026-11-18)
- `infra/` became `cdk/`; `agent/main.py` became `agent/research_agent.py` (reads like the notebook); added the boto3 and CLI
  notebooks, the shared `build_package.sh` and `schemas/`. Docs checked: V2 needs `platformVersion="V2"`, create takes minutes, poll
  `get_agent_runtime`; the docs' direct-deploy role lacks S3 read (notebooks add `s3:GetObject` on the one object); the docs' V2 sample
  calls `app.run()` at module level (done); packages should not contain `__pycache__` (the build script removes it).
- Offline only: 17 tests pass, `cdk synth` works, notebooks validated against botocore 1.44.5 / AWS CLI 2.40.3. Still not run live.

## Live deploy (2026-11-19, CDK path)
- Deployed `TavilyGraphAgentStack` with `cdk/deploy.sh` to AWS profile `demo-embeddings`, us-east-1 (never `eli-personal`).
  290 s; runtime `tavily_graph_agent-*`, gateway `tavily-graph-agent-*`. Secrets `tavily-graph-agent/*` hold the AuraDB
  password that must be reset; after the reset update the `tavily-graph-agent/neo4j-aura` secret.
- `test_deployed_agent.ipynb` passed end to end against AuraDB: session A, 5 questions, history kept (turns 1..5, recalls
  the first question); graph had 23 facts; session B (new runtime session) answered the Tesla question with 0 searches.
- Correction: `tools_used` came from `result.metrics.tool_metrics`, which accumulates over the session's Agent, so every
  later turn looked like it searched. Now `tools_this_turn()` reads the tool calls since the last user question.

## Tool redesign (2026-11-19)
- Replaced Tavily's remote MCP locally (its description told the model to search "before answering any question about
  specific facts" and exposed 17 parameters; MCPClient cannot change a server's descriptions) with two `@tool` functions,
  and renamed the Gateway operations to match. `search_memory` (described for user preferences) became `recall_web_facts`.
- Live results after the change. Local (gpt-4o-mini): session 1 searched 5 times, 14 facts; session 2 Tesla 0 searches,
  factories-vs-lithium 1; no-memory agent 1. AgentCore (Claude Sonnet 4.5, shared AuraDB graph, 36 facts): session A
  questions 1-3 recall + search, follow-up "which are South Korean?" no tools, "what was my first question?" no tools;
  session B Tesla answered from `recall_web_facts` only, follow-up "which is Japanese?" no tools.
- 27 tests pass (8 store, 19 Demo 02).
- `WebFactsGraph.dropped` keeps refused facts with the reason; the notebook prints them under "What the agent stored". Added after an empty graph in Kiro: an old kernel had the previous store (tavily_* tool names), so every fact was refused silently. Restart the kernel after pulling.
- Root README rewritten 2026-11-19 with three diagrams in `images/` (turn flow and graph model in D2, AWS architecture in draw.io with AWS4 icons); nothing account-specific in it.
- No credential values in the repo, not even fake ones (GitGuardian flagged fake Neo4j test values on 2026-11-19): offline
  tests take TAVILY_API_KEY / NEO4J_* from the environment or generate random ones (`offline_credentials()` in
  `tests/notebook_offline.py`); AWS keys in tests are always random; `.env.example` has empty values.
- Fixed: `cdk bootstrap --quiet` is not a valid flag (removed). The notebook's `%pip` cell fails in a uv venv without pip; harmless.
- Teardown: `AWS_PROFILE=demo-embeddings cdk/destroy.sh`.
