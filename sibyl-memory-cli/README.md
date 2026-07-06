# sibyl-memory-cli

Command-line interface for the **Sibyl Memory Plugin**.

```bash
pip install sibyl-memory-cli
```

This pulls in `sibyl-memory-client` (the local SDK) and `sibyl-memory-hermes` (the Hermes provider) automatically.

## Commands

```
sibyl init                    Open the browser activation page. Writes ~/.sibyl-memory/credentials.json.
sibyl migrate                 Guided onboarding: back up your existing memory/agent files, wire Sibyl
                              into every detected harness, populate Sibyl Memory from the backup, and
                              optionally slim the originals. Backup-first, never destructive.
sibyl setup [target]          Wire Sibyl as the memory provider for Hermes, Claude Code, and/or Codex.
                              target is one of: hermes | claude-code | codex (default: detect all).
sibyl status                  Show local credentials, DB size, and the server's view of your tier.
sibyl whoami                  One-line account summary (masked by default).
sibyl devices                 List the devices (active tokens) bound to your account.
sibyl devices revoke N        Revoke a device by index (run `sibyl devices` to see the indexes).
sibyl dashboard               Open the account dashboard (delegates to `sibyl status` for now).
sibyl upgrade                 Open the tier/billing flow: stake $SIBYL or subscribe in USDC.
sibyl update                  Check PyPI for newer sibyl-memory-* package releases.
sibyl update --apply          Update the installed Sibyl packages in place (pip install -U). This is the
                              canonical way to update the plugin, distinct from `sibyl upgrade` (which is
                              the tier/billing flow, not a package update).
sibyl health                  Run the SibylMemoryProvider self-check (schema version, DB path, tenant).
sibyl logout                  Remove local credentials (your memory.db is left untouched).
sibyl memory list [category]  List stored entities, optionally filtered by category (--limit N).
sibyl memory search <query>   Full-text search across entities, state, reference, and journal (--limit N).
sibyl memory recall <cat> <name>
                              Recall a single entity by category + name.
```

## Migrate (guided onboarding)

```bash
$ sibyl migrate
```

`sibyl migrate` moves your accumulated agent memory into Sibyl without risking
your existing files:

1. **Back up first.** Every memory/agent file it finds (`CLAUDE.md`,
   `AGENTS.md`, `.codex/config.toml`, `.hermes/*`, and similar) is copied to a
   timestamped backup folder and byte-verified before anything else happens.
2. **Wire Sibyl** into every detected harness — Claude Code (via
   `claude mcp add --scope user`), Codex (via `~/.codex/config.toml`), Hermes.
3. **Extract** — it prints a prompt you run in your own agent. The agent reads
   only from the backup and writes structured memory through the `sibyl-memory`
   tool. The extraction runs locally on your machine; Sibyl Labs never sees your
   files or memory.
4. **Verify** the new entries that landed in your local DB.
5. **Optionally trim** the originals — only if you confirm, and only because a
   verified backup exists. Your full pre-migration files are always preserved.

Flags: `--backup-dir PATH` (default: home), `--no-debloat` (skip the trim
step), `--yes` (skip the initial confirm; the trim step still asks separately).

> No warranty. Keep your backup until you've confirmed everything migrated.
> Sibyl Labs is not responsible for data loss.

## Activation

```bash
$ sibyl init

  Sibyl Memory Plugin · activation

  Session:     a1b2c3d4…e5f6
  Opening:     https://sibyllabs.org/plugin/activate?session=a1b2c3d4-…

  Sign in with your wallet in the browser. This terminal will pick up automatically.

  ⠹ waiting for browser activation … 9:42 left
```

The browser opens. Sign a SIWE message with your wallet. The terminal picks up the moment the binding lands. Credentials are written to `~/.sibyl-memory/credentials.json` at mode 0600.

## Upgrade

```bash
$ sibyl upgrade

  Sibyl Memory Plugin · upgrade

  Account            a1b2c3d4…e5f6
  Current tier       FREE
  Opening            https://sibyllabs.org/plugin/upgrade?session=…

  Two paths in your browser:
    1. Stake $SIBYL on Base (free unlimited if you qualify)
    2. Subscribe in USDC (monthly / quarterly / annual)
```

In the browser:
- **Stake**: connect your wallet (browser or Coinbase Smart Wallet), sign to bind, and the page checks your `$SIBYL` balance on Base. If you hold the threshold (default 100,000 $SIBYL liquid+staked, configurable), the local cap lifts.
- **Subscribe**: pick monthly ($29) / quarterly ($79) / annual ($290) USDC, sign the transfer, the server records the subscription. Tier flips immediately.

On either path, the CLI sees the tier change, rewrites `credentials.json`, and clears `tier_cache.json` so your next write picks up the new entitlement without delay.

## Status

```bash
$ sibyl status

  Sibyl Memory Plugin · status

  LOCAL
    Credentials       ~/.sibyl-memory/credentials.json
    Account           a1b2c3d4…e5f6
    Tier              FREE
    DB size           1,247,300 bytes (1.19 MB)
    Tier cache        free (checked 2026-05-16T18:12:03)

  SERVER
    Tier              FREE
    Source            free
    Cap bytes         2,097,152
    $SIBYL held       0
    Threshold         100,000
    Qualified         no
```

If `LOCAL` and `SERVER` tiers diverge, run `sibyl upgrade`.

## Environment overrides

For internal testing only:

```bash
SIBYL_API_BASE=https://staging.example.internal sibyl init
SIBYL_ACTIVATE_BASE=https://staging.example.internal/plugin/activate sibyl init
SIBYL_UPGRADE_BASE=https://staging.example.internal/plugin/upgrade sibyl upgrade
```

## Security

- `credentials.json` is written atomically at mode 0600.
- `session_token` is never printed in full: only a short slice.
- No memory content ever transits these endpoints. The CLI never reads `memory.db` content; it only checks file size.
- Wallet operations happen in the browser. The CLI sees only the resulting tier change.

## License

MIT.
