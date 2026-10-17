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

def criar_sessao(username: str, quando: str, user_agent: str = "") -> str:
    token = secrets.token_hex(32)
    _sb().table("user_sessions").insert({
        "token": token,
        "username": username,
        "criado_em": quando,
        "ultima_atividade": quando,
        "ativa": True,
        "user_agent": user_agent,
        "tela_atual": "",
    }).execute()
    return token


def sessao_ativa(token: str) -> bool:
    if not token:
        return False
    r = _sb().table("user_sessions").select("ativa").eq("token", token).maybe_single().execute()
    if not r or not r.data:
        return False
    return bool(r.data.get("ativa"))


def atualizar_atividade_sessao(token: str, quando: str, tela: str | None = None):
    if not token:
        return
    dados = {"ultima_atividade": quando}
    if tela is not None:
        dados["tela_atual"] = tela
    _sb().table("user_sessions").update(dados).eq("token", token).execute()


def encerrar_sessao(token: str, encerrada_por: str | None = None, quando: str | None = None):
    if not token:
        return
    _sb().table("user_sessions").update({
        "ativa": False,
        "encerrada_por": encerrada_por,
        "encerrada_em": quando,
    }).eq("token", token).execute()


def encerrar_sessoes_usuario(username: str, encerrada_por: str | None = None, quando: str | None = None):
    _sb().table("user_sessions").update({
        "ativa": False,
        "encerrada_por": encerrada_por,
        "encerrada_em": quando,
    }).eq("username", username).eq("ativa", True).execute()


def listar_sessoes_ativas() -> list:
    return _sb().table("user_sessions").select("*").eq("ativa", True).order("ultima_atividade", desc=True).execute().data or []


def listar_sessoes_do_dia(username: str, data: str) -> list:
    """Todas as sessões (ativas ou não) do usuário criadas na data informada (YYYY-MM-DD)."""
    rows = (
        _sb().table("user_sessions").select("*")
        .eq("username", username)
        .gte("criado_em", f"{data} 00:00:00")
        .lte("criado_em", f"{data} 23:59:59")
        .order("criado_em")
        .execute().data or []
    )
    return rows


def listar_sessoes_do_dia_todos(data: str) -> list:
    """Todas as sessões (ativas ou não) de todos os usuários criadas na data informada."""
    rows = (
        _sb().table("user_sessions").select("*")
        .gte("criado_em", f"{data} 00:00:00")
        .lte("criado_em", f"{data} 23:59:59")
        .order("criado_em")
        .execute().data or []
    )
    return rows


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


def get_consumo_vendas_periodo(data_ini: str, data_fim: str) -> list:
    """Só o necessário pra medir giro: o que saiu de cada lote nas vendas."""
    rows, offset = [], 0
    while True:
        lote = (_sb().table("sales").select("produto_pid,produto_tipo,consumo_lotes")
                .gte("data", data_ini).lte("data", data_fim).eq("excluida", False)
                .range(offset, offset + 999).execute().data or [])
        rows.extend(lote)
        if len(lote) < 1000:
            return rows
        offset += 1000


def get_ajustes_estoque(pid: str) -> list:
    """Ajustes de estoque por contagem de um produto (gravados no audit_log)."""
    try:
        return (_sb().table("audit_log").select("*")
                .eq("tipo_evento", "estoque_ajuste").eq("detalhes->>pid", pid)
                .order("data", desc=True).order("hora", desc=True).limit(100)
                .execute().data or [])
    except Exception:
        return []


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


def get_products_by_pids(pids: list) -> list:
    """Busca vários produtos de uma vez por pid (1 chamada) em vez de 1
    chamada por produto. Mesmo resultado de chamar get_product() em loop,
    usado onde vários produtos diferentes precisam ser lidos na mesma
    requisição (ex: cancelamento de comanda inteira)."""
    if not pids:
        return []
    rows = _sb().table("products").select("*").in_("pid", pids).execute().data or []
    return [_fix_product(p) for p in rows]


def get_product(pid: str) -> dict | None:
    rows = _sb().table("products").select("*").eq("pid", pid).limit(1).execute().data or []
    return _fix_product(rows[0]) if rows else None


def _sem_precos_local(dados: dict) -> dict:
    return {k: v for k, v in dados.items() if k != "precos_local"}


def _gravar_com_precos_local(gravar, dados: dict):
    """Grava; se a coluna precos_local ainda não existir (schema_precos_local.sql
    não rodado), grava de novo sem ela — o resto do produto não pode travar."""
    try:
        return gravar(dados)
    except Exception as e:
        if "precos_local" not in dados or "precos_local" not in str(e):
            raise
        return gravar(_sem_precos_local(dados))


def precos_local_disponivel() -> bool:
    """False enquanto schema_precos_local.sql não foi rodado no Supabase."""
    try:
        _sb().table("products").select("precos_local").limit(1).execute()
        return True
    except Exception:
        return False


def insert_product(product: dict):
    if not product.get("codigo"):
        product["codigo"] = gerar_codigo_rastreio("E", product.get("data", ""))
    _gravar_com_precos_local(lambda d: _sb().table("products").insert(d).execute(), product)


def update_product(product: dict):
    _gravar_com_precos_local(lambda d: _sb().table("products").update(d).eq("pid", product["pid"]).execute(), product)


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


def insert_sales(sales: list[dict]):
    """Grava várias linhas de uma vez, num INSERT só — atômico (ou tudo entra,
    ou nada entra), pra um carrinho com vários itens não ficar "pela metade"
    no banco se uma das linhas falhar (ex: erro de código duplicado, estoque
    mudando no meio do caminho etc.)."""
    if not sales:
        return
    # Busca codigo_produto em lote (até 2 consultas via .in_, uma em products
    # e outra em bar_products) em vez de 1 consulta por linha da venda.
    pids_estoque = {s["produto_pid"] for s in sales
                    if s.get("produto_pid") and not s.get("codigo_produto") and s.get("produto_tipo", "estoque") != "bar"}
    pids_bar = {s["produto_pid"] for s in sales
                if s.get("produto_pid") and not s.get("codigo_produto") and s.get("produto_tipo") == "bar"}
    codigos_map: dict[tuple[str, str], str | None] = {}
    if pids_estoque:
        rows = _sb().table("products").select("pid,codigo").in_("pid", list(pids_estoque)).execute().data or []
        codigos_map.update({("estoque", r["pid"]): r.get("codigo") for r in rows})
    if pids_bar:
        rows = _sb().table("bar_products").select("id,codigo").in_("id", list(pids_bar)).execute().data or []
        codigos_map.update({("bar", r["id"]): r.get("codigo") for r in rows})
    for sale in sales:
        if not sale.get("codigo"):
            sale["codigo"] = gerar_codigo_rastreio("V", sale.get("data", ""))
        if not sale.get("codigo_produto") and sale.get("produto_pid"):
            tipo = sale.get("produto_tipo", "estoque")
            sale["codigo_produto"] = codigos_map.get((tipo, sale["produto_pid"]))
    try:
        _sb().table("sales").insert(sales).execute()
    except Exception as e:
        raise RuntimeError(f"Erro ao registrar venda: {e}") from e


def update_sale(sale: dict):
    try:
        _sb().table("sales").update(sale).eq("uid", sale["uid"]).execute()
    except Exception as e:
        raise RuntimeError(f"Erro ao atualizar venda: {e}") from e


def update_sales_status_by_comanda(comanda_id: str, status: str):
    """Propaga o novo status (pendente/parcial/quitada) pra todas as vendas
    ainda ativas (não canceladas) ligadas a uma comanda."""
    try:
        _sb().table("sales").update({"status": status}) \
            .eq("comanda_id", comanda_id).eq("excluida", False).execute()
    except Exception as e:
        raise RuntimeError(f"Erro ao sincronizar status das vendas da comanda: {e}") from e


def update_sales_codigo_by_comanda(comanda_id: str, codigo: str):
    """Código de rastreio é gerado 1x na quitação da comanda e propagado
    pra todos os itens ativos ligados a ela — mesma venda inteira, mesmo
    código, igual já acontece com o status."""
    try:
        _sb().table("sales").update({"codigo": codigo}) \
            .eq("comanda_id", comanda_id).eq("excluida", False).execute()
    except Exception as e:
        raise RuntimeError(f"Erro ao sincronizar código de rastreio da comanda: {e}") from e


def update_sales_forma_pagamento_by_id_venda(id_venda: str, forma_pagamento: str, exclude_uid: str | None = None):
    """Forma de pagamento (rótulo textual) é uma propriedade da venda inteira,
    não do item — propaga pra todos os uids do mesmo id_venda."""
    try:
        q = _sb().table("sales").update({"forma_pagamento": forma_pagamento}) \
            .eq("id_venda", id_venda).eq("excluida", False)
        if exclude_uid:
            q = q.neq("uid", exclude_uid)
        q.execute()
    except Exception as e:
        raise RuntimeError(f"Erro ao sincronizar forma de pagamento da venda: {e}") from e


def get_sales_by_id_venda(id_venda: str) -> list:
    rows = _sb().table("sales").select("*").eq("id_venda", id_venda).eq("excluida", False).execute().data or []
    return [_fix_sale(s) for s in rows]


def get_pagamentos_venda(id_venda: str) -> list:
    """Leitura tolerante: se a tabela venda_pagamentos ainda não existir
    (migração não rodada) ou der erro transitório, cai pro comportamento
    antigo (sem divisão) em vez de derrubar a página."""
    try:
        return _sb().table("venda_pagamentos").select("*").eq("id_venda", id_venda).execute().data or []
    except Exception:
        return []


def get_pagamentos_por_id_vendas(ids_venda: list) -> list:
    if not ids_venda:
        return []
    try:
        return _sb().table("venda_pagamentos").select("*").in_("id_venda", ids_venda).execute().data or []
    except Exception:
        return []


def insert_pagamentos_venda(rows: list):
    """Gravação tolerante: mesmo padrão de get_pagamentos_venda — se a tabela
    venda_pagamentos ainda não existir (migração não rodada) ou der erro
    transitório, não derruba o registro da venda em si (que já foi salva
    antes desta chamada)."""
    if not rows:
        return
    try:
        _sb().table("venda_pagamentos").insert(rows).execute()
    except Exception:
        pass


def delete_pagamentos_venda(id_venda: str):
    try:
        _sb().table("venda_pagamentos").delete().eq("id_venda", id_venda).execute()
    except Exception as e:
        raise RuntimeError(f"Erro ao limpar pagamentos da venda: {e}") from e


def update_sales_comanda_nome(comanda_id: str, novo_nome: str):
    """Propaga o nome corrigido da comanda pra todas as vendas (inclusive já
    canceladas) ligadas a ela, pra não ficar um nome na comanda e outro nas
    vendas já registradas dela."""
    try:
        _sb().table("sales").update({"comanda_nome": novo_nome}) \
            .eq("comanda_id", comanda_id).execute()
    except Exception as e:
        raise RuntimeError(f"Erro ao sincronizar nome da comanda nas vendas: {e}") from e


def get_pagamentos_comanda_por_caixa(caixa_id: str, todas_comandas: list | None = None) -> list:
    """Varre todas as comandas e retorna os pagamentos (com dados da comanda
    anexados) cujo caixa_id bate com o caixa informado — usado na conciliação
    de caixa, já que pagamentos de comanda ficam dentro do JSON da comanda,
    não numa tabela própria.

    Se `todas_comandas` for informado (lista já buscada, ex: via
    get_all_commands()), reaproveita em vez de buscar de novo — evita
    varredura repetida da tabela inteira quando o chamador já tem os dados
    em mãos (ex: caixa_turno(), que precisa dessa mesma lista pra mais de
    uma coisa na mesma requisição)."""
    if not caixa_id:
        return []
    comandas = (
        todas_comandas if todas_comandas is not None
        else (_sb().table("commands").select("cid,nome,status,pagamentos").execute().data or [])
    )
    pagamentos = []
    for c in comandas:
        for p in (c.get("pagamentos") or []):
            if p.get("caixa_id") == caixa_id:
                pagamentos.append({
                    **p, "comanda_id": c.get("cid"), "comanda_nome": c.get("nome"),
                    "comanda_status": c.get("status"),
                })
    return pagamentos


def delete_sale(uid: str):
    _sb().table("sales").delete().eq("uid", uid).execute()


def cancelar_sale(uid: str, usuario: str, quando: str):
    try:
        _sb().table("sales").update({
            "excluida": True,
            "excluida_em": quando,
            "excluida_por": usuario,
            "status": "cancelada",
        }).eq("uid", uid).execute()
    except Exception as e:
        raise RuntimeError(f"Erro ao cancelar venda: {e}") from e


def cancelar_sales_em_lote(uids: list, usuario: str, quando: str):
    """Mesma operação de cancelar_sale, mas aplicada a vários uids numa só
    chamada (ex: cancelar todas as vendas de uma comanda de uma vez, em vez
    de 1 chamada de rede por venda)."""
    if not uids:
        return
    try:
        _sb().table("sales").update({
            "excluida": True,
            "excluida_em": quando,
            "excluida_por": usuario,
            "status": "cancelada",
        }).in_("uid", uids).execute()
    except Exception as e:
        raise RuntimeError(f"Erro ao cancelar vendas: {e}") from e


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
        if not isinstance(c.get("emprestimos"), list):
            c["emprestimos"] = []
        c.setdefault("margem_customizada", None)
        c.setdefault("codigo", None)
    return c


_COMMANDS_LISTA_OK = {"v": None}  # view commands_lista existe? (schema_comandas_lista.sql)


def _tabela_lista_comandas() -> str:
    """Listagens usam a view leve (sem consumo de estoque dos itens) quando
    ela existe; senão, a tabela completa."""
    if _COMMANDS_LISTA_OK["v"] is None:
        try:
            _sb().table("commands_lista").select("cid").limit(1).execute()
            _COMMANDS_LISTA_OK["v"] = True
        except Exception:
            return "commands"  # tenta de novo na próxima (SQL pode ser rodado depois)
    return "commands_lista" if _COMMANDS_LISTA_OK["v"] else "commands"


def get_all_commands() -> list:
    """Todas as comandas, SEM consumo_lotes/bar_consumo_components nos itens
    (quando a view existe) — só pra leitura/somatórios. Pra alterar e salvar
    uma comanda, use get_command(), que lê a comanda completa."""
    all_rows = []
    offset = 0
    tabela = _tabela_lista_comandas()
    while True:
        batch = _sb().table(tabela).select("*").order("cid").range(offset, offset + 999).execute().data or []
        all_rows.extend(batch)
        if len(batch) < 1000:
            break
        offset += 1000
    return [_fix_command(c) for c in all_rows]


def get_commands_by_ids(cids: list) -> list:
    """Busca várias comandas de uma vez por cid (1 chamada), em vez de
    db.get_all_commands() inteiro — mesma ideia de get_products_by_pids().
    Usado onde só um subconjunto pequeno de comandas é necessário (ex: as
    que aparecem nas vendas já filtradas de uma página)."""
    if not cids:
        return []
    rows = _sb().table("commands").select("*").in_("cid", cids).execute().data or []
    return [_fix_command(c) for c in rows]


def get_comandas_abertas_resumo(status_list: list) -> list:
    """Busca só um resumo enxuto (cid/nome/mesa/status/local_id) das comandas
    num conjunto de status, em vez de get_all_commands() inteiro (tabela toda,
    com os blobs de itens/histórico/pagamentos de TODA comanda, de qualquer
    status) — usado onde só uma listinha rápida de comandas abertas é
    necessária (ex: seletor de "enviar pra comanda" em Cadastrar Venda)."""
    if not status_list:
        return []
    rows = (
        _sb().table("commands").select("cid,nome,mesa,status,local_id")
        .in_("status", status_list).execute().data or []
    )
    return rows


def get_command(cid: str) -> dict | None:
    r = _sb().table("commands").select("*").eq("cid", cid).maybe_single().execute()
    return _fix_command(r.data) if r and r.data else None


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


def get_caixa_aberto(local_id: str | None = None) -> dict | None:
    try:
        q = _sb().table("caixas").select("*").eq("status", "aberto")
        if local_id:
            q = q.eq("local_id", local_id)
        r = q.maybe_single().execute()
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
    # excluida=False: venda cancelada com o caixa aberto não pode continuar
    # somando no turno/fechamento (antes somava e o caixa acusava falta).
    rows = (
        _sb().table("sales").select("*")
        .eq("caixa_id", caixa_id)
        .eq("fechado", False)
        .eq("excluida", False)
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
    r = _gravar_com_precos_local(lambda d: _sb().table("bar_products").insert(d).execute(), p)
    return r.data[0]["id"] if r.data else ""


def bar_get_simple_products() -> list:
    return _sb().table("bar_products").select("*").eq("active", True).eq("type", "simple").order("name").execute().data or []


def bar_update_product(pid: str, data: dict):
    _gravar_com_precos_local(lambda d: _sb().table("bar_products").update(d).eq("id", pid).execute(), data)


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
    """Busca todos os componentes de uma vez e agrupa por product_id.
    component_id referencia um produto do Estoque (ingrediente), não um
    bar_products — mesma regra já usada em bar_get_components() (products.pid
    é hex sem hífen, component_id vem como UUID com hífen)."""
    rows = _sb().table("bar_product_components").select("*").execute().data or []
    produtos_estoque = {p["pid"]: p for p in (_sb().table("products").select("pid,nome").execute().data or [])}
    grouped: dict = {}
    for row in rows:
        pid = row.get("product_id", "")
        cid = (row.get("component_id", "") or "").replace("-", "")
        comp_prod = produtos_estoque.get(cid, {})
        row["component"] = {"id": cid, "name": comp_prod.get("nome", "?")}
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
        c["local_id"] = c.get("local_id") or ""
    return c


def _sem_coluna_local(e: Exception) -> bool:
    """Erro de coluna local_id inexistente (schema_clientes_local.sql ainda não rodado)."""
    return "local_id" in str(e)


def get_all_clientes() -> list:
    rows = _sb().table("clientes").select("*").order("nome").execute().data or []
    return [_fix_cliente(c) for c in rows]


def get_cliente(cid: str) -> dict | None:
    r = _sb().table("clientes").select("*").eq("id", cid).maybe_single().execute()
    return _fix_cliente(r.data) if r and r.data else None


def insert_cliente(c: dict):
    try:
        try:
            _sb().table("clientes").insert(c).execute()
        except Exception as e:
            if not _sem_coluna_local(e):
                raise
            _sb().table("clientes").insert({k: v for k, v in c.items() if k != "local_id"}).execute()
    except Exception as e:
        raise RuntimeError(f"Erro ao cadastrar cliente: {e}") from e


def update_cliente(c: dict):
    try:
        try:
            _sb().table("clientes").update(c).eq("id", c["id"]).execute()
        except Exception as e:
            if not _sem_coluna_local(e):
                raise
            _sb().table("clientes").update({k: v for k, v in c.items() if k != "local_id"}).eq("id", c["id"]).execute()
    except Exception as e:
        raise RuntimeError(f"Erro ao atualizar cliente: {e}") from e


def delete_cliente(cid: str):
    try:
        _sb().table("clientes").delete().eq("id", cid).execute()
    except Exception as e:
        raise RuntimeError(f"Erro ao excluir cliente: {e}") from e


def tem_comandas_abertas_cliente(cliente_id: str) -> bool:
    rows = (
        _sb().table("commands").select("cid")
        .eq("cliente_id", cliente_id)
        .not_.in_("status", ["cancelada", "fechada", "quitada"])
        .execute().data or []
    )
    return bool(rows)


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


def get_saldos_clientes(cliente_ids: list) -> dict:
    """Mesma regra de get_saldo_cliente, mas calcula pra vários clientes de
    uma vez (1 chamada) em vez de 1 chamada por cliente. Usado na listagem
    de clientes, onde o saldo de todo mundo é exibido na mesma tela."""
    if not cliente_ids:
        return {}
    rows = (
        _sb().table("commands").select("cliente_id,itens")
        .in_("cliente_id", cliente_ids)
        .not_.in_("status", ["cancelada", "fechada", "quitada"])
        .execute().data or []
    )
    saldos = {cid: 0.0 for cid in cliente_ids}
    for cmd in rows:
        cid = cmd.get("cliente_id")
        if cid not in saldos:
            continue
        for item in (cmd.get("itens") or []):
            saldos[cid] += float(item.get("valor_venda", 0)) * int(item.get("quantidade", 0))
    return {cid: round(v, 2) for cid, v in saldos.items()}


# ==================== INVESTIDORES ====================

DONO_INVESTIDOR_ID = "dono"


def _fix_investidor(i: dict) -> dict:
    if i:
        i.setdefault("documento", "")
        i.setdefault("telefone", "")
        i.setdefault("email", "")
        i.setdefault("observacoes", "")
        i.setdefault("status", "ativo")
        i.setdefault("data_cadastro", "")
    return i


def get_all_investidores() -> list:
    rows = _sb().table("investidores").select("*").order("nome").execute().data or []
    return [_fix_investidor(i) for i in rows]


def get_investidor(iid: str) -> dict | None:
    r = _sb().table("investidores").select("*").eq("id", iid).maybe_single().execute()
    return _fix_investidor(r.data) if r and r.data else None


def insert_investidor(i: dict):
    try:
        _sb().table("investidores").insert(i).execute()
    except Exception as e:
        raise RuntimeError(f"Erro ao cadastrar investidor: {e}") from e


def update_investidor(i: dict):
    try:
        _sb().table("investidores").update(i).eq("id", i["id"]).execute()
    except Exception as e:
        raise RuntimeError(f"Erro ao atualizar investidor: {e}") from e


def delete_investidor(iid: str):
    try:
        _sb().table("investidores").delete().eq("id", iid).execute()
    except Exception as e:
        raise RuntimeError(f"Erro ao excluir investidor: {e}") from e


def get_or_create_dono_investidor() -> dict:
    """Investidor seed que representa o proprietário do sistema — usado
    automaticamente como dono de 100% de qualquer lote sem participantes
    informados. Idempotente: só cria na primeira vez que for preciso."""
    dono = get_investidor(DONO_INVESTIDOR_ID)
    if dono:
        return dono
    novo = {
        "id": DONO_INVESTIDOR_ID,
        "nome": "Proprietário do Sistema",
        "documento": "", "telefone": "", "email": "",
        "observacoes": "Investidor padrão — dono do negócio. Pode renomear livremente.",
        "status": "ativo",
        "data_cadastro": "",
    }
    insert_investidor(novo)
    return _fix_investidor(novo)


def _fix_lote_investimento(li: dict) -> dict:
    if li:
        li.setdefault("fornecedor", "")
        if not isinstance(li.get("participantes"), list):
            li["participantes"] = []
        li.setdefault("removido", False)
    return li


def get_all_lote_investimentos(incluir_removidos: bool = False) -> list:
    q = _sb().table("lote_investimentos").select("*")
    if not incluir_removidos:
        q = q.eq("removido", False)
    rows = q.order("data_entrada", desc=True).execute().data or []
    return [_fix_lote_investimento(li) for li in rows]


def get_lote_investimento(lid: str) -> dict | None:
    r = _sb().table("lote_investimentos").select("*").eq("id", lid).maybe_single().execute()
    return _fix_lote_investimento(r.data) if r and r.data else None


def insert_lote_investimento(li: dict):
    try:
        _sb().table("lote_investimentos").insert(li).execute()
    except Exception as e:
        raise RuntimeError(f"Erro ao registrar investimento do lote: {e}") from e


def marcar_lote_investimento_removido(lid: str):
    try:
        _sb().table("lote_investimentos").update({"removido": True}).eq("id", lid).execute()
    except Exception as e:
        raise RuntimeError(f"Erro ao remover investimento do lote: {e}") from e


def update_fornecedor_lotes_investimento(lids: list, nome_fornecedor: str):
    """Grava o mesmo nome de fornecedor em vários lotes (em blocos, pra não
    estourar a URL do Supabase)."""
    try:
        for ini in range(0, len(lids), 150):
            (_sb().table("lote_investimentos").update({"fornecedor": nome_fornecedor})
             .in_("id", lids[ini:ini + 150]).execute())
    except Exception as e:
        raise RuntimeError(f"Erro ao atualizar fornecedor dos lotes: {e}") from e


def update_lote_investimento(lid: str, campos: dict):
    try:
        _sb().table("lote_investimentos").update(campos).eq("id", lid).execute()
    except Exception as e:
        raise RuntimeError(f"Erro ao atualizar investimento do lote: {e}") from e


# ==================== FORNECEDORES ====================
# Tabelas criadas por schema_fornecedores.sql.

def _fix_fornecedor(f: dict) -> dict:
    if f:
        if not isinstance(f.get("apelidos"), list):
            f["apelidos"] = []
        for campo in ("cnpj", "telefone", "contato", "email", "chave_pix",
                      "forma_pagamento_padrao", "observacoes", "criado_por", "criado_em"):
            f[campo] = f.get(campo) or ""
        f["prazo_dias_padrao"] = int(f.get("prazo_dias_padrao") or 0)
        f["ativo"] = f.get("ativo") is not False
    return f


def _fix_fornecedor_compra(c: dict) -> dict:
    if c:
        if not isinstance(c.get("lote_ids"), list):
            c["lote_ids"] = []
        if not isinstance(c.get("parcelas"), list):
            c["parcelas"] = []
        c["valor_lotes"] = float(c.get("valor_lotes") or 0)
        c["valor_total"] = float(c.get("valor_total") or 0)
        for campo in ("numero_nota", "forma_pagamento", "observacoes", "criado_por", "criado_em"):
            c[campo] = c.get(campo) or ""
        c["removida"] = bool(c.get("removida"))
    return c


def fornecedores_disponivel() -> bool:
    """False enquanto schema_fornecedores.sql não foi rodado no Supabase."""
    try:
        _sb().table("fornecedores").select("id").limit(1).execute()
        _sb().table("fornecedor_compras").select("id").limit(1).execute()
        return True
    except Exception:
        return False


def get_all_fornecedores() -> list:
    rows = _sb().table("fornecedores").select("*").order("nome").execute().data or []
    return [_fix_fornecedor(f) for f in rows]


def get_fornecedor(fid: str) -> dict | None:
    rows = _sb().table("fornecedores").select("*").eq("id", fid).limit(1).execute().data or []
    return _fix_fornecedor(rows[0]) if rows else None


def insert_fornecedor(f: dict):
    try:
        _sb().table("fornecedores").insert(f).execute()
    except Exception as e:
        raise RuntimeError(f"Erro ao cadastrar fornecedor: {e}") from e


def update_fornecedor(f: dict):
    try:
        _sb().table("fornecedores").update(f).eq("id", f["id"]).execute()
    except Exception as e:
        raise RuntimeError(f"Erro ao atualizar fornecedor: {e}") from e


def get_all_fornecedor_compras(fornecedor_id: str | None = None) -> list:
    q = _sb().table("fornecedor_compras").select("*").eq("removida", False)
    if fornecedor_id:
        q = q.eq("fornecedor_id", fornecedor_id)
    rows = q.order("data_compra", desc=True).execute().data or []
    return [_fix_fornecedor_compra(c) for c in rows]


def get_fornecedor_compra(cid: str) -> dict | None:
    rows = _sb().table("fornecedor_compras").select("*").eq("id", cid).limit(1).execute().data or []
    return _fix_fornecedor_compra(rows[0]) if rows else None


def insert_fornecedor_compra(c: dict):
    try:
        _sb().table("fornecedor_compras").insert(c).execute()
    except Exception as e:
        raise RuntimeError(f"Erro ao registrar compra: {e}") from e


def update_fornecedor_compra(c: dict):
    try:
        _sb().table("fornecedor_compras").update(c).eq("id", c["id"]).execute()
    except Exception as e:
        raise RuntimeError(f"Erro ao atualizar compra: {e}") from e


def tem_vinculos_investidor(investidor_id: str) -> bool:
    lotes = get_all_lote_investimentos(incluir_removidos=False)
    for li in lotes:
        if any(p.get("investidor_id") == investidor_id for p in li.get("participantes", [])):
            return True
    pagamentos = (
        _sb().table("pagamentos_investidor").select("id")
        .eq("investidor_id", investidor_id).limit(1).execute().data or []
    )
    return bool(pagamentos)


def get_pagamentos_investidor(investidor_id: str) -> list:
    rows = (
        _sb().table("pagamentos_investidor").select("*")
        .eq("investidor_id", investidor_id).order("data", desc=True).execute().data or []
    )
    return rows


def get_totais_pagamentos_investidores(investidor_ids: list) -> dict:
    """Soma o total pago a vários investidores de uma vez (1 chamada) em
    vez de 1 chamada por investidor. Usado no dashboard de Investidores,
    onde o total de todo mundo é exibido na mesma tela."""
    if not investidor_ids:
        return {}
    rows = (
        _sb().table("pagamentos_investidor").select("investidor_id,valor")
        .in_("investidor_id", investidor_ids).execute().data or []
    )
    totais = {iid: 0.0 for iid in investidor_ids}
    for r in rows:
        iid = r.get("investidor_id")
        if iid in totais:
            totais[iid] += float(r.get("valor", 0) or 0)
    return totais


def insert_pagamento_investidor(p: dict):
    try:
        _sb().table("pagamentos_investidor").insert(p).execute()
    except Exception as e:
        raise RuntimeError(f"Erro ao registrar pagamento: {e}") from e


# ==================== DESPESAS ====================

def get_all_despesas_categorias() -> list:
    rows = _sb().table("despesas_categorias").select("*").order("nome").execute().data or []
    return rows


def insert_despesa_categoria(c: dict):
    try:
        _sb().table("despesas_categorias").insert(c).execute()
    except Exception as e:
        raise RuntimeError(f"Erro ao cadastrar categoria de despesa: {e}") from e


def get_all_despesas() -> list:
    rows = _sb().table("despesas").select("*").order("data", desc=True).execute().data or []
    return rows


def get_despesa(did: str) -> dict | None:
    r = _sb().table("despesas").select("*").eq("id", did).maybe_single().execute()
    return r.data if r and r.data else None


def insert_despesa(d: dict):
    try:
        _sb().table("despesas").insert(d).execute()
    except Exception as e:
        raise RuntimeError(f"Erro ao cadastrar despesa: {e}") from e


def update_despesa(d: dict):
    try:
        _sb().table("despesas").update(d).eq("id", d["id"]).execute()
    except Exception as e:
        raise RuntimeError(f"Erro ao atualizar despesa: {e}") from e


def delete_despesa(did: str):
    try:
        _sb().table("despesas").delete().eq("id", did).execute()
    except Exception as e:
        raise RuntimeError(f"Erro ao excluir despesa: {e}") from e


def get_all_despesas_recorrentes() -> list:
    rows = _sb().table("despesas_recorrentes").select("*").order("categoria").execute().data or []
    return rows


def get_despesa_recorrente(rid: str) -> dict | None:
    r = _sb().table("despesas_recorrentes").select("*").eq("id", rid).maybe_single().execute()
    return r.data if r and r.data else None


def insert_despesa_recorrente(r: dict):
    try:
        _sb().table("despesas_recorrentes").insert(r).execute()
    except Exception as e:
        raise RuntimeError(f"Erro ao cadastrar despesa recorrente: {e}") from e


def update_despesa_recorrente(r: dict):
    try:
        _sb().table("despesas_recorrentes").update(r).eq("id", r["id"]).execute()
    except Exception as e:
        raise RuntimeError(f"Erro ao atualizar despesa recorrente: {e}") from e


def delete_despesa_recorrente(rid: str):
    try:
        _sb().table("despesas_recorrentes").delete().eq("id", rid).execute()
    except Exception as e:
        raise RuntimeError(f"Erro ao excluir despesa recorrente: {e}") from e


# ==================== LOCAIS DE ESTOQUE ====================

def get_all_locais_estoque(incluir_inativos: bool = False) -> list:
    q = _sb().table("locais_estoque").select("*")
    if not incluir_inativos:
        q = q.eq("ativo", True)
    return q.order("nome").execute().data or []


def insert_local_estoque(l: dict):
    try:
        _sb().table("locais_estoque").insert(l).execute()
    except Exception as e:
        raise RuntimeError(f"Erro ao cadastrar local de estoque: {e}") from e


def insert_transferencia_estoque(t: dict):
    try:
        _sb().table("estoque_transferencias").insert(t).execute()
    except Exception as e:
        raise RuntimeError(f"Erro ao registrar transferência: {e}") from e


def get_all_transferencias_estoque() -> list:
    rows = _sb().table("estoque_transferencias").select("*").order("data", desc=True).order("hora", desc=True).execute().data or []
    return rows


# ==================== CATEGORIAS DE PRODUTO ====================
# Cada categoria (ex: "Cerveja") tem um "tipo" (bebida/comida/insumo) que
# define quais unidades de conteúdo fazem sentido pra ela (ml/L, kg/UN, UN
# — ver UNIDADES_CAT no front de estoque_novo.html/editar_produto.html).

def get_all_categorias() -> list:
    """Leitura tolerante: se a tabela categorias ainda não existir (migração
    não rodada), cai pras 3 categorias fixas de sempre em vez de derrubar
    a página de cadastro de produto."""
    try:
        return _sb().table("categorias").select("*").order("nome").execute().data or []
    except Exception:
        return [
            {"nome": "bebida", "tipo": "bebida"},
            {"nome": "comida", "tipo": "comida"},
            {"nome": "insumo", "tipo": "insumo"},
        ]


def insert_categoria(nome: str, tipo: str):
    try:
        _sb().table("categorias").insert({"nome": nome, "tipo": tipo}).execute()
    except Exception as e:
        raise RuntimeError(f"Erro ao criar categoria: {e}") from e


def update_categoria_tipo(nome: str, tipo: str):
    try:
        _sb().table("categorias").update({"tipo": tipo}).eq("nome", nome).execute()
    except Exception as e:
        raise RuntimeError(f"Erro ao atualizar categoria: {e}") from e


def delete_categoria(nome: str):
    try:
        _sb().table("categorias").delete().eq("nome", nome).execute()
    except Exception as e:
        raise RuntimeError(f"Erro ao excluir categoria: {e}") from e


# ==================== CACHE (desempenho) ====================
# Cada consulta ao Supabase custa ~0,2 s só de ida e volta. Dados de
# referência que quase não mudam (locais, usuários, categorias, pacotes)
# ficam guardados em memória por alguns minutos e são descartados na hora
# em que este processo grava algo neles. Com mais de um processo (gunicorn
# com vários workers), outro processo enxerga a mudança em até `ttl`.
# Comandas, vendas, caixa e estoque NÃO entram aqui — mudam o tempo todo.
import copy as _copy
import functools as _functools
import threading as _threading

_CACHE: dict = {}
_CACHE_LOCK = _threading.Lock()


def _cacheado(grupo: str, ttl: float):
    def deco(fn):
        @_functools.wraps(fn)
        def wrapper(*args, **kwargs):
            chave = (grupo, fn.__name__, args, tuple(sorted(kwargs.items())))
            agora = time.monotonic()
            with _CACHE_LOCK:
                item = _CACHE.get(chave)
            if item and agora - item[0] < ttl:
                return _copy.deepcopy(item[1])  # quem chama pode alterar a cópia
            valor = fn(*args, **kwargs)
            with _CACHE_LOCK:
                _CACHE[chave] = (agora, valor)
            return _copy.deepcopy(valor)
        return wrapper
    return deco


def invalidar_cache(*grupos: str):
    with _CACHE_LOCK:
        for chave in [k for k in _CACHE if k[0] in grupos]:
            del _CACHE[chave]


def _invalida(*grupos: str):
    def deco(fn):
        @_functools.wraps(fn)
        def wrapper(*args, **kwargs):
            try:
                return fn(*args, **kwargs)
            finally:
                invalidar_cache(*grupos)
        return wrapper
    return deco


def _memo_requisicao(fn):
    """Mesma consulta repetida dentro de UMA página (ex.: caixa aberto da
    Loja pedido 3x) vai ao banco uma vez só. Não passa de uma requisição."""
    @_functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            from flask import g, has_request_context
            if not has_request_context():
                return fn(*args, **kwargs)
            memo = g.setdefault("_db_memo", {})
        except Exception:
            return fn(*args, **kwargs)
        chave = (fn.__name__, args, tuple(sorted(kwargs.items())))
        if chave not in memo:
            memo[chave] = fn(*args, **kwargs)
        return _copy.deepcopy(memo[chave])
    return wrapper


def _limpa_memo_requisicao(fn):
    @_functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        finally:
            try:
                from flask import g, has_request_context
                if has_request_context():
                    g.pop("_db_memo", None)
            except Exception:
                pass
    return wrapper


def _memo_se_verdadeiro(fn):
    """Checagens "a tabela/coluna já existe?": depois que deu True, não muda mais."""
    ok = {"v": False}
    @_functools.wraps(fn)
    def wrapper():
        if not ok["v"]:
            ok["v"] = fn()
        return ok["v"]
    return wrapper


for _nome, _grupo, _ttl in [
    ("get_all_locais_estoque", "locais", 300),
    ("get_all_categorias", "categorias", 300),
    ("get_all_users", "users", 60),
    ("bar_get_all_products", "bar", 60),
    ("bar_get_simple_products", "bar", 60),
    ("bar_get_all_components_grouped", "bar", 60),
]:
    globals()[_nome] = _cacheado(_grupo, _ttl)(globals()[_nome])

for _nome, _grupos in [
    ("insert_local_estoque", ("locais",)),
    ("insert_categoria", ("categorias",)),
    ("update_categoria_tipo", ("categorias",)),
    ("delete_categoria", ("categorias",)),
    ("insert_user", ("users",)),
    ("update_user_permissions", ("users",)),
    ("update_user_cargo", ("users",)),
    ("set_user_ativo", ("users",)),
    ("set_user_arquivado", ("users",)),
    ("update_user_senha", ("users",)),
    ("update_user_pin", ("users",)),
    ("bar_insert_product", ("bar",)),
    ("bar_update_product", ("bar",)),
    ("bar_delete_product", ("bar",)),
    ("bar_insert_component", ("bar",)),
    ("bar_delete_component", ("bar",)),
    ("bar_delete_all_components", ("bar",)),
]:
    globals()[_nome] = _invalida(*_grupos)(globals()[_nome])

get_caixa_aberto = _memo_requisicao(get_caixa_aberto)
insert_caixa = _limpa_memo_requisicao(insert_caixa)
update_caixa = _limpa_memo_requisicao(update_caixa)
fornecedores_disponivel = _memo_se_verdadeiro(fornecedores_disponivel)
precos_local_disponivel = _memo_se_verdadeiro(precos_local_disponivel)
