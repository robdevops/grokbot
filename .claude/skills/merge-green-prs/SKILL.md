---
name: merge-green-prs
description: Check this repo's open pull requests and merge the ones from claude/* branches whose tests have passed AND that have auto-merge switched on (the "Auto merge when complete" checkbox in Claude Code's CI button). Use when asked to merge green PRs, watch PRs, or clear the PR queue. Never merges a PR from any other branch, or one with auto-merge off.
---

# Merge green PRs

Repo: the current repository (`owner/repo` from the git remote). Use the GitHub MCP tools (`mcp__github__*`), never `gh`.

## Which PRs are eligible
A PR is merged only if ALL of these hold; otherwise report it and leave it alone:
1. Open and not a draft.
2. Head branch starts with `claude/` and lives in this same repo (not a fork).
3. Base branch is the default branch.
4. `mergeable_state` is `clean` (not `blocked`, `behind`, `dirty`, `unstable`, `draft`, `unknown`).
5. Its check runs (`get_check_runs`) exist, are all `completed`, and every one has conclusion `success`.
6. No review with `CHANGES_REQUESTED` (`get_reviews`) and no unresolved review thread (`get_review_comments`).
7. Auto-merge is ON for the PR. That is the "Auto merge when complete" checkbox in Claude Code's CI button, which sets
   GitHub's auto-merge: `pull_request_read` `get` shows a non-null `auto_merge` object. Unticked means do not merge. If the
   field is absent you cannot tell, so treat it as off and say so. The checkbox is the user's switch: never enable or
   disable auto-merge yourself.

## Steps
1. `list_pull_requests` (state `open`). Skip, with the reason, everything that fails 1-3.
2. For each remaining PR, read it fresh from GitHub: `pull_request_read` `get`, `get_check_runs`, `get_reviews`,
   `get_review_comments`. Never rely on earlier results or memory for a PR's state.
3. Auto-merge off -> "auto-merge off", do nothing (green or not). Checks still `queued` or `in_progress` -> "waiting", do nothing. Any failure -> "red", say which check, do not merge.
   `behind` or conflicted -> report; do not update the branch or merge without being asked.
4. Eligible -> `merge_pull_request` with `merge_method: merge` and `expectedHeadSha` set to the head SHA you just read
   (so a push that lands in between aborts the merge).
5. Read the PR back from GitHub and confirm `merged: true`. If the session was subscribed to it, unsubscribe.
6. Finish with one table: PR number and title, outcome (merged / waiting / red / blocked / auto-merge off / skipped), and the reason.

## Never
- Merge a PR from a branch that doesn't start with `claude/`, a fork, a draft, one with auto-merge off, or one with a failing, missing or pending check.
- Skip, disable or re-run tests to get green; push to a PR's branch; force-push; approve reviews; close PRs; delete branches.
- State a PR's status without reading it from GitHub in this run.
