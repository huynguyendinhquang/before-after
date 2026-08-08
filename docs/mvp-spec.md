# Clinical Image Comparison MVP

Requirement IDs are stable references for implementation, tests, reviews, and UAT. IDs are never reused if a requirement is withdrawn.

## Goal

Replace the routine CorelDRAW PowerCLIP workflow with a desktop web app that lets clinic staff upload longitudinal patient images, place them in consistent comparison Frames, adjust crop, and export the Canvas as PNG or PDF.

The MVP organizes existing image files. Guided camera capture and automatic image alignment are later work.

## Users and permissions

| Role | Permissions |
|---|---|
| `BA-SEC-001` Admin | Manage users and canonical Shot Types; merge Shot Type Proposals; perform all Editor actions |
| `BA-SEC-002` Editor | Manage Patients, Captures, Comparison Sets, and exports |
| `BA-SEC-003` Viewer | View Patients, Captures, and Comparison Sets; cannot edit or export |

**`BA-SEC-004`**: Every create, edit, archive, Shot Type merge, and export records the acting user and time.

## Patient records

- **`BA-PAT-001`**: A Patient has a unique patient ID, name, and birth year.
- **`BA-PAT-002`**: Patient data is entered manually in the MVP.
- **`BA-PAT-003`**: The app records a Consent Confirmation, including who confirmed it and when, before accepting clinical images.
- **`BA-PAT-004`**: The app does not replace the HIS or store a full medical record or legal consent form.

## Capture Library

- **`BA-CAP-001`**: Each Patient has one Capture Library containing immutable original images.
- **`BA-CAP-002`**: A Capture has an authoritative Capture Date.
  - Read EXIF `DateTimeOriginal` when present and use it only as a suggestion.
  - Require the Editor to confirm or override the date before saving.
  - Do not infer the date from file modification time.
- **`BA-CAP-003`**: A Capture has a Shot Type.
  - Typeahead recommends canonical Shot Types.
  - If none fits, an Editor may create a Shot Type Proposal.
  - An Admin may promote or merge proposals into a canonical Shot Type; affected Captures follow the canonical group.
- **`BA-CAP-004`**: Archiving hides a Capture from normal Library selection without breaking existing Comparison Sets.
- **`BA-CAP-005`**: A Capture referenced by a Comparison Set cannot be hard-deleted. It must first be removed from every Set.

### Upload flows

**`BA-CAP-006`**: Both entry points are supported:

1. Upload from the Capture Library.
2. From a Comparison Set, add a Frame and either select an existing Capture or upload a new one. A new upload is stored once in the Capture Library and referenced by the Frame.

**`BA-CAP-007`**: Single-image upload is the primary flow. Batch upload is secondary and requires an explicit review of Capture Date and Shot Type for every image before commit.

## Comparison Sets

- **`BA-SET-001`**: A Patient may have multiple named Comparison Sets, such as “Full history” and “Summary”.
- **`BA-SET-002`**: A Comparison Set references Captures; it never copies original image files. One Capture may appear in multiple Sets and a Set may mix Shot Types.
- **`BA-SET-003`**: Duplicating a Set copies its layout, Frame selection, order, visibility, labels, and crop state while retaining references to the same Captures.
- **`BA-SET-004`**: Captures are ordered by Capture Date, oldest first, by default. Editors may reorder Frames manually.
- **`BA-SEC-005`**: Only one Editor may hold the edit lock for a Set. Other users see it read-only until the lock is released or expires.

## Canvas and grid

- **`BA-CAN-001`**: The Editor chooses a Canvas from presets including 16:9, 16:10, A4 landscape, and A4 portrait, or enters a custom size.
- **`BA-CAN-002`**: Layout is a structured grid, not a free-form drawing surface.
- **`BA-CAN-003`**: A Grid Preset consists of:
  - shared Frame aspect ratio;
  - column count.
  All Frames in the grid have the same size.
- **`BA-CAN-004`**: A Frame may be visible or hidden. Hidden Frames retain their configuration and are excluded from export.
- **`BA-CAN-005`**: A Frame uses cover fitting by default and supports manual pan and zoom. Crop, pan, and zoom belong to the Frame placement; they never mutate the Capture or affect the same Capture in another Set.

## Visible text

- **`BA-CAN-006`**: Every Set has a title.
- **`BA-CAN-007`**: Capture Date visibility has a Set-level default and a per-Frame override.
- **`BA-CAN-008`**: Patient ID, name, and birth year are optional Set-level output fields.
- **`BA-CAN-009`**: Hiding a label changes presentation only; it does not remove stored metadata.

## Export

- **`BA-EXP-001`**: Export PNG and PDF.
- **`BA-EXP-002`**: Output is WYSIWYG: Canvas dimensions, grid, order, crop, visibility, and labels match the editor.
- **`BA-EXP-003`**: Hidden Frames are not exported.
- **`BA-SEC-006`**: Viewer accounts cannot export.
- **`BA-SEC-007`**: Every export is audited.

## Deployment and durability

- **`BA-OPS-001`**: Optimize the MVP for desktop browsers on the clinic LAN.
- **`BA-OPS-002`**: Run one modular monolith backed by PostgreSQL for metadata and a managed filesystem for image assets; see [ADR-0001](adr/0001-lan-modular-monolith.md).
- **`BA-OPS-003`**: Preserve original image bytes. Rendered previews and exports are derivatives.
- **`BA-OPS-004`**: Back up PostgreSQL and image assets together every day to a second storage device.
- **`BA-OPS-005`**: Periodically verify that backups can be restored.

## Outside MVP

- Automatic image registration or AI alignment.
- Guided mobile camera capture or ghost overlays.
- HIS lookup or synchronization.
- Full consent-form and signature management.
- Free-form Frame positioning or mixed-size Frames.
- Cloud, multi-site synchronization, and offline clients.
- Automatic summary selection.

## Acceptance boundary

The MVP is complete when an authorized Editor can:

1. create or find a Patient and confirm consent;
2. add Captures from either supported entry point and confirm date and Shot Type;
3. create or duplicate a Comparison Set;
4. configure its Canvas and structured grid;
5. select, reorder, hide, pan, zoom, and label Frames without changing originals;
6. reopen the saved Set from another authorized desktop on the LAN;
7. export a matching PNG and PDF;
8. observe correct permission, edit-lock, audit, archive, and backup behavior.
