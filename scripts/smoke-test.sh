#!/usr/bin/env bash
# Usage: scripts/smoke-test.sh <endpoint-url> <api-key-secret-arn> [region]
set -euo pipefail
ENDPOINT=${1:?endpoint url (terraform output endpoint)}
SECRET=${2:?api key secret arn}
REGION=${3:-$(cut -d: -f4 <<<"$SECRET")}
BASE=${ENDPOINT%/v1/systemone}
KEY=$(aws secretsmanager get-secret-value --region "$REGION" --secret-id "$SECRET" \
  --query SecretString --output text | python3 -c 'import json,sys; print(json.load(sys.stdin)["key"])')

fail=0
check() { # name expected actual
  if [ "$2" = "$3" ]; then echo "ok   $1"; else echo "FAIL $1 (expected $2, got $3)"; fail=1; fi
}
status() { curl -s -o /dev/null -w '%{http_code}' --max-time 30 "$@"; }
BODY='{"state":"The build passed all tests.","questions":{"ok":{"type":"noul","instructions":"Is the build healthy?"}}}'

check "healthz without key"  200 "$(status "$BASE/healthz")"
check "no key rejected"      403 "$(status -H 'content-type: application/json' -d "$BODY" "$ENDPOINT")"
check "wrong key rejected"   403 "$(status -H 'x-api-key: nope' -H 'content-type: application/json' -d "$BODY" "$ENDPOINT")"
check "valid noul request"   200 "$(status -H "x-api-key: $KEY" -H 'content-type: application/json' -d "$BODY" "$ENDPOINT")"
check "unknown path"         404 "$(status -H "x-api-key: $KEY" "$BASE/nope")"

echo "--- sample response"
curl -s --max-time 30 -H "x-api-key: $KEY" -H 'content-type: application/json' -d "$BODY" "$ENDPOINT"; echo
exit $fail
