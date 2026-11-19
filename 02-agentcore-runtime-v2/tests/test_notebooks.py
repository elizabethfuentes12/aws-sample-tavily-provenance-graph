"""The deployment notebooks are valid and match the AWS API models. Offline: no AWS calls, no credentials.

- deploy_with_boto3.ipynb runs against botocore's service model (see notebook_offline.py): every request validates, the
  create calls are the ones the docs describe, cleanup is guarded and deletes in reverse order.
- deploy_with_cli.ipynb: every `aws <service> <command> --option` it uses exists in the installed AWS CLI.
"""

import json
import pathlib
import re
import shutil
import subprocess

import nbformat
import pytest

from notebook_offline import Recorder, offline_credentials, run_notebook

ROOT = pathlib.Path(__file__).resolve().parent.parent
BOTO3_NB = ROOT / "deploy_with_boto3.ipynb"
CLI_NB = ROOT / "deploy_with_cli.ipynb"

CREATE_ORDER = ["CreateSecret", "CreateApiKeyCredentialProvider", "CreateRole", "CreateGateway", "CreateGatewayTarget",
                "CreateBucket", "CreateAgentRuntime", "InvokeAgentRuntime"]
DELETE_ORDER = ["DeleteAgentRuntime", "DeleteGatewayTarget", "DeleteGateway", "DeleteApiKeyCredentialProvider",
                "DeleteRole", "DeleteBucket", "DeleteSecret"]


@pytest.mark.parametrize("path", [BOTO3_NB, CLI_NB], ids=lambda p: p.name)
def test_notebook_is_valid_nbformat(path):
    nbformat.validate(nbformat.read(path, as_version=4))


def _run(monkeypatch, tmp_path, **kwargs):
    recorder = Recorder()
    namespace = run_notebook(BOTO3_NB, monkeypatch, recorder, tmp_path, **kwargs)
    assert not recorder.problems, "\n".join(recorder.problems)
    return recorder, namespace


def test_boto3_calls_validate_and_follow_the_docs(monkeypatch, tmp_path):
    recorder, namespace = _run(monkeypatch, tmp_path)
    ops = recorder.ops()
    positions = [ops.index(op) for op in CREATE_ORDER]
    assert positions == sorted(positions), "resources are created out of order"
    assert not [op for op in ops if op.startswith("Delete")], "cleanup ran without CONFIRM_DELETE"
    params = dict(recorder.calls)
    runtime = params["CreateAgentRuntime"]
    assert runtime["platformVersion"] == "V2"
    code = runtime["agentRuntimeArtifact"]["codeConfiguration"]
    assert code["entryPoint"] == ["research_agent.py"] and code["runtime"] == "PYTHON_3_12"
    assert runtime["networkConfiguration"] == {"networkMode": "PUBLIC"}
    assert {"TAVILY_GATEWAY_URL", "NEO4J_SECRET_ID", "BEDROCK_MODEL_ID"} <= set(runtime["environmentVariables"])
    gateway = params["CreateGateway"]
    assert (gateway["protocolType"], gateway["authorizerType"]) == ("MCP", "AWS_IAM")
    key = params["CreateGatewayTarget"]["credentialProviderConfigurations"][0]
    assert key["credentialProviderType"] == "API_KEY"
    assert key["credentialProvider"]["apiKeyCredentialProvider"] == {
        "providerArn": "arn:aws:fake:us-east-1:123456789012:fake/fake", "credentialLocation": "HEADER",
        "credentialParameterName": "Authorization", "credentialPrefix": "Bearer "}
    assert json.loads(params["CreateGatewayTarget"]["targetConfiguration"]["mcp"]["openApiSchema"]["inlinePayload"]) == \
        json.loads((ROOT / "schemas" / "tavily-openapi.json").read_text())
    invoked = [p for op, p in recorder.calls if op == "InvokeAgentRuntime"]
    assert len(invoked) == 5 and len({p["runtimeSessionId"] for p in invoked}) == 5
    assert all(len(p["runtimeSessionId"]) >= 33 for p in invoked)
    # the account ID never reaches the notebook's printed output helper
    assert namespace["mask"](namespace["BUCKET"]).count("123456789012") == 0


def test_boto3_trust_and_permissions_are_scoped(monkeypatch, tmp_path):
    recorder, _ = _run(monkeypatch, tmp_path)
    roles = [p for op, p in recorder.calls if op == "CreateRole"]
    for role in roles:
        trust = json.loads(role["AssumeRolePolicyDocument"])["Statement"][0]
        assert trust["Principal"] == {"Service": "bedrock-agentcore.amazonaws.com"}
        assert set(trust["Condition"]) == {"StringEquals", "ArnLike"}
    policies = [json.loads(p["PolicyDocument"]) for op, p in recorder.calls if op == "PutRolePolicy"]
    for document in policies:
        for statement in document["Statement"]:
            actions = statement["Action"] if isinstance(statement["Action"], list) else [statement["Action"]]
            assert not any(a.endswith(":*") or a == "*" for a in actions), statement
    runtime_policy = json.dumps(policies[-1])
    for needed in ("bedrock-agentcore:InvokeGateway", "secretsmanager:GetSecretValue", "bedrock:InvokeModel", "s3:GetObject"):
        assert needed in runtime_policy


def test_boto3_cleanup_is_guarded_and_reverse_order(monkeypatch, tmp_path):
    recorder, _ = _run(monkeypatch, tmp_path, confirm_delete=True)
    ops = recorder.ops()
    deletes = [op for op in ops if op.startswith("Delete")]
    assert {"DeleteAgentRuntime", "DeleteGatewayTarget", "DeleteGateway", "DeleteApiKeyCredentialProvider", "DeleteRole",
            "DeleteBucket", "DeleteSecret"} <= set(deletes)
    first = [deletes.index(op) for op in DELETE_ORDER]
    assert first == sorted(first), f"cleanup order: {deletes}"
    assert deletes.count("DeleteRole") == 2 and ops.count("DeleteRolePolicy") == 2


def _cli_commands(path):
    """Yield (service, command, [options]) for every `aws ...` invocation in the %%bash cells."""
    notebook = nbformat.read(path, as_version=4)
    for cell in notebook.cells:
        if cell.cell_type != "code" or not cell.source.startswith("%%bash"):
            continue
        text = re.sub(r"\\\n\s*", " ", cell.source)              # join line continuations
        text = re.sub(r"<<-?\s*'?(\w+)'?.*?\n\1\n", "\n", text, flags=re.S)   # drop heredoc bodies
        for line in text.splitlines():
            line = re.sub(r"\"[^\"]*\"|'[^']*'", '""', line)           # quoted values may contain ) ; > or --
            for match in re.finditer(r"(?:^|[\s$(|;&])aws\s+(?:--[\w-]+\s+\S+\s+)*([a-z0-9-]+)\s+([a-z0-9-]+)", line):
                rest = re.split(r"[|)>;<]|&&", line[match.end():])[0]     # options of this command only
                yield match.group(1), match.group(2), re.findall(r"(?<=\s)--[a-z0-9-]+", rest)


@pytest.mark.skipif(shutil.which("aws") is None, reason="AWS CLI not installed")
def test_cli_commands_and_options_exist():
    help_cache = {}
    seen = set()
    for service, command, options in _cli_commands(CLI_NB):
        seen.add((service, command))
        if (service, command) not in help_cache:
            result = subprocess.run(["aws", service, command, "help"], capture_output=True, text=True,
                                    env={**__import__("os").environ, "AWS_PAGER": "", "PAGER": "cat", "MANPAGER": "cat"})
            assert result.returncode == 0, f"aws {service} {command}: {result.stderr[:200]}"
            help_cache[(service, command)] = re.sub(r".\x08", "", result.stdout)
        for option in options:
            assert re.search(rf"^\s+{re.escape(option)}\b", help_cache[(service, command)], re.M), f"aws {service} {command} has no {option}"
    assert ("bedrock-agentcore-control", "create-agent-runtime") in seen
    assert ("bedrock-agentcore", "invoke-agent-runtime") in seen


def _cli_json_files():
    """The JSON files the CLI notebook writes with heredocs, with $VARIABLES replaced by a placeholder."""
    notebook = nbformat.read(CLI_NB, as_version=4)
    files = {}
    for cell in notebook.cells:
        if cell.cell_type == "code":
            for name, body in re.findall(r"cat > build/cli/([\w-]+\.json) <<EOF\n(.*?)\nEOF", cell.source, re.S):
                files[name] = json.loads(re.sub(r"\$\{?\w+\}?", "x", body))
    return files


def test_cli_json_files_match_the_api_shapes():
    import boto3
    from botocore.validate import validate_parameters
    files = _cli_json_files()
    assert {"gateway-trust.json", "gateway-policy.json", "runtime-trust.json", "runtime-policy.json", "runtime-artifact.json",
            "runtime-env.json", "target-credentials.json"} <= set(files)
    creds = offline_credentials()
    control = boto3.client("bedrock-agentcore-control", region_name="us-east-1", aws_access_key_id=creds["AWS_ACCESS_KEY_ID"],
                           aws_secret_access_key=creds["AWS_SECRET_ACCESS_KEY"])
    model = control.meta.service_model
    runtime_input = model.operation_model("CreateAgentRuntime").input_shape.members
    target_input = model.operation_model("CreateGatewayTarget").input_shape.members
    validate_parameters({"agentRuntimeName": "x", "roleArn": "x", "agentRuntimeArtifact": files["runtime-artifact.json"],
                         "environmentVariables": files["runtime-env.json"],
                         "networkConfiguration": {"networkMode": "PUBLIC"}, "platformVersion": "V2"},
                        model.operation_model("CreateAgentRuntime").input_shape)
    validate_parameters({"gatewayIdentifier": "x", "targetConfiguration": {"mcp": {"openApiSchema": {"inlinePayload": "{}"}}},
                         "credentialProviderConfigurations": files["target-credentials.json"]},
                        model.operation_model("CreateGatewayTarget").input_shape)
    assert files["runtime-artifact.json"]["codeConfiguration"]["runtime"] in runtime_input["agentRuntimeArtifact"] \
        .members["codeConfiguration"].members["runtime"].enum
    assert target_input["credentialProviderConfigurations"].member.members["credentialProviderType"].enum.count("API_KEY") == 1


AWS_STUB = r'''#!/usr/bin/env python3
"""Stand-in for the AWS CLI: logs the call, prints a canned answer. Used to run the CLI notebook's bash offline."""
import json, os, pathlib, sys
args = sys.argv[1:]
log = pathlib.Path(os.environ["AWS_STUB_DIR"])
with open(log / "calls.log", "a") as f:
    f.write(" ".join(a for a in args if not a.startswith("{")) + "\n")
svc, cmd = (args + ["", ""])[:2]
opt = lambda name: args[args.index(name) + 1] if name in args else None
q = opt("--query") or ""
gone = (log / f"gone-{cmd.replace('delete-', 'get-')}")
if cmd == "get-caller-identity": print("123456789012")
elif cmd == "create-secret": print("arn:aws:secretsmanager:us-east-1:123456789012:secret:x")
elif cmd == "create-api-key-credential-provider":
    assert os.path.exists(opt("--cli-input-json").removeprefix("file://")), "provider.json missing"
    print("arn:aws:bedrock-agentcore:us-east-1:123456789012:token-vault/default/apikeycredentialprovider/x\tarn:aws:secretsmanager:us-east-1:123456789012:secret:y")
elif cmd == "create-role": print("arn:aws:iam::123456789012:role/x")
elif cmd == "create-gateway": print("gw-1234567890")
elif cmd == "get-gateway" and not gone.exists(): print("READY" if q == "status" else "arn:aws:bedrock-agentcore:us-east-1:123456789012:gateway/gw https://gw.example.com/mcp")
elif cmd == "create-gateway-target": print("target-1")
elif cmd == "get-gateway-target" and not gone.exists(): print("READY")
elif cmd == "create-agent-runtime": print("rt-1")
elif cmd == "get-agent-runtime" and not gone.exists():
    print({"status": "READY", "platformVersion": "V2"}.get(q, "arn:aws:bedrock-agentcore:us-east-1:123456789012:runtime/rt-1"))
elif cmd == "invoke-agent-runtime":
    json.dump({"answer": "fake", "tools_used": []}, open(args[-1], "w")); print("{}")
elif cmd == "list-agent-runtimes": print("rt-1")
elif cmd == "list-gateways": print("gw-1234567890")
elif cmd == "list-gateway-targets": print("target-1")
elif cmd == "list-role-policies": print("p1")
elif cmd.startswith("delete-"): gone.touch()
elif cmd.startswith("get-"): sys.exit(254)
'''


def _bash_cells():
    return [c.source.split("\n", 1)[1] for c in nbformat.read(CLI_NB, as_version=4).cells
            if c.cell_type == "code" and c.source.startswith("%%bash")]


def _run_cli_notebook(tmp_path, monkeypatch, confirm_delete):
    import os
    import sys
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "aws").write_text(AWS_STUB)
    (bin_dir / "aws").chmod(0o755)
    (bin_dir / "sleep").write_text("#!/bin/sh\n")                  # polling and IAM waits take no time
    (bin_dir / "sleep").chmod(0o755)
    (tmp_path / "schemas").mkdir()
    shutil.copy(ROOT / "schemas" / "tavily-openapi.json", tmp_path / "schemas")
    (tmp_path / "build_package.sh").write_text('mkdir -p build && echo zip > build/agent-code.zip\n')
    env = {**os.environ, "PATH": f"{bin_dir}:{pathlib.Path(sys.executable).parent}:{os.environ['PATH']}", "AWS_STUB_DIR": str(tmp_path),
           "PYTHONPATH": str(ROOT), "PYTHONDONTWRITEBYTECODE": "1", **offline_credentials()}
    outputs = []
    for source in _bash_cells():
        if confirm_delete:
            source = source.replace("CONFIRM_DELETE=false ", "CONFIRM_DELETE=true  ")
        result = subprocess.run(["bash", "-c", source], cwd=tmp_path, env=env, capture_output=True, text=True)
        assert result.returncode == 0, f"{source[:200]}\n--- stdout\n{result.stdout}\n--- stderr\n{result.stderr}"
        outputs.append(result.stdout + result.stderr)
    return (tmp_path / "calls.log").read_text().splitlines(), "\n".join(outputs), tmp_path, env


def test_cli_notebook_runs_against_a_stub_cli(tmp_path, monkeypatch):
    calls, output, workdir, env = _run_cli_notebook(tmp_path, monkeypatch, confirm_delete=False)
    commands = [" ".join(c.split()[:2]) for c in calls]
    create = [c for c in commands if c.startswith(("secretsmanager create", "bedrock-agentcore-control create", "iam create", "s3api create"))]
    assert create == ["secretsmanager create-secret", "bedrock-agentcore-control create-api-key-credential-provider",
                      "iam create-role", "bedrock-agentcore-control create-gateway", "bedrock-agentcore-control create-gateway-target",
                      "s3api create-bucket", "iam create-role", "bedrock-agentcore-control create-agent-runtime"]
    assert sum(c == "bedrock-agentcore invoke-agent-runtime" for c in commands) == 5
    assert not [c for c in commands if " delete-" in c], "dry run must not delete"
    assert "[dry run]" in output and "123456789012" not in output
    assert "--platform-version V2" in "\n".join(calls)
    # secrets never reach a command line or a printed line
    seen = "\n".join(calls) + output
    assert env["TAVILY_API_KEY"] not in seen and env["NEO4J_PASSWORD"] not in seen
    assert not (workdir / "build" / "cli" / "provider.json").exists() and not (workdir / "build" / "cli" / "neo4j-secret.json").exists()


def test_cli_notebook_cleanup_order(tmp_path, monkeypatch):
    calls, output, workdir, env = _run_cli_notebook(tmp_path, monkeypatch, confirm_delete=True)
    deletes = [c.split()[1] for c in calls if len(c.split()) > 1 and c.split()[1].startswith("delete-")]
    order = ["delete-agent-runtime", "delete-gateway-target", "delete-gateway", "delete-api-key-credential-provider",
             "delete-role-policy", "delete-role", "delete-object", "delete-bucket", "delete-secret"]
    positions = [deletes.index(name) for name in order]
    assert positions == sorted(positions), deletes
    assert not (workdir / "build" / "cli").exists()
