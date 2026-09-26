# Session recovery

Host-wide Codex checkpoint and interactive recovery tooling. Read README.md before
changing operation. Keep actual config, checkpoint text, receipts, socket paths,
host logs and private handoffs outside this public source tree. Configuration is
installed under ~/.config/session-recovery; saved handoffs remain under ~/codex-log
and private /tmp staging. Do not commit real user session IDs or host-specific logs.

Preserve existing global instructions and timer settings when changing installation.
Never launch live checkpoint requests or resume sessions as tests without explicit
user authorization. Test RPC/model invocation with mocks. Use no model, sandbox or
approval overrides. Installation is per-user; never invoke sudo or another privilege
mechanism. Run python3 -m unittest discover -s . -p 'test_*.py' -v for changes.

Shared memory uses stdlib SQLite FTS5 and private staging. Keep every query scoped
to one resolved project and retain source session/time provenance. Never add a
model/embedding dependency or cross-project recall without explicit user approval.
Run memory tests as part of the full suite; do not run live Codex model prompts.
