import os
from functools import lru_cache
from supabase import create_client, Client


@lru_cache(maxsize=1)
def _sb() -> Client:
    url = os.environ["SUPABASE_URL"]
    key = os.environ["SUPABASE_KEY"]
    return create_client(url, key)


# ==================== USERS ====================

def get_all_users() -> dict:
    rows = _sb().table("users").select("*").execute().data or []
    return {
        r["username"]: {
            "senha": r["senha"],
            "tipo": r["tipo"],
            "permissoes": list(r.get("permissoes") or []),
        }
        for r in rows
    }


def get_user(username: str) -> dict | None:
    r = _sb().table("users").select("*").eq("username", username).maybe_single().execute()
    if not r.data:
        return None
    d = r.data
    return {
        "senha": d["senha"],
        "tipo": d["tipo"],
        "permissoes": list(d.get("permissoes") or []),
    }


def update_user_permissions(username: str, permissoes: list):
    _sb().table("users").update({"permissoes": permissoes}).eq("username", username).execute()


# ==================== PRODUCTS ====================

def _fix_product(p: dict) -> dict:
    if p and not isinstance(p.get("lotes"), list):
        p["lotes"] = []
    return p


def get_all_products() -> list:
    rows = _sb().table("products").select("*").execute().data or []
    return [_fix_product(p) for p in rows]


def get_product(pid: str) -> dict | None:
    rows = _sb().table("products").select("*").eq("pid", pid).limit(1).execute().data or []
    return _fix_product(rows[0]) if rows else None


def insert_product(product: dict):
    _sb().table("products").insert(product).execute()


def update_product(product: dict):
    _sb().table("products").update(product).eq("pid", product["pid"]).execute()


def delete_product(pid: str):
    _sb().table("products").delete().eq("pid", pid).execute()


# ==================== SALES ====================

def _fix_sale(s: dict) -> dict:
    if s and not isinstance(s.get("consumo_lotes"), list):
        s["consumo_lotes"] = []
    return s


def get_all_sales() -> list:
    rows = _sb().table("sales").select("*").execute().data or []
    return [_fix_sale(s) for s in rows]


def get_sale(uid: str) -> dict | None:
    r = _sb().table("sales").select("*").eq("uid", uid).maybe_single().execute()
    return _fix_sale(r.data) if r.data else None


def get_sales_by_uids(uids: list) -> list:
    if not uids:
        return []
    rows = _sb().table("sales").select("*").in_("uid", uids).execute().data or []
    return [_fix_sale(s) for s in rows]


def get_sales_open_by_date(data: str) -> list:
    rows = (
        _sb().table("sales").select("*")
        .eq("data", data)
        .eq("fechado", False)
        .execute().data or []
    )
    return [_fix_sale(s) for s in rows]


def get_sales_closed_by_caixa_dia(dia: str) -> list:
    rows = (
        _sb().table("sales").select("*")
        .eq("caixa_dia", dia)
        .eq("fechado", True)
        .execute().data or []
    )
    return [_fix_sale(s) for s in rows]


def insert_sale(sale: dict):
    _sb().table("sales").insert(sale).execute()


def update_sale(sale: dict):
    _sb().table("sales").update(sale).eq("uid", sale["uid"]).execute()


def delete_sale(uid: str):
    _sb().table("sales").delete().eq("uid", uid).execute()


def mark_sales_fechado(uids: list, caixa_dia: str, fechado_em: str):
    if not uids:
        return
    _sb().table("sales").update({
        "fechado": True,
        "caixa_dia": caixa_dia,
        "fechado_em": fechado_em,
    }).in_("uid", uids).execute()


# ==================== COMMANDS ====================

def _fix_command(c: dict) -> dict:
    if c and not isinstance(c.get("itens"), list):
        c["itens"] = []
    return c


def get_all_commands() -> list:
    rows = _sb().table("commands").select("*").execute().data or []
    return [_fix_command(c) for c in rows]


def get_command(cid: str) -> dict | None:
    r = _sb().table("commands").select("*").eq("cid", cid).maybe_single().execute()
    return _fix_command(r.data) if r.data else None


def insert_command(command: dict):
    _sb().table("commands").insert(command).execute()


def update_command(command: dict):
    _sb().table("commands").update(command).eq("cid", command["cid"]).execute()


# ==================== CASH REGISTERS ====================

def _fix_caixa(r: dict) -> dict:
    if r and not isinstance(r.get("vendas_uids"), list):
        r["vendas_uids"] = []
    return r


def get_all_cash_registers() -> list:
    rows = _sb().table("cash_registers").select("*").execute().data or []
    return [_fix_caixa(r) for r in rows]


def get_cash_register_by_dia(dia: str) -> dict | None:
    rows = (
        _sb().table("cash_registers").select("*")
        .eq("dia", dia)
        .execute().data or []
    )
    return _fix_caixa(rows[-1]) if rows else None


def insert_cash_register(registro: dict):
    _sb().table("cash_registers").insert(registro).execute()


# ==================== BAR: PRODUCTS ====================

def bar_get_all_products() -> list:
    return _sb().table("bar_products").select("*").eq("active", True).order("name").execute().data or []


def bar_get_product(pid: str) -> dict | None:
    rows = _sb().table("bar_products").select("*").eq("id", pid).limit(1).execute().data or []
    return rows[0] if rows else None


def bar_insert_product(p: dict) -> str:
    r = _sb().table("bar_products").insert(p).execute()
    return r.data[0]["id"] if r.data else ""


def bar_get_simple_products() -> list:
    return _sb().table("bar_products").select("*").eq("active", True).eq("type", "simple").order("name").execute().data or []


def bar_update_product(pid: str, data: dict):
    _sb().table("bar_products").update(data).eq("id", pid).execute()


def bar_delete_product(pid: str):
    _sb().table("bar_products").update({"active": False}).eq("id", pid).execute()


# ==================== BAR: STOCK BATCHES ====================

def bar_get_batches(product_id: str) -> list:
    return (
        _sb().table("bar_stock_batches").select("*")
        .eq("product_id", product_id)
        .order("created_at")
        .execute().data or []
    )


def bar_get_available_batches(product_id: str) -> list:
    rows = (
        _sb().table("bar_stock_batches").select("*")
        .eq("product_id", product_id)
        .gt("quantity_remaining", 0)
        .order("created_at")
        .execute().data or []
    )
    return rows


def bar_insert_batch(batch: dict):
    _sb().table("bar_stock_batches").insert(batch).execute()


def bar_update_batch_remaining(batch_id: str, remaining: float):
    _sb().table("bar_stock_batches").update({"quantity_remaining": remaining}).eq("id", batch_id).execute()


def bar_get_stock_summary() -> dict:
    batches = _sb().table("bar_stock_batches").select("product_id, quantity_remaining").gt("quantity_remaining", 0).execute().data or []
    totals: dict = {}
    for b in batches:
        pid = b["product_id"]
        totals[pid] = totals.get(pid, 0) + float(b["quantity_remaining"])
    return totals


# ==================== BAR: PRODUCT COMPONENTS ====================

def bar_get_components(product_id: str) -> list:
    rows = (
        _sb().table("bar_product_components")
        .select("*")
        .eq("product_id", product_id)
        .execute().data or []
    )
    result = []
    for row in rows:
        cid = row.get("component_id", "")
        # component_id é UUID (com hífens), mas products.pid é TEXT hex sem hífens
        prod = get_product(cid.replace("-", ""))
        if prod is None:
            continue
        row["component"] = {"id": prod["pid"], "name": prod["nome"], "unit": prod.get("unit", "")}
        result.append(row)
    return result


def bar_insert_component(comp: dict):
    _sb().table("bar_product_components").insert(comp).execute()


def bar_delete_component(comp_id: str):
    _sb().table("bar_product_components").delete().eq("id", comp_id).execute()


def bar_delete_all_components(product_id: str):
    _sb().table("bar_product_components").delete().eq("product_id", product_id).execute()


def bar_get_packages_using_product(product_pid: str) -> list:
    """Returns all bar composite products that use product_pid as a component."""
    h = product_pid.replace("-", "")
    uuid_cid = f"{h[:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:]}" if len(h) == 32 else product_pid
    rows = (
        _sb().table("bar_product_components")
        .select("*")
        .eq("component_id", uuid_cid)
        .execute().data or []
    )
    result = []
    for row in rows:
        bar_prod = bar_get_product(row.get("product_id", ""))
        if bar_prod:
            row["pacote_nome"] = bar_prod.get("name", "")
            row["pacote_id"]   = bar_prod.get("id", "")
            result.append(row)
    return result


# ==================== BAR: SALES ====================

def bar_insert_sale(sale: dict) -> dict:
    r = _sb().table("bar_sales").insert(sale).select("*").execute()
    return r.data[0] if r.data else {}


def bar_insert_sale_items(items: list):
    if items:
        _sb().table("bar_sale_items").insert(items).execute()


def bar_get_all_sales() -> list:
    return (
        _sb().table("bar_sales").select("*")
        .order("created_at", desc=True)
        .execute().data or []
    )


def bar_get_sale_items(sale_id: str) -> list:
    return (
        _sb().table("bar_sale_items")
        .select("*, product:product_id(name, unit)")
        .eq("sale_id", sale_id)
        .execute().data or []
    )
