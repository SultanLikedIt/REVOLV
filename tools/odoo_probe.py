"""
Odoo Online (SaaS) External API probe.
Amac: free tier'da XML-RPC gercekten calisiyor mu, hangi alanlar mevcut,
yazma izni var mi -> hepsini olc.

Calistir:  python3 odoo_probe.py
Bagimlilik yok (stdlib).

Kimlik bilgileri koda gomulmez. Repo kokundeki .env dosyasindan ya da
ortam degiskenlerinden okunur:  ODOO_URL, ODOO_DB, ODOO_USER, ODOO_API_KEY
Sablon icin bkz. .env.example
"""

import os
import sys
import xmlrpc.client
from pathlib import Path

# ----------------------------------------------------------------------
RUN_WRITE_TEST = True   # False yaparsan sadece okuma testleri calisir

REQUIRED_VARS = ("ODOO_URL", "ODOO_DB", "ODOO_USER", "ODOO_API_KEY")
ENV_PATH = Path(__file__).resolve().parent.parent / ".env"   # repo root, not tools/
# ----------------------------------------------------------------------


def parse_env_file(path):
    """Minimal .env parser (stdlib only, python-dotenv yok).

    Destekler: KEY=VALUE, yorum satirlari (#), bos satirlar, istege bagli
    'export ' oneki, deger etrafinda tek/cift tirnak. Tirnaksiz degerlerde
    bastaki/sondaki bosluk kirpilir.
    """
    values = {}
    if not path.exists():
        return values

    for lineno, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        if "=" not in line:
            print(f"   [UYARI] .env satir {lineno} atlandi ('=' yok): {raw!r}")
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        if key:
            values[key] = value
    return values


def load_config():
    """Ortam degiskenleri .env dosyasini ezer. Eksik olan varsa net hata ver."""
    file_values = parse_env_file(ENV_PATH)
    config = {}
    for name in REQUIRED_VARS:
        value = os.environ.get(name) or file_values.get(name, "")
        config[name] = value.strip()

    missing = [name for name in REQUIRED_VARS if not config[name]]
    if missing:
        where = ENV_PATH if ENV_PATH.exists() else f"{ENV_PATH} (dosya yok)"
        sys.exit(
            "KONFIGURASYON HATASI: su degiskenler eksik veya bos -> "
            + ", ".join(missing)
            + f"\n  Arandigi yer : {where}\n"
            "  Cozum        : .env.example dosyasini .env olarak kopyala ve doldur\n"
            "                 (cp .env.example .env), ya da ayni isimli ortam\n"
            "                 degiskenlerini export et.\n"
            "  Not          : ODOO_API_KEY = Odoo > My Profile > Account Security\n"
            "                 > New API Key. Parola DEGIL."
        )
    return config


_cfg = load_config()
URL = _cfg["ODOO_URL"].rstrip("/")
DB = _cfg["ODOO_DB"]
USER = _cfg["ODOO_USER"]
KEY = _cfg["ODOO_API_KEY"]


def hr(t):
    print("\n" + "=" * 62)
    print(t)
    print("=" * 62)


# --- 1) BAGLANTI + VERSIYON -------------------------------------------
hr("1) VERSION")
common = xmlrpc.client.ServerProxy(f"{URL}/xmlrpc/2/common")
try:
    v = common.version()
    for k in sorted(v):
        print(f"   {k}: {v[k]}")
except Exception as e:
    sys.exit(f"   BAGLANTI HATASI: {type(e).__name__}: {e}")


# --- 2) AUTH ----------------------------------------------------------
hr("2) AUTH")
try:
    uid = common.authenticate(DB, USER, KEY, {})
except Exception as e:
    sys.exit(f"   AUTH EXCEPTION: {type(e).__name__}: {e}")

print(f"   uid = {uid}")
if not uid:
    sys.exit("   AUTH FAILED -> DB adi / login / API key yanlis, YA DA plan API'yi kapatiyor.")

models = xmlrpc.client.ServerProxy(f"{URL}/xmlrpc/2/object")


def call(model, method, args, kwargs=None):
    return models.execute_kw(DB, uid, KEY, model, method, args, kwargs or {})


# --- 3) READ TESTI ----------------------------------------------------
hr("3) READ")
try:
    print("   res.partner count :", call("res.partner", "search_count", [[]]))
    print("   product.product   :", call("product.product", "search_count", [[]]))
    print("   stock.quant       :", call("stock.quant", "search_count", [[]]))
except Exception as e:
    print(f"   READ HATASI: {type(e).__name__}: {e}")
    print("   -> 'stock.quant does not exist' ise Inventory app kurulu degil.")


# --- 4) SEMA: stock.quant --------------------------------------------
hr("4) stock.quant ALANLARI")
qf = call("stock.quant", "fields_get", [], {"attributes": ["string", "type", "readonly"]})
for f in ["quantity", "inventory_quantity", "inventory_quantity_auto_apply",
          "inventory_diff_quantity", "available_quantity",
          "product_id", "location_id", "lot_id", "inventory_date"]:
    meta = qf.get(f)
    if meta:
        print(f"   {f:32} type={meta.get('type'):12} readonly={meta.get('readonly')}")
    else:
        print(f"   {f:32} --- YOK ---")


# --- 5) SEMA: product.product ----------------------------------------
hr("5) product.product ALANLARI")
pf = call("product.product", "fields_get", [], {"attributes": ["string", "type", "selection"]})
for f in ["default_code", "tracking", "type", "is_storable", "uom_id", "active"]:
    meta = pf.get(f)
    if meta:
        extra = f"  secenekler={meta.get('selection')}" if meta.get("selection") else ""
        print(f"   {f:20} type={meta.get('type'):12}{extra}")
    else:
        print(f"   {f:20} --- YOK ---")


# --- 6) LOKASYONLAR ---------------------------------------------------
hr("6) INTERNAL LOKASYONLAR (complete_name formati)")
locs = call("stock.location", "search_read",
            [[["usage", "=", "internal"]]],
            {"fields": ["complete_name", "usage"], "limit": 25})
for l in locs:
    print(f"   id={l['id']:<5} {l['complete_name']}")
if not locs:
    print("   HIC YOK -> Inventory kurulu degil veya lokasyon tanimli degil.")


# --- 7) WRITE TESTI ---------------------------------------------------
if not RUN_WRITE_TEST:
    print("\n(write testi kapali)")
    sys.exit(0)

hr("7) WRITE TESTI")
if not locs:
    sys.exit("   Lokasyon yok, write testi atlandi.")

loc_id = locs[0]["id"]
print(f"   hedef lokasyon: {locs[0]['complete_name']} (id={loc_id})")

# 7a) test urunu olustur  (Odoo 18+ 'is_storable', oncesi 'type':'product')
payload = {"name": "ZZ API PROBE", "default_code": "ZZ-API-PROBE"}
if "is_storable" in pf:
    payload["is_storable"] = True
else:
    payload["type"] = "product"

try:
    pid = call("product.product", "create", [payload])
    print(f"   [OK] urun olusturuldu id={pid}")
except Exception as e:
    sys.exit(f"   [FAIL] CREATE reddedildi: {type(e).__name__}: {e}\n"
             "   -> Yazma izni yok. Plan kisiti olabilir. Contingency plani devreye girer.")

# 7b) quant olustur + uygula
try:
    qid = call("stock.quant", "create",
               [{"product_id": pid, "location_id": loc_id, "inventory_quantity": 7}])
    print(f"   [OK] quant olusturuldu id={qid}")

    call("stock.quant", "action_apply_inventory", [[qid]])
    print("   [OK] action_apply_inventory calisti")
except Exception as e:
    print(f"   [FAIL] quant/apply: {type(e).__name__}: {e}")
    print("   -> inventory_quantity_auto_apply alani varsa onu dene:")
    print("      stock.quant.create({... 'inventory_quantity_auto_apply': 7})")

# 7c) geri oku -> gercekten oturdu mu
try:
    back = call("stock.quant", "search_read",
                [[["product_id", "=", pid], ["location_id", "=", loc_id]]],
                {"fields": ["quantity", "inventory_quantity", "available_quantity"]})
    print(f"   GERI OKUMA: {back}")
    if back and back[0].get("quantity") == 7:
        print("   [OK] miktar gercekten yazildi. API tam calisiyor.")
    else:
        print("   [DIKKAT] quantity beklenen 7 degil -> apply oturmamis.")
except Exception as e:
    print(f"   [FAIL] geri okuma: {type(e).__name__}: {e}")

print("\n   TEMIZLIK: Odoo UI'dan 'ZZ API PROBE' urununu arsivle/sil.")

q = call("stock.quant", "search_read", [[["product_id", "=", pid]]], {"fields": ["quantity"]})[0]
call("stock.quant", "write", [[q["id"]], {"inventory_quantity_auto_apply": q["quantity"] + 3}])
print("AUTO_APPLY:", call("stock.quant", "read", [[q["id"]]], {"fields": ["quantity"]}))