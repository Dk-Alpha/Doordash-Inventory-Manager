# DoorDash Bulk Inventory Manager

A free, local, native desktop app for sellers who manage a DoorDash storefront
and need to make bulk changes to their catalog — without hand-editing
spreadsheets and hoping nothing breaks.

## The problem

DoorDash lets you export your inventory and re-upload a CSV to update it, but
that's where the tooling stops. In practice, sellers regularly need to:

- Take a list of items to change — often just a column of UPCs, sometimes
  with new prices or an active/inactive status attached — and figure out
  which of those UPCs already exist in the DoorDash catalog and which don't.
- Review and edit the matched items (price changes, activate/deactivate)
  before committing anything.
- Onboard the unmatched ones as brand-new SKUs, in DoorDash's exact upload
  format.
- Search and bulk-edit across thousands of SKUs — "find every Red Bull SKU,
  now narrow to the 12oz ones, now raise all their prices 5%."

Doing this in Excel is slow and quietly dangerous: Excel strips leading
zeros from UPCs stored as numbers, duplicate rows get silently dropped or
overwritten, there's no undo, and it's easy to bulk-edit the wrong filtered
set by accident. Real-world DoorDash/Google-Sheets exports also throw
curveballs — extra "Read only" annotation rows above the real header,
inconsistent encodings, values wrapped in stray punctuation — that choke a
naive CSV import entirely.

## What it does

1. **Import your master inventory** (the DoorDash export). Columns are
   auto-detected regardless of naming or order, with a confirmation screen
   before anything is written. Handles messy real-world exports: leading
   annotation rows, mixed encodings, malformed rows that would otherwise
   crash a plain CSV parser.
2. **Import a work list** — any UPC list, with or without price/status/name
   columns. Every work list gets its own ID and shows up in **Work Lists**.
   Every row is matched against your current inventory by a normalized UPC
   (leading-zero-safe) — the UPC only ever *locates* the item; a matched
   row's name/category/price always come from inventory, never from the
   work list file, since work-list data can be stale — and split
   automatically into:
   - **Work Lists** — matched rows, staged with their proposed changes for
     review and editing. Nothing touches your real inventory until you
     explicitly push a work list's changes (all at once, or row-by-row).
     Once you're done with a work list, mark it completed; every pushed
     change is tagged with the work list that caused it.
   - **New Items** — unmatched rows, flagged for onboarding, with inline
     fields to complete the required data before export.
3. **Two-stage search & filter** — e.g. type "red bull", then narrow to
   "12oz" — live, debounced, and works across Active/Inactive segments
   simultaneously. An advanced panel adds price range, category, and
   "changed this session" filters, all combinable.
4. **Safe bulk editing** — activate/deactivate, or change price (set exact,
   +/-%, or +/- a flat amount) across any filtered selection. Every bulk
   action shows a confirmation with the affected count and a sample of item
   names before it commits, and can be undone with one click.
5. **Full audit trail** — every price/status edit snapshots the original
   value, so you always know what changed before you export, and nothing is
   ever silently lost.
6. **Two clean exports** — a full updated-inventory CSV ready to re-upload
   to DoorDash, and a separate new-SKU CSV in DoorDash's exact template
   column order. The new-SKU export is blocked (with a clear message) until
   every required field is filled in, instead of shipping a broken file.
7. **Multi-store support** — manage several DoorDash `business_id`/`store_id`
   combinations from the same app, with an active-store selector always
   visible.

## Why it helps

- Turns a manual, error-prone spreadsheet exercise into a few clicks.
- Removes the most common data-corruption source in this workflow (Excel
  stripping UPC leading zeros).
- Makes "what's about to change" visible and reversible *before* you commit
  to a real DoorDash upload.
- Keeps working on export files that would otherwise crash a naive importer.
- Runs 100% locally — your catalog data never leaves your machine.

## Tech stack

PySide6 (Qt) for the desktop UI, SQLite for local session state (so imports,
edits, and undo history survive between runs), pandas for CSV/XLSX parsing.
Core import/matching/filtering logic is covered by an automated `pytest`
suite.

## Getting started

**Windows:** just double-click [`run_app.bat`](run_app.bat) — it creates a
virtual environment and installs dependencies on first run, then launches
the app every time after.

**Manual setup:**

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
python -m app.main
```

A `doordash_manager.db` SQLite file is created next to the app on first run
(override with the `DOORDASH_MANAGER_DB` environment variable).

## Run the tests

```bash
pytest
```

## Project layout

- `app/db.py` — SQLite schema (stores, inventory items, work lists + staged
  work-list items, changelog, import history, remembered column-mapping
  presets)
- `app/upc.py` — UPC normalization (handles Excel's leading-zero stripping)
- `app/matching.py` — the work-list-vs-inventory matching engine (pure
  function, unit tested)
- `app/importers.py` — CSV/XLSX reading: encoding/delimiter detection,
  leading-annotation-row detection, fuzzy column detection
- `app/repo.py` — data-access layer: filters, bulk status/price edits, undo,
  work-list staging/push/lifecycle, dashboard stats
- `app/export.py` — Updated Inventory / New SKU CSV exports in DoorDash's
  column order, with pre-export validation
- `app/ui/` — PySide6 UI: Dashboard / Existing Items / New Items / Work
  Lists / Import / Settings tabs

## Scope

**Built:** import + column mapping (with remembered presets), UPC matching
engine, Existing/New item segregation, work lists as a trackable staged-
review entity (open/completed lifecycle, per-work-list changelog tagging),
two-stage + advanced filtering, inline edit, bulk status/price edit with
confirmation + undo, multi-store support, both CSV exports with pre-export
validation/summary.

**Deliberately deferred:** a fuzzy UPC typo-suggestion panel, a packaged
installer (PyInstaller), category-taxonomy management.

## License

No license has been chosen yet — all rights reserved by default until one
is added.
