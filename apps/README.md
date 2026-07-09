# apps/ — the clients · **TypeScript, never Python** (ADR-058 D9)
`desktop/` Electron (canonical client + face — the agent's body). `mobile/` RN/Capacitor (Phase 4;
hands = Windy Hand cloud). `cli/` headless TS. One canonical UI codebase, thin shells, mobile-first.
Mic capture per the contract: AEC ON, AudioWorklet (never MediaRecorder), client-stamped timestamps.
