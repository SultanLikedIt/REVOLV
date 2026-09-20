#!/bin/bash
# Double-click me ONLY if a run was interrupted and you have checked Odoo.
# quantity_change is a DIFFERENCE, so applying the same file twice adds the
# same amounts twice. This wrapper exists to make that choice deliberate.
cd "$(dirname "$0")" || exit 1
echo "WARNING"
echo "The tool normally refuses a file it has already applied, because the"
echo "quantities in the file are DIFFERENCES, not final counts. Running the"
echo "same file twice would add the same amounts a second time."
echo
echo "Only continue if you have checked in Odoo that the first run did NOT"
echo "land. If you are not sure, close this window and ask for help."
echo
read -r -p "Type I HAVE CHECKED to continue: " answer
if [ "$answer" != "I HAVE CHECKED" ]; then
  echo "Cancelled. Nothing was changed."
  read -r -p "Press Enter to close this window..."
  exit 0
fi
echo
python3 odoo_sync.py --apply --force --report data/sync_report.csv
echo
read -r -p "Press Enter to close this window..."
