# Clinical before/after photography: landscape and architecture

Research date: 2026-08-07

## Problem

Clinic staff currently photograph patients at several views, then use CorelDRAW
PowerCLIP to crop each image into prepared frames. At later visits they try to
repeat the same views and place the images side by side. The desired system
should reduce both sources of manual work:

1. make the follow-up photograph comparable to the baseline at capture time;
2. align, crop, compare and export the resulting image set with minimal editing.

## What already exists

This is an established product category, split across three markets:

- aesthetic/plastic-surgery clinical photography;
- dermatology and longitudinal lesion imaging;
- wound documentation and measurement.

The closest products found are:

| Product | Relevant approach | Main gap or trade-off |
|---|---|---|
| [Dermi Atlas](https://dermi.ai/features/before-after-comparisons) | iPhone/iPad reference overlay, automatic alignment, split/grid/opacity comparisons, persistent crop/layout and PDF/image export | Commercial; integration and performance on foot/body views require a trial |
| [evooia](https://evooia.com/) | Guided distance, position and lighting; automatic aligned capture; slider, side-by-side and morph views | Cloud product; public material does not establish Android or EHR support |
| [PixioDoc](https://pixiodoc.com/) | Ghost overlay in the viewfinder, patient timeline, multiple comparison layouts and PDF export | Guidance rather than clearly documented automatic registration |
| [TouchMD Snap](https://www.touchmd.com/snap) | Mobile clinical capture with overlays, gridlines, levels and EMR ecosystem | Aesthetic/consultation oriented; automatic alignment is not documented clearly |
| [Canfield Mirror](https://canfieldscientific.com/imaging-systems/mirror/) / [IntelliStudio](https://canfieldscientific.com/imaging-systems/intellistudio/) | Controlled camera/lighting/pose protocols and baseline `MatchPose` overlay | High-cost Windows/studio hardware architecture |
| [Pixacare](https://www.pixacare.com/en/wound-documentation) | Structured wound timeline, scale marker, measurement, reports and EHR connectors | Less focused on repeat-pose guidance and montage |
| [imitoWound](https://imito.io/en/imitowound) / [eKare inSight](https://ekare.ai/wound-measurement-for-healthcare-providers/) | Mobile wound capture, measurement and hospital integration | Measurement-first rather than before/after composition |
| [ImageAssist](https://www.imageassist.com/) | Live SmartGuides/overlays and red/green positioning feedback | Commercial cloud workflow; claims require a local clinical evaluation |

Feature descriptions above are vendor claims, not independent validation. They
show that the industry usually solves the problem **before the shutter is
pressed**, using capture protocols, baseline overlays and controlled hardware,
rather than relying only on post-processing.

## Open-source landscape

No maintained open-source repository was found that combines standardized
capture, pose guidance, registration, patient/visit management, comparison and
clinical export end to end. Useful projects are building blocks:

- [OpenCV](https://github.com/opencv/opencv) — Apache-2.0; feature matching,
  affine/homography transforms and
  [`findTransformECC`](https://docs.opencv.org/4.x/dc/d6b/group__video__track.html)
  for 2D alignment.
- [MediaPipe](https://github.com/google-ai-edge/mediapipe) — Apache-2.0; body or
  face landmarks for live framing and pose quality checks.
- [elastix](https://github.com/SuperElastix/elastix) / ITKElastix — Apache-2.0;
  rigid, affine and non-rigid registration. It is powerful but too complex and
  potentially misleading for an initial clinical comparison workflow.
- [Fiji/ImageJ](https://fiji.github.io/) — desktop research/prototyping for
  registration, overlay, annotation and batch processing; not a clinic capture
  application.
- [OpenRV Web](https://github.com/lifeart/openrv-web) — MIT; reusable A/B,
  blend/flicker and annotation viewer concepts.
- [JuxtaposeJS](https://github.com/NUKnightLab/juxtapose) — MPL-2.0; a small
  before/after slider for images that are already aligned.

The practical conclusion is to reuse selected vision/viewer components, not to
adopt a large research platform such as Fiji or 3D Slicer as the product.

## Clinical photography constraints

Post-processing cannot compensate safely for inconsistent capture. Clinical
photography guidance recommends holding camera/lens, distance, pose, lighting,
background, orientation and scale constant between visits
([plastic-surgery standardization](https://pubmed.ncbi.nlm.nih.gov/9703100/),
[BMJ guidance](https://www.bmj.com/content/378/bmj-2021-067663),
[DermNet guidance](https://dermnetnz.org/topics/image-acquisition-in-dermatology)).

For feet and wounds, the workflow should define named views and include:

- an overview that establishes anatomical orientation;
- a regional view with surrounding landmarks;
- a perpendicular close-up with a metric/color reference where measurement or
  color matters.

Marker and wound must be approximately coplanar. Curved surfaces can introduce
material 2D measurement error; research using two adaptive markers reduced this
bias in one evaluated method
([study](https://pmc.ncbi.nlm.nih.gov/articles/PMC8875057/)).

Originals must remain immutable. Crop/alignment should produce derivatives with
the transform and tool version recorded. Full-face and equivalent identifying
images are sensitive identifiers; consent, access control and de-identification
need to be explicit
([HHS guidance](https://www.hhs.gov/hipaa/for-professionals/special-topics/de-identification/index.html)).

Non-rigid registration should not be a default. It can warp genuine changes in
swelling, scar, tissue shape or wound boundary. Initial automation should be
limited to quality checks, crop suggestions and rigid/similarity alignment,
with original/processed images visible together and a human approval step.

## Architecture options

Scores are relative, 1–5; higher is better. For cost and complexity, higher
means cheaper/simpler.

| Architecture | Workflow speed | Repeatability | Offline | Privacy control | HIS/EHR | Cost | Simplicity |
|---|---:|---:|---:|---:|---:|---:|---:|
| A. Current local web/desktop + manual crop | 2 | 2 | 5 | 4 | 1 | 5 | 5 |
| **B. Mobile capture companion + local case manager** | **5** | **4** | **4** | **4** | **3** | **4** | **4** |
| C. Centralized full web/PWA clinical system | 4 | 4 | 2–3 | 3 | 5 | 2 | 2 |
| D. Fixed camera/lighting rig | 5 | 5 | 4 | 4 | 2 | 1–2 | 1–3 |
| E. AI-first capture and registration | 4 | 4–5 | 3 | 2–3 | 3 | 2 | 2 |

### Decision

Choose **B: a mobile capture companion plus a local-first case manager**.
Continue using the existing Pillow board renderer as the export boundary. Add a
server only when multi-device or multi-site synchronization is proven necessary.
Treat computer vision as an assistive layer, not the system of record.

The highest-value intervention is a reference/ghost overlay while the employee
is holding the camera. This prevents mismatched images instead of attempting to
repair them later. Canfield, Dermi Atlas, PixioDoc, TouchMD and ImageAssist all
provide commercial precedent for this pattern. A Vanderbilt pilot also showed
that a mobile, EMR-linked standardized clinical-photography workflow is feasible
([paper](https://pmc.ncbi.nlm.nih.gov/articles/PMC10125695/)).

## Minimal target architecture

```text
Mobile capture UI
  - choose patient/case/visit
  - execute named shot protocol
  - baseline ghost overlay + device-level guides
  - blur/exposure/framing checks
  - local encrypted outbox
            |
            v
Local case manager / later sync API
  - patient reference, visits, captures, consent
  - immutable original assets
  - thumbnails and approved derivatives
            |
            +--> Comparison UI
            |      original side-by-side
            |      crop/pan/zoom
            |      blink/opacity overlay
            |      optional rigid alignment + confidence
            |
            +--> Existing board renderer
                   PNG/PDF export
```

Minimum domain objects:

- `PatientRef`: internal identifier; avoid unnecessary PII;
- `Case`: treatment episode;
- `Visit`: date, operator and capture device;
- `ShotTemplate`: body site, laterality, named view, instructions and framing;
- `Capture`: original asset, shot code, QC and approval state;
- `Derivative`: crop/alignment transform, source checksum and tool version;
- `Comparison`: baseline/follow-up pair and export layout;
- `Consent`: permitted uses and timestamp.

Do not begin with microservices. For one clinic, these can be modules in one
application and one database. Keep only the media-processing boundary explicit
so OpenCV can be added without coupling clinical records to image operations.

## Delivery sequence

### Phase 0 — protocol before software

Define and trial named foot views, floor/tripod marks, lighting, distance,
orientation, naming and consent. Measure current capture and CorelDRAW time.

### Phase 1 — replace PowerCLIP work

Extend the current app with patient visits, baseline/follow-up pairing,
interactive pan/zoom/crop, reusable comparison layouts and immutable originals.
This validates the workflow without camera or AI complexity.

### Phase 2 — guided mobile capture

Add a camera client that walks through `ShotTemplate`s and overlays the baseline
image. Start with opacity, grid, level and manual acceptance. Keep export into
the current renderer and, temporarily, CorelDRAW-compatible files if needed.

### Phase 3 — safe automation

Add blur/exposure/framing gates, landmark-based crop suggestions and then
OpenCV rigid/similarity registration. Store confidence; when confidence is low,
require recapture or manual adjustment. Never overwrite the original.

### Phase 4 — synchronization and HIS

Only after multi-device demand exists, add authenticated sync, encrypted object
storage, audit, retention and backup. Integrate through the HIS API or FHIR/SMART
boundary rather than direct database access
([SMART App Launch](https://hl7.org/fhir/smart-app-launch/STU2.2/app-launch.html)).

## Buy versus build

Before implementing Phases 2–4, run a short trial of **Dermi Atlas, PixioDoc and
evooia** using the clinic's real foot views. Measure:

- time per six-view baseline/follow-up set;
- repeatability without expert coaching;
- recapture rate;
- crop/alignment corrections still required;
- Vietnamese workflow fit, offline behavior, export/data portability and cost;
- consent, local data residency and HIS constraints.

If one product meets these requirements and permits data export, buying is
likely cheaper than building a complete clinical platform. Build is justified
if foot-specific protocols, local/offline data control, Vietnamese workflow or
custom board output are differentiators the commercial tools cannot satisfy.
