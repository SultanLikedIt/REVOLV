#!/bin/bash
# Double-click me. Reads data/incoming.csv and shows what WOULD change.
# Writes nothing to Odoo.
cd "$(dirname "$0")" || exit 1
echo "Checking data/incoming.csv against Odoo. Nothing will be changed."
echo
python3 odoo_sync.py --report data/sync_report.csv
echo
echo "Done. Nothing was written to Odoo."
read -r -p "Press Enter to close this window..."
