"""Offline tests for odoo_sync.py against a fake Odoo transport.

No network, no credentials, no .env: `XmlRpcTransport` and `load_config` are
patched out, the ledger is redirected to a temp directory, and retry sleeps are
skipped. The fake keeps its own products, locations and quants, and honours
`inventory_mode` the way the live database was measured to.

Run: python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import csv
import io
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import odoo_sync  # noqa: E402

STOCK = "WH/Stock"
HEADER = "material_sku,quantity_change,location\n"


class FakeTransport:
    """Stands in for XmlRpcTransport. Records every call it receives.

    `write_behaviour` controls the next write(s):
      "normal"          -- apply the value, return True
      "timeout_landed"  -- apply the value, then raise TransientError (once)
      "noop"            -- change nothing, return True (every time)
    """

    def __init__(self, products, locations, quants, write_behaviour="normal"):
        self.products = products            # [{"id", "default_code", "tracking"}]
        self.locations = locations          # [{"id", "complete_name"}]
        self.quants = {q["id"]: dict(q) for q in quants}   # {"id","product_id","location_id","quantity"}
        self.write_behaviour = write_behaviour
        self.calls: list[tuple[str, str]] = []
        self._next_id = 1000

    def authenticate(self):
        self.calls.append(("common", "authenticate"))
        return 2

    def methods(self, model=None):
        return [m for mod, m in self.calls if model is None or mod == model]

    def call(self, model, method, args, kwargs=None):
        self.calls.append((model, method))
        kwargs = kwargs or {}
        inventory_mode = kwargs.get("context", {}).get("inventory_mode", False)

        if model == "product.product" and method == "search_read":
            skus = dict((f, v) for f, _, v in args[0])["default_code"]
            return [dict(p) for p in self.products if p["default_code"] in skus]

        if model == "stock.location" and method == "search_read":
            names = dict((f, v) for f, _, v in args[0])["complete_name"]
            return [dict(l) for l in self.locations if l["complete_name"] in names]

        if model == "stock.quant" and method == "search_read":
            where = dict((f, v) for f, _, v in args[0])
            hits = [{"id": q["id"], "quantity": q["quantity"]} for q in self.quants.values()
                    if q["product_id"] == where["product_id"]
                    and q["location_id"] == where["location_id"]]
            return hits[: kwargs.get("limit", len(hits))]

        if model == "stock.quant" and method == "read":
            return [{"id": i, "quantity": self.quants[i]["quantity"]}
                    for i in args[0] if i in self.quants]

        if model == "stock.quant" and method == "create":
            vals = args[0]
            quant_id, self._next_id = self._next_id, self._next_id + 1
            # Measured: without inventory_mode the create returns an id and the
            # quantity stays 0.
            qty = vals["inventory_quantity_auto_apply"] if inventory_mode else 0.0
            self.quants[quant_id] = {"id": quant_id, "product_id": vals["product_id"],
                                     "location_id": vals["location_id"], "quantity": qty}
            return quant_id

        if model == "stock.quant" and method == "write":
            ids, vals = args
            if self.write_behaviour == "noop" or not inventory_mode:
                return True
            for i in ids:
                self.quants[i]["quantity"] = vals["inventory_quantity_auto_apply"]
            if self.write_behaviour == "timeout_landed":
                self.write_behaviour = "normal"
                raise odoo_sync.TransientError("stock.quant.write: timed out")
            return True

        raise AssertionError(f"unexpected call {model}.{method}")


def standard_fake(**kwargs):
    """The sample database from README: BRG-007 at 12, MOT-021 at 30, SLP-033
    with no quant at all, ENC-002 serial-tracked."""
    products = [
        {"id": 1, "default_code": "SADA-BRG-007", "tracking": "none"},
        {"id": 2, "default_code": "SADA-MOT-021", "tracking": "none"},
        {"id": 3, "default_code": "SADA-SLP-033", "tracking": "none"},
        {"id": 4, "default_code": "SADA-ENC-002", "tracking": "serial"},
    ]
    locations = [{"id": 8, "complete_name": STOCK}]
    quants = [
        {"id": 101, "product_id": 1, "location_id": 8, "quantity": 12.0},
        {"id": 102, "product_id": 2, "location_id": 8, "quantity": 30.0},
    ]
    return FakeTransport(products, locations, quants, **kwargs)


class SyncTestCase(unittest.TestCase):
    """Runs odoo_sync.main() end to end with the fake wired in."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.input = self.tmp / "incoming.csv"
        self.report = self.tmp / "report.csv"
        self.constructed: list[FakeTransport] = []
        self.fake = standard_fake()

        def make_transport(config):
            self.constructed.append(self.fake)
            return self.fake

        for target, value in (
            ("odoo_sync.XmlRpcTransport", make_transport),
            ("odoo_sync.load_config", lambda: {"ODOO_DB": "test"}),
            ("odoo_sync.LEDGER_PATH", self.tmp / "ledger.json"),
            ("odoo_sync.time.sleep", lambda seconds: None),
        ):
            patcher = mock.patch(target, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def write_input(self, body: str) -> None:
        self.input.write_text(HEADER + body, encoding="utf-8")

    def run_sync(self, *flags: str) -> int:
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            return odoo_sync.main(["--input", str(self.input),
                                   "--report", str(self.report), *flags])

    def report_rows(self) -> list[dict[str, str]]:
        with self.report.open(newline="", encoding="utf-8") as handle:
            return list(csv.DictReader(handle))


class ValidationTests(SyncTestCase):

    def test_unparseable_quantity_aborts_with_no_calls(self):
        self.write_input(f"SADA-BRG-007,5,{STOCK}\nSADA-MOT-021,twelve,{STOCK}\n")
        self.assertEqual(self.run_sync("--apply"), 2)
        self.assertEqual(self.constructed, [], "a transport was created")
        self.assertEqual(self.fake.calls, [])
        rows = self.report_rows()
        self.assertEqual([(r["row"], r["status"]) for r in rows],
                         [("3", "REJECTED_INVALID_QUANTITY")])
        self.assertIn("'twelve'", rows[0]["message"])

    def test_unquoted_comma_decimal_is_missing_field(self):
        self.write_input(f"SADA-BRG-007,1,5,{STOCK}\n")
        self.assertEqual(self.run_sync("--apply"), 2)
        self.assertEqual(self.fake.calls, [])
        [row] = self.report_rows()
        self.assertEqual(row["status"], "REJECTED_MISSING_FIELD")
        self.assertIn("comma decimal", row["message"])


class ApplyTests(SyncTestCase):

    def test_missing_sku_is_skipped_and_nothing_created(self):
        self.write_input(f"SADA-XXX-999,3,{STOCK}\n")
        self.assertEqual(self.run_sync("--apply"), 1)
        [row] = self.report_rows()
        self.assertEqual(row["status"], "SKIPPED_SKU_NOT_FOUND")
        self.assertNotIn("create", self.fake.methods())
        self.assertNotIn("write", self.fake.methods())

    def test_timed_out_write_that_landed_is_reread_not_reapplied(self):
        self.fake.write_behaviour = "timeout_landed"
        self.write_input(f"SADA-BRG-007,5,{STOCK}\n")
        self.assertEqual(self.run_sync("--apply"), 0)
        [row] = self.report_rows()
        self.assertEqual((row["status"], row["qty_before"], row["qty_after"]), ("OK", "12", "17"))
        self.assertEqual(self.fake.methods("stock.quant").count("write"), 1)
        self.assertEqual(self.fake.quants[101]["quantity"], 17.0)

    def test_write_returning_true_without_effect_is_not_verified(self):
        self.fake.write_behaviour = "noop"
        self.write_input(f"SADA-BRG-007,5,{STOCK}\n")
        self.assertEqual(self.run_sync("--apply"), 1)
        [row] = self.report_rows()
        self.assertEqual((row["status"], row["qty_before"], row["qty_after"]),
                         ("FAILED_NOT_VERIFIED", "12", "12"))

    def test_missing_quant_is_created_and_verified(self):
        self.write_input(f"SADA-SLP-033,6,{STOCK}\n")
        self.assertEqual(self.run_sync("--apply"), 0)
        [row] = self.report_rows()
        self.assertEqual((row["status"], row["qty_before"], row["qty_after"]), ("OK", "0", "6"))
        self.assertIn("quant created", row["message"])
        quant_calls = self.fake.methods("stock.quant")
        self.assertEqual(quant_calls.count("create"), 1)
        self.assertLess(quant_calls.index("create"), quant_calls.index("read"))
        [created] = [q for q in self.fake.quants.values() if q["product_id"] == 3]
        self.assertEqual(created["quantity"], 6.0)

    def test_dry_run_previews_repeated_sku_exactly_as_apply(self):
        self.write_input(f"SADA-MOT-021,10,{STOCK}\nSADA-MOT-021,-6,{STOCK}\n"
                         f"SADA-SLP-033,6,{STOCK}\nSADA-SLP-033,2,{STOCK}\n")
        self.assertEqual(self.run_sync(), 0)
        dry = self.report_rows()
        self.assertEqual({r["status"] for r in dry}, {"DRY_RUN_OK"})
        self.assertNotIn("write", self.fake.methods())
        self.assertNotIn("create", self.fake.methods())

        self.fake = standard_fake()          # same starting state, fresh database
        self.assertEqual(self.run_sync("--apply"), 0)
        applied = self.report_rows()
        self.assertEqual({r["status"] for r in applied}, {"OK"})

        columns = lambda rows: [(r["row"], r["qty_before"], r["qty_after"]) for r in rows]
        self.assertEqual(columns(dry), columns(applied))
        self.assertEqual(columns(applied),
                         [("2", "30", "40"), ("3", "40", "34"), ("4", "0", "6"), ("5", "6", "8")])

    def test_already_applied_file_is_refused(self):
        self.write_input(f"SADA-BRG-007,5,{STOCK}\n")
        self.assertEqual(self.run_sync("--apply"), 0)

        self.fake = standard_fake()
        self.constructed.clear()
        self.assertEqual(self.run_sync("--apply"), 2)
        self.assertEqual(self.constructed, [])
        self.assertEqual(self.fake.calls, [])


if __name__ == "__main__":
    unittest.main()
