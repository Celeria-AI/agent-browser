# Camoufox sidecar: local dev loop

Tight feedback loop for iterating on the `--engine camoufox` path without the
full release + E2B template rebuild cycle.

## What this solves

The production path for a Camoufox change looks like:

1. edit sidecar or Rust
2. push, merge to `main`, wait for the release workflow (~10 min)
3. bump `AGENT_BROWSER_VERSION` in the celeria Dockerfile
4. `pnpm run build:dev` — uploads, remote image build (~5–10 min)
5. spawn a sandbox, test

That's 20–30 minutes per iteration for a one-line change. The loop in this
doc replaces steps 2–5 with a direct `agent-browser --engine camoufox …`
invocation on your dev machine — seconds per Python-only iteration, ~15 sec
for an incremental Rust rebuild.

## What's in scope vs. out of scope

**This loop exercises:** Rust CLI → Python sidecar → Playwright → Camoufox
(patched Firefox) against real sites. It's enough to validate any sidecar
behavior change, any Rust action handler, and to answer questions like *"is
Cloudflare blocking us because of fingerprint or because of IP reputation?"*
If a site works from your home IP and fails in the E2B sandbox, the
difference is the IP.

**This loop does NOT exercise:** the E2B HOME/chmod dance, the
`celeria-coding` Dockerfile, the celeria API's `sandboxBrowser` tool wrapper,
the `AGENT_BROWSER_ENGINE=camoufox` env routing, or any Linux-specific
Camoufox fingerprint profile. Stealth outcomes on macOS won't perfectly
predict outcomes on a Linux sandbox, but for *our* code paths (command
routing, response shapes, sidecar lifecycle) the coverage is identical.

## One-time setup

### 1. Dedicated venv for the sidecar stack

Homebrew Python blocks system-wide pip installs under PEP 668, so everything
goes into a named venv. `~/.camoufox-dev/venv` is suggested; nothing else
depends on the exact path.

```bash
python3 -m venv ~/.camoufox-dev/venv
```

### 2. Install the sidecar in editable mode

```bash
~/.camoufox-dev/venv/bin/pip install -e ~/git/agent-browser/packages/camoufox-sidecar
```

Editable (`-e`) means your Python edits take effect on the next
`agent-browser` invocation with no reinstall. `camoufox`, `playwright`,
`geoip2`, and friends come in as transitive deps — no need to `pip install
camoufox[geoip]` separately.

### 3. Fetch browser binaries

```bash
~/.camoufox-dev/venv/bin/python -m camoufox fetch
~/.camoufox-dev/venv/bin/python -m playwright install firefox
```

- `camoufox fetch` — patched Firefox (~150 MB) plus a GeoIP2 database
  (~65 MB) into `~/Library/Caches/camoufox/`
- `playwright install firefox` — Playwright's Firefox driver into
  `~/Library/Caches/ms-playwright/`

Both are idempotent; re-running is a no-op once binaries are current.

### 4. Build the Rust binary

```bash
cargo build --release --manifest-path ~/git/agent-browser/cli/Cargo.toml
```

Cold build: 1–3 minutes. Incrementals: seconds. The output binary lands at
`~/git/agent-browser/cli/target/release/agent-browser`.

### 5. Shell shortcuts (optional but strongly recommended)

Add to your shell rc:

```bash
# Point agent-browser at the venv's Python so it finds camoufox_sidecar.
# Without this, agent-browser falls back to `python3` on PATH, which is
# Homebrew's and doesn't have the sidecar installed.
export AGENT_BROWSER_CAMOUFOX_PYTHON=~/.camoufox-dev/venv/bin/python

# Alias the dev binary so you don't collide with a system `agent-browser`.
alias abdev=~/git/agent-browser/cli/target/release/agent-browser
```

Reload your shell. Sanity check:

```bash
abdev --version
# agent-browser 0.26.0-celeria-camoufox.2  (or whatever your branch is on)

abdev --engine camoufox open https://example.com
# ⚠ Daemon version mismatch detected, restarting...
# ✓ Example Domain
#   https://example.com/
```

If you see `Example Domain` back, the whole chain works: Rust → sidecar →
Playwright → Camoufox → the internet.

## Process lifecycle (why you see a lingering Python process)

Agent-browser's architecture:

```
agent-browser CLI  →  spawns daemon  →  spawns sidecar  →  spawns Camoufox
 (short-lived)        (long-lived)       (long-lived)      (long-lived)
```

The daemon stays alive *across* CLI invocations so you can run `open`, then
`snapshot`, then `click`, without paying Camoufox's 2–5 second cold-start
cost on each command. The Python sidecar is a child of the daemon and lives
exactly as long as the daemon does.

So after one `abdev --engine camoufox open https://...` you should see
roughly: 1 daemon, 1 `python -m camoufox_sidecar`, 1 Playwright driver
(Node), 1 Camoufox process, and one or two Firefox plugin-containers. This
is correct. `ps aux | grep agent-browser` listing them is not a leak.

**They should go away when any of these happens:**
- you run `abdev close`
- the daemon's idle timeout fires (a few minutes of inactivity)
- the daemon's stdin is closed and it detects EOF

If you end up with multiple old daemons lingering from prior sessions (the
idle timeout isn't firing reliably for everyone in all setups), nuke them
with the Troubleshooting recipe below.

## Iteration workflows

### Python-only change (e.g. adding a launch kwarg default)

Edit `packages/camoufox-sidecar/camoufox_sidecar/session.py`. No rebuild.
Next `abdev --engine camoufox …` picks up the new code.

```bash
# edit session.py
abdev --engine camoufox open https://bot.sannysoft.com
abdev screenshot /tmp/x.png && open /tmp/x.png
abdev close
```

Loop time: ~3 sec plus page load.

### Rust change (e.g. new action handler in `actions.rs`)

```bash
cargo build --release --manifest-path ~/git/agent-browser/cli/Cargo.toml
abdev --engine camoufox <new command>
```

Loop time: ~10–30 sec for an incremental build, longer if you touched
something in a heavily-reexported module.

### Session stuck, ports in a weird state, nothing responds

Kill the daemon and any lingering Firefox:

```bash
abdev close              # graceful if daemon responds
pkill -f agent-browser   # nuclear if it doesn't
pkill -f camoufox
pkill -f firefox
rm -rf ~/.agent-browser  # daemon state — regenerated on next launch
```

The `⚠ Daemon version mismatch detected, restarting...` warning you'll see
after a rebuild is normal and benign — it means the in-memory daemon was
built against the old binary and is transparently being swapped out.

## Diagnostic: is a site blocking us on fingerprint or on IP?

This is the big value-add of local testing. The E2B sandbox IP is on
elevated-risk lists with most WAF vendors; your home IP usually isn't.

```bash
abdev --engine camoufox open https://<target-site>
sleep 10                 # give Cloudflare / similar a chance to auto-pass
abdev screenshot /tmp/before.png && open /tmp/before.png
```

Three outcomes, each tells you something different:

| Outcome | Meaning |
|---------|---------|
| Page loads cleanly, no challenge shown | E2B IP reputation is the blocker. Fingerprint is fine. |
| Challenge appears, auto-passes within ~10s | Passive check passes without interaction. Sandbox should work too if IP allows. |
| Challenge appears and sticks with checkbox / CAPTCHA | Fingerprint or behavior is flagged. Same failure mode you'd see in E2B. Fixing this locally is the right place to iterate. |

## Troubleshooting

### `camoufox: not available (reason: python3 does not have camoufox installed)`

`agent-browser doctor` will tell you this. You forgot to set
`AGENT_BROWSER_CAMOUFOX_PYTHON`, or the venv wasn't created. Re-check that
`$AGENT_BROWSER_CAMOUFOX_PYTHON -c "import camoufox_sidecar"` succeeds.

### Sidecar crashes immediately with `Could not find Camoufox binary`

You skipped `python -m camoufox fetch`. Run it in the venv's Python:
`~/.camoufox-dev/venv/bin/python -m camoufox fetch`.

### `Playwright Browser firefox is not found`

You skipped `playwright install firefox`. Same fix pattern:
`~/.camoufox-dev/venv/bin/python -m playwright install firefox`.

### Rust changes don't seem to take effect

Release builds go to `target/release/agent-browser`. Make sure your `abdev`
alias points there and not at a `target/debug/` binary you built months ago.
`which abdev` and `abdev --version` are your friends.

### "Daemon version mismatch" loops forever

Usually means a stale daemon from a previous build can't be killed cleanly.
Nuke it: `pkill -f agent-browser && rm -rf ~/.agent-browser`.

## Moving a validated change back to the production loop

Once a change works locally:

1. Commit + push to the branch
2. Open PR against `main` on the fork
3. Merge — the release workflow cuts
   `v0.26.0-celeria-camoufox.<next>` automatically (reads `package.json`
   version; if you're shipping multiple fixes, bump once in a `chore:` commit
   alongside the CHANGELOG rotation)
4. Bump `AGENT_BROWSER_VERSION` in `infra/e2b/celeria-coding/Dockerfile` on
   the celeria side
5. `pnpm run build:dev` in `infra/e2b/celeria-coding/` to rebuild the E2B
   template

See `docs/plans/2026-04-20-001-feat-agent-browser-camoufox-engine-plan.md`
(in the celeria repo) for the broader context.
