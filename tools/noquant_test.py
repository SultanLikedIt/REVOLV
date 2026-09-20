"""
THROWAWAY probe. Measures probe_findings.md section 7, open item 2:

  When NO stock.quant exists yet for a (product, location) pair, can a quant be
  created directly with inventory_quantity_auto_apply, and does the quantity
  actually land? Is context={'inventory_mode': True} required on CREATE as well
  as on WRITE?

Variant A: create WITH    context inventory_mode
Variant B: create WITHOUT context inventory_mode

Creates two throwaway products, measures, then cleans up after itself.
Also removes the ZZ-API-PROBE leftovers from the earlier probe runs.

Run:  python3 noquant_test.py
Stdlib only. Not a deliverable -- delete once the finding is written up.
"""

import os
import sys
import xmlrpc.client

TARGET_QTY = 4


# --- .env loader (same shape as diag.py) ------------------------------
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
def load_env(path=os.path.join(REPO_ROOT, ".env")):
    if not os.path.exists(path):
        sys.exit(f"{path} bulunamadi. .env.example'i kopyalayip doldur.")
    with open(path, encoding="utf-8") as fh:
        for raw in fh:
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            os.environ.setdefault(key.strip(), val.strip().strip("\"'"))


load_env()

URL = os.environ.get("ODOO_URL", "").rstrip("/")
DB = os.environ.get("ODOO_DB", "")
USER = os.environ.get("ODOO_USER", "")
KEY = os.environ.get("ODOO_API_KEY", "")

missing = [k for k, v in
           [("ODOO_URL", URL), ("ODOO_DB", DB), ("ODOO_USER", USER), ("ODOO_API_KEY", KEY)]
           if not v]
if missing:
    sys.exit("Eksik .env degiskenleri: " + ", ".join(missing))

common = xmlrpc.client.ServerProxy(f"{URL}/xmlrpc/2/common", allow_none=True)
uid = common.authenticate(DB, USER, KEY, {})
if not uid:
    sys.exit("AUTH FAILED")

models = xmlrpc.client.ServerProxy(f"{URL}/xmlrpc/2/object", allow_none=True)
print(f"uid={uid}  db={DB}\n")


def call(model, method, args, kwargs=None):
    return models.execute_kw(DB, uid, KEY, model, method, args, kwargs or {})


CTX = {"context": {"inventory_mode": True}}
QUANT_FIELDS = ["id", "location_id", "quantity", "inventory_quantity",
                "inventory_diff_quantity"]


def quants_of(product_id, internal_only=False):
    domain = [("product_id", "=", product_id)]
    if internal_only:
        domain.append(("location_id.usage", "=", "internal"))
    return call("stock.quant", "search_read", [domain], {"fields": QUANT_FIELDS})


def short(exc):
    text = str(exc)
    if isinstance(exc, xmlrpc.client.Fault):
        text = exc.faultString.strip().splitlines()[-1]
    return f"{type(exc).__name__}: {text[:200]}"


# ----------------------------------------------------------------------
# Locate WH/Stock
# ----------------------------------------------------------------------
locs = call("stock.location", "search_read",
            [[("complete_name", "=", "WH/Stock"), ("usage", "=", "internal")]],
            {"fields": ["id", "complete_name"]})
if len(locs) != 1:
    sys.exit(f"expected exactly one internal WH/Stock, got {locs}")
STOCK_ID = locs[0]["id"]
print(f"WH/Stock location id = {STOCK_ID}\n")

created_products = []   # cleaned up in the finally block


def run_variant(label, sku, name, use_context):
    """Create a fresh product, verify it has no quant, then create one."""
    print("=" * 70)
    print(f"{label}  create stock.quant {'WITH' if use_context else 'WITHOUT'} "
          f"context inventory_mode")
    print("=" * 70)

    # 1. fresh product. Odoo 19: storable goods are consu + is_storable.
    pid = call("product.product", "create", [{
        "name": name,
        "default_code": sku,
        "type": "consu",
        "is_storable": True,
    }])
    created_products.append(pid)
    print(f"  1. product created           id={pid}  default_code={sku}")

    # 2. confirm the (product, location) pair genuinely has no quant
    existing = quants_of(pid)
    print(f"  2. quants before             {existing}")
    if existing:
        print("     !! not a clean starting point; the measurement is void")
        return None

    # 3. create the quant
    values = {
        "product_id": pid,
        "location_id": STOCK_ID,
        "inventory_quantity_auto_apply": TARGET_QTY,
    }
    kwargs = CTX if use_context else {}
    try:
        qid = call("stock.quant", "create", [values], kwargs)
        print(f"  3. create returned           quant id={qid}")
    except Exception as exc:
        print(f"  3. create RAISED             {short(exc)}")
        print(f"\n  VERDICT {label}: FAIL - create raised, no quant written\n")
        return False

    # 4. read back -- the only success criterion
    after = quants_of(pid)
    print("  4. quants after:")
    for quant in after:
        print(f"       {quant}")

    internal = [q for q in after
                if q["id"] == qid or q["location_id"][0] == STOCK_ID]
    landed = next((q["quantity"] for q in internal if q["location_id"][0] == STOCK_ID), None)
    print(f"  5. quantity at WH/Stock      {landed!r}  (expected {TARGET_QTY})")

    ok = landed == float(TARGET_QTY)
    print(f"\n  VERDICT {label}: {'PASS' if ok else 'FAIL'} - "
          f"quantity {'landed' if ok else 'did NOT land'} on create"
          f"{'' if use_context else ' without inventory_mode'}\n")
    return ok


def purge_product(pid, tag):
    """Zero the stock, unlink the quants, then unlink or archive the product."""
    try:
        quants = quants_of(pid)
    except Exception as exc:
        print(f"    {tag} id={pid}: cannot read quants ({short(exc)})")
        return

    # Measured: this server refuses stock.quant.unlink over the API even with
    # inventory_mode and stock_manager rights. Zeroing the counted quantity is
    # the supported way to neutralise the stock, so try unlink, then fall back.
    for quant in quants:
        try:
            call("stock.quant", "unlink", [[quant["id"]]], CTX)
            print(f"    {tag} id={pid}: quant {quant['id']} deleted")
            continue
        except Exception as exc:
            print(f"    {tag} id={pid}: quant {quant['id']} not deletable ({short(exc)})")

        if quant["location_id"][1] == "WH/Stock" and quant["quantity"]:
            try:
                call("stock.quant", "write",
                     [[quant["id"]], {"inventory_quantity_auto_apply": 0}], CTX)
                print(f"    {tag} id={pid}: quant {quant['id']} zeroed instead")
            except Exception as exc:
                print(f"    {tag} id={pid}: quant {quant['id']} NOT zeroed ({short(exc)})")

    try:
        call("product.product", "unlink", [[pid]])
        print(f"    {tag} id={pid}: product DELETED")
        return
    except Exception as exc:
        print(f"    {tag} id={pid}: delete refused ({short(exc)})")

    try:
        call("product.product", "write", [[pid], {"active": False}])
        print(f"    {tag} id={pid}: product ARCHIVED instead")
    except Exception as exc:
        print(f"    {tag} id={pid}: archive also failed ({short(exc)})")


results = {}
try:
    results["A"] = run_variant("A", "ZZ-NOQUANT", "ZZ NOQUANT", use_context=True)
    results["B"] = run_variant("B", "ZZ-NOQUANT-2", "ZZ NOQUANT 2", use_context=False)
finally:
    print("=" * 70)
    print("CLEANUP")
    print("=" * 70)

    print("  throwaway products from this run:")
    for pid in created_products:
        purge_product(pid, "test")

    print("  ZZ-API-PROBE leftovers from the earlier probe runs:")
    try:
        stale = call("product.product", "search_read",
                     [[("default_code", "=", "ZZ-API-PROBE")]],
                     {"fields": ["id", "default_code"], "context": {"active_test": False}})
        if not stale:
            print("    none found")
        for product in stale:
            purge_product(product["id"], "stale")
    except Exception as exc:
        print(f"    lookup failed ({short(exc)})")

print()
print("=" * 70)
print("SUMMARY")
print("=" * 70)
print(f"  A  create WITH    inventory_mode : "
      f"{'PASS' if results.get('A') else 'FAIL' if results.get('A') is False else 'VOID'}")
print(f"  B  create WITHOUT inventory_mode : "
      f"{'PASS' if results.get('B') else 'FAIL' if results.get('B') is False else 'VOID'}")
print()
if results.get("A") and not results.get("B"):
    print("  => context IS required on create, exactly as on write.")
elif results.get("A") and results.get("B"):
    print("  => context is NOT required on create; it is required only on write.")
elif not results.get("A"):
    print("  => creating a quant directly does not work; such rows must be held for manual entry.")
