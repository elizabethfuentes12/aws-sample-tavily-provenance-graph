"""Run deploy_with_boto3.ipynb offline: every boto3 call is checked against the botocore service model, none is sent.

`run_notebook()` executes the notebook's code cells with `BaseClient._make_api_call` replaced by a function that
  1. validates the request with botocore's own validator (required members, types, lengths) plus enum and pattern checks
     on the strings the notebook wrote itself,
  2. records the call, and
  3. returns a response built from the operation's output shape (status READY, one list item named like the resource
     the notebook created, ResourceNotFoundException after a delete).
The build script, `time.sleep` and the network are stubbed. No AWS credentials are used.
"""

import datetime
import io
import json
import os
import pathlib
import re
import secrets
import shutil
import subprocess
import zipfile

import botocore.client


def offline_credentials() -> dict:
    """Credentials for the offline notebook runs. The repo holds no value, real or fake:
    TAVILY_API_KEY, NEO4J_URI and NEO4J_PASSWORD come from the environment when set (the tests then also prove the
    real values never reach a command line or a printed line), otherwise a random value is generated for this run.
    AWS keys are always random: every AWS call is stubbed, so real AWS credentials are never handed to the stubs."""
    def rand() -> str:
        return secrets.token_hex(16)
    return {
        "AWS_ACCESS_KEY_ID": rand(), "AWS_SECRET_ACCESS_KEY": rand(),
        "TAVILY_API_KEY": os.environ.get("TAVILY_API_KEY") or os.environ.get("TAVILY_KEY") or rand(),
        "NEO4J_URI": os.environ.get("NEO4J_URI") or f"neo4j+s://{rand()}.invalid",
        "NEO4J_PASSWORD": os.environ.get("NEO4J_PASSWORD") or rand(),
    }


import botocore.exceptions
import nbformat
from botocore.validate import validate_parameters

ROOT = pathlib.Path(__file__).resolve().parent.parent
FAKE_ACCOUNT = "123456789012"
GET_AFTER_DELETE = {"DeleteAgentRuntime": "GetAgentRuntime", "DeleteGateway": "GetGateway",
                    "DeleteGatewayTarget": "GetGatewayTarget"}
LIST_ITEM_NAME = {"ListGateways": ("CreateGateway", "name"), "ListAgentRuntimes": ("CreateAgentRuntime", "agentRuntimeName")}
TOKENS = {"nextToken", "NextToken", "Marker", "NextMarker", "IsTruncated"}


class Recorder:
    def __init__(self):
        self.calls = []        # (operation, params)
        self.gone = set()
        self.problems = []

    def ops(self):
        return [op for op, _ in self.calls]


def _check_strings(shape, value, path, problems):
    """Enum and pattern checks that botocore's validator does not do. Values that came from fake responses are skipped."""
    kind = shape.type_name
    if kind == "structure" and isinstance(value, dict):
        for key, item in value.items():
            if key in shape.members:
                _check_strings(shape.members[key], item, f"{path}.{key}", problems)
    elif kind == "list" and isinstance(value, list):
        for index, item in enumerate(value):
            _check_strings(shape.member, item, f"{path}[{index}]", problems)
    elif kind == "map" and isinstance(value, dict):
        for key, item in value.items():
            _check_strings(shape.value, item, f"{path}[{key}]", problems)
    elif kind == "string" and isinstance(value, str) and "fake" not in value:
        if shape.enum and value not in shape.enum:
            problems.append(f"{path}={value!r} not in {shape.enum}")
        pattern = shape.metadata.get("pattern")
        if pattern and not re.fullmatch(pattern, value, re.DOTALL):
            problems.append(f"{path} does not match {pattern}")


def _fake_value(shape, name, region, depth=0):
    kind = shape.type_name
    if kind == "structure":
        if depth > 3:
            return {}
        return {key: _fake_value(member, key, region, depth + 1)
                for key, member in shape.members.items() if key not in TOKENS}
    if kind == "list":
        return [_fake_value(shape.member, name, region, depth + 1)] if depth <= 3 else []
    if kind == "map":
        return {}
    if kind == "string":
        if shape.enum:
            return "READY" if "READY" in shape.enum else shape.enum[0]
        if name == "status":
            return "READY"
        if name.lower().endswith("arn"):
            return f"arn:aws:fake:{region}:{FAKE_ACCOUNT}:fake/fake"
        return "fake"
    if kind in ("integer", "long"):
        return 1
    if kind in ("float", "double"):
        return 1.0
    if kind == "boolean":
        return False
    if kind == "timestamp":
        return datetime.datetime(2026, 1, 1, tzinfo=datetime.timezone.utc)
    if kind == "blob":
        return b"fake"
    return None


def install(monkeypatch, recorder):
    answer = json.dumps({"answer": "fake answer", "tools_used": ["tavily___search_web"]}).encode()

    def fake_make_api_call(self, operation_name, api_params):
        model = self.meta.service_model
        operation = model.operation_model(operation_name)
        service = model.service_name
        try:
            if operation.input_shape is not None:
                validate_parameters(api_params, operation.input_shape)
                problems = []
                _check_strings(operation.input_shape, api_params, f"{service}.{operation_name}", problems)
                recorder.problems.extend(problems)
        except botocore.exceptions.ParamValidationError as error:
            recorder.problems.append(f"{service}.{operation_name}: {error}")
        recorder.calls.append((operation_name, api_params))

        if GET_AFTER_DELETE.get(operation_name):
            recorder.gone.add(GET_AFTER_DELETE[operation_name])
        if operation_name in recorder.gone and operation_name.startswith("Get"):
            raise botocore.exceptions.ClientError(
                {"Error": {"Code": "ResourceNotFoundException", "Message": "gone"}}, operation_name)

        if operation.output_shape is None:
            return {}
        response = _fake_value(operation.output_shape, "", self.meta.region_name)
        if service == "sts":
            response["Account"] = FAKE_ACCOUNT
        if operation_name in LIST_ITEM_NAME:
            create_op, field = LIST_ITEM_NAME[operation_name]
            created = [p for op, p in recorder.calls if op == create_op]
            key = "items" if operation_name == "ListGateways" else "agentRuntimes"
            response[key][0][field if operation_name != "ListGateways" else "name"] = created[-1][field] if created else "fake"
        if operation_name == "InvokeAgentRuntime":
            response["response"] = io.BytesIO(answer)
        if operation_name == "ListGatewayTargets":
            response["items"][0]["targetId"] = "fake-target"
        return response

    monkeypatch.setattr(botocore.client.BaseClient, "_make_api_call", fake_make_api_call)


def run_notebook(path, monkeypatch, recorder, workdir, *, confirm_delete=False):
    """Execute the code cells of the notebook in one namespace, offline. `workdir` is a scratch copy of the demo folder."""
    shutil.copytree(ROOT / "schemas", workdir / "schemas")
    monkeypatch.syspath_prepend(str(ROOT))      # for `from invoke_agent import SERIES`
    monkeypatch.chdir(workdir)
    for key, value in {**offline_credentials(), "AWS_DEFAULT_REGION": "us-east-1", "AWS_REGION": "us-east-1"}.items():
        monkeypatch.setenv(key, value)
    install(monkeypatch, recorder)
    monkeypatch.setattr("time.sleep", lambda *_: None)

    def fake_run(command, *args, **kwargs):          # build_package.sh: a tiny zip instead of the 41 MB one
        assert command[:2] == ["bash", "build_package.sh"], command
        (workdir / "build").mkdir(exist_ok=True)
        with zipfile.ZipFile(workdir / "build" / "agent-code.zip", "w") as archive:
            archive.writestr("research_agent.py", "")
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(subprocess, "run", fake_run)

    namespace = {"__name__": "__notebook__"}
    notebook = nbformat.read(path, as_version=4)
    for index, cell in enumerate(c for c in notebook.cells if c.cell_type == "code"):
        source = cell.source
        if confirm_delete:
            source = source.replace("CONFIRM_DELETE = False", "CONFIRM_DELETE = True")
        exec(compile(source, f"{path.name}[code {index}]", "exec"), namespace)
    return namespace
