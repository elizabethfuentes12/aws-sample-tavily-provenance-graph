# Deploy the Graph Agent: AgentCore Runtime V2, Tavily through AgentCore Gateway, Neo4j AuraDB

**Problem:** The local agent in Demo 01 reads the Tavily key from your shell, uses OpenAI and keeps its graph on your laptop. In production you want the key out of the agent, IAM in front of every call, and one graph that every session shares. And you want to see each resource that makes that possible, not only a stack that hides them.

**Solution:** Deploy the same agent to [AgentCore Runtime platform version V2](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-how-it-works.html?trk=87c4c426-cddf-4799-a299-273337552ad8&sc_channel=el), with no container, in one of three ways that create the same resources: a boto3 notebook, an AWS CLI notebook, or a CDK stack. The `Agent(...)` call in `agent/research_agent.py` is identical to the notebook's. Three things change:

| | Demo 01 (local) | Demo 02 (AgentCore) |
|---|---|---|
| model | `OpenAIModel("gpt-4o-mini")` | `BedrockModel(...)` |
| `web_tools` | `[search_web, read_web_page]`, two `@tool` functions on Tavily's API | `[MCPClient(aws_iam_streamablehttp_client(gateway_url))]`: the same two tools from AgentCore Gateway, key in AgentCore Identity |
| Neo4j | local instance | AuraDB, credentials in Secrets Manager |

plus the `BedrockAgentCoreApp` entrypoint. `web_facts_store.py` is the same file (a test checks it). In `research_agent.py` the three changes are marked `# AgentCore change 1/2/3`.

---

## Choose how to deploy

| | [`deploy_with_boto3.ipynb`](deploy_with_boto3.ipynb) | [`deploy_with_cli.ipynb`](deploy_with_cli.ipynb) | [`cdk/`](cdk/) |
|---|---|---|---|
| Tool | boto3 (`>= 1.43.95`) | AWS CLI v2 (`>= 2.36.46`), `%%bash` cells | AWS CDK (`aws-cdk-lib==2.273.0`, CLI `2.1126.0` or newer) |
| Best for | Learning each API call and its permissions | Learning the CLI commands, scripting | Repeatable stack, one `deploy` and one `destroy` |
| Resource names | prefix `tavily-graph-boto3` | prefix `tavily-graph-cli` | `tavily-graph-agent` |
| Secrets | from env vars | from env vars, via mode-600 files | from env vars, stored by `cdk/deploy.sh` |
| Cleanup | last section, guarded by `CONFIRM_DELETE` | last cell, guarded by `CONFIRM_DELETE=false` (dry run) | `cdk/destroy.sh` |

Use one path at a time. The names differ, so the paths do not collide if you try a second one, but each creates its own Gateway, Runtime and secret.

All three create:

| Resource | Purpose |
|----------|---------|
| Secrets Manager secret (AuraDB credentials) | the agent reads it at startup |
| AgentCore Identity API key credential provider | holds the Tavily key; the agent never sees it |
| IAM role for the Gateway | reads the key from AgentCore Identity (trust scoped to this account and gateway) |
| AgentCore Gateway (MCP, IAM inbound) | callers need `bedrock-agentcore:InvokeGateway` |
| Gateway target `tavily` (OpenAPI) | `search_web` and `read_web_page` on Tavily's API, with `Authorization: Bearer <key>` |
| IAM execution role for the Runtime | logs, X-Ray, Bedrock models, plus invoke this gateway, read this secret, read this code object |
| AgentCore Runtime, platform version V2 | runs `research_agent.py` from a .zip, network mode PUBLIC |

The notebooks also create a private S3 bucket (all public access blocked) for the code package. CDK uses its bootstrap bucket. The CDK path creates its secrets in `cdk/deploy.sh`, outside the template, so no secret value appears in a template.

### Build the code package (all paths)

`build_package.sh` installs the agent's dependencies as linux/arm64 wheels (`uv pip install --python-platform aarch64-manylinux2014 --python-version 3.12 --only-binary=:all:`), copies `agent/*.py` next to them and writes `build/package/` (for CDK) and `build/agent-code.zip` (for S3). It needs `uv` and `zip`, not Docker. The notebooks and `cdk/deploy.sh` call it for you. The package is about 117 MB unzipped and 41 MB zipped; the limits are 750 MB and 250 MB.

## Architecture

```
invoke_agent.py ──InvokeAgentRuntime (IAM)──▶ AgentCore Runtime V2 (research_agent.py)
                                                 │  Strands Agent + MemoryManager(WebFactsGraph)
                                                 ├──MCP + SigV4──▶ AgentCore Gateway (IAM inbound)
                                                 │                    └─ target "tavily" ──Authorization: Bearer <key from AgentCore Identity>──▶ api.tavily.com
                                                 └──neo4j+s──▶ Neo4j AuraDB (credentials from Secrets Manager)
```

### How is Tavily connected?

AgentCore documents Tavily as a [built-in integration template](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/gateway-target-integrations.html?trk=87c4c426-cddf-4799-a299-273337552ad8&sc_channel=el): server `https://api.tavily.com`, API key auth. Templates are added only in the AWS Management Console, so all three paths create an [OpenAPI target](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/gateway-schema-openapi.html?trk=87c4c426-cddf-4799-a299-273337552ad8&sc_channel=el) with the same server and auth.

The operations in `schemas/tavily-openapi.json` are ours, not the template's. Gateway turns each `operationId` and `description` into the tool name and description the model reads on every call, so they follow tool-design practice: what the tool does, when to use it and when not, what each parameter means, what comes back. They are the same as the local notebook's `@tool` functions (`tests/test_static.py` checks names, descriptions and parameters). Gateway names them `tavily___search_web` and `tavily___read_web_page`. Only five search parameters are exposed, and no oneOf/anyOf or `securitySchemes`, as Gateway requires.

### How do I see traces?

The package includes the AWS Distro for OpenTelemetry (`aws-opentelemetry-distro>=0.18.0`, plus `strands-agents[otel]`), and every deploy path starts the agent with `entryPoint: ["opentelemetry-instrument", "research_agent.py"]`, as the [direct code deployment guide](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-get-started-code-deploy-python.html?trk=87c4c426-cddf-4799-a299-273337552ad8&sc_channel=el) shows. Spans for the agent loop, each model call, `recall_web_facts`, the Gateway tools and the memory injection go to the `spans` stream of the runtime's log group and to the GenAI Observability dashboard in CloudWatch. One-time account setup: turn on [CloudWatch Transaction Search](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/observability-configure.html?trk=87c4c426-cddf-4799-a299-273337552ad8&sc_channel=el).

### What changes for Runtime V2?

V2 starts the agent from a snapshot taken after startup ([optimize for V2](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-v2-optimize.html?trk=87c4c426-cddf-4799-a299-273337552ad8&sc_channel=el)). `agent/research_agent.py` follows that guidance:

| Startup (in the snapshot) | Per request (in the handler) |
|---------------------------|------------------------------|
| imports, the Bedrock model client, the Neo4j driver and `WebFactsGraph` (schema and full-text index), then `app.run()` | the Gateway `MCPClient` and the `Agent`, so tools added to the Gateway later appear without a redeploy |

The store generates fact ids and timestamps inside `add()`, per call, never at startup. If AuraDB is unreachable at startup, the process fails and runtime creation reports a health check error (the agent must be healthy within 120 seconds). V2 create and update take several minutes, because the runtime prepares a snapshot; all three paths poll until `READY`.

`entryPoint` is an array of files, so the entry file can have any name. The docs use `main.py` as an example only; this demo uses `research_agent.py`.

## Quick Start

```bash
# 1. Export the secrets in the shell that starts Jupyter or the CDK script (they are never printed or written to a template)
#    TAVILY_API_KEY (or TAVILY_KEY), and NEO4J_URI, NEO4J_USERNAME, NEO4J_PASSWORD, NEO4J_DATABASE from your AuraDB
#    credentials file. Simplest: fill ../01-local-graph-agent/.env and load it:
set -a && source ../01-local-graph-agent/.env && set +a

# 2. Deploy, with one of:
uv venv && uv pip install -r requirements.txt jupyter
uv run jupyter lab deploy_with_boto3.ipynb          # or deploy_with_cli.ipynb
./cdk/deploy.sh                                     # CDK: secrets, package, cdk deploy, writes AGENT_RUNTIME_ARN to .env

# 3. Ask the agent (the CDK script wrote AGENT_RUNTIME_ARN to .env; the notebooks ask for you)
uv run python invoke_agent.py --series

# 4. Offline checks: schema rules, same agent as Demo 01, notebooks validated against the AWS API models
uv run pytest

# 5. Clean up: the last section of the notebook, or
./cdk/destroy.sh
```

For CDK only: `cd cdk && python3 -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt`, run `../build_package.sh`, then `cdk synth` (with `CDK_DEFAULT_ACCOUNT` and `CDK_DEFAULT_REGION` set if you have no credentials) or `cdk deploy`.

## Cost and cleanup

AgentCore Runtime and Gateway are pay-per-use, Secrets Manager charges per secret per month, Bedrock charges per token, and Tavily and AuraDB follow your own plans (see the [root README](../README.md#pricing)). Nothing runs when nobody calls the agent, but the secrets and the S3 bucket remain until you delete them. Run the cleanup of the path you used. It deletes in reverse order (runtime, gateway target, gateway, credential provider, roles, code object and bucket, secrets) and leaves your AuraDB instance untouched. Runtime deletion is asynchronous, and V2 can keep a deleted runtime's snapshot for up to 8 hours; AgentCore manages that.

## Test the deployed agent: sessions, conversation and graph memory

`test_deployed_agent.ipynb` talks to the runtime with `invoke_agent_runtime`. One `runtimeSessionId` is one microVM that keeps its context between invocations ([sessions](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-sessions.html)), and `research_agent.py` keeps one `Agent` per microVM, created on the session's first request. So:

| Session | Questions | Expected |
|---|---|---|
| A (one `runtimeSessionId`) | battery makers, factories, lithium, then "which of those companies are South Korean?" | Tavily through Gateway for the first three; the fourth needs session A's conversation |
| B (a new `runtimeSessionId`) | "who supplies Tesla?", factories + lithium, "which of the companies you just mentioned is Japanese?" | the Tesla question answered from the graph session A filled; the follow-up uses session B's own conversation |
| A again | "what was my first question?" | session A still has its conversation |

The notebook also reads the facts from AuraDB and stops both sessions at the end (`stop_runtime_session`).

## Files

| File | Purpose |
|------|---------|
| `agent/research_agent.py` | The Demo 01 agent with the three AgentCore changes, and the `BedrockAgentCoreApp` entrypoint |
| `agent/web_facts_store.py` | Same file as Demo 01 |
| `agent/requirements.txt` | The agent's dependencies (installed as arm64 wheels by `build_package.sh`) |
| `build_package.sh` | Builds `build/package/` and `build/agent-code.zip`, shared by all paths |
| `schemas/tavily-openapi.json` | Tavily target schema, shared by all paths |
| `deploy_with_boto3.ipynb` | Step-by-step deploy with boto3, with polling helpers and guarded cleanup |
| `deploy_with_cli.ipynb` | The same steps with `aws` commands in `%%bash` cells |
| `cdk/` | CDK app: `app.py`, `infra/graph_agent_stack.py`, `stack_constructs/tavily_gateway.py`, `stack_constructs/graph_agent_runtime.py`, `deploy.sh`, `destroy.sh`, `write_env.py` |
| `test_deployed_agent.ipynb` | Sessions A and B against the deployed runtime: conversation per session, graph shared |
| `invoke_agent.py` | Ask the deployed agent, one question or the five-question series (each in a new runtime session) |
| `tests/` | `test_static.py` (schema rules, store and prompt identical to Demo 01, V2 startup/request split), `test_notebooks.py` (notebooks vs the AWS API models), `notebook_offline.py` (the offline runner) |

## Honest limits

- **Verified offline:** the offline tests pass. Every boto3 call in `deploy_with_boto3.ipynb` validates against botocore's service model (types, required members, lengths, enums, name patterns) when the notebook runs with calls stubbed; cleanup deletes in reverse order and does nothing without `CONFIRM_DELETE`. Every `aws` command and option in `deploy_with_cli.ipynb` exists in the installed CLI (2.40.3), and its bash cells run against a stub `aws`. `cdk synth` produces a runtime with `PlatformVersion: V2` and code from S3, a Gateway with `AuthorizerType: AWS_IAM`, and a Tavily target sending `Authorization: Bearer` from an API key provider. The arm64 package builds.
- **Not verified against a live account:** any deploy, Gateway accepting the schema and calling Tavily, the exact trust and permission statements against live IAM (they follow the docs), the text format Gateway returns for Tavily results (`web_facts_store.urls_in` parses Tavily's JSON and falls back to bare URLs), and the end-to-end run.
- The docs' direct-deploy execution role does not list S3 access, although the same page says the role needs it for the package. The notebooks add `s3:GetObject` on the one code object; if the runtime reaches `READY` without it, remove it.
- The AuraDB credentials are read at startup, so they are frozen in the V2 snapshot. After you rotate the password, update the runtime (a new version takes a new snapshot). The Neo4j socket opened at startup does not survive a restore; the driver reconnects on the first query.
- The CDK `Gateway` construct turns on semantic search by default, which adds an extra search tool to the Gateway. The agent's `^tavily___` filter hides it. The notebooks do not enable it.
- The secret dynamic reference in `ApiKeyCredentialProvider.ApiKey` (CDK path) follows the CDK guidance to pass a reference instead of a literal; CloudFormation does not document dynamic-reference support for that property specifically. CDK also warns that the Gateway role's read access to the Identity-managed secret uses a wildcard prefix (`bedrock-agentcore-identity!*`), because the secret ARN is only known after deploy. The notebooks scope it to the exact secret ARN.
- `aws-cdk-lib` is pinned to 2.273.0: the first release with `platform_version` that still synthesizes with CDK CLI 2.1126.0. Newer libraries need CLI 2.1148.2 or later.

## Troubleshooting

| Issue | Solution |
|-------|----------|
| `Unknown parameter in input: "platformVersion"` | boto3 is older than 1.43.95 (`uv pip install -U boto3`). For the CLI, `--platform-version` needs AWS CLI v2 2.36.46 or later. |
| `build/package has no research_agent.py` (CDK) | Run `./build_package.sh` (`cdk/deploy.sh` does it). |
| `ConflictException` on update or delete | The runtime is still `CREATING` or `UPDATING`; V2 takes minutes. Poll `get-agent-runtime` until `READY`. |
| Runtime create fails a health check | V2 needs a healthy `/ping` within 120 seconds; check the runtime logs in CloudWatch (`/aws/bedrock-agentcore/runtimes/...`). Usual causes: AuraDB unreachable or the secret unreadable. |
| `AccessDeniedException` calling the Gateway | The runtime role needs `bedrock-agentcore:InvokeGateway` on this gateway; check you use this gateway's URL. |
| Target stays in `FAILED` or `UPDATE_UNSUCCESSFUL` | Read `statusReasons` in `get-gateway-target`; the usual cause is the Gateway role missing `GetResourceApiKey`, `GetWorkloadAccessToken` or the secret read. |
| Tavily returns 401 | The API key in the credential provider is wrong. Update it (`update-api-key-credential-provider`) or recreate the provider. |
| A rerun fails with `already exists` | A previous run stopped halfway. Run the cleanup section (it finds resources by name), then start again. |
| `Cloud assembly schema version mismatch` | Keep `aws-cdk-lib==2.273.0` or update the CLI (`npm i -g aws-cdk`). |

**Tested versions:** boto3 1.44.5 (minimum 1.43.95), AWS CLI 2.40.3 (minimum 2.36.46), aws-cdk-lib 2.273.0 with CDK CLI 2.1126.0 (synth), Strands 1.65.0, mcp-proxy-for-aws 1.8.0, bedrock-agentcore SDK (latest at build).

## References

- [AgentCore Runtime: platform versions](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-how-it-works.html?trk=87c4c426-cddf-4799-a299-273337552ad8&sc_channel=el) and [optimize for V2](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-v2-optimize.html?trk=87c4c426-cddf-4799-a299-273337552ad8&sc_channel=el)
- [Direct code deployment for Python](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-get-started-code-deploy-python.html?trk=87c4c426-cddf-4799-a299-273337552ad8&sc_channel=el) and [Runtime permissions](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-permissions.html?trk=87c4c426-cddf-4799-a299-273337552ad8&sc_channel=el)
- [Gateway permissions](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/gateway-prerequisites-permissions.html?trk=87c4c426-cddf-4799-a299-273337552ad8&sc_channel=el), [create a gateway](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/gateway-create-api.html?trk=87c4c426-cddf-4799-a299-273337552ad8&sc_channel=el), [inbound auth](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/gateway-inbound-auth.html?trk=87c4c426-cddf-4799-a299-273337552ad8&sc_channel=el)
- [Gateway integration templates (Tavily)](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/gateway-target-integrations.html?trk=87c4c426-cddf-4799-a299-273337552ad8&sc_channel=el), [OpenAPI targets](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/gateway-schema-openapi.html?trk=87c4c426-cddf-4799-a299-273337552ad8&sc_channel=el), [outbound auth](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/gateway-outbound-auth.html?trk=87c4c426-cddf-4799-a299-273337552ad8&sc_channel=el), [tool naming](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/gateway-tool-naming.html?trk=87c4c426-cddf-4799-a299-273337552ad8&sc_channel=el)
- [Strands: deploy to AgentCore Runtime](https://strandsagents.com/docs/user-guide/sdk/deploy/deploy_to_bedrock_agentcore/python/index.md) and [MCP tools](https://strandsagents.com/docs/user-guide/sdk/tools/mcp-tools/index.md)
- [boto3 `bedrock-agentcore-control`](https://docs.aws.amazon.com/boto3/latest/reference/services/bedrock-agentcore-control.html) and [AWS CLI `bedrock-agentcore-control`](https://docs.aws.amazon.com/cli/latest/reference/bedrock-agentcore-control/index.html)
