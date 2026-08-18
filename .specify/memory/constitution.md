<!--
Sync Impact Report
==================
Version change: 1.0.0 (reconstructed baseline) → 1.1.0
Note: this repository's constitution file was an unfilled template. The v1.0.0
baseline principles (video-downloader era) were reconstructed from the amendment
input and are ratified here together with the v1.1.0 amendment.

Modified principles:
- "Single Output Format (MP4 Only)" → "One Feature, One Output Format"
  (generalized: video downloader → MP4 only; audio extractor → MP3 only;
  no silent format/quality substitution)

Added sections:
- Principle VII: Legal & Ethical Use (new)
- Technology Constraints: FFmpeg (libmp3lame) declared the sole
  media-processing boundary for transcoding; yt-dlp reaffirmed as an approved
  core dependency now shared by two features

Removed sections: none

Templates status:
- ✅ .specify/templates/plan-template.md — Constitution Check gate is generic;
  derives gates from this file at plan time; no edit required
- ✅ .specify/templates/spec-template.md — no constitution-coupled content
- ✅ .specify/templates/tasks-template.md — no constitution-coupled content
- ✅ .specify/templates/commands/*.md — no outdated references found

Deferred items:
- TODO(RATIFICATION_DATE): original adoption date of the v1.0.0 baseline is
  unknown (prior filled constitution not present in this repository)
- ✅ Resolved 2026-07-21: the audio-extractor spec was created as
  specs/002-extract-audio/spec.md (§10 "Legal & Ethical Use"); the rationale
  reference below was updated from the originally cited
  specs/002-record-voice/spec.md path, which never existed in this repository
-->

# YT Voice Recorder Constitution

Governs two sibling features: the YouTube video downloader and the
YouTube-to-MP3 audio extractor.

## Core Principles

### I. Simplicity First

The system MUST run as a single process with no database, no message broker,
no cache layer, and no authentication subsystem. Any addition of one of these
components is a constitutional amendment, not an implementation detail.
Rationale: the product is a short-lived media-fetch utility; operational
surface area is the primary cost driver.

### II. Fail Loudly

Every failure MUST surface through the typed error taxonomy; exceptions MUST
NOT be swallowed, downgraded to logs, or converted to silent fallbacks. Each
error type maps to a documented HTTP status code. Rationale: silent partial
success in media pipelines produces corrupt or misleading output that users
cannot detect.

### III. Contract-First Async Job API

All public endpoints MUST be defined with Pydantic v2 models and published in
the OpenAPI document before implementation. Long-running work (download,
extraction, transcoding) MUST be exposed as asynchronous jobs: submit →
poll/notify → fetch result. Synchronous blocking endpoints for media work are
prohibited.

### IV. One Feature, One Output Format

Each feature declares exactly one output format: the video downloader produces
MP4 only; the audio extractor produces MP3 only. No feature may silently
substitute formats or qualities. If the declared format or requested quality
cannot be produced, the job MUST fail with a typed error (Principle II) rather
than deliver an alternative.

### V. Privacy by Default

The system keeps no user history. Downloaded and transcoded artifacts MUST be
purged on delivery or on TTL expiry, whichever comes first. No URLs, titles,
or media metadata are persisted beyond the lifetime of the job that needs
them.

### VI. Bounded Capacity with Explicit Refusal

Capacity limits (concurrent jobs, queue depth, artifact storage) MUST be
enforced and exceeded requests refused explicitly with 429 (per-origin limit)
or 503 (global capacity), never queued unboundedly or degraded silently.
Admission MUST apply per-origin fairness so one client cannot starve others.

### VII. Legal & Ethical Use

The UI MUST display a notice that users may only download video or extract
audio from content they own or are authorized to use. The project MUST NOT
circumvent DRM or access controls; content that requires such circumvention
is refused with a typed error. Rationale: the tool's legitimacy depends on
operating only on lawfully accessible content.

## Technology Constraints

- **yt-dlp** is an approved core dependency, shared by both the video
  downloader and the audio extractor. No other retrieval library may be
  introduced without amendment.
- **FFmpeg (with libmp3lame)** is the sole media-processing boundary for
  transcoding. All format conversion flows through it; no alternative or
  in-process transcoders.
- **Configuration** comes exclusively from environment variables. No config
  files, no hardcoded secrets or tokens (`.env` for local development).
- **Testing** MUST be offline and deterministic: the media boundary (yt-dlp
  and FFmpeg invocations) is mocked in all automated tests. Tests that reach
  the network do not merge.

## Development Workflow

- Features follow Spec-Driven Development: spec → plan → tasks → implement,
  with the plan's Constitution Check gating Phase 0.
- Changes are the smallest viable diff; unrelated refactors are separate work.
- Every plan MUST re-verify Principles I–VII; violations require an entry in
  the plan's Complexity Tracking table with justification or the plan is
  rejected.

## Governance

This constitution supersedes all other practices. Amendments require a
documented rationale, a semantic version bump (MAJOR: principle removal or
redefinition; MINOR: new principle or materially expanded guidance; PATCH:
clarification), and propagation across dependent templates before merge.
Compliance is reviewed at every plan's Constitution Check and at PR review.

**Amendment 1.1.0 rationale**: extends the constitution to the sibling
YouTube-to-MP3 audio extractor feature per specs/002-extract-audio/spec.md §10
— generalizes the single-output-format rule (IV), reaffirms yt-dlp as a shared
core dependency, designates FFmpeg (libmp3lame) as the sole transcoding
boundary, and adds the Legal & Ethical Use principle (VII). All other
principles stand unchanged and apply to both features.

**Version**: 1.1.0 | **Ratified**: TODO(RATIFICATION_DATE): original v1.0.0 adoption date unknown — no prior filled constitution in this repository | **Last Amended**: 2026-07-21
