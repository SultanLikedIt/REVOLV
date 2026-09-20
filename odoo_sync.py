#!/usr/bin/env python3
"""One-off inventory migration: CSV stock deltas -> Odoo `stock.quant`.

Reads `data/incoming.csv` (`material_sku`, `quantity_change`, `location`),
applies each delta, writes `data/sync_report.csv`. Dry run unless `--apply`.

Three measured facts shape this script (see docs/probe_findings.md):

1. `inventory_quantity_auto_apply` only takes effect when the call carries
   `context={'inventory_mode': True}` -- on create as well as on write.
   Without it the call succeeds and changes nothing.
2. That field is an ABSOLUTE counted quantity; the CSV supplies a DELTA, so the
   operation is read-then-write and re-running a file doubles it -- hence the
   run ledger.
3. Return values prove nothing. This server raised on a write that landed,
   returned True on a write that did not, and returned a valid quant id on a
   create that left the quantity at 0. Read-back is the only success test.

Stdlib only. Python 3.11+.
"""

from __future__ import annotations

import argparse, csv, hashlib, json, os, random, re, socket, sys, time, xmlrpc.client
from collections import Counter
from dataclasses import dataclass
from decimal import Decimal
from http.client import HTTPException
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
LEDGER_PATH = ROOT / ".odoo_sync_ledger.json"
REQUIRED_VARS = ("ODOO_URL", "ODOO_DB", "ODOO_USER", "ODOO_API_KEY")
REQUIRED_COLUMNS = ("material_sku", "quantity_change", "location")
REPORT_COLUMNS = ("row", "material_sku", "status", "qty_before", "qty_after", "message")

MIN_CALL_INTERVAL = 1.0      # Odoo Cloud tolerates ~1 call/s, no parallel calls
MAX_ATTEMPTS = 3
INVENTORY_CONTEXT = {"context": {"inventory_mode": True}}
TOLERANCE = Decimal("0.000001")   # absorbs the float round trip, nothing more
MAX_MAGNITUDE = Decimal("10000")

# Accepts 12, -12, 12.5, .5, +3 and nothing else -- so it also rejects empties,
# text, Italian comma decimals ("1,5") and scientific notation ("1e3"), all of
# which Decimal() would otherwise accept or mis-handle.
NUMERIC_RE = re.compile(r"^[+-]?(?:\d+(?:\.\d+)?|\.\d+)$")

# Report status vocabulary, in the order Ops meets it in the spreadsheet legend:
# OK, DRY_RUN_OK | SKIPPED_SKU_NOT_FOUND, SKIPPED_LOCATION_NOT_FOUND | HELD_LOT_TRACKED
# | REJECTED_INVALID_QUANTITY, REJECTED_MISSING_FIELD | FAILED_API_UNAVAILABLE,
# FAILED_API_ERROR, FAILED_NOT_VERIFIED, FAILED_UNEXPECTED
OK_STATUSES = frozenset({"OK", "DRY_RUN_OK"})


class TransientError(RuntimeError):
    """Transport-level failure. Possibly safe to retry, never certainly so."""


class PermanentError(RuntimeError):
    """Business or access error from the server. Retrying cannot help."""


class AbortRun(RuntimeError):
    """Stop before touching Odoo. Maps to exit code 2."""


@dataclass(frozen=True)
class InputRow:
    number: int
    sku: str
    delta: Decimal
    location: str


@dataclass
class Result:
    row: int
    material_sku: str
    status: str
    qty_before: str = ""
    qty_after: str = ""
    message: str = ""


def to_decimal(value: Any) -> Decimal:
    return Decimal(str(value))


def fmt_qty(value: Decimal) -> str:
    return format(value.normalize(), "f")


def load_config() -> dict[str, str]:
    """Credentials from the environment or a gitignored `.env`, never source."""
    values: dict[str, str] = {}
    env_path = ROOT / ".env"
    if env_path.exists():
        for raw in env_path.read_text(encoding="utf-8").splitlines():
            line = raw.strip().removeprefix("export ").lstrip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            values[key.strip()] = value.strip().strip("\"'")
    config = {name: os.environ.get(name) or values.get(name, "") for name in REQUIRED_VARS}
    if missing := [name for name, value in config.items() if not value]:
        raise AbortRun(f"missing credentials: {', '.join(missing)}")
    config["ODOO_URL"] = config["ODOO_URL"].rstrip("/")
    return config


def read_ledger() -> dict[str, Any]:
    if not LEDGER_PATH.exists():
        return {}
    try:
        return json.loads(LEDGER_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise AbortRun(f"ledger {LEDGER_PATH.name} is corrupt ({exc}); inspect it by hand")


def read_rows(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        raise AbortRun(f"input file not found: {path}")
    # utf-8-sig strips the BOM Excel writes, which would otherwise corrupt the
    # first column name and fail the header check for no visible reason.
    with path.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames is None:
            raise AbortRun(f"input file is empty: {path}")
        fields = {name.strip() for name in reader.fieldnames}
        if missing := [name for name in REQUIRED_COLUMNS if name not in fields]:
            raise AbortRun(f"input file is missing column(s): {', '.join(missing)}")
        return list(reader)


def validate(records: list[dict[str, str]]) -> tuple[list[InputRow], list[Result]]:
    """Validate the whole file before any network call.

    A format error means the file itself is untrustworthy, so the caller refuses
    to start rather than half-applying a migration. Resolution errors (unknown
    SKU or location) are one row's problem and are handled later, per row.
    """
    good: list[InputRow] = []
    bad: list[Result] = []

    for number, record in enumerate(records, start=2):   # row 1 is the header
        sku = (record.get("material_sku") or "").strip()
        location = (record.get("location") or "").strip()
        qty = (record.get("quantity_change") or "").strip()
        status, problem = "REJECTED_INVALID_QUANTITY", ""

        # csv collects surplus fields under the None key. The usual cause is an
        # unquoted Italian comma decimal: "SKU,1,5,WH/Stock" parses as four
        # fields and would otherwise slip through as delta 1 at location "5".
        if record.get(None):
            status, problem = ("REJECTED_MISSING_FIELD",
                               "too many fields; an unquoted comma decimal such as 1,5 does this")
        elif not sku or not location:
            status, problem = ("REJECTED_MISSING_FIELD",
                               f"empty required field: {'material_sku' if not sku else 'location'}")
        elif not qty:
            problem = "quantity_change is empty"
        elif "," in qty:
            problem = f"comma decimal {qty!r}; use a dot (1.5, not 1,5)"
        elif not NUMERIC_RE.match(qty):
            problem = f"not a plain decimal number: {qty!r}"
        elif abs(Decimal(qty)) > MAX_MAGNITUDE:
            problem = f"magnitude {qty} exceeds the sanity bound {MAX_MAGNITUDE}"
        elif Decimal(qty) == 0:
            problem = "quantity_change is zero"

        if problem:
            bad.append(Result(number, sku, status, message=problem))
        else:
            good.append(InputRow(number, sku, Decimal(qty), location))

    return good, bad


class XmlRpcTransport:
    """The only part of the script that knows the wire protocol.

    XML-RPC is deprecated in Odoo 19 and JSON-2 was verified working on the same
    database, so the swap must stay a one-class change.
    """

    def __init__(self, config: dict[str, str], timeout: float = 30.0) -> None:
        self.db, self.user, self.key = config["ODOO_DB"], config["ODOO_USER"], config["ODOO_API_KEY"]
        socket.setdefaulttimeout(timeout)
        url = config["ODOO_URL"]
        self._common = xmlrpc.client.ServerProxy(f"{url}/xmlrpc/2/common")
        self._models = xmlrpc.client.ServerProxy(f"{url}/xmlrpc/2/object")
        self._uid: int | None = None
        self._last = 0.0

    def authenticate(self) -> int:
        self._throttle()
        try:
            uid = self._common.authenticate(self.db, self.user, self.key, {})
        except xmlrpc.client.Fault as exc:
            raise PermanentError(f"authentication rejected: {exc.faultString}") from exc
        except (OSError, HTTPException, xmlrpc.client.ProtocolError) as exc:
            raise TransientError(f"cannot reach the server: {exc}") from exc
        if not uid:
            raise PermanentError("authentication returned no uid; check db, user and API key")
        self._uid = int(uid)
        return self._uid

    def _throttle(self) -> None:
        if (wait := MIN_CALL_INTERVAL - (time.monotonic() - self._last)) > 0:
            time.sleep(wait)
        self._last = time.monotonic()

    def call(self, model: str, method: str, args: list[Any],
             kwargs: dict[str, Any] | None = None) -> Any:
        self._throttle()
        try:
            return self._models.execute_kw(self.db, self._uid, self.key,
                                           model, method, args, kwargs or {})
        except xmlrpc.client.Fault as exc:
            raise PermanentError(
                f"{model}.{method}: {exc.faultString.strip().splitlines()[-1]}") from exc
        except (OSError, HTTPException, xmlrpc.client.ProtocolError) as exc:
            raise TransientError(f"{model}.{method}: {exc}") from exc


class OdooClient:
    """Business-level operations. Holds the retry and read-back policy."""

    def __init__(self, transport: XmlRpcTransport) -> None:
        self.transport = transport
        self.products: dict[str, list[dict[str, Any]]] = {}
        self.locations: dict[str, list[int]] = {}

    def _retry(self, model: str, method: str, args: list[Any],
               kwargs: dict[str, Any] | None = None) -> Any:
        """Retry wrapper for calls that are safe to repeat. Reads only."""
        last: TransientError | None = None
        for attempt in range(MAX_ATTEMPTS):
            if attempt:
                time.sleep(2.0 ** attempt + random.uniform(0.0, 0.5))   # jittered backoff
            try:
                return self.transport.call(model, method, args, kwargs)
            except TransientError as exc:
                last = exc
        raise TransientError(f"gave up after {MAX_ATTEMPTS} attempts: {last}")

    def prefetch(self, skus: list[str], names: list[str]) -> None:
        """Resolve every distinct SKU and location up front: at ~1 call/second,
        two batched lookups beat several hundred serialised round trips."""
        for product in self._retry("product.product", "search_read",
                                   [[("default_code", "in", skus)]],
                                   {"fields": ["id", "default_code", "tracking"]}):
            self.products.setdefault(product["default_code"], []).append(product)
        for loc in self._retry("stock.location", "search_read",
                               [[("complete_name", "in", names), ("usage", "=", "internal")]],
                               {"fields": ["id", "complete_name"]}):
            self.locations.setdefault(loc["complete_name"], []).append(loc["id"])

    def find_quant(self, product_id: int, location_id: int) -> dict[str, Any] | None:
        found = self._retry("stock.quant", "search_read",
                            [[("product_id", "=", product_id), ("location_id", "=", location_id)]],
                            {"fields": ["id", "quantity"], "limit": 1})
        return found[0] if found else None

    def read_quantity(self, quant_id: int) -> Decimal:
        found = self._retry("stock.quant", "read", [[quant_id]], {"fields": ["quantity"]})
        if not found:
            raise PermanentError(f"quant {quant_id} disappeared between write and verify")
        return to_decimal(found[0]["quantity"])

    def apply_quantity(self, product_id: int, location_id: int,
                       quant_id: int | None, target: Decimal) -> Decimal:
        """Write or create the quant, then return the value read back.

        Both paths re-resolve the quant before retrying, because a transport
        error is ambiguous: this server was measured raising on a write that had
        landed, and a blind retry of a create would insert a duplicate quant
        rather than repeat a harmless error.
        """
        values = {"inventory_quantity_auto_apply": float(target)}
        last: TransientError | None = None
        for attempt in range(MAX_ATTEMPTS):
            if attempt:
                time.sleep(2.0 ** attempt + random.uniform(0.0, 0.5))
                if found := self.find_quant(product_id, location_id):
                    quant_id = found["id"]
                    if abs(to_decimal(found["quantity"]) - target) <= TOLERANCE:
                        return to_decimal(found["quantity"])
            try:
                if quant_id is None:
                    quant_id = self.transport.call(
                        "stock.quant", "create",
                        [{"product_id": product_id, "location_id": location_id, **values}],
                        INVENTORY_CONTEXT)
                else:
                    self.transport.call("stock.quant", "write", [[quant_id], values],
                                        INVENTORY_CONTEXT)
            except TransientError as exc:
                last = exc
                continue
            return self.read_quantity(quant_id)
        raise TransientError(f"gave up after {MAX_ATTEMPTS} attempts: {last}")


def process_row(client: OdooClient, row: InputRow, apply_changes: bool,
                projection: dict[tuple[int, int], tuple[Decimal, bool]]) -> Result:
    result = lambda status, before="", after="", message="": Result(
        row.number, row.sku, status, before, after, message)

    products = client.products.get(row.sku, [])
    if len(products) != 1:
        # default_code is not unique in Odoo, so picking one of several would
        # write to an arbitrary part.
        detail = ("no active product has this internal reference" if not products
                  else f"ambiguous: {len(products)} products share this reference "
                       f"(ids {', '.join(str(p['id']) for p in products)})")
        return result("SKIPPED_SKU_NOT_FOUND", message=detail)

    product = products[0]
    if product["tracking"] != "none":
        return result("HELD_LOT_TRACKED",
                      message=f"tracking={product['tracking']}; needs a lot/serial, do it manually")

    locations = client.locations.get(row.location, [])
    if len(locations) != 1:
        detail = ("no internal location with this name" if not locations
                  else f"ambiguous: matches {len(locations)} internal locations")
        return result("SKIPPED_LOCATION_NOT_FOUND", message=f"{row.location!r}: {detail}")

    # No quant yet means no stock yet: start from zero and create it. Measured:
    # create with inventory_mode lands the quantity and raises the counterpart
    # adjustment line, exactly as a write does -- and is verified the same way.
    #
    # Apply re-reads before every row, so a SKU appearing twice chains correctly.
    # A dry run writes nothing, so it must chain the same way in memory or the
    # preview for the second occurrence would start from the pre-run value and
    # disagree with the apply it is supposed to predict.
    key = (product["id"], locations[0])
    quant = None
    if not apply_changes and (seen := projection.get(key)) is not None:
        before, exists = seen
    else:
        quant = client.find_quant(*key)
        before, exists = (to_decimal(quant["quantity"]), True) if quant else (Decimal(0), False)
    target = before + row.delta

    if target < 0:
        return result("REJECTED_INVALID_QUANTITY", fmt_qty(before),
                      message=f"delta {fmt_qty(row.delta)} would drive stock to {fmt_qty(target)}")
    if not apply_changes:
        projection[key] = (target, True)
        return result("DRY_RUN_OK", fmt_qty(before), fmt_qty(target),
                      "dry run: nothing written" + ("" if exists else "; quant would be created"))

    after = client.apply_quantity(product["id"], locations[0],
                                  quant["id"] if quant else None, target)
    if abs(after - target) > TOLERANCE:
        return result("FAILED_NOT_VERIFIED", fmt_qty(before), fmt_qty(after),
                      f"read-back is {fmt_qty(after)}, expected {fmt_qty(target)}")
    return result("OK", fmt_qty(before), fmt_qty(after),
                  "verified by read-back" + ("" if quant else "; quant created"))


def process_rows(client: OdooClient, rows: list[InputRow], apply_changes: bool) -> list[Result]:
    results = []
    projection: dict[tuple[int, int], tuple[Decimal, bool]] = {}   # dry run only
    for row in rows:
        try:
            results.append(process_row(client, row, apply_changes, projection))
        except TransientError as exc:
            results.append(Result(row.number, row.sku, "FAILED_API_UNAVAILABLE", message=str(exc)))
        except PermanentError as exc:
            results.append(Result(row.number, row.sku, "FAILED_API_ERROR", message=str(exc)))
        except Exception as exc:   # one bad row must never end the run
            results.append(Result(row.number, row.sku, "FAILED_UNEXPECTED",
                                  message=f"{type(exc).__name__}: {exc}"))
    return results


def write_report(path: Path, results: list[Result]) -> None:
    """Doubles as the rollback record: qty_before is the prior value of every
    line the run touched."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(REPORT_COLUMNS)
        for item in sorted(results, key=lambda r: r.row):
            writer.writerow([item.row, item.material_sku, item.status,
                             item.qty_before, item.qty_after, item.message])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Apply CSV stock deltas to Odoo. Dry run unless --apply is given.")
    parser.add_argument("--input", type=Path, default=ROOT / "data" / "incoming.csv")
    parser.add_argument("--report", type=Path, default=ROOT / "data" / "sync_report.csv")
    parser.add_argument("--apply", action="store_true", help="write to Odoo (default: dry run)")
    parser.add_argument("--force", action="store_true",
                        help="apply a file the ledger records as already applied")
    args = parser.parse_args(argv)

    try:
        records = read_rows(args.input)
        digest = hashlib.sha256(args.input.read_bytes()).hexdigest()
        if previous := read_ledger().get(digest):
            note = f"this exact file was already applied on {previous['applied_at']}"
            if args.apply and not args.force:
                # quantity_change is a delta, so a second run silently doubles it.
                raise AbortRun(f"{note}. Re-applying would add the deltas a second time; "
                               "use --force only if you are certain the first run did not land.")
            print(f"  ! {note}", file=sys.stderr, flush=True)

        valid, rejected = validate(records)
        print(f"  {len(records)} row(s) read, {len(valid)} valid, {len(rejected)} rejected",
              flush=True)
        if rejected:
            write_report(args.report, rejected)
            raise AbortRun(f"{len(rejected)} row(s) failed format validation. Nothing was sent "
                           f"to Odoo.\n  Fix {args.input} and rerun; see {args.report}.")
        if not valid:
            raise AbortRun("no data rows to process")

        config = load_config()
        transport = XmlRpcTransport(config)
        print(f"  authenticated as uid={transport.authenticate()} on {config['ODOO_DB']}")
        client = OdooClient(transport)
        client.prefetch(sorted({row.sku for row in valid}), sorted({row.location for row in valid}))
        results = process_rows(client, valid, args.apply)

    except AbortRun as exc:
        print(f"\n  ABORTED: {exc}", file=sys.stderr)
        return 2
    except (TransientError, PermanentError) as exc:
        print(f"\n  ABORTED: {exc}", file=sys.stderr)   # auth or prefetch: nothing written
        return 2
    except KeyboardInterrupt:
        print("\n  ABORTED: interrupted by the operator.", file=sys.stderr)
        return 2

    write_report(args.report, results)
    counts = Counter(result.status for result in results)
    print("\n" + "-" * 62)
    print(f"  {'APPLIED' if args.apply else 'DRY RUN - nothing was written'}")
    print(f"  rows processed : {len(results)}")
    for status, count in sorted(counts.items()):
        print(f"  {status:<28} {count}")
    print(f"  report         : {args.report}\n" + "-" * 62)

    if args.apply and counts["OK"]:
        ledger = read_ledger()
        ledger[digest] = {"file": args.input.name, "counts": dict(counts),
                          "applied_at": time.strftime("%Y-%m-%dT%H:%M:%S%z")}
        LEDGER_PATH.write_text(json.dumps(ledger, indent=2, sort_keys=True), encoding="utf-8")

    return 0 if all(result.status in OK_STATUSES for result in results) else 1


if __name__ == "__main__":
    sys.exit(main())
