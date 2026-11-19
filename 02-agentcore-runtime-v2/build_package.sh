#!/usr/bin/env bash
# build_package.sh: builds the AgentCore Runtime code package (linux/arm64, Python 3.12). No Docker.
# Shared by the three ways to deploy (deploy_with_boto3.ipynb, deploy_with_cli.ipynb, cdk/).
#
# Output (git-ignored):
#   build/package/         the agent code and its dependencies (CDK zips this folder as an asset)
#   build/agent-code.zip   the same content as a .zip, to upload to S3 (boto3 and CLI notebooks)
#
# Steps from https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-get-started-code-deploy-python.html
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
AGENT_DIR="$SCRIPT_DIR/agent"
OUT="$SCRIPT_DIR/build/package"
ZIP="$SCRIPT_DIR/build/agent-code.zip"

command -v uv >/dev/null || { echo "uv is required (https://docs.astral.sh/uv/)" >&2; exit 1; }
command -v zip >/dev/null || { echo "zip is required" >&2; exit 1; }
rm -rf "$OUT" "$ZIP" && mkdir -p "$OUT"
# AgentCore Runtime supports only arm64, so download linux/arm64 wheels whatever machine this runs on.
uv pip install --quiet \
  --python-platform aarch64-manylinux2014 \
  --python-version 3.12 \
  --target "$OUT" \
  --only-binary=:all: \
  -r "$AGENT_DIR/requirements.txt"
cp "$AGENT_DIR"/*.py "$OUT/"
# The docs recommend leaving __pycache__ out of the package.
find "$OUT" -type d -name __pycache__ -prune -exec rm -rf {} +
# AgentCore Runtime needs 644 on files and 755 on directories.
find "$OUT" -type d -exec chmod 755 {} +
find "$OUT" -type f -exec chmod 644 {} +
(cd "$OUT" && zip -qr -X "$ZIP" .)
du -sh "$OUT" | awk '{print "Package: " $1 " unzipped (limit 750 MB)"}'
du -sh "$ZIP" | awk '{print "Zip:     " $1 " (limit 250 MB)"}'
