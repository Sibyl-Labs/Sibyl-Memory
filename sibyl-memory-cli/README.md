# sibyl-memory-cli

Command-line interface for the **Sibyl Memory Plugin**.

```bash
pip install sibyl-memory-cli
```

This pulls in `sibyl-memory-client` (the local SDK) and `sibyl-memory-hermes` (the Hermes provider) automatically.

## Commands

```
sibyl init                  Open the browser activation page. Writes ~/.sibyl-memory/credentials.json.
sibyl upgrade               Open the upgrade page. Stake $SIBYL or subscribe in USDC.
sibyl status                Show local credentials, DB size, and the server's view of your tier.
sibyl health                Run the SibylMemoryProvider self-check (schema version, DB path, tenant).
```

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
- **Stake** — connect your wallet (browser or Coinbase Smart Wallet), sign to bind, and the page checks your `$SIBYL` balance on Base. If you hold the threshold (default 100,000 $SIBYL liquid+staked, configurable), the local cap lifts.
- **Subscribe** — pick monthly ($29) / quarterly ($79) / annual ($290) USDC, sign the transfer, the server records the subscription. Tier flips immediately.

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
SIBYL_API_BASE=https://staging.api.sibyllabs.org sibyl init
SIBYL_ACTIVATE_BASE=https://staging.sibyllabs.org/plugin/activate sibyl init
SIBYL_UPGRADE_BASE=https://staging.sibyllabs.org/plugin/upgrade sibyl upgrade
```

## Security

- `credentials.json` is written atomically at mode 0600.
- `session_token` is never printed in full — only a short slice.
- No memory content ever transits these endpoints. The CLI never reads `memory.db` content; it only checks file size.
- Wallet operations happen in the browser. The CLI sees only the resulting tier change.

## License

MIT.
