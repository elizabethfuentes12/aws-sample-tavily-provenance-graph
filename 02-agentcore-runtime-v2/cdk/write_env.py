"""Parse CDK outputs.json and write AGENT_RUNTIME_ARN to the demo's .env file."""
import json
import pathlib
import sys


def set_env_key(env_path: pathlib.Path, key: str, value: str) -> None:
    lines = env_path.read_text().splitlines() if env_path.exists() else []
    updated, new_lines = False, []
    for line in lines:
        if line.startswith(f"{key}=") or line.startswith(f"# {key}="):
            new_lines.append(f"{key}={value}")
            updated = True
        else:
            new_lines.append(line)
    if not updated:
        new_lines.append(f"{key}={value}")
    env_path.write_text("\n".join(new_lines) + "\n")


outputs = json.loads(pathlib.Path(sys.argv[1]).read_text())
env_file = (pathlib.Path(sys.argv[2]) if len(sys.argv) > 2 else pathlib.Path(__file__).parent.parent) / ".env"
for stack_outputs in outputs.values():
    if "AgentRuntimeArn" in stack_outputs:
        set_env_key(env_file, "AGENT_RUNTIME_ARN", stack_outputs["AgentRuntimeArn"])
        print(f"  {env_file}: AGENT_RUNTIME_ARN={stack_outputs['AgentRuntimeArn']}")
print("Done, .env updated.")
