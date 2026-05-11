"""
Extract a single issue (and its comments) from GHArchive via BigQuery.

Usage:
    uv run scripts/issue-migration/extract.py <issue_number> [--out path.json]

Requires `bq` CLI authenticated (gcloud auth login + a billing-enabled project).
Set BQ_PROJECT to your GCP project (used for query billing).
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

SOURCE_REPO = "encode/httpx"

QUERY_TEMPLATE = """
SELECT
  type,
  created_at,
  actor.login AS actor,
  JSON_EXTRACT_SCALAR(payload, '$.action') AS action,
  JSON_EXTRACT_SCALAR(payload, '$.issue.number') AS issue_number,
  JSON_EXTRACT_SCALAR(payload, '$.issue.title') AS issue_title,
  JSON_EXTRACT_SCALAR(payload, '$.issue.body') AS issue_body,
  JSON_EXTRACT_SCALAR(payload, '$.issue.state') AS issue_state,
  JSON_EXTRACT_SCALAR(payload, '$.issue.html_url') AS issue_url,
  JSON_EXTRACT_SCALAR(payload, '$.issue.user.login') AS issue_author,
  JSON_EXTRACT_SCALAR(payload, '$.comment.body') AS comment_body,
  JSON_EXTRACT_SCALAR(payload, '$.comment.user.login') AS comment_author,
  JSON_EXTRACT_SCALAR(payload, '$.comment.created_at') AS comment_created_at,
  payload
FROM `githubarchive.year.*`
WHERE repo.name = '{repo}'
  AND type IN ('IssuesEvent', 'IssueCommentEvent')
  AND JSON_EXTRACT_SCALAR(payload, '$.issue.number') = '{issue_number}'
ORDER BY created_at ASC
"""


def run_bq(query: str, project: str) -> list[dict[str, Any]]:
    cmd = [
        "bq",
        "--project_id",
        project,
        "query",
        "--use_legacy_sql=false",
        "--format=json",
        "--max_rows=10000",
        query,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        print(result.stderr, file=sys.stderr)
        raise SystemExit(result.returncode)
    return json.loads(result.stdout)


def reconstruct(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Collapse the event stream into a final issue snapshot."""
    if not rows:
        raise SystemExit("No events found for this issue. It may predate GHArchive (pre-2011) or the number is wrong.")

    issue: dict[str, Any] = {
        "number": None,
        "title": None,
        "body": None,
        "author": None,
        "url": None,
        "state": "open",
        "created_at": None,
        "closed_at": None,
        "comments": [],
    }

    for row in rows:
        if row["type"] == "IssuesEvent":
            issue["number"] = int(row["issue_number"])
            issue["title"] = row["issue_title"] or issue["title"]
            issue["body"] = row["issue_body"] if row["issue_body"] is not None else issue["body"]
            issue["author"] = row["issue_author"] or issue["author"]
            issue["url"] = row["issue_url"] or issue["url"]
            if row["action"] == "opened":
                issue["created_at"] = row["created_at"]
            if row["action"] == "closed":
                issue["state"] = "closed"
                issue["closed_at"] = row["created_at"]
            if row["action"] == "reopened":
                issue["state"] = "open"
                issue["closed_at"] = None
        elif row["type"] == "IssueCommentEvent" and row["action"] == "created":
            issue["comments"].append(
                {
                    "author": row["comment_author"],
                    "body": row["comment_body"],
                    "created_at": row["comment_created_at"] or row["created_at"],
                }
            )

    return issue


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("issue_number", type=int)
    parser.add_argument("--out", type=Path, default=None)
    parser.add_argument("--project", default=os.environ.get("BQ_PROJECT"))
    args = parser.parse_args()

    if not args.project:
        raise SystemExit("Set BQ_PROJECT env var or pass --project (billing-enabled GCP project).")

    query = QUERY_TEMPLATE.format(repo=SOURCE_REPO, issue_number=args.issue_number)
    rows = run_bq(query, args.project)
    issue = reconstruct(rows)

    out = args.out or Path(f"scripts/issue-migration/data/issue-{args.issue_number}.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(issue, indent=2))
    print(f"Wrote {out} ({len(issue['comments'])} comments, state={issue['state']})")


if __name__ == "__main__":
    main()
