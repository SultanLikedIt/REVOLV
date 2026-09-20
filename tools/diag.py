"""
stock.quant yazma yolu teshisi.

Amac: inventory alanlarini yazmanin hangi yolu GERCEKTEN etki ediyor?
  A) inventory_quantity_auto_apply + context inventory_mode
  B) inventory_quantity + action_apply_inventory + context inventory_mode
  C) servis kullanicisinin stock manager yetkisi var mi

Calistir:  python3 diag.py
Bagimlilik yok (stdlib).
"""

import os
import sys
import xmlrpc.client

# --- .env yukleyici (stdlib) ------------------------------------------
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

URL  = os.environ.get("ODOO_URL", "").rstrip("/")
DB   = os.environ.get("ODOO_DB", "")
USER = os.environ.get("ODOO_USER", "")
KEY  = os.environ.get("ODOO_API_KEY", "")

missing = [k for k, v in
           [("ODOO_URL", URL), ("ODOO_DB", DB), ("ODOO_USER", USER), ("ODOO_API_KEY", KEY)]
           if not v]
if missing:
    sys.exit("Eksik .env degiskenleri: " + ", ".join(missing))

QID = int(os.environ.get("PROBE_QUANT_ID", "3"))   # probe ciktisindaki quant id

# --- baglanti ---------------------------------------------------------
common = xmlrpc.client.ServerProxy(f"{URL}/xmlrpc/2/common", allow_none=True)
uid = common.authenticate(DB, USER, KEY, {})
if not uid:
    sys.exit("AUTH FAILED")
print(f"uid={uid}  quant_id={QID}\n")

models = xmlrpc.client.ServerProxy(f"{URL}/xmlrpc/2/object", allow_none=True)


def call(model, method, args, kwargs=None):
    return models.execute_kw(DB, uid, KEY, model, method, args, kwargs or {})


def show(tag):
    r = call("stock.quant", "read", [[QID]],
             {"fields": ["quantity", "inventory_quantity", "inventory_diff_quantity"]})
    print(f"   {tag:14} {r}")


CTX = {"context": {"inventory_mode": True}}

show("BASLANGIC")

# --- A) auto_apply + inventory_mode -----------------------------------
print("\nA) inventory_quantity_auto_apply = 10  (inventory_mode=True)")
try:
    print("   write ->", call("stock.quant", "write",
                              [[QID], {"inventory_quantity_auto_apply": 10}], CTX))
except Exception as e:
    print("   HATA:", type(e).__name__, str(e)[:300])
show("SONRA")

# --- B) iki adimli + inventory_mode -----------------------------------
print("\nB) inventory_quantity = 12 + action_apply_inventory  (inventory_mode=True)")
try:
    print("   write ->", call("stock.quant", "write",
                              [[QID], {"inventory_quantity": 12}], CTX))
    show("APPLY ONCESI")
    print("   apply ->", call("stock.quant", "action_apply_inventory", [[QID]], CTX))
except Exception as e:
    print("   HATA:", type(e).__name__, str(e)[:300])
show("SONRA")

# --- C) yetki ---------------------------------------------------------
print("\nC) yetki")
for grp in ["stock.group_stock_user", "stock.group_stock_manager"]:
    try:
        print(f"   {grp:32} ->", call("res.users", "has_group", [[uid], grp]))
    except Exception as e:
        print(f"   {grp:32} -> sorgulanamadi ({type(e).__name__})")

print("\nBEKLENEN: A veya B'den SONRA satirinda quantity degismis olmali.")
print("Ikisi de degismediyse yetki/context sorunu, C ciktisina bak.")