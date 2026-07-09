# brains/ — the Brain socket · **Python**
`mind.py` → POST api.windymind.ai/v1/chat (the ONE real path; SSE stream:true).
`openai_compat.py` = fallback for non-Mind endpoints. Never a provider SDK directly (ADR-058 D1).
