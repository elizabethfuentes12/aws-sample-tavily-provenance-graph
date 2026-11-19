"""Ask the deployed graph agent a question through AgentCore Runtime (InvokeAgentRuntime, IAM auth).

    uv run python invoke_agent.py "Who supplies the batteries in Tesla cars?"
    uv run python invoke_agent.py --series      # the five demo questions, each in a new runtime session

Needs AGENT_RUNTIME_ARN in .env (cdk/deploy.sh writes it; the notebooks print it) and `bedrock-agentcore:InvokeAgentRuntime` on it.
"""

import json
import os
import sys
import uuid

import boto3
from dotenv import load_dotenv

load_dotenv()

SERIES = [
    "Which companies make the batteries for the best-selling electric cars?",
    "Where are the largest EV battery factories, and which companies run them?",
    "Which countries mine and refine most of the lithium used in EV batteries?",
    "Who supplies the batteries in Tesla cars?",
    "Do the big battery makers build factories in the countries that refine their lithium?",
]


def ask(client, arn: str, prompt: str) -> dict:
    response = client.invoke_agent_runtime(
        agentRuntimeArn=arn,
        runtimeSessionId=str(uuid.uuid4()),       # new session per question: the graph is the only memory
        contentType="application/json",
        accept="application/json",
        payload=json.dumps({"prompt": prompt}).encode(),
    )
    return json.loads(response["response"].read().decode())


def main() -> None:
    arn = os.getenv("AGENT_RUNTIME_ARN")
    if not arn:
        sys.exit("AGENT_RUNTIME_ARN is not set. Deploy first (notebooks or cdk/deploy.sh).")
    client = boto3.client("bedrock-agentcore", region_name=os.getenv("AWS_REGION", "us-east-1"))
    questions = SERIES if sys.argv[1:] == ["--series"] else [" ".join(sys.argv[1:]) or SERIES[0]]
    for i, q in enumerate(questions, 1):
        out = ask(client, arn, q)
        print(f"\nQ{i}: {q}\n  tools used: {out.get('tools_used')}\n  {out.get('answer', out)}")


if __name__ == "__main__":
    main()
