# Probe findings — measured, not assumed

Target: Odoo Online SaaS database, Inventory app only.
Probes: `odoo_probe.py` (schema + first write), `diag.py` (write-path isolation),
`noquant_test.py` (quant creation + deletion constraints).
Transport: XML-RPC at `/xmlrpc/2/`.

---

## 1. Environment

| Item | Measured value |
|---|---|
| Server version | `saas~19.4+e` (`['saas~19', 4, 0, 'final', 0, 'e']`) |
| Auth | `common.authenticate` returns `uid = 2` using an API key, no password |
| Read access | Works (`res.partner.search_count` → 2) |
| Write access | Works, verified by read-back |
| JSON-2 endpoint | Works — `POST /json/2/res.partner/search_count` with a Bearer API key returns `2` |
| Plan | **Trial** — the server traceback exposes `custom/trial/saas_trial/controllers/main.py` |

### Assumption to declare in the PDF

Odoo's documentation states the External API is available only on Custom pricing
plans, not on One App Free or Standard. This was **contradicted in practice**:
XML-RPC authentication, reads and writes all succeeded on the database above.

However the database is a **trial**, whose entitlements may exceed those of the
eventual One App Free plan. The submission therefore:

- states the discrepancy explicitly rather than claiming free-tier API access,
- keeps the contingency path (emit a validated, field-mapped import file for Ops
  to upload through the Odoo UI) as a first-class part of the design.

### Deprecation note

XML-RPC and JSON-RPC are deprecated in Odoo 19 and scheduled for removal, with
JSON-2 as the replacement. The target database is already on `saas~19.4`, and
JSON-2 was verified working on it. This justifies putting the transport behind a
thin adapter so business logic is unaffected by the migration.

---

## 2. Write-path isolation — the core finding

Three variants were run against the same quant (`id=3`, starting at 7.0):

| Variant | Return value | Actual effect |
|---|---|---|
| `write(inventory_quantity_auto_apply=10)`, **no context** | `True` | **none** — still 7.0 |
| `write(inventory_quantity_auto_apply=10)` + `context={'inventory_mode': True}` | `True` | 7.0 → **10.0** |
| `write(inventory_quantity=12)` + `action_apply_inventory()` + same context | **raises `Fault`** | 10.0 → **12.0** |

Group membership was checked to rule out permissions: the user is in both
`stock.group_stock_user` and `stock.group_stock_manager`. The first variant's
silent no-op was therefore **purely a missing-context problem**, not an access
problem.

### Two rules fall directly out of this

**Rule 1 — `inventory_mode` is mandatory.**
`stock.quant` inventory fields only take effect when the call carries
`context={'inventory_mode': True}`. Without it, `write()` returns `True` and
changes nothing. A silent no-op is worse than an error.

**Rule 2 — the return value is not the success criterion; read-back is.**
Three measured cases, on three different methods, each lying in a different
direction:

| Call | Returned | Actually happened |
|---|---|---|
| `action_apply_inventory` + context | **raised `Fault`** | the write **landed** (10.0 → 12.0) |
| `write(auto_apply)` **without** context | `True` | **nothing** — still 7.0 |
| `create(auto_apply)` **without** context | a **valid quant id** | quant created, quantity stayed **0.0** |

Neither "it threw", nor "it returned True", nor "it returned an id" tells you what
happened. Only re-reading the record does. This is the measured justification for
the guard rule: after any write **or create**, re-read and compare against the
expected value; before retrying a call that errored, re-resolve the record first,
because it may already have been applied — and in the create case a blind retry
would insert a **duplicate quant** rather than repeat a harmless error.

### Correction on `allow_none`

An earlier assumption was that constructing the client proxy with
`xmlrpc.client.ServerProxy(url, allow_none=True)` would fix the
`action_apply_inventory` fault. **It does not.** `diag.py` used exactly that and
still faulted. The traceback shows the marshaller is **server-side**
(`OdooMarshaller(allow_none=False)` in Odoo's own XML-RPC controller), so a
client-side flag has no bearing on it.

Consequence: any Odoo method returning `None` will always fault over XML-RPC.
Either avoid such methods, or use the JSON-2 endpoint.

### Chosen write path

`write(inventory_quantity_auto_apply=<target>)` with
`context={'inventory_mode': True}`.

One call, clean `True`, no `None`-marshalling fault. The two-step path is kept as
the documented rejected alternative, with the fault as the reason.

**`auto_apply` is an absolute counted quantity; the CSV supplies a delta.**
So the operation is read-then-write:

```
current = quant.quantity
target  = current + quantity_change
write(inventory_quantity_auto_apply = target, context={'inventory_mode': True})
verify(read_back.quantity == target)
```

That read-then-write is exactly where idempotency matters: re-running the same
file re-applies the delta on top of the already-updated value.

### When no quant exists yet — measured by `noquant_test.py`

Two throwaway products were created on a clean database and a quant was created
directly for each, with and without the context:

| Variant | `create` returned | Quantity at WH/Stock |
|---|---|---|
| `create(auto_apply=4)` **with** `context={'inventory_mode': True}` | quant id `5` | **4.0** — landed |
| `create(auto_apply=4)` **without** the context | quant id `7` | **0.0** — silent no-op |

So the context is required on **create** exactly as on **write**, and the
context-less create fails in the same success-shaped way: a valid id comes back
and nothing happened.

The successful create also produced a **counterpart quant of `-4.0` at the
`Inventory adjustment` location** (id 11), mirroring what a write produces. A
create through `auto_apply` is therefore a genuine two-sided inventory
adjustment, not a raw row insert — which is why it is safe to use it for rows
whose (product, location) pair has no stock line yet.

**Consequence for the script:** a missing quant is not an error and does not need
holding for manual handling. Treat the current quantity as `0`, compute
`target = 0 + delta`, create with the context, then read back and verify on
exactly the same path as an update.

---

## 3. Schema — `stock.quant`

| Field | Type | Writable |
|---|---|---|
| `quantity` | float | **no** (computed) |
| `inventory_quantity` | float | yes — counted qty, needs `action_apply_inventory` |
| `inventory_quantity_auto_apply` | float | yes — counted qty, applies immediately |
| `inventory_diff_quantity` | float | no |
| `available_quantity` | float | no |
| `product_id` | many2one | yes |
| `location_id` | many2one | yes |
| `lot_id` | many2one | yes |
| `inventory_date` | date | yes |

Observed during the two-step run: after writing `inventory_quantity = 12` on a
quant holding 10, `inventory_diff_quantity` read back as `2.0`; after applying,
`inventory_quantity` reset to `0.0` and `quantity` became `12.0`.

---

## 4. Schema — `product.product`

| Field | Type | Values |
|---|---|---|
| `default_code` | char | the SKU anchor — match `material_sku` against this. **Not unique** |
| `tracking` | selection | `none` (By Quantity), `lot` (By Lots), `serial` (By Unique Serial Number) |
| `type` | selection | `consu` (Goods), `service`, `combo` |
| `is_storable` | boolean | storability lives here now |
| `uom_id` | many2one | |
| `active` | boolean | |

**`default_code` is not unique — measured, not theorised.** Odoo enforces no
constraint on it, and the probe runs proved it: two separate products, ids `1`
and `2`, both carried `ZZ-API-PROBE`. A SKU lookup therefore returns a *list*,
and resolving an ambiguous match by taking the first hit would write a stock
adjustment to an arbitrary part. This is the measured reason the script treats
"matches more than one product" as a skip with the ids recorded in the report,
and leaves the fix with Procurement, who own master data.

**Trap:** in Odoo 19 the value `'product'` no longer exists in `type`. A model
trained on older Odoo will very likely emit `{'type': 'product'}`. Creating a
storable good requires `{'type': 'consu', 'is_storable': True}`. The system
prompt must forbid the old value explicitly.

**Lot/serial:** `tracking` really does expose `lot`/`serial`. Space-grade
components are typically serialised, and a tracked product's quant cannot be
adjusted without a `lot_id`. v1 processes untracked products only; tracked rows
are held with status `HELD_LOT_TRACKED` for manual handling.

---

## 5. Locations

Only one internal location exists on a fresh database:

```
id=5   WH/Stock
```

`complete_name` has **no company prefix** here. Resolve the CSV `location` column
against `stock.location.complete_name` with `usage = 'internal'`. Unresolved
locations are skipped, never created.

---

## 6. Rate limiting

Odoo Cloud's acceptable use policy describes roughly one call per second with no
parallel calls as acceptable. The read-then-write-then-verify pattern costs three
calls per row, so a 500-row file takes on the order of 25 minutes. The script
must throttle, and the user guide must state that the slowness is intentional so
nobody interrupts a half-finished run.

---

## 7. Nothing can be deleted over the API

Cleaning up after `noquant_test.py` measured a constraint that matters for
rollback:

| Attempt | Result |
|---|---|
| `stock.quant.unlink`, with `inventory_mode` context | `Fault: Contact your administrator to request access if necessary.` |
| `product.product.unlink` | `Fault: ... is referenced from table "stock_move"` / `"stock_quant"` |
| `stock.quant.write(auto_apply=0)` + context | **works** — quantity zeroed |
| `product.product.write(active=False)` | **works** — product archived |

The user holds `stock.group_stock_manager`, so this is not a permissions gap to
be fixed by granting more rights; quant deletion is simply not exposed. Any
product with inventory history is likewise pinned by foreign keys.

**Consequence for rollback:** rollback is a **reverse adjustment, not a
deletion**. Undoing a migration means writing the prior quantity back through
the same `auto_apply` path — which is exactly why the sync report records
`qty_before` for every row it touched. There is no "undo" to call, and the
audit trail of both the original adjustment and its reversal stays in Odoo,
which for space-grade parts is the desirable behaviour rather than a limitation.

---

## 8. Still unmeasured

- The **minimum** group required for inventory writes. The probe user is admin
  and holds both `stock.group_stock_user` and `stock.group_stock_manager`, so the
  floor was not isolated. Any least-privilege claim in the PDF must stay at the
  level of "a dedicated non-admin service user with Inventory rights", not a
  specific group, unless this gets tested.
- Whether JSON-2 tolerates `None` returns where XML-RPC does not. Relevant only
  if the two-step path is ever revisited.

Closed since the first draft: `auto_apply` behaviour when no quant exists is now
measured (section 2), as is the unavailability of deletion (section 7).