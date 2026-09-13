# Encrypted closing research monitor

GitHub-hosted only; no accounts, orders, broker tokens or shared passphrase. Schedules: Korean close weekdays 16:20 KST; US close Tue-Sat 07:20 KST. Schedules can be delayed. Holidays follow XKRX/XNYS calendars.

MONITORING_DATA_KEY is a dedicated repository Actions Secret. The browser unwraps the matching encrypted data key only after the existing passphrase unlock. Ledger and public snapshot are AES-256-GCM ciphertext. Plaintext is never committed.

Frozen Korean quant/LLM cohorts use the same exported pure WBQ replay functions. US VOO/SPMO observations are price-only benchmark returns from first cloud observation, not dividends, FX, fees or a new US strategy. No monthly reselection or LLM calls run here. Other strategy cards and backtests remain dated snapshots until explicitly exported.

Cloud owns monitoring/state.enc.json; PC owns its independent private database. Never overwrite the cloud ledger from a PC snapshot. New cohorts require an explicit reviewed import preserving existing observations and the key. Historical revisions and missing expected closes fail the run and leave the published snapshot unchanged. Completed cohorts remain frozen for review. Failure notifications are available through GitHub Actions subscription settings; no new Telegram sender is configured.

Deployment uses an explicit Pages artifact because GITHUB_TOKEN commits do not trigger branch-based Pages builds. The browser polls every five minutes after unlock. Source templates live in WorldBestQuant/deploy/cloud_monitor. Do not disable local collection or order services when migrating publication.
