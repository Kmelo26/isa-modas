import os
import time
import secrets
import string
from werkzeug.security import generate_password_hash, check_password_hash
from supabase import create_client, Client

_client: Client | None = None
_client_ts: float = 0
_CLIENT_TTL = 270  # recria a cada 4,5 min — evita idle > 5 min sem overhead por request


def _sb() -> Client:
    global _client, _client_ts
    now = time.monotonic()
    if _client is None or (now - _client_ts) > _CLIENT_TTL:
        _client = create_client(
            os.environ["SUPABASE_URL"],
            os.environ["SUPABASE_KEY"],
        )
        _client_ts = now
    return _client


def _reset_client():
    global _client, _client_ts
    _client = None
    _client_ts = 0
    try:
        from flask import g
        if hasattr(g, "_supabase"):
            del g._supabase
    except RuntimeError:
        pass


# ==================== USERS ====================

def hash_senha(txt: str) -> str:
    return generate_password_hash(txt)


def verificar_senha(hash_: str, txt: str) -> bool:
    if not hash_:
        return False
    try:
        if check_password_hash(hash_, txt):
            return True
    except Exception:
        pass
    # Ponte pra contas que ainda não passaram pelo backfill (senha em texto puro).
    return hash_ == txt


def hash_pin(txt: str) -> str:
    return generate_password_hash(txt)


def verificar_pin(hash_: str, txt: str) -> bool:
    if not hash_ or not txt:
        return False
    try:
        return check_password_hash(hash_, txt)
    except Exception:
        return False


def _fix_user(d: dict) -> dict:
    return {
        "senha": d["senha"],
        "tipo": d["tipo"],
        "cargo": d.get("cargo") or "funcionario",
        "nome": d.get("nome") or "",
        "permissoes": list(d.get("permissoes") or []),
        "ativo": (d.get("ativo") is not False),
        "arquivado": bool(d.get("arquivado")),
        "pin_hash": d.get("pin_hash") or "",
        "data_criacao": d.get("data_criacao") or "",
        "ultimo_login": d.get("ultimo_login") or "",
    }


def get_all_users() -> dict:
    rows = _sb().table("users").select("*").execute().data or []
    return {r["username"]: _fix_user(r) for r in rows}


def get_user(username: str) -> dict | None:
    rows = _sb().table("users").select("*").eq("username", username).limit(1).execute().data or []
    if not rows:
        return None
    return _fix_user(rows[0])


def insert_user(dados: dict):
    try:
        _sb().table("users").insert(dados).execute()
    except Exception as e:
        raise RuntimeError(f"Erro ao criar usuário: {e}") from e


def update_user_permissions(username: str, permissoes: list):
    _sb().table("users").update({"permissoes": permissoes}).eq("username", username).execute()


def update_user_cargo(username: str, cargo: str):
    _sb().table("users").update({"cargo": cargo}).eq("username", username).execute()


def set_user_ativo(username: str, ativo: bool):
    _sb().table("users").update({"ativo": ativo}).eq("username", username).execute()


def set_user_arquivado(username: str, arquivado: bool):
    _sb().table("users").update({"arquivado": arquivado}).eq("username", username).execute()


def update_user_senha(username: str, senha_hash: str):
    _sb().table("users").update({"senha": senha_hash}).eq("username", username).execute()


def update_user_pin(username: str, pin_hash: str):
    _sb().table("users").update({"pin_hash": pin_hash}).eq("username", username).execute()


def update_user_ultimo_login(username: str, quando: str):
    _sb().table("users").update({"ultimo_login": quando}).eq("username", username).execute()


# ==================== SESSIONS ====================

def criar_sessao(username: str, quando: str) -> str:
    token = secrets.token_hex(32)
    _sb().table("user_sessions").insert({
        "token": token,
        "username": username,
        "criado_em": quando,
        "ultima_atividade": quando,
        "ativa": True,
    }).execute()
    return token


def sessao_ativa(token: str) -> bool:
    if not token:
        return False
    r = _sb().table("user_sessions").select("ativa").eq("token", token).maybe_single().execute()
    if not r or not r.data:
        return False
    return bool(r.data.get("ativa"))


def atualizar_atividade_sessao(token: str, quando: str):
    if not token:
        return
    _sb().table("user_sessions").update({"ultima_atividade": quando}).eq("token", token).execute()


def encerrar_sessao(token: str, encerrada_por: str | None = None):
    if not token:
        return
    _sb().table("user_sessions").update({
        "ativa": False,
        "encerrada_por": encerrada_por,
    }).eq("token", token).execute()


def encerrar_sessoes_usuario(username: str, encerrada_por: str | None = None):
    _sb().table("user_sessions").update({
        "ativa": False,
        "encerrada_por": encerrada_por,
    }).eq("username", username).eq("ativa", True).execute()


def listar_sessoes_ativas() -> list:
    return _sb().table("user_sessions").select("*").eq("ativa", True).order("ultima_atividade", desc=True).execute().data or []


# ==================== AUDIT LOG ====================

def registrar_log(usuario: str, tipo_evento: str, descricao: str, data: str, hora: str,
                   resultado: str = "sucesso", autorizado_por: str | None = None, detalhes: dict | None = None):
    try:
        _sb().table("audit_log").insert({
            "id": secrets.token_hex(12),
            "data": data,
            "hora": hora,
            "usuario": usuario,
            "autorizado_por": autorizado_por,
            "tipo_evento": tipo_evento,
            "descricao": descricao,
            "resultado": resultado,
            "detalhes": detalhes or {},
        }).execute()
    except Exception:
        pass  # auditoria nunca deve derrubar a operação principal


def get_logs(data_ini: str = "", data_fim: str = "", usuario: str = "", tipo_evento: str = "", resultado: str = "") -> list:
    q = _sb().table("audit_log").select("*")
    if data_ini:
        q = q.gte("data", data_ini)
    if data_fim:
        q = q.lte("data", data_fim)
    if usuario:
        q = q.eq("usuario", usuario)
    if tipo_evento:
        q = q.eq("tipo_evento", tipo_evento)
    if resultado:
        q = q.eq("resultado", resultado)
    rows = q.order("data", desc=True).order("hora", desc=True).limit(500).execute().data or []
    return rows


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
    if not product.get("codigo"):
        product["codigo"] = gerar_codigo_rastreio("E", product.get("data", ""))
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


_CODIGO_RASTREIO_CHARS = string.ascii_uppercase + string.digits


def gerar_codigo_rastreio(prefixo: str, criado_em: str = "") -> str:
    """Gera um código no formato "<PREFIXO>-XXXXX" (5 alfanuméricos), globalmente
    único e permanente. Prefixo identifica a origem: E=estoque, P=pacote do bar,
    V=venda.

    A unicidade é garantida pela tabela `tracking_codes` (chave primária em
    `codigo`, guardando o código completo com prefixo): o código só é
    considerado válido depois que o INSERT nessa ledger tiver sucesso. Se
    colidir com um código já emitido (mesmo de um registro cancelado/excluído),
    tenta outro — o código nunca é reaproveitado.
    """
    for _ in range(20):
        codigo = prefixo + "-" + "".join(secrets.choice(_CODIGO_RASTREIO_CHARS) for _ in range(5))
        try:
            _sb().table("tracking_codes").insert({"codigo": codigo, "criado_em": criado_em}).execute()
            return codigo
        except Exception:
            continue
    raise RuntimeError("Não foi possível gerar um código de rastreio único.")


def get_all_sales() -> list:
    all_rows = []
    offset = 0
    while True:
        batch = (
            _sb().table("sales").select("*")
            .eq("excluida", False)
            .range(offset, offset + 999)
            .execute().data or []
        )
        all_rows.extend(batch)
        if len(batch) < 1000:
            break
        offset += 1000
    return [_fix_sale(s) for s in all_rows]


def get_sales_by_date_range(data_ini: str, data_fim: str) -> list:
    all_rows = []
    offset = 0
    while True:
        batch = (
            _sb().table("sales").select("*")
            .gte("data", data_ini)
            .lte("data", data_fim)
            .eq("excluida", False)
            .range(offset, offset + 999)
            .execute().data or []
        )
        all_rows.extend(batch)
        if len(batch) < 1000:
            break
        offset += 1000
    return [_fix_sale(s) for s in all_rows]


def get_sale(uid: str) -> dict | None:
    r = _sb().table("sales").select("*").eq("uid", uid).maybe_single().execute()
    return _fix_sale(r.data) if r and r.data else None


def get_sales_by_uids(uids: list) -> list:
    if not uids:
        return []
    rows = _sb().table("sales").select("*").in_("uid", uids).execute().data or []
    return [_fix_sale(s) for s in rows]


def get_sales_by_product(pid: str) -> list:
    all_rows = []
    offset = 0
    while True:
        batch = (
            _sb().table("sales").select("*")
            .eq("produto_pid", pid)
            .eq("excluida", False)
            .range(offset, offset + 999)
            .execute().data or []
        )
        all_rows.extend(batch)
        if len(batch) < 1000:
            break
        offset += 1000
    return [_fix_sale(s) for s in all_rows]


def get_sales_canceladas() -> list:
    all_rows = []
    offset = 0
    while True:
        batch = (
            _sb().table("sales").select("*")
            .eq("excluida", True)
            .range(offset, offset + 999)
            .execute().data or []
        )
        all_rows.extend(batch)
        if len(batch) < 1000:
            break
        offset += 1000
    return [_fix_sale(s) for s in all_rows]


def get_sales_open_by_date(data: str) -> list:
    rows = (
        _sb().table("sales").select("*")
        .eq("data", data)
        .eq("fechado", False)
        .eq("excluida", False)
        .execute().data or []
    )
    return [_fix_sale(s) for s in rows]


def get_sales_closed_by_caixa_dia(dia: str) -> list:
    rows = (
        _sb().table("sales").select("*")
        .eq("caixa_dia", dia)
        .eq("fechado", True)
        .eq("excluida", False)
        .execute().data or []
    )
    return [_fix_sale(s) for s in rows]


def _lookup_codigo_produto(pid: str, produto_tipo: str) -> str | None:
    tabela = "bar_products" if produto_tipo == "bar" else "products"
    coluna_id = "id" if produto_tipo == "bar" else "pid"
    r = _sb().table(tabela).select("codigo").eq(coluna_id, pid).maybe_single().execute()
    return (r.data or {}).get("codigo") if r else None


def insert_sale(sale: dict):
    if not sale.get("codigo"):
        sale["codigo"] = gerar_codigo_rastreio("V", sale.get("data", ""))
    if not sale.get("codigo_produto") and sale.get("produto_pid"):
        sale["codigo_produto"] = _lookup_codigo_produto(sale["produto_pid"], sale.get("produto_tipo", "estoque"))
    try:
        _sb().table("sales").insert(sale).execute()
    except Exception as e:
        raise RuntimeError(f"Erro ao registrar venda: {e}") from e


def update_sale(sale: dict):
    try:
        _sb().table("sales").update(sale).eq("uid", sale["uid"]).execute()
    except Exception as e:
        raise RuntimeError(f"Erro ao atualizar venda: {e}") from e


def delete_sale(uid: str):
    _sb().table("sales").delete().eq("uid", uid).execute()


def cancelar_sale(uid: str, usuario: str, quando: str):
    try:
        _sb().table("sales").update({
            "excluida": True,
            "excluida_em": quando,
            "excluida_por": usuario,
        }).eq("uid", uid).execute()
    except Exception as e:
        raise RuntimeError(f"Erro ao cancelar venda: {e}") from e


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
    if c:
        if not isinstance(c.get("itens"), list):
            c["itens"] = []
        if not isinstance(c.get("pagamentos"), list):
            c["pagamentos"] = []
        c.setdefault("mesa", "")
        c.setdefault("limite", None)
        c.setdefault("vencimento", "")
        c.setdefault("observacao", "")
        c.setdefault("data_quitada", "")
        if not isinstance(c.get("historico"), list):
            c["historico"] = []
    return c


def get_all_commands() -> list:
    rows = _sb().table("commands").select("*").execute().data or []
    return [_fix_command(c) for c in rows]


def get_command(cid: str) -> dict | None:
    r = _sb().table("commands").select("*").eq("cid", cid).maybe_single().execute()
    return _fix_command(r.data) if r.data else None


def insert_command(command: dict):
    try:
        _sb().table("commands").insert(command).execute()
    except Exception as e:
        raise RuntimeError(f"Erro ao criar comanda: {e}") from e


def update_command(command: dict):
    try:
        res = _sb().table("commands").update(command).eq("cid", command["cid"]).execute()
    except Exception as e:
        raise RuntimeError(f"Erro ao atualizar comanda: {e}") from e
    if not res.data:
        raise RuntimeError("Erro ao atualizar comanda: nenhuma linha foi alterada.")


def delete_command(cid: str):
    try:
        _sb().table("commands").delete().eq("cid", cid).execute()
    except Exception as e:
        raise RuntimeError(f"Erro ao remover comanda: {e}") from e


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


# ==================== CAIXAS (PDV) ====================

def _fix_caixa_sessao(c: dict) -> dict:
    if c:
        c.setdefault("valor_abertura", 0)
        c.setdefault("total_dinheiro", 0)
        c.setdefault("total_pix", 0)
        c.setdefault("total_cartao_cred", 0)
        c.setdefault("total_cartao_deb", 0)
        c.setdefault("total_fiado", 0)
        c.setdefault("total_sangrias", 0)
        c.setdefault("total_suprimentos", 0)
        c.setdefault("total_despesas", 0)
        c.setdefault("total_vendas", 0)
        c.setdefault("qtd_vendas", 0)
        c.setdefault("operador_fechamento", "")
        c.setdefault("data_fechamento", "")
        c.setdefault("hora_fechamento", "")
        if not isinstance(c.get("conferencia"), dict):
            c["conferencia"] = {}
        if not isinstance(c.get("diferencas"), dict):
            c["diferencas"] = {}
        if not isinstance(c.get("vendas_uids"), list):
            c["vendas_uids"] = []
    return c


def get_caixa_aberto() -> dict | None:
    try:
        r = _sb().table("caixas").select("*").eq("status", "aberto").maybe_single().execute()
        return _fix_caixa_sessao(r.data) if r.data else None
    except Exception:
        return None


def get_caixa_sessao(cid: str) -> dict | None:
    try:
        r = _sb().table("caixas").select("*").eq("id", cid).maybe_single().execute()
        return _fix_caixa_sessao(r.data) if r.data else None
    except Exception:
        return None


def get_all_caixas() -> list:
    rows = _sb().table("caixas").select("*").order("data_abertura", desc=True).execute().data or []
    return [_fix_caixa_sessao(c) for c in rows]


def get_sales_open_by_caixa(caixa_id: str) -> list:
    rows = (
        _sb().table("sales").select("*")
        .eq("caixa_id", caixa_id)
        .eq("fechado", False)
        .execute().data or []
    )
    return [_fix_sale(s) for s in rows]


def insert_caixa(c: dict):
    try:
        _sb().table("caixas").insert(c).execute()
    except Exception as e:
        raise RuntimeError(f"Erro ao abrir caixa: {e}") from e


def update_caixa(c: dict):
    try:
        _sb().table("caixas").update(c).eq("id", c["id"]).execute()
    except Exception as e:
        raise RuntimeError(f"Erro ao atualizar caixa: {e}") from e


# ==================== CAIXA MOVIMENTACOES ====================

def get_movimentacoes_caixa(caixa_id: str) -> list:
    return (
        _sb().table("caixa_movimentacoes").select("*")
        .eq("caixa_id", caixa_id)
        .order("hora")
        .execute().data or []
    )


def insert_movimentacao(m: dict):
    try:
        _sb().table("caixa_movimentacoes").insert(m).execute()
    except Exception as e:
        raise RuntimeError(f"Erro ao registrar movimentação: {e}") from e


def get_all_movimentacoes() -> list:
    return (
        _sb().table("caixa_movimentacoes").select("*")
        .order("caixa_id").order("hora")
        .execute().data or []
    )


# ==================== BAR: PRODUCTS ====================

def bar_get_all_products() -> list:
    return _sb().table("bar_products").select("*").eq("active", True).order("name").execute().data or []


def bar_get_product(pid: str) -> dict | None:
    rows = _sb().table("bar_products").select("*").eq("id", pid).limit(1).execute().data or []
    return rows[0] if rows else None


def bar_insert_product(p: dict) -> str:
    if not p.get("codigo"):
        p["codigo"] = gerar_codigo_rastreio("P")
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


def bar_get_cost_summary() -> dict:
    """Custo médio ponderado por unidade {product_id: custo_unit} dos lotes com estoque."""
    batches = (
        _sb().table("bar_stock_batches")
        .select("product_id, quantity_remaining, quantity_total, total_cost")
        .gt("quantity_remaining", 0)
        .execute().data or []
    )
    totais_qtd: dict = {}
    totais_custo: dict = {}
    for b in batches:
        pid = b["product_id"]
        qtd = float(b.get("quantity_remaining", 0) or 0)
        qty_total = float(b.get("quantity_total", 0) or 0)
        total_cost = float(b.get("total_cost", 0) or 0)
        custo_unit = (total_cost / qty_total) if qty_total > 0 else 0
        totais_qtd[pid] = totais_qtd.get(pid, 0) + qtd
        totais_custo[pid] = totais_custo.get(pid, 0) + qtd * custo_unit
    return {
        pid: totais_custo[pid] / totais_qtd[pid]
        for pid in totais_qtd if totais_qtd[pid] > 0
    }


def bar_get_composite_cost_summary() -> dict:
    """Custo total {bar_product_id: custo_total} via componentes — mesma fórmula do Editar Pacote."""
    rows = _sb().table("bar_product_components").select("*").execute().data or []
    all_prods = {p["pid"]: p for p in (get_all_products() or [])}
    costs: dict = {}
    for row in rows:
        pid  = row.get("product_id", "")
        cid  = row.get("component_id", "").replace("-", "")
        qty  = float(row.get("quantity", 0) or 0)
        unit = (row.get("unidade_uso") or "UN")
        comp = all_prods.get(cid)
        if not comp or qty <= 0:
            continue
        valor_investido = float(comp.get("valor_investido", 0) or 0)
        conteudo_valor  = float(comp.get("conteudo_valor",  0) or 0)
        if unit != "UN" and conteudo_valor > 0:
            custo_uso = valor_investido / conteudo_valor
        else:
            custo_uso = valor_investido
        costs[pid] = costs.get(pid, 0) + qty * custo_uso
    return costs


# ==================== BAR: PRODUCT COMPONENTS ====================

def bar_get_all_components_grouped() -> dict:
    """Busca todos os componentes de uma vez e agrupa por product_id."""
    rows = _sb().table("bar_product_components").select("*").execute().data or []
    bar_prods = {p["id"]: p for p in (_sb().table("bar_products").select("id,name,unit").execute().data or [])}
    grouped: dict = {}
    for row in rows:
        pid = row.get("product_id", "")
        cid = row.get("component_id", "")
        comp_prod = bar_prods.get(cid, {})
        row["component"] = {"id": cid, "name": comp_prod.get("name", "?"), "unit": comp_prod.get("unit", "")}
        grouped.setdefault(pid, []).append(row)
    return grouped


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


# ==================== CLIENTES ====================

def _fix_cliente(c: dict) -> dict:
    if c:
        c.setdefault("apelido", "")
        c.setdefault("telefone", "")
        c.setdefault("cpf", "")
        c.setdefault("rg", "")
        c.setdefault("endereco", "")
        c.setdefault("observacoes", "")
        c.setdefault("limite_credito", 0)
        c.setdefault("dia_vencimento", None)
        c.setdefault("proximo_vencimento", "")
        c.setdefault("data_cadastro", "")
    return c


def get_all_clientes() -> list:
    rows = _sb().table("clientes").select("*").order("nome").execute().data or []
    return [_fix_cliente(c) for c in rows]


def get_cliente(cid: str) -> dict | None:
    r = _sb().table("clientes").select("*").eq("id", cid).maybe_single().execute()
    return _fix_cliente(r.data) if r.data else None


def insert_cliente(c: dict):
    try:
        _sb().table("clientes").insert(c).execute()
    except Exception as e:
        raise RuntimeError(f"Erro ao cadastrar cliente: {e}") from e


def update_cliente(c: dict):
    try:
        _sb().table("clientes").update(c).eq("id", c["id"]).execute()
    except Exception as e:
        raise RuntimeError(f"Erro ao atualizar cliente: {e}") from e


def get_saldo_cliente(cliente_id: str) -> float:
    rows = (
        _sb().table("commands").select("itens")
        .eq("cliente_id", cliente_id)
        .not_.in_("status", ["cancelada", "fechada", "quitada"])
        .execute().data or []
    )
    total = 0.0
    for cmd in rows:
        for item in (cmd.get("itens") or []):
            total += float(item.get("valor_venda", 0)) * int(item.get("quantidade", 0))
    return round(total, 2)
