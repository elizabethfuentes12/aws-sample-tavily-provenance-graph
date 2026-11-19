#!/usr/bin/env bash
# deploy.sh: deploys TavilyGraphAgentStack (Demo 02, CDK path).
# The same resources can be created with deploy_with_boto3.ipynb or deploy_with_cli.ipynb; use one path, not several.
#
# First deploy needs, exported in the shell:
#   TAVILY_API_KEY (or TAVILY_KEY)         https://app.tavily.com/home
#   NEO4J_URI NEO4J_USERNAME (or NEO4J_USER) NEO4J_PASSWORD   your Neo4j AuraDB instance (neo4j+s://<id>.databases.neo4j.io)
# They are stored in Secrets Manager (created here, outside CDK) and never written to a template.
#
# What it creates: 2 secrets, an AgentCore Gateway with a Tavily target and an API key credential provider,
# and an AgentCore Runtime (platform version V2) running ../agent from a code package.
# After deploy, AGENT_RUNTIME_ARN is written to ../.env. Run ./destroy.sh to remove everything.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

REGION="${AWS_REGION:-us-east-1}"
TAVILY_API_KEY="${TAVILY_API_KEY:-${TAVILY_KEY:-}}"
export AWS_REGION="$REGION" AWS_DEFAULT_REGION="$REGION"
TAVILY_SECRET="tavily-graph-agent/tavily-api-key"
NEO4J_SECRET="tavily-graph-agent/neo4j-aura"

ACCOUNT=$(aws sts get-caller-identity --query Account --output text 2>/dev/null) || {
  echo "ERROR: could not resolve AWS account. Configure credentials (aws configure or AWS_PROFILE)." >&2; exit 1; }

echo "=========================================="
echo "  Demo 02, graph agent on AgentCore Runtime V2"
echo "  Account : $ACCOUNT"
echo "  Region  : $REGION"
echo "=========================================="
echo "Press Ctrl+C within 5 s to cancel."
sleep 5

create_secret_once() {  # name, description, value-producing command
  if aws secretsmanager describe-secret --secret-id "$1" >/dev/null 2>&1; then
    echo "Secret $1 exists, leaving it as is."
  else
    aws secretsmanager create-secret --name "$1" --description "$2" --secret-string "$3" >/dev/null
    echo "Created secret $1"
  fi
}

if ! aws secretsmanager describe-secret --secret-id "$TAVILY_SECRET" >/dev/null 2>&1; then
  : "${TAVILY_API_KEY:?export TAVILY_API_KEY before the first deploy}"
fi
create_secret_once "$TAVILY_SECRET" "Tavily API key for the graph agent demo" "${TAVILY_API_KEY:-}"

if ! aws secretsmanager describe-secret --secret-id "$NEO4J_SECRET" >/dev/null 2>&1; then
  : "${NEO4J_URI:?export NEO4J_URI (AuraDB, neo4j+s://...) before the first deploy}"
  : "${NEO4J_PASSWORD:?export NEO4J_PASSWORD before the first deploy}"
  NEO4J_JSON=$(python3 -c 'import json,os;print(json.dumps({"uri":os.environ["NEO4J_URI"],"username":os.getenv("NEO4J_USERNAME") or os.getenv("NEO4J_USER","neo4j"),"password":os.environ["NEO4J_PASSWORD"],"database":os.getenv("NEO4J_DATABASE","neo4j")}))')
fi
create_secret_once "$NEO4J_SECRET" "Neo4j AuraDB connection for the graph agent demo" "${NEO4J_JSON:-}"

../build_package.sh

python3 -m venv .venv
source .venv/bin/activate
if command -v uv &>/dev/null; then uv pip install -q -r requirements.txt; else pip install -q -r requirements.txt; fi

cdk bootstrap "aws://$ACCOUNT/$REGION"
cdk deploy TavilyGraphAgentStack --require-approval never --outputs-file cdk-outputs.json
python3 write_env.py cdk-outputs.json ..
echo ""
echo "Deploy complete. Try: cd .. && uv run python invoke_agent.py \"your question\""
