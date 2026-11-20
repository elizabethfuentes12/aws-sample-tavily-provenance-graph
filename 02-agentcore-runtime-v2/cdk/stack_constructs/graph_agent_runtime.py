"""AgentCore Runtime (platform version V2) running the graph agent from a code package, no container.

`../build_package.sh` installs the agent's dependencies for linux/arm64 into `build/package` (AgentCore Runtime
supports only arm64) and copies the agent code next to them. CDK zips that folder as an asset.
Direct code deployment: https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-get-started-code-deploy-python.html
Platform versions:      https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-how-it-works.html
"""

import pathlib

from aws_cdk import Stack
from aws_cdk import aws_bedrockagentcore as agentcore
from aws_cdk import aws_iam as iam
from aws_cdk import aws_secretsmanager as secretsmanager
from constructs import Construct

# stack_constructs/ -> cdk/ -> 02-agentcore-runtime-v2/build/package (written by ../build_package.sh)
PACKAGE_DIR = pathlib.Path(__file__).resolve().parents[2] / "build" / "package"
ENTRYPOINT = "research_agent.py"
# The docs' direct-code-deploy entryPoint for observability: ADOT wraps the agent process.
RUN_WITH = ["opentelemetry-instrument"]


class GraphAgentRuntime(Construct):
    def __init__(self, scope: Construct, construct_id: str, *,
                 runtime_name: str, gateway: agentcore.Gateway, neo4j_secret_name: str,
                 model_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)
        stack = Stack.of(self)

        if not (PACKAGE_DIR / ENTRYPOINT).exists():
            raise RuntimeError(f"{PACKAGE_DIR} has no {ENTRYPOINT}. Run ../build_package.sh before cdk synth/deploy.")

        neo4j_secret = secretsmanager.Secret.from_secret_name_v2(self, "Neo4jSecret", neo4j_secret_name)

        self.runtime = agentcore.Runtime(
            self, "Runtime",
            runtime_name=runtime_name,
            description="Research agent with a Neo4j knowledge graph; Tavily through AgentCore Gateway",
            agent_runtime_artifact=agentcore.AgentRuntimeArtifact.from_code_asset(
                path=str(PACKAGE_DIR),
                runtime=agentcore.AgentCoreRuntime.PYTHON_3_12,
                entrypoint=RUN_WITH + [ENTRYPOINT],
            ),
            platform_version=agentcore.PlatformVersion.V2,
            network_configuration=agentcore.RuntimeNetworkConfiguration.using_public_network(),
            environment_variables={
                "TAVILY_GATEWAY_URL": gateway.gateway_url,
                "NEO4J_SECRET_ID": neo4j_secret_name,
                "BEDROCK_MODEL_ID": model_id,
            },
        )

        # Observability: AgentCore lets X-Ray deliver the agent's spans to its own log group (needs ADOT >= 0.18.0).
        self.runtime.add_to_role_policy(iam.PolicyStatement(
            sid="SpansToAgentLogGroup",
            actions=["logs:PutResourcePolicy"],
            resources=[f"arn:aws:logs:{stack.region}:{stack.account}:log-group:/aws/bedrock-agentcore/runtimes/*"],
        ))

        # Least privilege for what research_agent.py calls.
        gateway.grant_invoke(self.runtime)
        neo4j_secret.grant_read(self.runtime)
        self.runtime.add_to_role_policy(iam.PolicyStatement(
            sid="InvokeBedrockModel",
            actions=["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"],
            resources=[
                "arn:aws:bedrock:*::foundation-model/*",
                f"arn:aws:bedrock:{stack.region}:{stack.account}:inference-profile/*",
            ],
        ))
