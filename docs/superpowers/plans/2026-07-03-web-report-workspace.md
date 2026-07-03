# Web Report Workspace Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Keep completed backtest history from compressing the main report workspace.

**Architecture:** Change only the Web console presentation layer in `jq_tushare_sdk/web/app.py`. The backend still returns all jobs, while the browser filters inline display to queued/running/failed jobs and keeps completed jobs available through history records.

**Tech Stack:** Python stdlib HTTP server, generated HTML/CSS/JavaScript, local browser verification.

## Global Constraints

- Preserve existing Web API behavior.
- Do not add mock data or new tests.
- Preserve the existing visual language: restrained dashboard controls, 8px-or-less radii, compact operational layout.
- Update version metadata and user-facing docs for this visible behavior change.

---

### Task 1: Inline Job List Filtering

**Files:**
- Modify: `jq_tushare_sdk/web/app.py`

**Interfaces:**
- Consumes: existing `state.jobs` array returned by `/api/jobs`.
- Produces: filtered inline rendering for only `queued`, `running`, and `failed` jobs.

- [x] Update `renderJobs()` to compute `visibleJobs = state.jobs.filter(...)`.
- [x] Render nothing when `visibleJobs.length === 0`.
- [x] Keep failed jobs visible even when they have no report path.
- [x] Keep the existing report auto-refresh path unchanged.

### Task 2: Short-Lived Success Notices

**Files:**
- Modify: `jq_tushare_sdk/web/app.py`

**Interfaces:**
- Consumes: existing `setNotice(message, tone)` calls.
- Produces: success/info notices that auto-hide; error notices that remain visible.

- [x] Add a notice timer to UI state.
- [x] Clear the timer when a new notice appears.
- [x] Auto-hide non-error notices after a short delay.
- [x] Do not auto-hide `tone === 'error'` notices.

### Task 3: Layout Polish

**Files:**
- Modify: `jq_tushare_sdk/web/app.py`

**Interfaces:**
- Consumes: existing `.job-list.compact`, `.job-item`, and `#report-frame` CSS.
- Produces: less vertical pressure above the report and stable report height.

- [x] Remove task-list margin when there are no visible jobs.
- [x] Add an active display state only while active/failed jobs are rendered.
- [x] Keep the report iframe height calculation compatible with the compact header.

### Task 4: Version And Documentation

**Files:**
- Modify: `VERSION`
- Modify: `jq_tushare_sdk/__init__.py`
- Modify: `pyproject.toml`
- Modify: `README.md`
- Modify: `CHANGELOG.md`

**Interfaces:**
- Consumes: existing version bump script.
- Produces: consistent SemVer metadata and user-facing changelog entry.

- [x] Run the version bump script with a patch increment.
- [x] Confirm README current version and changelog entry were updated.

### Task 5: Verification

**Files:**
- Read: generated Web console page in the in-app browser.

**Interfaces:**
- Consumes: local running server at `http://127.0.0.1:8790/`.
- Produces: evidence that syntax and browser behavior match the design.

- [x] Run Python syntax check.
- [x] Run JavaScript syntax check for `_app_js()`.
- [x] Reload the Web console.
- [x] Verify completed jobs are not rendered in the inline task list.
- [x] Verify the selected report remains visible.
