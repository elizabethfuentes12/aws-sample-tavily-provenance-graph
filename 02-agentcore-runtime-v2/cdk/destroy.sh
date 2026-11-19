#!/usr/bin/env bash
# destroy.sh, removes the stack and the two secrets deploy.sh created. Your AuraDB instance is not touched.
set -euo pipefail
cd "$(cd "$(dirname "$0")" && pwd)"
REGION="${AWS_REGION:-us-east-1}"
export AWS_REGION="$REGION" AWS_DEFAULT_REGION="$REGION"
source .venv/bin/activate 2>/dev/null || true
cdk destroy TavilyGraphAgentStack --force
for s in tavily-graph-agent/tavily-api-key tavily-graph-agent/neo4j-aura; do
  aws secretsmanager delete-secret --secret-id "$s" --force-delete-without-recovery >/dev/null 2>&1 \
    && echo "Deleted secret $s" || echo "Secret $s already gone."
done
