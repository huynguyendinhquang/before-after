# Before / After — Decision Archive

The `main` branch is intentionally documentation-only. It preserves the product, domain, architecture, safety, and delivery decisions made while exploring the clinic image-comparison MVP.

The complete working implementation is preserved on branch [`v1`](https://github.com/huynguyendinhquang/before-after/tree/v1), snapshot commit [`00f42d7`](https://github.com/huynguyendinhquang/before-after/commit/00f42d7).

## Current decisions

- Deploy as a clinic-LAN modular monolith.
- Model the workflow as Patient → Capture Library → Comparison Set → Frame.
- Preserve immutable original Captures; crop, pan, zoom, labels, and visibility belong to each Frame placement.
- Use structured equal-size grids and server-owned PNG/PDF rendering.
- Treat Capture Date as editable clinical metadata, with EXIF only as a suggestion.
- Keep the three frontend directions as prototypes on `v1`; no frontend variant has been accepted as the final product UI.
- Do not continue implementation directly on `main`. Start from `v1` or create a new implementation branch after revisiting these decisions.

## Documents

- [MVP specification](docs/mvp-spec.md)
- [Domain model](docs/agents/domain.md)
- [Architecture landscape](docs/architecture-landscape.md)
- [LAN modular-monolith ADR](docs/adr/0001-lan-modular-monolith.md)
- [Implementation archive ADR](docs/adr/0002-v1-archive-and-docs-main.md)
- [Image policy](docs/image-policy.md)
- [Implementation plan](docs/implementation-plan.md)
- [Deployment notes](docs/deployment.md)
