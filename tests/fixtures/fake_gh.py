#!/usr/bin/env python3
"""Fake `gh` for the timesheet tests: one commit for `gh api .../commits`, one pull request for
`gh pr list` (created searches only). Records its argv to $FAKE_GH_RECORD when set."""

import json
import os
import sys

args = sys.argv[1:]
record = os.environ.get("FAKE_GH_RECORD")
if record:
    with open(record, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(args) + "\n")
if args[:1] == ["api"]:
    print(
        json.dumps(
            {
                "sha": "a" * 40,
                "date": "2026-11-17T10:00:00Z",
                "email": "owner@example.com",
                "subject": "Add the timesheet",
                "url": "https://github.com/acme/web/commit/" + "a" * 40,
            }
        )
    )
elif args[:2] == ["pr", "list"]:
    search = args[args.index("--search") + 1]
    items = []
    if search.startswith("created:"):
        items.append(
            {
                "number": 9,
                "title": "Draft timesheets",
                "url": "https://github.com/acme/web/pull/9",
                "createdAt": "2026-11-18T08:00:00Z",
                "mergedAt": None,
            }
        )
    print(json.dumps(items))
else:
    sys.exit(2)
