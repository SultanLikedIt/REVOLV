# Odoo inventory sync — Revolv Space take-home

A take-home screening exercise for the **AI Operations Intern** role at Revolv
Space. The brief: propose a clean, cost-free way to build and roll out a one-off
inventory migration into Odoo SaaS, where no custom code can be uploaded and
Studio would force a paid plan.

The submission itself is a 2-page PDF plus a single-sheet spreadsheet
(`data/sync_demo.xlsx`). This repo is the working evidence behind it.

## What the tool does

`odoo_sync.py` reads a CSV of stock changes from Procurement — `material_sku`,
`quantity_change`, `location` — and applies each one to the matching stock line
in Odoo over the External API. It validates the whole file before it opens a
connection, and after every write it re-reads the record and compares, because
this database was measured returning success on a write that did nothing and
raising an error on a write that landed. Every row ends in the report with a
status saying what happened to it, and the run is refused outright if the same
file has been applied before, since `quantity_change` is a difference and a
second run would add the same amounts again.

No dependencies — Python 3.11+ standard library only.

## How to run it

```bash
cp .env.example .env
$EDITOR .env            # ODOO_URL, ODOO_DB, ODOO_USER, ODOO_API_KEY

python3 odoo_sync.py             # dry run: resolves and previews, writes nothing
python3 odoo_sync.py --apply     # writes to Odoo
```

**A dry run is the default.** There is no `--dry-run` flag; `--apply` is what
turns writing on. The dry run does everything except the write, and its
`qty_after` column is what the apply will produce.

| Flag | Effect |
|---|---|
| *(none)* | Dry run. Reads Odoo, previews every row, writes nothing. |
| `--apply` | Perform the writes. |
| `--force` | Apply a file the ledger already records as applied. Use only if you have checked that the first run did not land. |
| `--input` / `--report` | Override the default paths under `data/`. |

Exit codes: `0` every row OK, `1` some rows not OK, `2` aborted before any write.

If any variable is missing the script exits before opening a connection and
names the ones it needs. Credentials are never passed on the command line and
never appear in the source.

### Double-click wrappers

For Procurement and Ops staff who should not have to open a terminal, four
macOS wrappers sit in the repo root and do the same thing:

| File | What it does |
|---|---|
| `1 Check the file (dry run).command` | Previews `data/incoming.csv`. Writes nothing. |
| `2 Apply to Odoo.command` | Applies it, after you type `yes`. |
| `3 Apply again (force).command` | The `--force` path. Requires typing `I HAVE CHECKED`, and explains why before asking. |
| `4 Open the last report.command` | Opens the newest report in your spreadsheet program. |

Each holds the window open at the end so the summary can be read.

## The repo

| Path | Contents |
|---|---|
| `odoo_sync.py` | The tool. One file, standard library only. |
| `prompts/system_prompt.md` | The hand-authored system prompt and user query the script was generated from. Kept in step with the code it describes. |
| `docs/probe_findings.md` | Everything measured against the live database: schema, the write-path isolation, the three cases where a return value lied, and what cannot be done over the API. Written before the tool, and the reason the tool looks the way it does. |
| `data/` | Sample input, the reports from a real run, and `sync_demo.xlsx` — the worked example. |
| `tools/` | The probes that produced `docs/probe_findings.md`. Evidence, not part of the tool. |
| `tests/` | Offline tests against a fake Odoo client. Run: `python3 -m unittest discover -s tests -v` |

### `tools/`

These are throwaway probes, kept because they are the evidence for every claim
in `docs/probe_findings.md`:

- `odoo_probe.py` — does the External API work on this plan, what are the real
  field names, does a write actually land?
- `diag.py` — isolates which write path genuinely takes effect, across three
  variants.
- `noquant_test.py` — what happens when no stock line exists yet, and what the
  API refuses to delete.

### `data/`

| File | What it is |
|---|---|
| `incoming_broken.csv` | The file as Procurement might realistically send it: 8 good rows, one unreadable quantity, one Italian comma decimal that splits the row. |
| `incoming.csv` | The same file with those two corrected. The default input. |
| `report_1_rejected.csv` | What the tool produced from the broken file: it refused the whole file and sent nothing. |
| `report_2_dryrun.csv` | The preview of the corrected file. |
| `report_2_applied.csv` | The same file actually applied. Identical `before`/`after` columns to the preview, on all ten rows. |
| `sync_demo.xlsx` | All of the above on one sheet, with a plain-language legend for every status. |

**These are reproducible.** The sample products are reset to their starting
quantities before the reports are regenerated — `SADA-BRG-007` 12,
`SADA-MOT-021` 30, `SADA-HRN-014` 8, `SADA-SLP-033` deliberately with no stock
line at all so the create path is exercised, and `SADA-ENC-002` serial-tracked
so it is held back. Without that reset the numbers would drift, because each run
adds its differences on top of the last.

## A note on scope

The brief asks for roughly 1.5 hours of work and says scope discipline is part
of what is assessed. There is deliberately no Docker, no database, no scheduler
and no web interface here. The one thing that did get spent on is verification:
what is in `docs/probe_findings.md` was measured against a live database rather
than remembered, and several of the design decisions in the tool exist only
because a measurement contradicted the documentation.
