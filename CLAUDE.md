
## CI: self-hosted runner (since 2026-07)
GitHub Actions runs on OUR runner (kit0-windytalk on the Kit 0 VPS), not GitHub's cloud.
Always `runs-on: [self-hosted, linux, x64]` — NEVER `ubuntu-latest` (billing-locked; runner-lint enforces).
Jobs stuck "Queued" = runner down, not billing: ssh Kit 0 → cd /home/github-runner/runners/windytalk && sudo ./svc.sh status
Full runbook: ~/kit-army-config/docs/ci-runner-runbook.md
