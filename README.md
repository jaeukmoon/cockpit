# WorldBestQuant

Public case study and encrypted read-only dashboard for a personal quant investing system.

- `index.html`: public aggregate performance and system overview
- `dashboard.html`: passphrase-gated dashboard
- `data/*.enc.json`: PBKDF2-SHA256 (600k iterations) + AES-256-GCM ciphertext

The public page contains no capital amounts, account data, current holdings, or order controls. Private dashboard content is decrypted only in the browser.

## Consolidated personal assets

After unlocking `dashboard.html`, **My Assets** opens first; **Strategies & Research** preserves the existing research dashboard and cloud closing-price overlay.

- `data/assets.enc.json` is a separate encrypted registered-holdings snapshot. It contains account summaries, merged positions, allocation, unrealized cost-basis contribution, FX sensitivity, retrieval health, and observed daily valuations.
- Amounts are hidden by default. Decrypted data and keys stay in memory and are cleared on relock. The assets iframe is script-only sandboxed, with no order API.
- This is not broker balance synchronization. Paper accounts, unknown cash, and unrecorded trades are excluded. Historical period returns, dividends, and realized FX P&L remain unavailable until transaction history is integrated.
- The WBQ home Windows 08:00 portfolio task generates and publishes this encrypted asset snapshot. The page checks for new snapshots every 60 seconds; this does not create live broker prices. When the collector PC or provider is unavailable, timestamps and collection status expose the gap. Existing cloud research monitoring is separate.
- The exporter lives in WBQ at `scripts/export_assets_locked.py`; daily observed valuations are local-only in `data/portfolio_dashboard.db`. No earlier performance is fabricated.

## Trading decisions and execution journal

The **Trading decisions / Journal** tab uses separate `data/activity.enc.json`
ciphertext, decrypted only after the existing passphrase unlock. Its script-only
sandbox has no network or order access. Relocking clears its plaintext and stops
refreshes. Failed refreshes retain the previous snapshot with a visible warning.

The Home Windows exporter `scripts/export_trading_activity_locked.py` in WBQ reads
local SQLite ledgers in read-only transactions without broker credentials. Coin
entry conditions are recomputed from hash-validated saved market bars and registered
strategy source; they cannot reconstruct historical cash, liquidity, or add-on orders.
Missing sources, failed executions, stale worker heartbeats, and empty ledgers are
distinct states. Korean and US broker PAPER fills are labeled separately from coin
LIVE fills. Unknown costs or unprovable acquisition history never become net profit.

The Home Windows publication task refreshes the encrypted snapshot every fifteen
minutes while the user is logged in. The browser checks for published snapshots
every minute. A twenty-minute-old publication or overdue decision shows a warning;
PC downtime and deployment delay are not treated as live healthy status.

## Closing prices and site publication

- `research-monitor.yml` collects completed-session research prices on its closing schedules. It has its own concurrency group and cannot hold up a site deployment. An unavailable interior candidate bar or a zero-volume daily bar preserves the affected research window's prior observations and marks it `PARTIAL_HELD`; independent valid quotes still update. A validated partial refresh completes successfully, and the dashboard displays the hold explicitly.
- US VOO/SPMO requests use fixed exchange-local date bounds and at most three attempts (30-second request timeout, 5/15-second retry delays). Missing completed US closes, timeouts/connections and HTTP 408/429/500/502/503/504 may retry. If still unavailable, a validated last-good snapshot is published as `DELAYED` with unchanged observation dates, financial contents and generation time; the encrypted ledger stays byte-identical. The dashboard shows the target session and retained date. No old close is relabeled as fresh. A later normal refresh retries automatically through the existing schedules and clears the delay when validated data returns.
- Safe logs identify the provider, symbol, expected/received session, reason and attempt count without prices, response bodies or keys. Missing latest Korean closes or index sessions, malformed/invalid provider data, non-transient HTTP errors, corporate-action anomalies, changed recorded history and coding/receipt/publication errors still fail closed. A delayed run without a valid last-good snapshot also fails. External service availability cannot be guaranteed.
- `publish_state.py` publishes only the two encrypted monitoring outputs whose bytes match the collector receipt. It incorporates unrelated newer main commits, but refuses overlapping state or collector-code changes so a stale collection cannot overwrite them.
- `publish-pages.yml` runs on every main push and after the two cloud data workflows finish. It packages the latest committed main from an explicit public-file allowlist, without any decryption key. New publication runs supersede older ones. The deploy job has a five-minute timeout and refuses a package superseded by newer main commits.
- Each site artifact contains `deployment-version.json` with its source commit and file hashes. After deployment, the workflow checks the manifest and every HTML, JavaScript, encrypted-data and other public file at the normal served URLs. Old or missing bytes fail verification; a successful upload alone is not publication evidence.
- `recover-pages.yml` checks every 15 minutes (GitHub scheduling may be delayed). It cancels obsolete runs of the dedicated main-site publisher, or a latest run stalled for more than 20 minutes. Only its own stalled cancellation can receive one automatic rerun. A cached recovery record allows cancellation to finish after the current check; manually cancelled runs are not retried. Research collectors and broker services are outside its scope.

These workflows publish a read-only dashboard. They do not place orders, move funds, or enable trading. Local verification uses disposable public-site, Git-remote and Actions fixtures with no broker access:

```sh
python -m unittest discover -s tests -v
python -m unittest discover -s scripts/cloud_monitor -p 'test_*.py' -v
node --test tests/test_monitoring_status.mjs
```
