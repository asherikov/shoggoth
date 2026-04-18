#!/usr/bin/env bash
set -euo pipefail

# JSON payload is passed as the first argument (per webhookd behavior)
# Reference: https://github.com/ncarlier/webhookd
PAYLOAD="$1"

# Extract values from JSON payload
EVENT_TYPE=$(printf '%s' "$PAYLOAD" | jq -r '.event_type // empty')
ACTION=$(printf '%s' "$PAYLOAD" | jq -r '.action // empty')
ISSUE_ID=$(printf '%s' "$PAYLOAD" | jq -r '.issue.id // empty')
ISSUE_URL=$(printf '%s' "$PAYLOAD" | jq -r '.issue.url // empty')

# Log the incoming request
echo "Received Redmine issue update webhook"
echo "Issue ID: $ISSUE_ID"
echo "Issue URL: $ISSUE_URL"
echo "Action: $ACTION"

# Run docker command in slave container
docker run --rm docker-registry.shoggoth.local/slave_noble:latest \
  echo "Processing Redmine issue $ISSUE_ID ($ISSUE_URL)"

echo "Successfully processed Redmine issue update"
