# Session Recovery for Codex

Current release: **1.0**.

**Pick up where you left off, and let Codex sessions working on the same project share what they learned.**

Session Recovery saves short handoff notes for your local Codex sessions. After a disconnect, run `codex-recover`, choose a session by number, and Codex resumes that session with instructions to read its saved handoff. There is no session ID to copy.

It also provides `codex-memory`: a small, local knowledge store for project decisions, findings, and lessons. Sessions working on one project can consult each other's notes during normal work, while separate projects keep separate memories.

## Why use it?

A conversation can contain a lot of history, but returning to work still means finding the important parts: what was decided, what changed, which tests passed, and what to do next. This tool keeps those details in a concise checkpoint.

It is especially useful when a router restarts overnight, an SSH connection drops, or several Codex sessions contribute to the same project.

| Situation | How it helps |
| --- | --- |
| Your connection drops during a long task. | Choose the saved session from a numbered menu and resume with its handoff. |
| Your router resets at a predictable time. | Schedule checkpoint requests and collection before the reset. |
| Two Codex sessions work on the same project. | One session can find decisions and test findings recorded by the other. |
| You return to a project after a break. | Read recent notes or search for the problem you are working on. |
| You maintain several unrelated projects. | Searches stay within the current project; notes from other projects are excluded. |

### What you gain

- **Less repeated explanation:** handoffs record the goal, decisions, progress, evidence, and next steps.
- **Easier recovery:** choose a number instead of looking up a session ID.
- **Shared project knowledge:** retain useful discoveries beyond a single conversation.
- **A lightweight setup:** Python and SQLite, with no Ollama, embedding model, or database server to manage.
- **Local storage:** saved notes and the search database stay in your account, outside the public source tree.
- **Your existing Codex setup:** recovery uses your login without choosing a different model or changing approval or sandbox settings.

Memory collection and search make no model calls. Asking Codex to write a checkpoint or resume work uses your normal Codex model access and tokens. Notes included in a Codex prompt are processed through that existing setup.

## System requirements

| Requirement | Details |
| --- | --- |
| Operating system | Linux with a working **systemd user manager** for the supplied installer and schedules. |
| Python | **Python 3.11 or newer**, with `venv`, pip, and SQLite FTS5 support. FTS5 is SQLite's text-search feature; the installer checks for it. |
| Codex | Codex CLI on your `PATH`, signed in, with an existing local app-server daemon and an accessible Unix socket. Having the CLI installed alone is not enough for scheduled session discovery. |
| Python dependency | `websockets>=15.0.1,<16`, installed using the steps below. |
| Account | Install and run under the same normal Linux account that runs Codex. No sudo is used by the installer. |
| Git | Used to recognize repository boundaries and linked worktrees. Non-Git folders are also supported. |

The transport was tested with Codex **0.157.1** and websockets **15.0.1**. Codex's app-server protocol is experimental, so compatibility may need checking after a Codex upgrade. The bundled installation is intended for Linux; Windows and macOS are not documented installation targets.

## Quick start

### 1. Install

Clone the project into a permanent location, then install it:

```sh
git clone https://github.com/elkentaro/codex-recovery.git
cd codex-recovery
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python install.py --enable
```

This installs the `codex-recover` and `codex-memory` commands, adds checkpoint and memory guidance to your global Codex instructions, and enables three timers: nightly checkpoint requests, nightly collection, and memory collection every minute.

**The default nightly schedule is 02:40 and 02:55 in Asia/Tokyo**, intended for a 03:00 router reset. If that does not suit you, use your own timezone and times in the installation command instead:

```sh
.venv/bin/python install.py --timezone Europe/London --request-time 01:40:00 --verify-time 01:55:00 --enable
```

Leave enough time between the request and collection for Codex to write its notes. Schedule settings are retained when you rerun the installer.

If the installed commands are not found, add this line to your Bash or Zsh startup file and open a new terminal:

```sh
export PATH="$HOME/.local/bin:$PATH"
```

The installer prints this advice when needed; it does not edit your shell configuration. These are executable commands, so no aliases are required.

Keep both the checkout and its `.venv` directory: the installed commands use their absolute paths. If you move the checkout, rerun the installer from its new location using the new environment.

### 2. Check the schedules

```sh
systemctl --user list-timers 'codex-checkpoints*' 'codex-project-memory*'
```

Your systemd user manager must stay running for the schedules to run. If you need them to work while logged out, check whether your account has lingering enabled:

```sh
loginctl show-user "$USER" -p Linger
```

The installer does not enable lingering or change host administration settings.

### 3. Recover when you need to

```sh
codex-recover
```

Choose a number from the list of saved sessions. The menu shows each project's directory and checkpoint time, newest first. Codex opens the corresponding original session and is instructed to read the saved checkpoint before continuing.

Press Enter or type `q` to cancel. To inspect the list without launching anything:

```sh
codex-recover --list
```

A new installation may have no saved checkpoints yet. Installation itself sends no model prompts; see [Manual operation](#manual-operation) if you want to request and collect a checkpoint before the next scheduled run.

## Share knowledge during everyday work

The installed instructions ask Codex to consult project memory before significant work, record useful findings, and save handoffs at meaningful milestones. These instructions guide the agent; they do not guarantee that every action is recorded.

You can also use memory yourself. Run these commands **from the project you want to work on**:

```sh
codex-memory search "build failure"
codex-memory recent
codex-memory remember --kind decision "Keep the current build tool until the plugin supports its replacement."
codex-memory project
```

| Command | Purpose |
| --- | --- |
| `search "words"` | Find notes using keyword search. |
| `recent` | Show the project's latest notes. |
| `show RECORD_ID` | Read a complete note using an ID returned by search. |
| `remember --kind finding "text"` | Save a finding; `decision` and `lesson` are also supported. |
| `project` | Check which project boundary is being used. |
| `capture` | Queue the current Codex session's saved checkpoint; intended for use inside a Codex session. |

Search results include the source session and timestamp. Collection normally happens within about a minute, so a new note may not appear immediately. Search matches words, not meanings; try concrete terms from your issue. It does not generate a synthesized answer or resolve conflicting notes.

For example, one session can record why a dependency was pinned. A second session investigating an upgrade can find that explanation and check whether it still applies, rather than repeat the investigation.

### What counts as the same project?

- A Git repository, its subdirectories, and its linked worktrees share memory.
- Nested repositories and separate clones have separate memories.
- A non-Git folder defaults to its exact directory path. You can configure a shared root for its subfolders; see the advanced reference below.

There is no cross-project search command. Notes are dated evidence, not permission to take actions or continue another session's unfinished task. Check important claims against current files and tests.

## How recovery works

1. **Request:** before the scheduled disconnect, the tool asks eligible sessions on your existing local Codex daemon to write a short handoff.
2. **Collect:** it checks the saved files and copies valid handoffs into your private recovery archive.
3. **Resume:** you choose a saved session, and Codex resumes it with instructions to read its handoff and verify the current state before continuing.

The shared-memory collector runs separately every minute. It indexes saved handoffs and explicitly contributed notes without reading raw conversation archives or invoking a model. Recovery can include up to three relevant excerpts from other sessions in the same project. A missing memory database does not prevent recovery from the selected checkpoint.

This is a handoff system, **not a backup of your source code or a replacement for Codex's saved session history**. The original session history and project directory must still exist. It does not restore deleted sessions, repair a connection, or keep a process running through a disconnect.

Checkpoints can also miss recent work if Codex is busy, waiting for approval, or unavailable. File validation checks structure, not whether the summary is factually correct. Recovery uses collected archive copies; newer notes still waiting in temporary storage are not selected automatically.

## Where your data lives

These are the default locations; the private configuration can change them.

| Location | Contents |
| --- | --- |
| `~/.config/session-recovery/config.json` | Your private configuration and schedule. |
| `/tmp/codex-checkpoints-UID/` | Temporary handoffs and queued notes; `UID` is your numeric Linux user ID. |
| `~/codex-log/threads/THREAD_ID/MEMORY.md` | Collected handoff for each session. |
| `~/codex-log/project-memory/memory.sqlite3` | Shared project notes and observed checkpoint history. |
| `~/codex-log/runs/` | Daily collection reports. |
| `~/.codex/AGENTS.md` | A marked instruction block, alongside your existing instructions. |

Private files use mode 600 and private directories use mode 700. Do not put credentials in notes or publish your configuration, handoffs, database, or logs. Project filtering separates search results; it is not an access-control boundary between people sharing one Linux account.

A host reboot may clear `/tmp`; collected copies in your home directory remain. The memory database contains retained history, not just a disposable search cache. Back it up using SQLite's backup API if you need to preserve it. Automatic retention cleanup is not currently provided.

## Check your version

```sh
codex-recover --version
codex-memory --version
```

Both commands report `Session Recovery 1.0` for this release. From the checkout,
`python3 install.py --version` reports the same version without installing anything.

## Updating an existing installation

After updating this checkout, refresh its dependencies and rerun the installer:

```sh
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python install.py --enable
```

`--enable` enables all three timers. To add only the shared-memory timer while preserving whether nightly recovery is enabled:

```sh
.venv/bin/python install.py --enable-memory
```

Running the installer without either flag refreshes the installation and preserves existing timer enablement. It retains private configuration and unrelated global instructions, and backs up changed instruction and unit files. It does not interrupt an active checkpoint service or launch a Codex session.

## Troubleshooting

| Problem | What to check |
| --- | --- |
| `codex-recover: command not found` | Ensure `~/.local/bin` is on your `PATH`, then reopen the terminal. |
| The recovery list is empty | A checkpoint must be requested, written, and collected first. Check the nightly service logs below. |
| No sessions are discovered | Check that your existing local Codex daemon is running under this account and that the private config points to its accessible socket. |
| A new memory note is missing | Allow about a minute, check `codex-memory project`, and inspect the memory collector logs. |
| Nightly jobs do not run while logged out | Check the user timers and whether the user manager remains running. |
| Memory capture reports a thread-identity error | The checkpoint must identify the current session using `Thread ID: UUID` (also accepts `Thread: UUID`) or its scheduled marker. If both are present, their IDs must agree with the actual `CODEX_THREAD_ID`. |
| Recovery cannot open a session | Confirm that its original Codex history and project directory still exist. |

```sh
journalctl --user -u codex-checkpoints.service -u codex-checkpoints-verify.service --since today
journalctl --user -u codex-project-memory.service --since today
```

## Manual operation

Run these commands from the checkout using its virtual environment:

```sh
.venv/bin/python checkpoints.py discover
.venv/bin/python checkpoints.py request          # Preview; sends no prompts.
.venv/bin/python checkpoints.py request --send   # Ask sessions to save; uses model tokens.
```

Allow the sessions time to write their handoffs, then collect them:

```sh
.venv/bin/python checkpoints.py verify
codex-recover
```

To collect shared memory manually:

```sh
.venv/bin/python project_memory.py sync
```

Stop the schedules without deleting saved data:

```sh
systemctl --user disable --now codex-checkpoints.timer codex-checkpoints-verify.timer codex-project-memory.timer
```

Stopping timers leaves the global Codex instructions in place. To stop those instructions for future sessions too, remove only the marked Session Recovery block from `~/.codex/AGENTS.md`, preserving other instructions.

## Advanced reference

<details>
<summary>Configuration, command options, and scheduling behavior</summary>

### Configuration and installation

- [config.example.json](config.example.json) contains portable configuration examples.
- Installer-managed units are regenerated. Change schedules through installer options or private configuration, rather than editing generated units.
- `.venv/bin/python install.py --import-config /path/to/old/config.json` imports an older private configuration. It refuses to overwrite an existing destination configuration.
- When invoking Python scripts directly, put a custom `--config PATH` before the subcommand.
- The installer checks dependencies and unit definitions before installing them. It does not install system packages, rewrite shell files, or overwrite unrelated commands.
- The bundled installer requires systemd. For a custom scheduler, `project_memory.py sync --no-rpc` imports queued notes and known handoffs without daemon discovery.

### Memory options

- `search` and `recent` accept `--limit` from 1 to 20; the default is 5.
- Memory query/contribution commands accept `--cwd /path/to/project` when called from elsewhere.
- Omit the text argument to `remember` to read from standard input, avoiding shell expansion in note text.
- `capture` uses the actual `CODEX_THREAD_ID` environment value. Notes submitted in an ordinary shell without a thread are attributed to `manual`.
- Each session owns its checkpoint. `capture` queues that exact version so a later overwrite does not erase the milestone before collection.
- Distinct observed versions are retained; identical records from the same session are deduplicated. A version overwritten before collection without being queued cannot be recovered.
- Queries open the database read-only. Contributions use private temporary staging, allowing agents to contribute without home-directory write access or network access.

To group non-Git subfolders, add a `memory` entry to your private configuration:

```json
"memory": {"project_roots": ["/path/to/non-git-project"]}
```

The longest matching configured root wins for non-Git directories. Git repository boundaries always take precedence.

### Recovery and scheduling details

- `codex-recover --thread SESSION_UUID` prints a quoted resume command; it does not launch it.
- `.venv/bin/python checkpoints.py recover` opens the same menu without the installed command. Piped or redirected recovery calls list only.
- Recovery launches `codex resume --cd PROJECT SESSION_UUID PROMPT`. It does not stop or interrupt an already running session.
- Eligible sessions are loaded, top-level sessions on this account's existing local daemon. Subagents, ephemeral sessions, history-only sessions, other accounts, and other daemons are excluded.
- Active turns receive a checkpoint request through their existing turn. Idle sessions receive a checkpoint-only turn. Sessions waiting for approval or input are left alone.
- A delivered request does not prove a file was saved. Uncertain delivery is not automatically retried that day; verified, unchanged idle sessions can be skipped on later nights.
- Manual requests share the nightly deduplication records. After inspecting a failed request, `request --send --retry-thread SESSION_UUID` explicitly replaces only that request while preserving its previous record. Do not delete reports to force retries.
- Verification can be rerun; exit code 2 means missing, stale, or incomplete handoffs, or absent reports. Mechanical fallback notes cannot replace a session's explanation of its task.
- Schedules have no persistent catch-up: a midday login will not trigger missed overnight prompts. The tool does not stop work at reset time.

</details>

## Development

The release number is defined once in [version.py](version.py). For future releases,
update `__version__` there and keep the release number and examples in this README in sync.

Run the test suite from the checkout:

```sh
.venv/bin/python -m unittest discover -s . -p 'test_*.py' -v
```

Tests use temporary directories and fake session calls. They do not launch real Codex turns or guarantee model response quality.

Keep private configuration, handoffs, logs, databases, and real session IDs out of Git. Before publishing a copy of this project, review the files and choose a license; this source tree does not implicitly grant one.
