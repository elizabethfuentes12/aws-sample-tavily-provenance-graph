from aws_cdk import CfnOutput, Stack
from constructs import Construct
from stack_constructs import GraphAgentRuntime, TavilyGateway

# Secrets created by deploy.sh (outside CDK, so no secret value enters a template or the CDK context).
TAVILY_SECRET_NAME = "tavily-graph-agent/tavily-api-key"
NEO4J_SECRET_NAME = "tavily-graph-agent/neo4j-aura"
DEFAULT_MODEL_ID = "us.anthropic.claude-sonnet-4-5-20250929-v1:0"


class GraphAgentStack(Stack):
    """Demo 02: Tavily behind AgentCore Gateway + the graph agent on AgentCore Runtime V2 + Neo4j AuraDB.

    Account and Region come from the deploy credentials. Run deploy.sh; outputs are written to ../.env.
    """

    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)
        model_id = self.node.try_get_context("bedrock_model_id") or DEFAULT_MODEL_ID

        tavily = TavilyGateway(self, "Tavily", gateway_name="tavily-graph-agent", target_name="tavily",
                               api_key_secret_name=TAVILY_SECRET_NAME)
        agent = GraphAgentRuntime(self, "Agent", runtime_name="tavily_graph_agent", gateway=tavily.gateway,
                                  neo4j_secret_name=NEO4J_SECRET_NAME, model_id=model_id)

        CfnOutput(self, "AgentRuntimeArn", value=agent.runtime.agent_runtime_arn,
                  description="Set this as AGENT_RUNTIME_ARN in the demo .env file")
        CfnOutput(self, "TavilyGatewayUrl", value=tavily.gateway.gateway_url)
        CfnOutput(self, "TavilyGatewayArn", value=tavily.gateway.gateway_arn)
