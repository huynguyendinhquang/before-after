# ADR 0002: Archive the implementation on `v1` and keep `main` documentation-only

- Status: Accepted
- Date: 2026-08-10

## Context

The MVP exploration produced a broad implementation spanning patient identity and consent, immutable clinical image storage, structured comparison sets, frame placement, export, lifecycle administration, batch capture, deployment and recovery work, a local auto-login demo, and three throwaway frontend variants.

The implementation became larger than the product direction that had actually been validated. In particular, the frontend direction and the final production operating model had not been accepted through clinic use. Continuing to treat this code as the default `main` branch would make implementation momentum look like product certainty.

## Decision

1. Preserve the complete implementation on branch `v1` at snapshot commit `00f42d7`.
2. Keep `main` documentation-only.
3. Preserve product requirements, domain language, architecture decisions, image policy, delivery history, and operational notes on `main`.
4. Treat the frontend layouts on `v1` as disposable prototypes, not an accepted production design.
5. Begin future implementation from an explicit decision: either continue from `v1`, or create a new implementation branch from the documented domain and requirements.

## Consequences

- No runnable application is expected on `main`.
- The implementation remains recoverable and reviewable without dominating the repository default view.
- Future work must deliberately select which earlier implementation decisions to retain.
- Documentation may describe historical implementation details; those details are evidence, not a commitment to reuse the code.
