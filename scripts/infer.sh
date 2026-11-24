#!/usr/bin/env bash
# Usage: scripts/infer.sh [request.json]
# Uses `terraform output` and the standard AWS credential chain (e.g. AWS_PROFILE).
set -euo pipefail
cd "$(dirname "$0")/.."
ENDPOINT=${DECIDER_ENDPOINT:-$(terraform output -raw endpoint)}
SECRET=${DECIDER_API_KEY_SECRET_ARN:-$(terraform output -raw api_key_secret_arn)}
REGION=$(cut -d: -f4 <<<"$SECRET")
KEY=$(aws secretsmanager get-secret-value --region "$REGION" \
  --secret-id "$SECRET" --query SecretString --output text \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["key"])')
REQUEST=${1:-examples/triage.json}
curl --fail-with-body --silent --show-error --max-time 60 \
  -H "x-api-key: $KEY" -H 'content-type: application/json' \
  --data-binary "@$REQUEST" "$ENDPOINT" | python3 -m json.tool
