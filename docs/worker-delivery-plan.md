# Spec-Driven Worker Delivery Plan

This plan defines how the Clinical Image Comparison MVP is implemented and reviewed by isolated GPT workers while the main agent acts as orchestrator, advisor, and merge gatekeeper.

## Worker runtime

Every implementation and review worker uses exactly:

```text
model: gpt-5.6-luna
thinking: max
```

If that runtime is unavailable or cannot be confirmed, stop instead of silently falling back to another model or thinking level.

## Source-of-truth hierarchy

When sources disagree, higher items win:

1. `CONTEXT.md` — domain language and meaning.
2. `docs/mvp-spec.md` — product behavior, permissions, and acceptance boundary.
3. `docs/adr/` — accepted architectural trade-offs.
4. `docs/implementation-plan.md` — slice order and implementation constraints.
5. The GitHub issue for the active slice — execution contract.
6. Code and tests — implementation evidence, never a replacement for the spec.

`docs/architecture-landscape.md` is research context, not a source of MVP requirements.

Workers may not modify `CONTEXT.md`, `mvp-spec.md`, or an accepted ADR to make their implementation pass. They return a decision request and stop. The orchestrator advises on the trade-off; the user confirms product or architectural changes before source documents are updated.

## Topology

```text
Main agent — Orchestrator + Advisor + Gatekeeper
├── B — Builder/Fixer
├── S — Spec Reviewer
├── Q — Standards Reviewer
└── R — Security/Data-Integrity Reviewer when triggered
```

### Main agent

- Selects the next unblocked slice and pins its base SHA.
- Creates the slice contract and validates Definition of Ready.
- Assigns workers and isolated worktrees.
- Reads actual diffs and reruns required gates; never trusts summaries alone.
- Arbitrates findings against the source hierarchy.
- Advises the user when a product/architecture decision is required.
- Is the only actor allowed to merge or close the slice issue.
- Does not bypass review by writing the feature itself.

### Builder/Fixer

- Implements exactly one slice in one branch/worktree.
- Writes executable acceptance tests before implementation and captures red/green evidence.
- Owns migrations and cleanup behavior required by that slice.
- Commits code but never merges or approves its own work.
- Is resumed as the Fixer after review; a reviewer never becomes the fixer.

### Spec Reviewer

- Reviews `BASE_SHA...HEAD` against requirement IDs, the slice issue, and `mvp-spec.md`.
- Reports missing/partial requirements, wrong behavior, and scope creep.
- Does not review style and does not edit code.

### Standards Reviewer

- Reviews the same fixed diff against `AGENTS.md`, `CONTEXT.md`, ADRs, implementation-plan constraints, and the code-review smell baseline.
- Reports architecture drift, vocabulary drift, speculative abstractions, duplicated logic, and broken module seams.
- Does not decide whether the product requirement itself is correct and does not edit code.

### Security/Data-Integrity Reviewer

Required whenever a slice touches authentication, authorization, Patient data, consent, uploads, EXIF, storage paths, immutable originals, audit, transactions, archive/delete, locks, export, migrations, backup, deployment, secrets, media parsing, or a new dependency. In this project it is expected for nearly every slice.

It reviews negative paths and data-loss/privacy risks independently from Standards and Spec, and never edits code.

## Concurrency policy

The nine implementation slices are dependency-ordered and share hotspots such as `models.py`, migrations, `app/__init__.py`, and `board.py`. Therefore:

- Run **one Builder only** at a time.
- Do not implement adjacent slices in parallel.
- After Builder handoff, launch S, Q, and triggered R reviewers in one parallel wave.
- The orchestrator may rerun tests while reviewers read the diff, using a separate database and media root.
- Each reviewer has an isolated checkout and isolated test resources.

This avoids merge coordination that would cost more than the small repo gains from parallel coding.

## Baseline prerequisite

The current domain/spec/plan files are untracked. Before the first worktree is created:

1. Review the working tree and exclude transient `.pi/` artifacts.
2. Commit `AGENTS.md`, `CONTEXT.md`, and intended `docs/` files as the specification baseline.
3. Confirm `git status --porcelain` is clean.
4. Assign stable requirement IDs in `mvp-spec.md`.
5. Record that commit as the first `BASE_SHA`.

Without this commit, isolated workers will not see the agreed spec.

## Requirement IDs

Every independently testable requirement receives a stable ID:

```text
BA-PAT-*   Patient and consent
BA-CAP-*   Capture Library and Shot Types
BA-SET-*   Comparison Sets and lifecycle
BA-CAN-*   Canvas, grid, Frame, crop, labels
BA-EXP-*   Preview and export
BA-SEC-*   Roles, audit, locks, privacy
BA-OPS-*   Deployment, backup, restore
```

IDs do not change when wording improves and are never reused after withdrawal. Keep the ID table in `mvp-spec.md`; do not create a second requirements source.

## GitHub issue structure

Use one parent MVP issue and child issues for Slice 0–8 from `implementation-plan.md`. Native dependencies serialize the slices. Execution evidence lives in GitHub comments; durable product/architecture decisions live in repo docs.

Use existing triage vocabulary:

- `needs-triage`: contract draft;
- `needs-info`: blocked by a user/spec decision;
- `ready-for-agent`: Definition of Ready passes;
- `ready-for-human`: work requires an unavailable human/environment action;
- `wontfix`: deliberately rejected.

Do not add per-slice Markdown files merely to mirror issue content.

## Slice contract

Before dispatch, the orchestrator prepares this packet:

```text
slice ID and issue
base SHA
user-observable outcome
in scope / out of scope
requirement IDs
Given/When/Then acceptance scenarios
domain invariants and ADR constraints
allowed files / ownership
migration, rollback, and failure cleanup expectations
red command / green command
test database and media-root setup
security/data-integrity triggers
required handoff evidence
```

### Definition of Ready

A slice is dispatchable only when:

1. It has one bounded user outcome.
2. Every behavior maps to a requirement ID.
3. Positive, negative, and permission scenarios are explicit.
4. Domain terms, permissions, data ownership, and output behavior are unambiguous.
5. Dependencies and base SHA are fixed.
6. File ownership does not overlap another active Builder.
7. Test environment and gate commands are runnable.
8. Migration, rollback/cleanup, and security triggers are known.
9. Fixtures contain no real clinical data.
10. The worker need not invent a product or architecture decision.

If any item fails, do not spawn Builder; apply `needs-info` or `needs-triage`.

## Per-slice execution loop

### 1. Prepare

- Create `agent/slice-NN` from the merged previous-slice SHA.
- Create one isolated Builder worktree.
- Send the slice contract and exact runtime requirement.

### 2. Red

Builder adds the smallest executable checks that express the contract, runs them before implementation, and records:

- command;
- expected failing assertion;
- base/test commit SHA;
- reason the failure proves the requirement is not already satisfied.

If a red state is impossible, Builder records a justified exception; “tests were added after coding” is not an exception.

### 3. Green

Builder implements only enough to pass the slice contract, reruns tests on clean PostgreSQL/media fixtures, commits, and hands off:

```text
slice ID, base SHA, head SHA
commit list and changed files
migration/data impact
test commands and exact results
requirement → test/evidence matrix
known deviations or risks
review focus
clean git status
```

### 4. Independent review wave

The orchestrator verifies the fixed point and non-empty diff, then launches S, Q, and triggered R in parallel against the same `BASE_SHA...HEAD`.

Each finding contains:

- severity;
- file and line;
- violated requirement, source rule, or security invariant;
- reproduction/evidence;
- required correction.

Reports remain separate. A Standards pass cannot hide a Spec failure, and vice versa.

### 5. Fix

- Orchestrator removes duplicates and rejects findings unsupported by source/evidence.
- Resume the same Builder on the same branch/worktree.
- Builder adds fix commits; no amend, rebase, or history rewrite after review begins.
- All reviewers rerun against the new HEAD, including previously passing axes.
- If Builder is unavailable, start a new Luna Max Fixer on the same branch after inspecting and preserving worktree state.

### 6. Gate and merge

The orchestrator independently runs migrations, tests, and slice acceptance commands with isolated resources, inspects the diff, and performs UAT for user-visible outcomes. Only then:

- merge with a visible slice commit/merge SHA;
- attach final evidence matrix to the issue;
- close the issue;
- use the merge SHA as the next slice's base.

## Severity policy

| Severity | Meaning | Policy |
|---|---|---|
| S0 Critical | Clinical-data leak, auth bypass, original corruption/loss, restore failure | Stop all work; never merge |
| S1 Blocker | Requirement failure, broken permission/audit/lock/migration | Slice cannot merge |
| S2 Major | Material correctness or maintainability defect | Fix before merge unless explicitly proven outside the MVP contract and tracked with owner |
| S3 Minor | Cosmetic or non-material documentation issue | May merge with a follow-up issue |

Missing review, missing evidence, dirty worktree, failing test, or wrong test environment means **not done**, regardless of severity labels.

Workers cannot waive S0–S2. Only the orchestrator can classify/defer an S2, with written evidence; any S2 touching the MVP contract, security, privacy, integrity, or recovery remains blocking.

## Slice ownership and review focus

| Slice | Builder outcome | Mandatory review focus |
|---:|---|---|
| 0 | Executable prototype/trust-boundary baseline | Image parser, EXIF, limits, deterministic render |
| 1 | Login, Patient, consent, audit | Role/CSRF/session, transaction/audit, PostgreSQL migration |
| 2 | Capture Library and immutable originals | Upload validation, path safety, SHA, atomic write, cleanup |
| 3 | Persistent Set/Frame | Same-Patient references, ordering, reopen, authorization |
| 4 | Grid/crop/visibility/lock | Geometry parity, original immutability, lost-update prevention |
| 5 | PNG/PDF export | WYSIWYG version, Viewer 403, no-store, derivative cleanup |
| 6 | Duplicate/archive/admin/merge | Referential integrity, last Admin, audit completeness |
| 7 | Reviewed batch upload | Explicit metadata review, all-or-nothing rollback |
| 8 | LAN deployment and recovery | HTTPS/secrets, paired backup generation, isolated restore |

## Stop conditions

The orchestrator stops and asks the user when:

- a requirement is missing, contradictory, or admits materially different user behavior;
- a new domain term or changed meaning is required;
- implementation would contradict an accepted ADR;
- a worker proposes AI, HIS, cloud, microservices, free layout, or another out-of-scope capability;
- retention, consent, permissions, storage, migration, or recovery trade-offs are unresolved;
- required human infrastructure or UAT is unavailable.

Resolution path:

- product behavior changes → update `mvp-spec.md` and requirement IDs;
- domain meaning changes → update `CONTEXT.md`;
- hard-to-reverse implementation trade-off → create/supersede ADR;
- sequencing only → update `implementation-plan.md`.

After a source change, pin a new base SHA and regenerate the active slice contract. Never continue on a temporary verbal assumption.

## Definition of Done — slice

A slice is done only when:

- every contract requirement ID has green evidence;
- red evidence exists or has a valid recorded exception;
- tests pass on the specified environment;
- Spec and Standards reviews pass;
- Security/Data-Integrity review passes when triggered;
- migration and failure cleanup are verified;
- no unresolved S0/S1 or blocking S2 remains;
- user-visible UAT passes;
- the orchestrator reruns gates and inspects the actual diff;
- final evidence is attached to the GitHub issue and the branch is merged.

## Definition of Done — MVP

The MVP meets requirements only when all are true:

1. Slices 0–8 are merged in dependency order.
2. One hundred percent of `BA-*` requirements map to passing automated tests or explicit UAT evidence.
3. A clean PostgreSQL database migrates to head and the app boots with production configuration checks.
4. Editor completes the full Acceptance Boundary from `mvp-spec.md`; Admin and Viewer permissions behave correctly.
5. A saved Set reopens from another LAN desktop.
6. Preview, PNG, and PDF represent the same persisted Set version; hidden Frames stay hidden and original checksums remain unchanged.
7. Audit, edit lock, archive/delete guard, Shot Type merge, and reviewed batch rollback pass negative tests.
8. PostgreSQL and media from one backup generation restore into an isolated environment and pass login/read/export smoke tests.
9. There are no unresolved S0/S1 or MVP/security/data-integrity S2 findings.
10. The user signs off the final LAN UAT checklist using de-identified fixtures.

Passing unit tests alone is insufficient; passing worker review alone is insufficient; a demo without restore and security evidence is insufficient.

## Final UAT checklist

- `UAT-001`: create Patient and Consent Confirmation.
- `UAT-002`: upload single and reviewed batch Captures; confirm Date and Shot Type.
- `UAT-003`: create and duplicate a Comparison Set.
- `UAT-004`: configure preset/custom Canvas and structured grid.
- `UAT-005`: reorder, hide, pan, zoom, and label without changing originals.
- `UAT-006`: reopen from another authorized desktop.
- `UAT-007`: export matching PNG and PDF; Viewer cannot export.
- `UAT-008`: verify edit lock, audit, archive/delete guard, backup, and restore.

Store only build SHA, date, tester role, pass/fail, and non-sensitive evidence in GitHub. Never upload clinical images, Patient names, or original filenames to the tracker.
