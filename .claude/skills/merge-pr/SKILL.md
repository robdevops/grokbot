---
name: merge-pr
description: Merge a pull request when the user asks for it ("merge", "pr merge", "merge it"), after checking its CI passed. One-line reply. Never merges on your own, never subscribes to PRs, and never uses GitHub's auto-merge.
---

# Merge a PR when asked

Use the GitHub MCP tools (`mcp__github__*`), never `gh`. Only act when the user asks. Do not watch PRs, subscribe to them,
or merge anything unprompted, and do not turn on GitHub's auto-merge or touch the CI button's boxes.

## Which PR
The one the user names, otherwise the PR for the current branch. If there is no clear candidate, say so in one line.

## Steps
1. Read it fresh from GitHub: `pull_request_read` `get`, `get_check_runs`, `get_reviews`, `get_review_comments`.
2. Merge only if ALL hold: open and not a draft; in this repo (not a fork); `mergeable_state` is `clean`; every check run is
   `completed` with conclusion `success`; no review is `CHANGES_REQUESTED`; no unresolved review thread.
3. Call `merge_pull_request` with `merge_method: merge` and `expectedHeadSha` set to the head SHA you just read.
4. Read the PR back and confirm `merged: true`. If the session is subscribed to it, unsubscribe.
5. Reply with one line: `#N merged (sha)`.

## When it cannot merge (one line)
- CI still running or missing: say so, and do not wait or poll.
- CI red: say which check failed.
- Conflict, changes requested or an open thread: name it. Do not update the branch or fix it unless asked.

## Never
- Merge without being asked, or merge a draft or a PR with failing, missing or pending checks.
- Skip, disable or re-run tests to get green; force-push; approve reviews; close PRs; delete branches.
