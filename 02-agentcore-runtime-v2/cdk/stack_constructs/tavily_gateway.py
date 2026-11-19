"""AgentCore Gateway with a Tavily target: the 'Tavily Search' integration template, as code.

AgentCore documents Tavily as a built-in integration template (server https://api.tavily.com, API key auth),
added through the console:
https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/gateway-target-integrations.html
CloudFormation has no resource for console templates, so this construct creates the equivalent target:
an OpenAPI target with the same server and auth. The operations are ours (search_web, read_web_page): Gateway turns
each operationId and description into the tool name and description the model reads.

- Inbound: AWS IAM (SigV4). Callers need `bedrock-agentcore:InvokeGateway`. (The CDK default would create a
  Cognito user pool instead.)
- Outbound: an API key credential provider in AgentCore Identity, sent as `Authorization: Bearer <key>`.
  The key is passed as a reference to a Secrets Manager secret, so its value is not in the synthesized template.
- Tool names: Gateway prefixes the operationId with the target name (devguide: `${target_name}___${tool_name}`).
"""

import pathlib

from aws_cdk import SecretValue
from aws_cdk import aws_bedrockagentcore as agentcore
from constructs import Construct

# Shared with the boto3 and CLI notebooks: 02-agentcore-runtime-v2/schemas/tavily-openapi.json
SCHEMA_PATH = pathlib.Path(__file__).resolve().parents[2] / "schemas" / "tavily-openapi.json"


class TavilyGateway(Construct):
    def __init__(self, scope: Construct, construct_id: str, *,
                 gateway_name: str, target_name: str, api_key_secret_name: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        self.api_key_provider = agentcore.ApiKeyCredentialProvider(
            self, "TavilyApiKey",
            api_key_credential_provider_name=f"{gateway_name}-tavily-key",
            api_key=SecretValue.secrets_manager(api_key_secret_name),
        )

        self.gateway = agentcore.Gateway(
            self, "Gateway",
            gateway_name=gateway_name,
            description="Tavily web search and extract as MCP tools",
            authorizer_configuration=agentcore.GatewayAuthorizer.using_aws_iam(),
        )

        schema = agentcore.ApiSchema.from_local_asset(str(SCHEMA_PATH))
        schema.bind(self)

        self.target = self.gateway.add_open_api_target(
            "Tavily",
            gateway_target_name=target_name,
            description="Web tools: search_web (/search) and read_web_page (/extract)",
            api_schema=schema,
            credential_provider_configurations=[
                agentcore.GatewayCredentialProvider.from_api_key_identity(
                    self.api_key_provider,
                    credential_location=agentcore.ApiKeyCredentialLocation.header(
                        credential_parameter_name="Authorization",
                        credential_prefix="Bearer ",
                    ),
                ),
            ],
        )
