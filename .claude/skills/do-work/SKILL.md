---
name: do-work
description: Drive a msg change end-to-end through the multi-agent workflow — tech-lead plans, specialist agents implement, a PR is opened against github.com/mohanadkaleia/msg, code-reviewer and security-reviewer loops run until clean, the PR is merged, and the Linear ticket is closed. Use this for any substantive code change (typically one Linear ENG-xx ticket). Skip for trivial edits (typos, single-line tweaks, doc-only changes) where the loop adds more overhead than value.
---

# /do-work — multi-agent change workflow (msg)

You are driving a change through the msg agent loop. Do NOT do the implementation work yourself — your job is to dispatch the right agents in the right order, relay findings between them, merge when clean, and return the PR URL.

The task is usually a Linear ticket (`ENG-xx`, team `engineering`, project `msg`). If the task description is vague and has no ticket, ask one clarifying question before starting.

## Hard rules

- Do not skip steps. Do not collapse steps (e.g. "I'll just review it myself instead of dispatching code-reviewer").
- Do not implement code yourself. Server/CLI work goes to `python-engineer`. Web (`web/`) work goes to `ui-engineer`. CI / compose / Docker work goes to `devops-engineer`. Planning and review-triage go to `tech-lead`. Reviews go to `code-reviewer` and `security-reviewer`.
- All agents run on **Opus 4.8** (`model: opus`) unless the user overrides. If a named agent type is not registered in the current session, dispatch a general-purpose agent on the same model and instruct it to read and adopt `.claude/agents/<role>.md` from the repo as its role.
- Each review loop has a **hard cap of 3 rounds**. If a reviewer still has findings after 3 rounds, stop the loop, surface the remaining findings to the user, and ask how to proceed.
- Never use `--no-verify`, `git push --force`, or any destructive git operation without explicit user approval mid-run.
- Locked decisions D1–D14 in `docs/technical-design.md` are not relitigated inside a ticket. If implementation reveals a locked decision is wrong, stop and surface it — the fix is a TDD revision, not a drive-by change.
- Keep user-facing narration to one short sentence per phase transition — the user can read the agent outputs.

## The workflow

### 1. Plan (tech-lead)

Dispatch the `tech-lead` agent with the ticket ID and description. Ask it to produce a concrete implementation plan in the session file `.claude/chat/<eng-xx>-<slug>.md` (`## Implementation Plan`): files to change/create, which agents implement which steps, test plan, risks.

Surface the plan to the user in a few bullets and proceed — do not wait for explicit approval unless the plan introduces a destructive operation, a new runtime dependency outside the TDD §4.1 stack, or a schema/protocol change.

Mark the Linear ticket **In Progress**.

### 2. Create a working branch

Branch off up-to-date `main`, using the ticket's Linear branch name so the PR auto-links:

```
git checkout main && git pull && git checkout -b mohanad/eng-<n>-<slug>
```

For parallel tickets, run each in its own git worktree so they don't collide.

### 3. Implement (specialist agents, parallel where independent)

Dispatch the implementation agents named in the plan:
- **`python-engineer`** — anything under `server/` (msgd: core, api, db, ws, projections, export, plugins; tests) or `cli/` (msgctl).
- **`ui-engineer`** — anything under `web/`.
- **`devops-engineer`** — `.github/workflows/`, `docker-compose.yml`, `Dockerfile`, deploy/backup scripts.

If multiple agents are needed AND their files are disjoint, dispatch them in a single message so they run concurrently. If one depends on another's output, run them sequentially.

Each agent must add/update tests for new behavior and pass `uv run ruff check`, `uv run ruff format --check`, `uv run mypy`, `uv run pytest` (or the web equivalents) before reporting done.

### 4. Open the PR

Once the implementation agents report done:
1. Commit with the convention `<change_type>[ENG-<n>]: <desc>` where `<change_type>` ∈ `feat`, `bug`, `refactor`, `misc`. Include the commit trailers required by the current session (Co-Authored-By + Claude-Session).
2. Push the branch and open the PR with `gh pr create` — title `<change_type>[ENG-<n>]: <desc>`, body with: summary, the ticket's acceptance criteria as a checklist, a link to the Linear issue, verification output (test/lint results), and the session footer.
3. Capture the PR URL.

### 5. Code review loop (cap: 3 rounds)

- Dispatch the `code-reviewer` agent with the PR number/URL; it leaves inline comments on the PR via `gh` and a verdict summary.
- Read its comments (`gh api repos/{owner}/{repo}/pulls/{n}/comments` and the review summary).
- Zero substantive findings → exit the loop.
- Otherwise: dispatch `tech-lead` to triage (address vs. push back, with reasoning), then the appropriate implementation agent(s) to apply the fixes, commit and push to the same branch, and reply/resolve the addressed comments.
- Repeat up to 3 rounds; if findings remain, stop and ask the user.

### 6. Security review loop (cap: 3 rounds)

Same shape with the `security-reviewer` agent, run AFTER code review is clean — running them in parallel produces conflicting fix instructions on the same lines. Only medium+ findings block.

### 7. Merge and close

Both reviews clean = approved:
1. Confirm CI is green on the PR (`gh pr checks`) once CI exists.
2. Merge: `gh pr merge <n> --squash --delete-branch`.
3. Mark the Linear ticket **Done**.
4. Print the PR URL on its own line, prefixed with `PR:`. That's the success signal — nothing else after it.

## When to short-circuit

If at any point the user says "stop", "pause", or "I'll take it from here", halt immediately, summarize where things stand (step, agents run, branch, PR URL if any), and yield.

If a step fails irrecoverably (tests can't pass after 2 implementation rounds, push rejected, merge conflict you can't resolve cleanly), stop the loop, surface the failure clearly, and ask for guidance — do not invent a workaround.
