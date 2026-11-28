# Product

<!-- impeccable:product-schema 1 -->

## Platform

web

## Users

- **Primary for now: the project demo.** The UI is the face of a student capstone project (專題). It must hold up when shown to teachers, reviewers, or judges, both on a laptop and through phone demo mode (`手機展示模式.cmd`, phone on the same Wi-Fi).
- **Also a real tool: citizen reporters.** Dashcam owners in Taiwan who want to turn footage of dangerous or illegal driving (馬路三寶) into a police report without filling in each city's web form by hand. The flow must actually work for them, not just look like it does.
- The user is one person reviewing their own footage. Every account sees only its own drafts, clips, cases, and reporter profile.

## Product Purpose

Shorten the path from dashcam video to a filed traffic-violation report:

**Upload video + GPS → AI analysis (live preview + progress) → review violations → confirm report form → submit (autoForm auto-fill, or simulated)**

Success means a user can go from raw footage to a correctly pre-filled report for the right city in a few minutes, and they trust what they are submitting because they checked it themselves.

## Positioning

A complete end-to-end pipeline, not just a detector or just a form filler. Real perception models (vehicles, plates, traffic lights, road markings) and violation rules produce candidate events. The UI then carries each candidate through human review into the actual municipal reporting form, with GPS-based city routing and evidence clips attached.

## Operating Context

- Runs locally: `python server.py` (standard library only, plus opencv) serves http://127.0.0.1:8765. `--lan` opens it to phones on the same network.
- Two analysis modes, switched on the upload page: **real model** (needs the inference service `../inference_service`, GPU, and downloaded weights) and **simulated demo** (reads real video and draws boxes, but detections are fake; no GPU needed). The UI must always make clear which mode is active.
- Video sources: per-account clip library, direct upload, cloud links (Drive/Dropbox/OneDrive), long-video auto-splitting, and live RTSP/HTTP streams (real model only; optional low-fps triage).
- GPS from a common location, coordinates, a map click, or CSV/GPX/NMEA files. Simulated tracks exist and are always labelled 模擬軌跡.
- Every step autosaves to a draft. Refreshing or restarting the server returns the user to where they stopped.
- autoForm submission defaults to **test mode (fill but don't send)**. A real send requires an extra explicit confirmation. A fully simulated send is also available.
- Desktop first. Phone must work well enough for the demo but is secondary.

## Capabilities and Constraints

- **AI results are candidates, never verdicts.** The backend's `confirmed` means "met the configured conditions"; `suspected` is weaker. The UI must present both as candidates for the user to judge, show the condition checklist (判定條件 x/y) and evidence clip, and let the user ignore any event. No report is built or sent without the user's explicit action.
- Five violation rules: red-light stop-line crossing, double-yellow and double-white line crossing, car in the motorcycle waiting area, stopping on a crosswalk. The mapping lives in `core/analyzers.py` (`VIOLATION_TYPES`) and `core/remote.py` (`RULE_TYPES`).
- Plate readings may be missing, late (backfilled), or in conflict with the event-time plate. The UI must show that state honestly and prompt the user to fill in or check the plate.
- Login is local (PBKDF2-hashed). "Continue with Google" and "forgot password" are **simulated** and must not pretend otherwise.
- Front end is plain `web/index.html`, `app.js`, `style.css`, with no build step. External assets: Google Fonts (Noto Sans TC) and Leaflet from unpkg, with OSM tiles.
- All copy is Traditional Chinese (`zh-Hant`).

## Brand Commitments

- Name: **馬路三寶檢舉輔助系統**. The name is colloquial and a little cheeky, but the job is civic and legal.
- Voice: plain, direct Traditional Chinese. Say clearly what is simulated, what is a candidate, and what will actually be sent.

## Evidence on Hand

- Real pipeline outputs: violation events with conditions, evidence clips, and plate readings from `run_pipeline.py` via the inference service.
- Sample GPS track: `samples/sample_track.csv`. Per-account clip library under `clips/`.
- Evaluation numbers live in `../docs/refactor_log.md` (small labelled set; each rule has only 5–14 labelled events). Do not turn them into accuracy claims in the UI.
- No testimonials, users, partnerships with police agencies, or official endorsements exist. Never imply any.

## Product Principles

1. **The human decides.** AI proposes candidates; the user reviews, edits, and explicitly confirms before anything becomes a report or leaves the machine.
2. **Never blur real and simulated.** Simulated analysis, simulated GPS, simulated login, and simulated submission are always labelled where they appear.
3. **Show the evidence, not a score.** Conditions, clips, timestamps, and location justify each candidate better than a confidence number.
4. **Never lose work.** Every step autosaves and resumes.
5. **Demo-ready, real-use-honest.** It has to impress in a presentation without claiming capabilities the real flow doesn't have.

## Accessibility & Inclusion

No formal standard has been set yet (open decision). Baseline: readable Traditional Chinese at desktop sizes, and status shown by text as well as colour (confirmed/suspected, real/simulated).
