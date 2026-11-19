"""Builds 02-agentcore-runtime-v2/test_deployed_agent.ipynb. Run: python scripts/build_test_deployed_notebook.py"""
import os, pathlib
os.chdir(pathlib.Path(__file__).resolve().parent.parent / "02-agentcore-runtime-v2")
import json, pathlib
C=[]
def md(t): C.append(("markdown", t.strip("\n")))
def code(t): C.append(("code", t.strip("\n")))
md("""
# Talk to the deployed agent: sessions, conversation and graph memory

The agent on AgentCore Runtime is the notebook's agent. This notebook checks that it behaves the same way:

| Notebook (Demo 01) | AgentCore Runtime | What it shows |
|---|---|---|
| `researcher`, three questions | session A: one `runtimeSessionId`, four invocations | the same session keeps its conversation |
| `new_session` | session B: a new `runtimeSessionId` | a new agent with no history answers from the graph |

Why this works: one `runtimeSessionId` is one microVM that keeps its context between invocations
([sessions](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-sessions.html)), and
`research_agent.py` keeps one `Agent` per microVM. A new session id is a new microVM, so a new `Agent`. The graph in
AuraDB is the only thing the two sessions share.

**Prerequisites:** the runtime deployed with one of the three paths (`deploy_with_boto3.ipynb`, `deploy_with_cli.ipynb`
or `cdk/`), `AGENT_RUNTIME_ARN` in `.env` or the shell, AWS credentials allowed to call
`bedrock-agentcore:InvokeAgentRuntime` and `bedrock-agentcore:StopRuntimeSession` on it, and (for the graph cells) the
AuraDB connection in `NEO4J_*`.
""")
code("%pip install -q boto3 python-dotenv neo4j certifi")
md("## Configuration")
code("""
import json
import os
import uuid

import boto3
import certifi
from dotenv import load_dotenv

os.environ.setdefault("SSL_CERT_FILE", certifi.where())   # AuraDB's root CA on macOS (see Demo 01)
load_dotenv()
REGION = os.getenv("AWS_REGION", "us-east-1")
AGENT_RUNTIME_ARN = os.environ["AGENT_RUNTIME_ARN"]

agentcore = boto3.client("bedrock-agentcore", region_name=REGION)
print("runtime:", AGENT_RUNTIME_ARN.split("/")[-1])
""")
md("""
## One helper: ask the agent in a session

`invoke_agent_runtime` sends the prompt to the runtime; `runtimeSessionId` picks the session (at least 33 characters,
a UUID is 36). The runtime returns the answer, the tools the agent called and how many questions this session's
conversation holds. [InvokeAgentRuntime](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-invoke-agent.html)
""")
code("""
def ask(session_id: str, prompt: str) -> dict:
    response = agentcore.invoke_agent_runtime(
        agentRuntimeArn=AGENT_RUNTIME_ARN,
        runtimeSessionId=session_id,
        contentType="application/json",
        accept="application/json",
        payload=json.dumps({"prompt": prompt}).encode(),
    )
    out = json.loads(response["response"].read())
    print(f"session {session_id[:8]} · question {out['turns_in_session']} in this session · tools: {out['tools_used'] or 'none'}")
    print(out["answer"].strip(), "\\n")
    return out
""")
md("""
## Session A: fill the graph, and keep the conversation

Same `runtimeSessionId` for every question. The graph starts empty, so the agent calls Tavily (`tavily___search_web`
through AgentCore Gateway). The fourth question says "those companies": only the conversation of this session knows
which ones.
""")
code("""
SESSION_A = str(uuid.uuid4())
ask(SESSION_A, "Which companies make the batteries for the best-selling electric cars?")
""")
code('ask(SESSION_A, "Where are the largest EV battery factories, and which companies run them?")')
code('ask(SESSION_A, "Which countries mine and refine most of the lithium used in EV batteries?")')
code('ask(SESSION_A, "Which of those companies are South Korean?")')
md("""
## What the agent stored in AuraDB

The same store as the notebook (`WebFactsGraph`), now written by the runtime. Read-only query.
""")
code("""
from neo4j import GraphDatabase

driver = GraphDatabase.driver(os.environ["NEO4J_URI"],
                              auth=(os.getenv("NEO4J_USERNAME") or os.getenv("NEO4J_USER", "neo4j"), os.environ["NEO4J_PASSWORD"]))
facts, _, _ = driver.execute_query(
    "MATCH (s:WebEntity)<-[:SUBJECT]-(f:WebFact)-[:OBJECT]->(o:WebEntity), (f)-[:FROM]->(src:WebSource) "
    "RETURN s.name AS subject, f.predicate AS predicate, o.name AS object, src.url AS source ORDER BY f.at",
    database_=os.getenv("NEO4J_DATABASE", "neo4j"))
print(len(facts), "facts")
for f in facts:
    print(f"{f['subject']} -[{f['predicate']}]-> {f['object']}   {f['source']}")
""")
md("""
## Session B: a new session, the same graph

A new `runtimeSessionId` is a new microVM and a new `Agent`: no conversation from session A. For the Tesla question the
memory manager injects the facts session A stored, so the agent can answer without Tavily (`tools: recall_web_facts` or
`none`). The follow-up continues session B's own conversation.
""")
code("""
SESSION_B = str(uuid.uuid4())
ask(SESSION_B, "Who supplies the batteries in Tesla cars?")
""")
code('ask(SESSION_B, "Do the big battery makers build factories in the countries that refine their lithium?")')
code('ask(SESSION_B, "Which of the companies you just mentioned is Japanese?")')
md("""
## Back to session A

Session A still has its own conversation (until it is idle for 15 minutes or stopped). It answers about "the first
question" without being told what it was.
""")
code('ask(SESSION_A, "What was my first question, and what did you answer?")')
md("""
## Stop the sessions

Stopping a session ends its microVM (the conversation is gone; the graph stays). Otherwise it stops by itself after 15
idle minutes. [StopRuntimeSession](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-sessions.html)
""")
code("""
for session_id in (SESSION_A, SESSION_B):
    agentcore.stop_runtime_session(agentRuntimeArn=AGENT_RUNTIME_ARN, runtimeSessionId=session_id)
    print("stopped", session_id[:8])
driver.close()
""")
nb={"cells":[],"metadata":{"kernelspec":{"display_name":"Python 3","language":"python","name":"python3"},"language_info":{"name":"python"}},"nbformat":4,"nbformat_minor":5}
for i,(k,t) in enumerate(C):
    c={"cell_type":k,"id":f"cell-{i:02d}","metadata":{},"source":t.splitlines(keepends=True)}
    if k=="code": c.update(execution_count=None,outputs=[])
    nb["cells"].append(c)
json.dump(nb,open("test_deployed_agent.ipynb","w"),indent=1)
print(len(C),"cells")
