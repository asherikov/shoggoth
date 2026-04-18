#!/usr/bin/env bash
set -euo pipefail

curl -X POST http://localhost:8080/issue-updated \
  -H "Content-Type: application/json" \
  -d '{
    "event_type": "issue",
    "action": "updated",
    "issue": {
      "id": 123,
      "url": "https://redmine.example.com/issues/123"
    },
    "actor": { "id": 1, "login": "admin", "name": "Admin" },
    "changes": [
      {
        "field": "status_id",
        "kind": "attribute",
        "old": { "raw": 1, "text": "New" },
        "new": { "raw": 2, "text": "In Progress" }
      }
    ],
    "last_note": {
      "id": 42,
      "notes": "This is a test update",
      "created_on": "2026-04-17T10:00:00Z"
    },
    "project": {
      "id": 1,
      "identifier": "test-project",
      "name": "Test Project"
    }
  }'
