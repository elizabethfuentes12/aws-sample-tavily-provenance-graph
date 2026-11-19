import aws_cdk as cdk

from infra import GraphAgentStack

app = cdk.App()

# env=None: CDK resolves account and region from the active AWS credentials.
# No account ID is hardcoded anywhere in this app.
GraphAgentStack(app, "TavilyGraphAgentStack")

app.synth()
