---
name: merge-after-ci
description: Merge pull requests from claude/* branches yourself once their CI passes, silently. Use right after opening a PR, when a claude/* PR is pointed out, or when a CI-finished event arrives for one. Never merges anything that is not from a claude/* branch in this repo, and never uses GitHub's own auto-merge.
---

# Merge after CI passes

Use the GitHub MCP tools (`mcp__github__*`), never `gh`. **No chatter**: do not report PR status, CI progress, subscriptions
or merges, and do not answer PR events (opened, checks finished, merged, subscription notices). Speak only for the cases under
"Say something" below, in one line.

## Which PRs
Open, not a draft, head branch starts with `claude/` in this same repo, base is the default branch. Anything else: do nothing.

## Steps
1. Right after a PR is opened (or when one is pointed out), subscribe to it (`subscribe_pr_activity`) so you hear when CI completes.
2. When its `check` finishes (a `check_suite.completed` event) or if CI is already done, read the PR fresh from GitHub:
   `pull_request_read` `get`, `get_check_runs`, `get_reviews`, `get_review_comments`. Never rely on earlier results.
3. Merge only if ALL hold: `mergeable_state` is `clean`; every check run is `completed` with conclusion `success`; no review
   is `CHANGES_REQUESTED`; no unresolved review thread. Then call `merge_pull_request` with `merge_method: merge` and
   `expectedHeadSha` set to the head SHA you just read (a push that lands in between aborts the merge). Say nothing.
4. CI still running: do nothing and wait for the next event. CI red: fix it if the fix is small and yours, push, and wait again.
5. After the merge the subscription ends by itself.

## Say something (one line each)
- CI failed and you cannot fix it, or the PR conflicts with `main` and the conflict is not a plain merge.
- A review asks for changes you cannot make on your own.

## Never
- Enable or use GitHub's auto-merge, or touch the CI button's boxes.
- Merge a PR from another branch, a fork, a draft, or one whose checks have failed or are missing.
- Skip, disable or re-run tests to get green; force-push; approve reviews; close PRs; delete branches.
