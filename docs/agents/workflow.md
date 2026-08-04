# Development Workflow

How work moves from idea to merged code in this repo. Written to replace the previous habit of building whatever came to mind and pushing it straight to `main`.

## When something needs a ticket

- **A new feature, signal, detector, panel, OSC route, or behavior** → must go through a ticket first: `to-tickets` → `triage` → `implement`. Nothing gets built ad hoc.
- **A bugfix or correction of already-broken existing behavior** → can be done directly, no ticket required.

If it's unclear which bucket something falls into, treat it as needing a ticket — the cost of an unnecessary ticket is far lower than the cost of another unrequested feature.

## Who can put something on the tracker

Claude may draft a ticket when it spots an opportunity worth pursuing (e.g. via `/to-tickets`), but **nothing is ever filed on GitHub without the user explicitly approving the ticket's content first**. A drafted-but-unapproved ticket is just a proposal in the conversation, not a tracked issue.

## Branch and PR model

One branch per ticket, one PR per branch. No commits land directly on `main` for anything that needed a ticket (see above).

- **Backend-only changes** — a ticket whose diff touches only `model/`, `api/routes.py`, `transport/`, `osc/`, `storage/`, `core.py`, `config.py`, `main.py`, `simulator/`, `tests/`:
  the implementing agent runs `python3 -m tests.run` locally on the branch and merges the PR via `gh pr merge` only if it's green. No GitHub Actions workflow is involved — this mirrors the project's existing "no build/lint setup" stance (see `CLAUDE.md`).
- **Any change touching `api/static/` (the frontend panel)** — no auto-merge, ever. `tests/run.py` has zero JS/frontend coverage, so a green backend run proves nothing about a frontend change. The PR opens and waits for the user's own review and merge.
- A ticket that touches both backend and frontend files follows the stricter (frontend) rule.

`main` has no GitHub branch-protection rule turned on — this is discipline enforced by this document, not by repo configuration. Solo, pre-release project: a hard technical gate would add friction (e.g. in a genuine emergency) without a real benefit here.

## Why it's like this

Before this was written, features got added the moment they were thought of, with a lot of latitude given to the agent and not always full understanding of what was being proposed — which produced features nobody had actually asked for, and a `main` branch with no review step at all. The ticket-first rule moves the "do I actually want this" checkpoint to before any code is written; the branch/PR model moves the "does this actually work" checkpoint to before it lands on `main`.
