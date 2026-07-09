# agents/ — the Agent socket · **Python**
`connect.py` pairs via windy-connect (CLI is `windy`, not `windy-connect`).
`windyfly.py` adapts the Windy Fly JSON-RPC bridge (`agent.respond`, `agent.respond_stream` once landed).
Strip the status-banner prefix from bridge replies before TTS (see docs/PROBE_RESULTS.md).
