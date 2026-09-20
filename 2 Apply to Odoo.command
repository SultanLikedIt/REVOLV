#!/bin/bash
# Double-click me. Applies data/incoming.csv to Odoo for real.
cd "$(dirname "$0")" || exit 1
echo "This will CHANGE stock quantities in Odoo, using data/incoming.csv."
echo "Run 'Check the file' first if you have not already."
echo
read -r -p "Type yes to continue: " answer
if [ "$answer" != "yes" ]; then
  echo "Cancelled. Nothing was changed."
  read -r -p "Press Enter to close this window..."
  exit 0
fi
echo
python3 odoo_sync.py --apply --report data/sync_report.csv
echo
echo "Finished. Open data/sync_report.csv to see what happened to each row."
read -r -p "Press Enter to close this window..."
