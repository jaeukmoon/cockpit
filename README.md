# WorldBestQuant

Public case study and encrypted read-only dashboard for a personal quant investing system.

- `index.html`: public aggregate performance and system overview
- `dashboard.html`: passphrase-gated dashboard
- `data/*.enc.json`: PBKDF2-SHA256 (600k iterations) + AES-256-GCM ciphertext

The public page contains no capital amounts, account data, current holdings, or order controls. Private dashboard content is decrypted only in the browser.
