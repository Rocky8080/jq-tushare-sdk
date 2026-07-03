# Web Report Workspace Design

## Goal

Make the Web console report area stay dominant after repeated backtest runs. The main page should show the current command surface, active work, errors, and the selected report; completed historical runs should live in the history page instead of accumulating above the report.

## Confirmed Design

- Keep the top parameter bar compact and aligned with the existing visual style.
- Show only queued, running, and failed jobs in the inline task list.
- Hide completed jobs from the main page task list once their report is available.
- Continue to auto-refresh completed reports and auto-open the selected/latest report.
- Use short-lived success notices for routine success states so they do not permanently consume vertical space.
- Keep error notices visible until replaced by another message.
- Preserve manual controls in the settings panel for data checks and report refresh.
- Keep the history page as the canonical place to search and reopen completed runs.

## Out Of Scope

- No change to the backtest engine, data cache, readiness API, or report generator.
- No new mocked data or unit tests, following the repository-level AGENTS.md guidance.
- No visual redesign of report HTML contents.

## Verification

- Python syntax check for the Web app module.
- JavaScript syntax check for the generated app script.
- Browser verification on `http://127.0.0.1:8790/` that completed jobs no longer stack above the report and the report remains visible.
