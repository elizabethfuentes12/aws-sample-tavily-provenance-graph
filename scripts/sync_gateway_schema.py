"""Writes 02-agentcore-runtime-v2/schemas/tavily-openapi.json from the @tool functions of the Demo 01 notebook.

The model reads the same tool names, descriptions and parameters locally (the @tool functions) and on AgentCore
(Gateway turns each operationId and description of this OpenAPI into a tool). The notebook is the source; run this after
editing its web-tools cell:  python scripts/build_local_notebook.py && python scripts/sync_gateway_schema.py
tests/test_static.py fails if the two drift.
"""
import json
import pathlib

import nbformat

ROOT = pathlib.Path(__file__).resolve().parent.parent
SCHEMA = ROOT / "02-agentcore-runtime-v2" / "schemas" / "tavily-openapi.json"
norm = lambda text: " ".join(text.split())

cell = next(c.source for c in nbformat.read(ROOT / "01-local-graph-agent" / "test_local_graph_agent.ipynb", as_version=4).cells
            if c.cell_type == "code" and "def search_web(" in c.source)
scope = {"TAVILY_KEY": "unused"}
exec(cell, scope)
specs = {t.tool_name: t.tool_spec for t in scope["web_tools"]}

schema = json.loads(SCHEMA.read_text())
for path, name in (("/search", "search_web"), ("/extract", "read_web_page")):
    spec, op = specs[name], schema["paths"][path]["post"]
    op["operationId"] = name
    op["summary"] = norm(spec["description"].split("\n\n")[0])
    op["description"] = norm(spec["description"])
    props = {}
    for pname, prop in spec["inputSchema"]["json"]["properties"].items():
        q = {k: v for k, v in prop.items() if k != "default" or v is not None}
        q["description"] = norm(q["description"])
        props[pname] = q
    if path == "/search":
        props["max_results"].update(minimum=1, maximum=10)
    else:
        props["urls"]["maxItems"] = 3
    op["requestBody"]["content"]["application/json"]["schema"] = {
        "type": "object", "required": spec["inputSchema"]["json"]["required"], "properties": props}
SCHEMA.write_text(json.dumps(schema, indent=2) + "\n")
print("wrote", SCHEMA.relative_to(ROOT))
