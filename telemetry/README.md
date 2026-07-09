# telemetry/ — content-free events · **Python**
`emit.py` → admin.windyword.ai /v1/events (ADR-WA-001), platform=windy-talk. Envelope is
`{"events":[...]}`; required per event: service, event_type, actor_type. Fire-and-forget, async,
≤200ms, swallows all errors, no-op unless configured, NEVER content. Missing telemetry is a bug.
