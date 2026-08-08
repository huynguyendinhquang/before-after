# Clinical Image Comparison

This context organizes longitudinal clinical photographs into consistent visual comparisons across patient visits.

## Language

**Comparison Set**:
A named comparison composition for one Patient, with its own Capture selection, order, Canvas, and grid layout; it may mix anatomical views. Multiple Comparison Sets may reference the same Captures.
_Avoid_: Capture Library, temporary collage

**Patient**:
The person whose Captures are compared, identified in this app by a unique patient ID, name, and birth year.
_Avoid_: Full medical record, anonymous case

**Consent Confirmation**:
An audited confirmation that consent was obtained before clinical images are stored; the legal form remains outside this app.
_Avoid_: Full consent record, unsigned assumption

**Capture**:
One clinical image in a Patient's Capture Library, together with its authoritative Capture Date.
_Avoid_: Upload, file, photo slot

**Capture Library**:
The Patient's complete collection of Captures. It stores each Capture once so multiple Comparison Sets can reference it.
_Avoid_: Comparison Set, duplicated set assets

**Capture Date**:
The date associated with a Capture. EXIF `DateTimeOriginal` may suggest it, but a user override is authoritative.
_Avoid_: Upload date, file modification date

**Shot Type**:
A canonical clinic-managed category for the anatomical position and camera view of a Capture.
_Avoid_: Filename convention, free-form caption

**Shot Type Proposal**:
An Editor-created label used when no recommended Shot Type fits, pending an Admin's promotion or merge into a canonical Shot Type.
_Avoid_: Canonical Shot Type, typo treated as permanent vocabulary

**Frame**:
A visible rectangle that presents one Capture with crop, pan, and zoom specific to that placement.
_Avoid_: Slot, PowerCLIP container

**Canvas**:
The preset or custom-sized output surface on which a Comparison Set's grid is arranged.
_Avoid_: Editor viewport, page imposed by the application

**Grid Preset**:
A fixed arrangement defined by a shared Frame aspect ratio and a column count.
_Avoid_: Free canvas, arbitrary layout
