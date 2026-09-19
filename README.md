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
