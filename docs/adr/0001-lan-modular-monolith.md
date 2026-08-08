---
status: accepted
---

# Run the MVP as a LAN modular monolith

Deploy one desktop-oriented web application inside the clinic LAN, with PostgreSQL owning metadata and a managed filesystem holding immutable original images and derivatives. This was chosen over browser-local storage, cloud-first deployment, and microservices because the clinic needs shared records, named-user permissions, auditability, local control of clinical images, and simple operations; database and media must be backed up together every day. The trade-off is that the MVP depends on the clinic server and LAN and does not provide offline or multi-site access. Keep image rendering as an in-process module until measured load requires a separate worker.
