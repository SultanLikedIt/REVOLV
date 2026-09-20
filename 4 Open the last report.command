#!/bin/bash
# Double-click me to open the most recent report in your spreadsheet program.
cd "$(dirname "$0")" || exit 1
latest=$(ls -t data/*report*.csv 2>/dev/null | head -1)
if [ -z "$latest" ]; then
  echo "No report found yet. Run 'Check the file' or 'Apply to Odoo' first."
  read -r -p "Press Enter to close this window..."
  exit 1
fi
echo "Opening $latest"
open "$latest"
