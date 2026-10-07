---
name: auto-merge
description: After opening or being handed a pull request from a claude/* branch, turn on GitHub auto-merge so it merges when CI completes, and stay silent about it. Use whenever a PR is created, or when asked to auto-merge. Never merges anything that is not from a claude/* branch in this repo.
---

# Auto-merge when CI completes

Use the GitHub MCP tools (`mcp__github__*`), never `gh`. **No chatter**: do not report PR status, CI progress, subscriptions
or merges, and do not answer PR events (opened, checks finished, merged, subscription notices). Speak only for the cases under
"Say something" below, in one line.

## When
Right after a PR is opened from this session, or when a `claude/*` PR is pointed out to you.

## Eligible PRs
Open, not a draft, head branch starts with `claude/` in this same repo, base is the default branch. Anything else: do nothing.

## Steps
1. `enable_pr_auto_merge` with `mergeMethod: MERGE`. GitHub then merges it itself once the required `check` passes. Done: say nothing.
2. If that fails because the PR is already mergeable and the `check` run is already completed and successful
   (`pull_request_read` `get_check_runs`), merge it now with `merge_pull_request`, `merge_method: merge`, and
   `expectedHeadSha` set to the head SHA you just read. Say nothing.
3. Do not subscribe to the PR, and do not unsubscribe or comment. The user watches CI in the Claude Code CI button.

## Say something (one line each)
- Auto-merge could not be enabled for any other reason (for example "Allow auto-merge" is off in the repo settings).
- CI failed and you cannot fix it, or the PR conflicts with `main` and the conflict is not a plain merge.

## Never
- Merge or enable auto-merge on a PR from another branch, a fork, a draft, or one whose checks have failed or are missing.
- Skip, disable or re-run tests to get green; force-push; approve reviews; close PRs; delete branches.
- Turn auto-merge off for a PR the user switched it on for.
