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
    r = _sb().table("products").select("*").eq("pid", pid).maybe_single().execute()
    return _fix_product(r.data) if r.data else None


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
