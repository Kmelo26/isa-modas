from flask import Flask, render_template, request, redirect, session, url_for, send_file, jsonify
import os
import time
import uuid
from collections import defaultdict
from datetime import datetime, date, timezone, timedelta

BRT = timezone(timedelta(hours=-3))
def now_brt():  return datetime.now(BRT)
def today_brt(): return datetime.now(BRT).date()
from io import BytesIO

from dotenv import load_dotenv
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas

import db

load_dotenv()

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "isa_modas_secreto")


# ================== ERRO DE CONEXÃO SUPABASE ==================
try:
    import httpx as _httpx

    @app.errorhandler(_httpx.ReadError)
    def handle_read_error(_):
        db._reset_client()
        url_base = request.url.split("?")[0]
        return (
            f"<div style='font-family:sans-serif;margin:60px auto;max-width:500px;text-align:center;'>"
            f"<h2>⚠️ Conexão com o banco perdida</h2>"
            f"<p style='color:#aaa;'>A conexão com o Supabase caiu momentaneamente. "
            f"Clique em tentar novamente — o sistema se reconecta automaticamente.</p>"
            f"<a href='{url_base}' style='display:inline-block;margin-top:20px;"
            f"padding:12px 28px;background:#1a1a1a;border:1px solid #555;border-radius:8px;"
            f"color:#fff;text-decoration:none;font-size:15px;'>⟳ Tentar novamente</a>"
            f"</div>",
            503,
        )
except ImportError:
    pass


# ================== FILTRO DATA BR (JINJA) ==================
def br_date(value):
    if value is None:
        return ""
    s = str(value).strip()
    if not s:
        return ""
    try:
        if "/" in s and len(s) >= 10:
            s = datetime.strptime(s[:10], "%Y/%m/%d").strftime("%Y-%m-%d")
        elif "-" in s and len(s) >= 10:
            s = datetime.strptime(s[:10], "%Y-%m-%d").strftime("%Y-%m-%d")
    except Exception:
        return s
    return f"{s[8:10]}/{s[5:7]}/{s[0:4]}"

app.jinja_env.filters["br_date"] = br_date


@app.context_processor
def inject_erro_autorizacao():
    return {"erro_autorizacao": session.pop("_erro_autorizacao", None)}


# ================== AUTH HELPERS ==================
def usuario_logado():
    return "usuario" in session


def normalizar_tipo(tipo):
    if isinstance(tipo, str):
        tipo = tipo.strip().lower()
    else:
        tipo = "funcionario"
    if tipo == "cliente":
        tipo = "funcionario"
    return tipo


PERMISSOES_TODAS = [
    # Operacional
    "ver_vendas", "visualizar_venda", "criar_vendas", "editar_vendas", "cancelar_vendas",
    "aplicar_desconto", "editar_desconto", "ver_valor_comandas",
    # Indicadores e Métricas
    "ver_margem_lucro", "ver_lucro_venda", "ver_faturamento_diario",
    "ver_qtd_vendas", "ver_ticket_medio", "ver_lucro_periodo",
    # Estoque
    "ver_estoque", "visualizar_produto", "editar_estoque", "cadastrar_produto", "editar_produto", "excluir_produto",
    # Dados Sensíveis do Estoque
    "ver_valor_estoque", "ver_margem_estoque", "ver_custo_produtos",
    # Financeiro
    "ver_financeiro", "abrir_fechar_caixa",
    # Informações Financeiras
    "ver_caixa", "ver_historico_caixa", "ver_lucro_liquido", "ver_despesas", "exportar_financeiro",
    # Críticas
    "excluir_vendas", "alterar_preco_venda", "alterar_custo_produto",
    "gerenciar_usuarios", "alterar_permissoes",
    "ver_vendas_canceladas",
]

CARGOS = ["admin", "gerente", "vendedor", "caixa", "funcionario", "estoquista"]

# Modelo sugerido de permissões por cargo — só um atalho de preenchimento na
# tela de usuários; não trava nem sobrescreve nada sozinho (o admin sempre
# pode personalizar cada permissão individualmente depois de aplicar).
CARGO_PERMISSOES_PADRAO = {
    "gerente": [
        "ver_vendas", "visualizar_venda", "criar_vendas", "editar_vendas", "cancelar_vendas",
        "aplicar_desconto", "ver_valor_comandas",
        "ver_margem_lucro", "ver_lucro_venda", "ver_faturamento_diario", "ver_qtd_vendas", "ver_ticket_medio",
        "ver_estoque", "visualizar_produto", "editar_estoque", "cadastrar_produto", "editar_produto",
        "ver_valor_estoque", "ver_margem_estoque", "ver_custo_produtos",
        "ver_financeiro", "abrir_fechar_caixa",
        "ver_caixa", "ver_historico_caixa", "ver_lucro_liquido", "ver_despesas", "exportar_financeiro",
    ],
    "caixa": [
        "ver_vendas", "visualizar_venda", "criar_vendas",
        "ver_estoque", "visualizar_produto",
        "abrir_fechar_caixa", "ver_caixa",
    ],
    "estoquista": [
        "ver_estoque", "visualizar_produto", "editar_estoque", "cadastrar_produto", "editar_produto",
    ],
}


def tem_permissao(permissao):
    if session.get("tipo") == "admin":
        return True
    permissoes = session.get("permissoes", [])
    if not isinstance(permissoes, list):
        permissoes = []
    return permissao in permissoes


# ================== CONVERSÕES ==================
def to_int(v, default=0):
    try:
        return int(v)
    except Exception:
        return default


def to_float(v, default=0.0):
    try:
        return float(v)
    except Exception:
        return default


def parse_date_yyyy_mm_dd(s):
    try:
        return datetime.strptime((s or "").strip(), "%Y-%m-%d").date()
    except Exception:
        return None


def normalizar_data_str(s):
    s = (s or "").strip()
    if not s:
        return now_brt().strftime("%Y-%m-%d")
    if "/" in s and len(s) >= 10:
        try:
            return datetime.strptime(s[:10], "%Y/%m/%d").strftime("%Y-%m-%d")
        except Exception:
            pass
    if "-" in s and len(s) >= 10:
        try:
            return datetime.strptime(s[:10], "%Y-%m-%d").strftime("%Y-%m-%d")
        except Exception:
            pass
    return now_brt().strftime("%Y-%m-%d")


def normalizar_pagamento(forma):
    f = (forma or "").strip().lower()
    if "pix" in f:
        return "pix"
    if "dinheiro" in f or "dinhei" in f:
        return "dinheiro"
    if "credito" in f or "crédito" in f or "cred" in f:
        return "credito"
    if "debito" in f or "débito" in f or "deb" in f:
        return "debito"
    if "fiado" in f:
        return "fiado"
    if "cartao" in f or "cartão" in f or "card" in f:
        return "credito"
    return "credito"


# ================== FIFO / LOTES ==================
def estoque_qtd_total(produto):
    lotes = produto.get("lotes", [])
    if not isinstance(lotes, list):
        return 0.0
    return sum(to_float(l.get("qtd", 0)) for l in lotes)


def estoque_valor_total(produto):
    lotes = produto.get("lotes", [])
    if not isinstance(lotes, list):
        return 0.0
    return sum(to_float(l.get("qtd", 0)) * to_float(l.get("custo_unit", 0)) for l in lotes)


def atualizar_campos_derivados_estoque(produto):
    qtd = estoque_qtd_total(produto)
    valor = estoque_valor_total(produto)
    produto["quantidade"] = qtd
    if qtd > 0:
        produto["valor_investido"] = round(valor / qtd, 4)
    # qtd == 0: mantém o último valor_investido; só atualiza ao entrar novo lote


def entrada_estoque_lote(produto, data, qtd, custo_unit, usuario=""):
    qtd = to_int(qtd, 0)
    custo_unit = to_float(custo_unit, 0)
    if qtd <= 0:
        raise ValueError("Quantidade inválida para entrada de estoque.")
    if custo_unit <= 0:
        raise ValueError("Custo unitário inválido.")

    produto.setdefault("lotes", [])
    produto["lotes"].append({
        "data": normalizar_data_str(data),
        "qtd": qtd,
        "custo_unit": custo_unit,
        "criado_por": usuario,
    })
    atualizar_campos_derivados_estoque(produto)


def consumir_fifo(produto, qtd_vender):
    qtd_vender = to_int(qtd_vender, 0)
    if qtd_vender <= 0:
        raise ValueError("Quantidade inválida.")

    lotes = produto.get("lotes", [])
    if not isinstance(lotes, list):
        lotes = []
        produto["lotes"] = lotes

    estoque_total = sum(to_float(l.get("qtd", 0)) for l in lotes)
    if qtd_vender > estoque_total:
        raise ValueError(f"Quantidade insuficiente. Tem {estoque_total}, pediu {qtd_vender}.")

    restante = qtd_vender
    custo_total = 0.0
    consumo = []

    i = 0
    while restante > 0:
        lote = lotes[i]
        qtd_lote = to_int(lote.get("qtd", 0))
        custo_unit = to_float(lote.get("custo_unit", 0))
        data_lote = normalizar_data_str(lote.get("data"))

        usar = min(restante, qtd_lote)
        custo_total += usar * custo_unit
        consumo.append({"data_lote": data_lote, "qtd": usar, "custo_unit": custo_unit})

        lote["qtd"] = qtd_lote - usar
        restante -= usar

        if lote["qtd"] <= 0:
            lotes.pop(i)
        else:
            i += 1

    atualizar_campos_derivados_estoque(produto)
    return custo_total, consumo


def devolver_fifo(produto, consumo_lotes):
    if not consumo_lotes or not isinstance(consumo_lotes, list):
        return

    produto.setdefault("lotes", [])
    lotes = produto["lotes"]

    for c in reversed(consumo_lotes):
        lotes.insert(0, {
            "data": normalizar_data_str(c.get("data_lote")),
            "qtd": to_int(c.get("qtd", 0)),
            "custo_unit": to_float(c.get("custo_unit", 0))
        })

    atualizar_campos_derivados_estoque(produto)


def devolver_bar_componentes(components_consumo):
    """Restaura consumo de componentes de pacote bar de volta ao estoque regular."""
    for comp in (components_consumo or []):
        pid = comp.get("pid", "")
        consumo = comp.get("consumo", [])
        if not pid or not consumo:
            continue
        produto = db.get_product(pid)
        if not produto:
            continue
        produto.setdefault("lotes", [])
        for c in reversed(consumo):
            produto["lotes"].insert(0, {
                "data": normalizar_data_str(c.get("data_lote")),
                "qtd": to_float(c.get("qty", c.get("qtd", 0))),
                "custo_unit": to_float(c.get("unit_cost", c.get("custo_unit", 0)))
            })
        atualizar_campos_derivados_estoque(produto)
        db.update_product(produto)


# ================== CAIXA ==================
def montar_relatorio_caixa_por_dia(dia):
    registro = db.get_cash_register_by_dia(dia)
    if not registro:
        return None

    uids = registro.get("vendas_uids", [])
    if uids:
        vendas_dia = db.get_sales_by_uids(uids)
    else:
        vendas_dia = db.get_sales_closed_by_caixa_dia(dia)

    itens = []
    mapa = {}
    for v in vendas_dia:
        nome = str(v.get("nome", "")).strip()
        unit = to_float(v.get("valor_venda", 0))
        key = (nome.lower(), round(unit, 2))
        if key not in mapa:
            mapa[key] = {"nome": nome, "unit": unit, "qtd": 0}
            itens.append(mapa[key])
        mapa[key]["qtd"] += to_int(v.get("quantidade", 0))

    pagamentos = {
        "pix": {"qtd_vendas": 0, "valor": 0.0},
        "dinheiro": {"qtd_vendas": 0, "valor": 0.0},
        "cartao": {"qtd_vendas": 0, "valor": 0.0},
    }
    total = 0.0
    lucro = 0.0

    for v in vendas_dia:
        forma = normalizar_pagamento(v.get("forma_pagamento"))
        qtd = to_int(v.get("quantidade", 0))
        unit = to_float(v.get("valor_venda", 0))
        inv = to_float(v.get("valor_investido", 0))
        valor_venda = unit * qtd
        total += valor_venda
        lucro += (unit - inv) * qtd
        pagamentos[forma]["qtd_vendas"] += 1
        pagamentos[forma]["valor"] += valor_venda

    return {
        "registro": registro,
        "dia": dia,
        "itens": itens,
        "pagamentos": pagamentos,
        "total": round(total, 2),
        "lucro": round(lucro, 2),
        "qtd_vendas": len(vendas_dia),
    }


def montar_previa_caixa_por_dia(dia):
    dia = normalizar_data_str(dia)
    vendas_dia = db.get_sales_open_by_date(dia)

    itens = []
    mapa = {}
    pagamentos = {
        "pix": {"qtd_vendas": 0, "valor": 0.0},
        "dinheiro": {"qtd_vendas": 0, "valor": 0.0},
        "cartao": {"qtd_vendas": 0, "valor": 0.0},
    }
    total = 0.0
    total_investido = 0.0
    lucro = 0.0

    for v in vendas_dia:
        forma = normalizar_pagamento(v.get("forma_pagamento"))
        qtd = to_int(v.get("quantidade", 0))
        unit = to_float(v.get("valor_venda", 0))
        inv_unit = to_float(v.get("valor_investido", 0))
        valor_venda = unit * qtd
        valor_inv = inv_unit * qtd

        total += valor_venda
        total_investido += valor_inv
        lucro += (unit - inv_unit) * qtd
        pagamentos[forma]["qtd_vendas"] += 1
        pagamentos[forma]["valor"] += valor_venda

        nome = str(v.get("nome", "")).strip()
        key = (nome.lower(), round(unit, 2))
        if key not in mapa:
            mapa[key] = {"nome": nome, "unit": unit, "qtd": 0, "total": 0.0}
            itens.append(mapa[key])
        mapa[key]["qtd"] += qtd
        mapa[key]["total"] += valor_venda

    return {
        "dia": dia,
        "itens": itens,
        "pagamentos": pagamentos,
        "total": round(total, 2),
        "total_investido": round(total_investido, 2),
        "lucro": round(lucro, 2),
        "qtd_vendas": len(vendas_dia),
    }


# ================== MAPA DE TELAS (monitoramento) ==================
ENDPOINT_PARA_TELA = {
    "vendas": "🛒 Vendas", "cadastrar_venda_lote": "🛒 Vendas", "cadastrar_venda": "🛒 Vendas",
    "ver_venda": "🛒 Vendas", "editar_venda": "🛒 Vendas", "vendas_canceladas": "🛒 Vendas",
    "comandas": "📋 Comandas", "comanda_detalhe": "📋 Comandas",
    "estoque": "📦 Estoque", "estoque_novo": "📦 Estoque", "estoque_produto": "📦 Estoque",
    "editar_produto": "📦 Estoque", "editar_produto_alias": "📦 Estoque",
    "bar_produtos": "📦 Estoque (Bar)", "bar_produto_novo": "📦 Estoque (Bar)", "bar_produto_editar": "📦 Estoque (Bar)",
    "caixa_abrir": "💰 Caixa", "caixa_turno": "💰 Caixa", "caixa_fechar": "💰 Caixa",
    "caixa_movimentacao": "💰 Caixa", "caixa_historico_novo": "💰 Caixa", "caixa_historico": "💰 Caixa",
    "caixa_detalhe": "💰 Caixa", "caixa_relatorio": "💰 Caixa", "fechar_caixa": "💰 Caixa",
    "financeiro": "📈 Financeiro",
    "permissoes": "⚙ Administração", "usuarios_online": "⚙ Administração", "logs": "⚙ Administração",
}


def _tela_atual_da_request() -> str:
    return ENDPOINT_PARA_TELA.get(request.endpoint or "", "")


# ================== ATUALIZA SESSÃO ==================
@app.before_request
def atualizar_permissoes_da_sessao():
    if "usuario" not in session:
        return

    usuario = session.get("usuario", "")

    # Sessão foi encerrada remotamente (admin clicou "Encerrar Sessão") ou o
    # usuário foi bloqueado/arquivado desde o último login.
    token = session.get("_token")
    try:
        if token and not db.sessao_ativa(token):
            session.clear()
            session["_sessao_encerrada"] = True
            return
    except Exception:
        pass

    try:
        user_data = db.get_user(usuario)
    except Exception:
        return  # falha de rede transitória — mantém sessão atual

    if not user_data or user_data.get("arquivado") or not user_data.get("ativo", True):
        session.clear()
        return

    tipo_atual = normalizar_tipo(user_data.get("tipo", "funcionario"))

    if usuario == "admin" or tipo_atual == "admin":
        session["tipo"] = "admin"
        session["permissoes"] = PERMISSOES_TODAS[:]
        session["cargo"] = "admin"
    else:
        session["tipo"] = tipo_atual
        session["cargo"] = user_data.get("cargo") or "funcionario"
        permissoes = user_data.get("permissoes", [])
        if not isinstance(permissoes, list):
            permissoes = []
        permissoes = [p for p in permissoes if p in PERMISSOES_TODAS]
        session["permissoes"] = permissoes

    if token:
        try:
            db.atualizar_atividade_sessao(token, now_brt().strftime("%Y-%m-%d %H:%M:%S"), tela=_tela_atual_da_request())
        except Exception:
            pass


# ================== LOGIN ==================
@app.route("/login", methods=["GET", "POST"])
def login():
    sessao_encerrada = session.pop("_sessao_encerrada", False)

    if request.method == "POST":
        usuario = request.form.get("usuario", "").strip()
        senha = request.form.get("senha", "").strip()

        user_data = db.get_user(usuario)
        hoje = now_brt().strftime("%Y-%m-%d")
        hora = now_brt().strftime("%H:%M")

        senha_ok = user_data and db.verificar_senha(user_data.get("senha"), senha)

        if user_data and senha_ok and (user_data.get("arquivado") or not user_data.get("ativo", True)):
            db.registrar_log(usuario, "login", "Tentativa de login em conta desativada/arquivada", hoje, hora, resultado="negado")
            return render_template("login.html", erro="Conta desativada. Fale com o administrador.")

        if user_data and senha_ok:
            session.clear()
            session["usuario"] = usuario

            tipo = normalizar_tipo(user_data.get("tipo", "funcionario"))

            if usuario == "admin" or tipo == "admin":
                session["tipo"] = "admin"
                session["permissoes"] = PERMISSOES_TODAS[:]
                session["cargo"] = "admin"
            else:
                session["tipo"] = tipo
                session["cargo"] = user_data.get("cargo") or "funcionario"
                perms = user_data.get("permissoes", [])
                if not isinstance(perms, list):
                    perms = []
                perms = [p for p in perms if p in PERMISSOES_TODAS]
                session["permissoes"] = perms

            quando = f"{hoje} {hora}:00"
            session["_token"] = db.criar_sessao(usuario, quando, user_agent=request.headers.get("User-Agent", ""))
            db.update_user_ultimo_login(usuario, quando)
            db.registrar_log(usuario, "login", "Login realizado", hoje, hora)

            return redirect(url_for("vendas"))

        db.registrar_log(usuario or "(desconhecido)", "login", "Usuário ou senha inválidos", hoje, hora, resultado="negado")
        return render_template("login.html", erro="Usuário ou senha inválidos")

    return render_template("login.html", sessao_encerrada=sessao_encerrada)


@app.route("/logout")
def logout():
    usuario = session.get("usuario", "")
    token = session.get("_token")
    if token:
        try:
            db.encerrar_sessao(token, usuario, quando=now_brt().strftime("%Y-%m-%d %H:%M:%S"))
        except Exception:
            pass
    if usuario:
        db.registrar_log(usuario, "logout", "Logout realizado", now_brt().strftime("%Y-%m-%d"), now_brt().strftime("%H:%M"))
    session.clear()
    return redirect(url_for("login"))


# ================== VENDAS ==================
def _grupo_key(v):
    """Chave de agrupamento de uma linha de venda em 'uma venda' (carrinho ou
    comanda): comanda_id > id_venda (carrinho) > uid isolado como fallback."""
    if v.get("comanda_id"):
        return "cmd:" + str(v["comanda_id"])
    if v.get("id_venda"):
        return "iv:" + str(v["id_venda"])
    return "uid:" + str(v.get("uid"))


def _id_display(v):
    """Identificador de venda exibido pra usuário (mesmo pra toda linha do
    mesmo carrinho/comanda): CMD-XXXXXXXX pra comanda, id_venda pra balcão."""
    comanda_id = v.get("comanda_id")
    if comanda_id:
        return "CMD-" + str(comanda_id)[:8].upper()
    if v.get("id_venda"):
        return str(v["id_venda"])
    return str(v.get("uid", ""))[:8]


@app.route("/")
@app.route("/vendas")
def vendas():
    if not usuario_logado():
        return redirect(url_for("login"))

    if session.get("tipo") != "admin" and not tem_permissao("ver_vendas"):
        return redirect(url_for("login"))

    data_ini_raw = request.args.get("data_ini", "").strip()
    data_fim_raw = request.args.get("data_fim", "").strip()
    busca_produto_raw = request.args.get("busca_produto", "").strip()
    codigo_raw = request.args.get("codigo", "").strip().upper()
    forma_pagamento_raw = request.args.get("forma_pagamento", "").strip()
    status_raw = request.args.get("status", "").strip()
    margem_min_raw = request.args.get("margem_min", "").strip()
    margem_max_raw = request.args.get("margem_max", "").strip()
    ordenar_raw = request.args.get("ordenar", "data_desc").strip()

    # quick date shortcuts
    hoje_date = today_brt()
    from datetime import timedelta
    if data_ini_raw == "hoje":
        data_ini_raw = hoje_date.strftime("%Y-%m-%d")
        data_fim_raw = data_ini_raw
    elif data_ini_raw == "semana":
        data_ini_raw = (hoje_date - timedelta(days=hoje_date.weekday())).strftime("%Y-%m-%d")
        data_fim_raw = hoje_date.strftime("%Y-%m-%d")
    elif data_ini_raw == "mes":
        data_ini_raw = hoje_date.replace(day=1).strftime("%Y-%m-%d")
        data_fim_raw = hoje_date.strftime("%Y-%m-%d")

    if not request.args:
        data_ini_raw = hoje_date.strftime("%Y-%m-%d")
        data_fim_raw = hoje_date.strftime("%Y-%m-%d")

    data_ini = parse_date_yyyy_mm_dd(data_ini_raw)
    data_fim = parse_date_yyyy_mm_dd(data_fim_raw)

    if data_ini_raw and data_fim_raw:
        vendas_data = db.get_sales_by_date_range(data_ini_raw, data_fim_raw)
    else:
        vendas_data = db.get_all_sales()
    busca_produto = busca_produto_raw.lower()

    def margem_calc(v):
        preco = float(v.get("valor_venda") or 0)
        custo = float(v.get("valor_investido") or 0)
        return ((preco - custo) / preco * 100) if preco > 0 else 0

    vendas_filtradas = []
    for v in vendas_data:
        if not isinstance(v, dict):
            continue
        ok = True
        nome = str(v.get("nome", "")).lower()
        if busca_produto and busca_produto not in nome:
            ok = False
        if codigo_raw and codigo_raw not in _id_display(v).upper():
            ok = False
        d = parse_date_yyyy_mm_dd(v.get("data", ""))
        if data_ini and d and d < data_ini:
            ok = False
        if data_fim and d and d > data_fim:
            ok = False
        if forma_pagamento_raw:
            pgto = str(v.get("forma_pagamento", "")).lower()
            if forma_pagamento_raw.lower() not in pgto:
                ok = False
        if status_raw and (v.get("status") or "quitada") != status_raw:
            ok = False
        if margem_min_raw:
            try:
                if margem_calc(v) < float(margem_min_raw):
                    ok = False
            except ValueError:
                pass
        if margem_max_raw:
            try:
                if margem_calc(v) > float(margem_max_raw):
                    ok = False
            except ValueError:
                pass
        if ok:
            vendas_filtradas.append(v)

    # ── Agrupa as linhas filtradas em "vendas" (1 carrinho ou 1 comanda = 1 venda) ──
    grupos_map = defaultdict(list)
    for v in vendas_filtradas:
        grupos_map[_grupo_key(v)].append(v)

    def _montar_grupo(chave, itens_grupo):
        itens_ordenados = sorted(itens_grupo, key=lambda v: (v.get("data") or "", v.get("hora") or ""))
        primeiro = itens_ordenados[0]
        valor_total = sum(float(v.get("valor_venda") or 0) * float(v.get("quantidade") or 1) for v in itens_grupo)
        custo_total = sum(float(v.get("valor_investido") or 0) * float(v.get("quantidade") or 1) for v in itens_grupo)
        margem_grupo = ((valor_total - custo_total) / valor_total * 100) if valor_total > 0 else 0.0

        formas = []
        for v in itens_grupo:
            f = normalizar_pagamento(v.get("forma_pagamento", "")) if v.get("forma_pagamento") else None
            if f and f not in formas:
                formas.append(f)

        statuses = set((v.get("status") or "quitada") for v in itens_grupo)
        if "pendente" in statuses:
            status_grupo = "pendente"
        elif "parcial" in statuses:
            status_grupo = "parcial"
        elif statuses == {"cancelada"}:
            status_grupo = "cancelada"
        else:
            status_grupo = "quitada"

        comanda_id = primeiro.get("comanda_id")
        id_display = _id_display(primeiro)

        return {
            "grupo_key": chave,
            "id_display": id_display,
            "comanda_id": comanda_id,
            "comanda_nome": primeiro.get("comanda_nome"),
            "data": primeiro.get("data"),
            "hora": primeiro.get("hora"),
            "qtd_itens": len(itens_grupo),
            "qtd_unidades": sum(int(float(v.get("quantidade") or 1)) for v in itens_grupo),
            "valor_total": round(valor_total, 2),
            "margem": round(margem_grupo, 2),
            "formas_pagamento": formas,
            "status": status_grupo,
            "pode_cancelar_inteira": status_grupo in ("quitada", "cancelada"),
            "itens": itens_ordenados,
        }

    vendas_agrupadas = [_montar_grupo(k, itens) for k, itens in grupos_map.items()]

    if ordenar_raw == "margem_asc":
        vendas_agrupadas.sort(key=lambda g: g["margem"])
    elif ordenar_raw == "margem_desc":
        vendas_agrupadas.sort(key=lambda g: g["margem"], reverse=True)
    elif ordenar_raw == "valor_desc":
        vendas_agrupadas.sort(key=lambda g: g["valor_total"], reverse=True)
    elif ordenar_raw == "nome_asc":
        vendas_agrupadas.sort(key=lambda g: str(g["itens"][0].get("nome", "")).lower())
    else:
        vendas_agrupadas.sort(key=lambda g: (g["data"] or "", g["hora"] or ""), reverse=True)

    # stats calculados sobre as linhas filtradas (dinheiro não muda com o agrupamento)
    total_vendido = sum(
        float(v.get("valor_venda") or 0) * float(v.get("quantidade") or 1)
        for v in vendas_filtradas
    )
    num_vendas = len(vendas_agrupadas)  # "venda" agora é carrinho/comanda, não item
    itens_vendidos = sum(int(float(v.get("quantidade") or 1)) for v in vendas_filtradas)
    ticket_medio = (total_vendido / num_vendas) if num_vendas else 0.0

    pgto_totais = {"pix": 0.0, "cartao": 0.0, "dinheiro": 0.0}
    for v in vendas_filtradas:
        pgto = str(v.get("forma_pagamento", "")).lower()
        val = float(v.get("valor_venda") or 0) * float(v.get("quantidade") or 1)
        if "pix" in pgto:
            pgto_totais["pix"] += val
        elif "cart" in pgto:
            pgto_totais["cartao"] += val
        elif "dinheiro" in pgto:
            pgto_totais["dinheiro"] += val
    pgto_pct = {
        k: round(val / total_vendido * 100, 1) if total_vendido else 0.0
        for k, val in pgto_totais.items()
    }

    try:
        pagina = int(request.args.get("pagina", "1"))
    except ValueError:
        pagina = 1
    if pagina < 1:
        pagina = 1

    por_pagina = 20
    total_registros = len(vendas_agrupadas)
    total_paginas = (total_registros + por_pagina - 1) // por_pagina

    if total_paginas >= 1 and pagina > total_paginas:
        pagina = total_paginas

    inicio = (pagina - 1) * por_pagina
    fim = inicio + por_pagina
    vendas_pagina = vendas_agrupadas[inicio:fim]
    inicio_reg = inicio + 1 if vendas_agrupadas else 0
    fim_reg = min(fim, total_registros)

    nomes_sugestoes = sorted(set(
        v.get("nome", "").strip() for v in vendas_data
        if isinstance(v, dict) and v.get("nome", "").strip()
    ))

    return render_template(
        "vendas.html",
        vendas=vendas_pagina,
        usuario=session.get("usuario", ""),
        tipo=session.get("tipo"),
        permissoes=session.get("permissoes", []),
        data_ini_raw=data_ini_raw,
        data_fim_raw=data_fim_raw,
        busca_produto=busca_produto_raw,
        busca_codigo=codigo_raw,
        forma_pagamento_raw=forma_pagamento_raw,
        status_raw=status_raw,
        margem_min_raw=margem_min_raw,
        margem_max_raw=margem_max_raw,
        ordenar_raw=ordenar_raw,
        pagina=pagina,
        total_paginas=total_paginas,
        total_registros=total_registros,
        inicio_reg=inicio_reg,
        fim_reg=fim_reg,
        nomes_sugestoes=nomes_sugestoes,
        total_vendido=total_vendido,
        num_vendas=num_vendas,
        itens_vendidos=itens_vendidos,
        ticket_medio=ticket_medio,
        pgto_totais=pgto_totais,
        pgto_pct=pgto_pct,
    )


# ================== EXPORTAR VENDAS CSV ==================
@app.route("/vendas/exportar")
def vendas_exportar():
    if not usuario_logado():
        return redirect(url_for("login"))
    if session.get("tipo") != "admin" and not tem_permissao("exportar_financeiro"):
        return redirect(url_for("login"))

    import csv, io as _io
    from datetime import timedelta

    vendas_data = db.get_all_sales()

    data_ini_raw = request.args.get("data_ini", "").strip()
    data_fim_raw = request.args.get("data_fim", "").strip()
    busca_produto_raw = request.args.get("busca_produto", "").strip()
    codigo_raw = request.args.get("codigo", "").strip().upper()
    forma_pagamento_raw = request.args.get("forma_pagamento", "").strip()
    margem_min_raw = request.args.get("margem_min", "").strip()
    ordenar_raw = request.args.get("ordenar", "data_desc").strip()

    hoje_date = today_brt()
    if data_ini_raw == "hoje":
        data_ini_raw = hoje_date.strftime("%Y-%m-%d")
        data_fim_raw = data_ini_raw
    elif data_ini_raw == "semana":
        data_ini_raw = (hoje_date - timedelta(days=hoje_date.weekday())).strftime("%Y-%m-%d")
        data_fim_raw = hoje_date.strftime("%Y-%m-%d")
    elif data_ini_raw == "mes":
        data_ini_raw = hoje_date.replace(day=1).strftime("%Y-%m-%d")
        data_fim_raw = hoje_date.strftime("%Y-%m-%d")

    data_ini = parse_date_yyyy_mm_dd(data_ini_raw)
    data_fim = parse_date_yyyy_mm_dd(data_fim_raw)
    busca_produto = busca_produto_raw.lower()

    def margem_calc_exp(v):
        preco = float(v.get("valor_venda") or 0)
        custo = float(v.get("valor_investido") or 0)
        return ((preco - custo) / preco * 100) if preco > 0 else 0

    vendas_filtradas = []
    for v in vendas_data:
        if not isinstance(v, dict):
            continue
        ok = True
        nome = str(v.get("nome", "")).lower()
        if busca_produto and busca_produto not in nome:
            ok = False
        if codigo_raw and codigo_raw not in _id_display(v).upper():
            ok = False
        d = parse_date_yyyy_mm_dd(v.get("data", ""))
        if data_ini and d and d < data_ini:
            ok = False
        if data_fim and d and d > data_fim:
            ok = False
        if forma_pagamento_raw:
            pgto = str(v.get("forma_pagamento", "")).lower()
            if forma_pagamento_raw.lower() not in pgto:
                ok = False
        if margem_min_raw:
            try:
                if margem_calc_exp(v) < float(margem_min_raw):
                    ok = False
            except ValueError:
                pass
        if ok:
            vendas_filtradas.append(v)

    def chave_data_exp(v):
        d = parse_date_yyyy_mm_dd(v.get("data", ""))
        return d or datetime.min.date()

    if ordenar_raw == "margem_asc":
        vendas_filtradas.sort(key=margem_calc_exp)
    elif ordenar_raw == "margem_desc":
        vendas_filtradas.sort(key=margem_calc_exp, reverse=True)
    elif ordenar_raw == "valor_desc":
        vendas_filtradas.sort(
            key=lambda v: float(v.get("valor_venda") or 0) * float(v.get("quantidade") or 1),
            reverse=True
        )
    elif ordenar_raw == "nome_asc":
        vendas_filtradas.sort(key=lambda v: str(v.get("nome", "")).lower())
    else:
        vendas_filtradas.sort(key=chave_data_exp, reverse=True)

    output = _io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["Data", "Hora", "Produto", "Tipo", "Qtd", "Preço Unit.", "Custo Unit.", "Total", "Lucro", "Margem%", "Pagamento"])
    for v in vendas_filtradas:
        preco = float(v.get("valor_venda") or 0)
        custo = float(v.get("valor_investido") or 0)
        qtd = float(v.get("quantidade") or 1)
        total = preco * qtd
        lucro = (preco - custo) * qtd
        margem = round(((preco - custo) / preco * 100) if preco > 0 else 0, 1)
        writer.writerow([
            v.get("data", ""),
            v.get("hora", ""),
            v.get("nome", ""),
            v.get("produto_tipo", "estoque"),
            qtd,
            f"{preco:.2f}",
            f"{custo:.2f}",
            f"{total:.2f}",
            f"{lucro:.2f}",
            f"{margem}%",
            v.get("forma_pagamento", ""),
        ])

    csv_bytes = output.getvalue().encode("utf-8-sig")
    return send_file(
        BytesIO(csv_bytes),
        mimetype="text/csv",
        as_attachment=True,
        download_name=f"vendas_{data_ini_raw}_a_{data_fim_raw}.csv",
    )


# ================== EDITAR VENDA ==================
@app.route("/vendas/editar/<uid>", methods=["GET", "POST"])
def editar_venda(uid):
    if not usuario_logado():
        return redirect(url_for("login"))

    if session.get("tipo") != "admin" and not tem_permissao("editar_vendas"):
        return redirect(url_for("vendas"))

    venda = db.get_sale(uid)
    if not venda:
        return redirect(url_for("vendas"))

    estoque = db.get_all_products()

    if venda.get("fechado"):
        return render_template(
            "editar_vendas.html",
            venda=venda,
            estoque=estoque,
            erro="Esta venda já foi fechada no caixa e não pode ser editada.",
            tipo=session.get("tipo"),
            permissoes=session.get("permissoes", [])
        )

    erro = ""

    if request.method == "POST":
        nova_data = normalizar_data_str(request.form.get("data", venda.get("data", "")))

        fp_form = (request.form.get("forma_pagamento") or "").strip()
        nova_forma_pagamento = normalizar_pagamento(fp_form) if fp_form else venda.get("forma_pagamento", "cartao")

        novo_pid = (request.form.get("produto_pid") or "").strip() or str(venda.get("produto_pid", "")).strip()
        nova_qtd = to_int(request.form.get("quantidade", venda.get("quantidade", 1)), 0)

        if nova_qtd <= 0:
            erro = "Quantidade inválida."

        produto_novo = next((p for p in estoque if str(p.get("pid", "")) == str(novo_pid)), None)
        if not erro and not produto_novo:
            erro = "Produto não encontrado no estoque."

        if not erro:
            pid_antigo = str(venda.get("produto_pid", "")).strip()
            consumo_antigo = venda.get("consumo_lotes", [])

            if pid_antigo == str(produto_novo.get("pid", "")):
                produto_antigo = produto_novo
            else:
                produto_antigo = next((p for p in estoque if str(p.get("pid", "")) == pid_antigo), None)

            if produto_antigo and consumo_antigo:
                devolver_fifo(produto_antigo, consumo_antigo)

            try:
                custo_total, consumo_novo = consumir_fifo(produto_novo, nova_qtd)
            except ValueError as e:
                erro = str(e)

        if not erro:
            custo_unit_medio = (custo_total / nova_qtd) if nova_qtd else 0.0

            venda["data"] = nova_data
            venda["forma_pagamento"] = nova_forma_pagamento
            venda["produto_pid"] = produto_novo.get("pid")
            venda["nome"] = produto_novo.get("nome", venda.get("nome", ""))
            venda["quantidade"] = nova_qtd
            venda["valor_venda"] = to_float(produto_novo.get("valor_venda", venda.get("valor_venda", 0)))
            venda["valor_investido"] = round(custo_unit_medio, 4)
            venda["consumo_lotes"] = consumo_novo

            db.update_sale(venda)
            db.update_product(produto_novo)
            if produto_antigo and produto_antigo is not produto_novo:
                db.update_product(produto_antigo)

            return redirect(url_for("vendas"))

    return render_template(
        "editar_vendas.html",
        venda=venda,
        estoque=estoque,
        erro=erro,
        tipo=session.get("tipo"),
        permissoes=session.get("permissoes", [])
    )


# ================== CANCELAR VENDA (arquiva em Vendas Canceladas) ==================
def _cancelar_venda_individual(uid: str, usuario: str):
    """Cancela uma única linha de venda: devolve estoque, ajusta o caixa e marca
    cancelada. Reaproveitado tanto pra cancelar 1 item quanto por 'cancelar
    venda inteira' (que chama isto pra cada item do grupo)."""
    venda = db.get_sale(uid)
    if not venda or venda.get("excluida"):
        return None

    pid = str(venda.get("produto_pid", "")).strip()
    produto = db.get_product(pid)
    consumo_lotes = venda.get("consumo_lotes", [])

    if produto and consumo_lotes:
        devolver_fifo(produto, consumo_lotes)
        db.update_product(produto)

    # Atualiza o caixa se a venda pertencia a uma sessão
    caixa_id = str(venda.get("caixa_id") or "").strip()
    if caixa_id:
        caixa = db.get_caixa_sessao(caixa_id)
        if caixa:
            caixa["vendas_uids"] = [u for u in (caixa.get("vendas_uids") or []) if u != uid]
            valor = to_float(venda.get("valor_venda", 0))
            forma = normalizar_pagamento(venda.get("forma_pagamento", ""))
            campo_map = {
                "dinheiro": "total_dinheiro",
                "pix":      "total_pix",
                "credito":  "total_cartao_cred",
                "debito":   "total_cartao_deb",
                "fiado":    "total_fiado",
            }
            campo = campo_map.get(forma)
            if campo:
                caixa[campo] = max(0.0, to_float(caixa.get(campo, 0)) - valor)
            caixa["total_vendas"] = max(0.0, to_float(caixa.get("total_vendas", 0)) - valor)
            caixa["qtd_vendas"]   = max(0, int(caixa.get("qtd_vendas", 0)) - 1)
            db.update_caixa(caixa)

    db.cancelar_sale(uid, usuario, now_brt().strftime("%Y-%m-%d %H:%M"))
    db.registrar_log(
        usuario, "venda_cancelada", f"Cancelou venda: {venda.get('nome', '')} ({venda.get('codigo') or uid[:8]})",
        now_brt().strftime("%Y-%m-%d"), now_brt().strftime("%H:%M"),
    )
    return venda


@app.route("/vendas/excluir/<uid>", methods=["GET", "POST"])
def excluir_venda(uid):
    if not usuario_logado():
        return redirect(url_for("login"))

    if request.method == "GET":
        # compatibilidade com o link antigo — mostra a confirmação/PIN em vez de agir direto
        return redirect(url_for("vendas"))

    redir = autorizar_acao("cancelar_vendas")
    if redir:
        return redir

    _cancelar_venda_individual(uid, session.get("usuario", ""))
    return redirect(url_for("vendas"))


@app.route("/vendas/cancelar_grupo", methods=["POST"])
def cancelar_venda_grupo():
    """Cancela todos os itens de uma 'venda agrupada' (carrinho de balcão ou
    comanda já quitada) de uma vez só, reaproveitando _cancelar_venda_individual
    item a item. Comandas ainda em aberto/parcial não são canceláveis por aqui
    — o botão já vem desabilitado no front pra esses casos."""
    if not usuario_logado():
        return redirect(url_for("login"))

    grupo_key = (request.form.get("grupo_key") or "").strip()
    if not grupo_key:
        return redirect(url_for("vendas"))

    redir = autorizar_acao("cancelar_vendas")
    if redir:
        return redir

    usuario = session.get("usuario", "")

    if grupo_key.startswith("cmd:"):
        comanda_id = grupo_key[4:]
        comanda = db.get_command(comanda_id)
        if comanda and comanda.get("status") == "quitada":
            for item in comanda.get("itens", []):
                sale_uid = item.get("sale_uid")
                if sale_uid:
                    _cancelar_venda_individual(sale_uid, usuario)
    elif grupo_key.startswith("iv:"):
        id_venda_alvo = grupo_key[3:]
        todas = db.get_all_sales()
        uids = [v["uid"] for v in todas if v.get("id_venda") == id_venda_alvo]
        for uid in uids:
            _cancelar_venda_individual(uid, usuario)
    elif grupo_key.startswith("uid:"):
        _cancelar_venda_individual(grupo_key[4:], usuario)

    return redirect(url_for("vendas"))


@app.route("/vendas/ver/<uid>")
def ver_venda(uid):
    if not usuario_logado():
        return redirect(url_for("login"))

    if session.get("tipo") != "admin" and not tem_permissao("ver_vendas"):
        return redirect(url_for("vendas"))

    venda = db.get_sale(uid)
    if not venda:
        return redirect(url_for("vendas"))

    custo = venda.get("valor_investido", 0) or 0
    valor_venda = venda.get("valor_venda", 0) or 0
    margem = ((valor_venda - custo) / valor_venda * 100) if valor_venda > 0 else 0

    return render_template(
        "ver_venda.html",
        venda=venda,
        margem=margem,
        tipo=session.get("tipo"),
        permissoes=session.get("permissoes", [])
    )


# ================== VENDAS CANCELADAS ==================
@app.route("/vendas/canceladas")
def vendas_canceladas():
    if not usuario_logado():
        return redirect(url_for("login"))
    if session.get("tipo") != "admin" and not tem_permissao("ver_vendas_canceladas"):
        return redirect(url_for("vendas"))

    canceladas = db.get_sales_canceladas()
    canceladas.sort(key=lambda v: v.get("excluida_em", "") or "", reverse=True)

    return render_template(
        "vendas_canceladas.html",
        vendas=canceladas,
        tipo=session.get("tipo"),
        permissoes=session.get("permissoes", []),
        usuario=session.get("usuario", "")
    )


@app.route("/vendas/canceladas/excluir/<uid>", methods=["POST"])
def excluir_venda_definitivo(uid):
    if not usuario_logado():
        return redirect(url_for("login"))
    if session.get("tipo") != "admin" and not tem_permissao("excluir_vendas"):
        return redirect(url_for("vendas_canceladas"))

    venda = db.get_sale(uid)
    if venda and venda.get("excluida"):
        db.delete_sale(uid)

    return redirect(url_for("vendas_canceladas"))


# ================== ESTOQUE ==================
@app.route("/estoque", methods=["GET"])
def estoque():
    if not usuario_logado():
        return redirect(url_for("login"))

    if not tem_permissao("ver_estoque"):
        return redirect(url_for("vendas"))

    estoque_data = db.get_all_products()
    estoque_data.sort(key=lambda p: str(p.get("nome", "")).strip().lower())

    def compute_status(p):
        qty   = float(p.get("quantidade", 0) or 0)
        min_s = float(p.get("estoque_minimo", 0) or 0)
        if min_s <= 0:
            return "OK"
        if qty < min_s * 0.5:
            return "CRÍTICO"
        elif qty < min_s:
            return "BAIXO"
        return "OK"

    for p in estoque_data:
        p["_status"] = compute_status(p)

    # KPIs sobre todo o estoque
    total_produtos       = len(estoque_data)
    total_estoque_un     = int(sum(float(p.get("quantidade", 0) or 0) for p in estoque_data))
    estoque_baixo_count  = sum(1 for p in estoque_data if p["_status"] in ("BAIXO", "CRÍTICO"))
    valor_total_estoque  = round(sum(
        float(p.get("valor_investido", 0) or 0) * float(p.get("quantidade", 0) or 0)
        for p in estoque_data
    ), 2)

    categorias = sorted(set(
        (p.get("categoria") or "").strip() for p in estoque_data
        if (p.get("categoria") or "").strip()
    ))

    q             = (request.args.get("q", "")          or "").strip()
    codigo_filtro = (request.args.get("codigo", "")      or "").strip().upper()
    cat_filtro    = (request.args.get("categoria", "")   or "").strip()
    status_filtro = (request.args.get("status", "")      or "").strip()
    margem_min_raw = (request.args.get("margem_min", "") or "").strip()
    margem_max_raw = (request.args.get("margem_max", "") or "").strip()

    estoque_filtrado = estoque_data
    if q:
        q_low = q.lower()
        estoque_filtrado = [p for p in estoque_filtrado if q_low in str(p.get("nome", "")).lower()]
    if codigo_filtro:
        estoque_filtrado = [p for p in estoque_filtrado if (p.get("codigo") or "").upper() == codigo_filtro]
    if cat_filtro:
        estoque_filtrado = [p for p in estoque_filtrado if (p.get("categoria") or "") == cat_filtro]
    if status_filtro:
        estoque_filtrado = [p for p in estoque_filtrado if p["_status"] == status_filtro]
    if margem_min_raw or margem_max_raw:
        def _margem_p(p):
            venda = float(p.get("valor_venda", 0) or 0)
            custo = float(p.get("valor_investido", 0) or 0)
            return ((venda - custo) / venda * 100) if venda > 0 else 0
        if margem_min_raw:
            m_min = to_float(margem_min_raw)
            estoque_filtrado = [p for p in estoque_filtrado if _margem_p(p) >= m_min]
        if margem_max_raw:
            m_max = to_float(margem_max_raw)
            estoque_filtrado = [p for p in estoque_filtrado if _margem_p(p) <= m_max]

    nomes_sugestoes = sorted(set(
        p.get("nome", "").strip() for p in estoque_data
        if p.get("nome", "").strip()
    ))

    return render_template(
        "estoque.html",
        estoque=estoque_filtrado,
        q=q,
        codigo_filtro=codigo_filtro,
        cat_filtro=cat_filtro,
        status_filtro=status_filtro,
        margem_min_raw=margem_min_raw,
        margem_max_raw=margem_max_raw,
        categorias=categorias,
        total_produtos=total_produtos,
        total_estoque_un=total_estoque_un,
        estoque_baixo_count=estoque_baixo_count,
        valor_total_estoque=valor_total_estoque,
        erro="",
        tipo=session.get("tipo"),
        permissoes=session.get("permissoes", []),
        nomes_sugestoes=nomes_sugestoes,
    )


@app.route("/estoque/exportar")
def estoque_exportar():
    if not usuario_logado():
        return redirect(url_for("login"))
    if not tem_permissao("ver_estoque"):
        return redirect(url_for("vendas"))

    from io import StringIO
    import csv as csv_mod

    produtos = db.get_all_products()
    produtos.sort(key=lambda p: str(p.get("nome", "")).strip().lower())

    buf = StringIO()
    w = csv_mod.writer(buf, delimiter=";")
    w.writerow(["Nome", "Categoria", "Estoque", "Est. mínimo", "Custo médio", "Preço venda", "Lucro un."])
    for p in produtos:
        lucro = float(p.get("valor_venda", 0) or 0) - float(p.get("valor_investido", 0) or 0)
        w.writerow([
            p.get("nome", ""),
            p.get("categoria", ""),
            int(float(p.get("quantidade", 0) or 0)),
            p.get("estoque_minimo", ""),
            f"{float(p.get('valor_investido', 0) or 0):.2f}".replace(".", ","),
            f"{float(p.get('valor_venda', 0) or 0):.2f}".replace(".", ","),
            f"{lucro:.2f}".replace(".", ","),
        ])

    from flask import Response
    return Response(
        "\ufeff" + buf.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=estoque.csv"}
    )


# ================== CADASTRAR PRODUTO ==================
@app.route("/estoque/novo", methods=["GET", "POST"])
def estoque_novo():
    if not usuario_logado():
        return redirect(url_for("login"))

    if not tem_permissao("cadastrar_produto"):
        return redirect(url_for("vendas"))

    erro = ""

    if request.method == "POST":
        nome          = (request.form.get("nome", "") or "").strip()
        data          = (request.form.get("data", "") or "").strip()
        valor_venda   = to_float(request.form.get("valor_venda", "0"))
        forma_entrada = request.form.get("forma_entrada", "unidade")
        tipo_custo    = request.form.get("tipo_custo", "total")
        data_entrada  = request.form.get("data_entrada") or data or today_brt().isoformat()

        if forma_entrada == "caixa":
            qtd_caixas = to_int(request.form.get("qtd_caixas"), 0)
            en_upc     = to_int(request.form.get("en_upc"), 1)
            quantidade = qtd_caixas * en_upc
        else:
            quantidade = to_int(request.form.get("quantidade"), 0)

        if tipo_custo == "total":
            custo_total     = to_float(request.form.get("custo_total"), 0.0)
            valor_investido = custo_total / quantidade if quantidade > 0 else 0.0
        else:
            valor_investido = to_float(request.form.get("valor_investido"), 0.0)
        extras = {
            "categoria":          request.form.get("categoria") or None,
            "tipo_controle":      request.form.get("tipo_controle") or "UN",
            "conteudo_valor":     to_float(request.form.get("conteudo_valor"), None) if request.form.get("conteudo_valor") else None,
            "conteudo_unidade":   request.form.get("conteudo_unidade") or None,
            "unidade_venda":      request.form.get("unidade_venda") or "UN",
            "estoque_minimo":     to_int(request.form.get("estoque_minimo"), 0),
            "validade":           request.form.get("validade") or None,
            "fornecedor":         request.form.get("fornecedor") or None,
            "tipo_conversao":     request.form.get("tipo_conversao") or None,
            "unidades_por_caixa": to_int(request.form.get("unidades_por_caixa"), None) if request.form.get("unidades_por_caixa") else None,
            "observacoes":        request.form.get("observacoes") or None,
        }

        if not nome:
            erro = "Informe o nome do produto."
        elif quantidade <= 0:
            erro = "Quantidade inválida."
        elif valor_investido <= 0:
            erro = "Informe o custo do produto."
        else:
            nome_key = nome.lower()
            estoque_data = db.get_all_products()
            existente = next(
                (p for p in estoque_data if str(p.get("nome", "")).strip().lower() == nome_key), None
            )

            if existente:
                existente["data"] = data
                existente["valor_venda"] = valor_venda
                existente.update(extras)
                try:
                    entrada_estoque_lote(existente, data_entrada, quantidade, valor_investido, usuario=session.get("usuario", ""))
                    db.update_product(existente)
                    db.registrar_log(
                        session.get("usuario", ""), "estoque_alterado",
                        f"Alterou estoque: {nome} (+{quantidade})",
                        now_brt().strftime("%Y-%m-%d"), now_brt().strftime("%H:%M"),
                    )
                except ValueError as e:
                    erro = str(e)
            else:
                novo = {
                    "pid": uuid.uuid4().hex,
                    "data": data,
                    "nome": nome,
                    "valor_venda": valor_venda,
                    "lotes": [],
                    "criado_por": session.get("usuario", ""),
                    **extras,
                }
                try:
                    entrada_estoque_lote(novo, data_entrada, quantidade, valor_investido, usuario=session.get("usuario", ""))
                except ValueError as e:
                    erro = str(e)

                if not erro:
                    db.insert_product(novo)
                    db.registrar_log(
                        session.get("usuario", ""), "produto_criado", f"Cadastrou produto: {nome}",
                        now_brt().strftime("%Y-%m-%d"), now_brt().strftime("%H:%M"),
                    )

            if not erro:
                return redirect(url_for("estoque"))

    return render_template(
        "estoque_novo.html",
        erro=erro,
        hoje=today_brt().isoformat(),
        fornecedores=[],
        tipo=session.get("tipo"),
        permissoes=session.get("permissoes", []),
    )


# ================== EDITAR PRODUTO ==================
@app.route("/estoque/editar/<pid>", methods=["GET", "POST"])
def editar_produto(pid):
    if not usuario_logado():
        return redirect(url_for("login"))

    # Ver o formulário exige só poder ver o estoque — quem chega até aqui sem
    # a permissão de editar precisa de autorização por PIN pra salvar (abaixo).
    if not tem_permissao("ver_estoque"):
        return redirect(url_for("vendas"))

    produto = db.get_product(pid)
    if not produto:
        return redirect(url_for("estoque"))

    erro = ""

    if request.method == "POST":
        redir = autorizar_acao("editar_produto")
        if redir:
            return redir

        pode_preco = session.get("tipo") == "admin" or tem_permissao("alterar_preco_venda")
        pode_custo = session.get("tipo") == "admin" or tem_permissao("alterar_custo_produto")

        nome              = (request.form.get("nome") or "").strip()
        data              = (request.form.get("data") or "").strip()
        valor_venda_atual = to_float(produto.get("valor_venda"), 0.0)
        valor_venda       = to_float(request.form.get("valor_venda"), valor_venda_atual) if pode_preco else valor_venda_atual
        valor_investido   = to_float(request.form.get("valor_investido"), 0.0)

        if not nome:
            erro = "Informe o nome do produto."
        elif valor_venda <= 0:
            erro = "Preço de venda inválido."
        else:
            produto["nome"]        = nome
            produto["data"]        = data
            produto["valor_venda"] = valor_venda
            if pode_custo and valor_investido > 0:
                produto["valor_investido"] = valor_investido
            produto["quantidade"] = estoque_qtd_total(produto)
            produto["categoria"]          = request.form.get("categoria") or None
            produto["tipo_controle"]      = request.form.get("tipo_controle") or "UN"
            produto["conteudo_valor"]     = to_float(request.form.get("conteudo_valor"), None) if request.form.get("conteudo_valor") else None
            produto["conteudo_unidade"]   = request.form.get("conteudo_unidade") or None
            produto["unidade_venda"]      = request.form.get("unidade_venda") or "UN"
            produto["estoque_minimo"]     = to_int(request.form.get("estoque_minimo"), 0)
            produto["validade"]           = request.form.get("validade") or None
            produto["fornecedor"]         = request.form.get("fornecedor") or None
            produto["tipo_conversao"]     = request.form.get("tipo_conversao") or None
            produto["unidades_por_caixa"] = to_int(request.form.get("unidades_por_caixa"), None) if request.form.get("unidades_por_caixa") else None
            produto["observacoes"]        = request.form.get("observacoes") or None
            db.update_product(produto)
            db.registrar_log(
                session.get("usuario", ""), "produto_editado", f"Editou produto: {nome}",
                now_brt().strftime("%Y-%m-%d"), now_brt().strftime("%H:%M"),
            )
            return redirect(url_for("estoque"))

    return render_template(
        "editar_produto.html",
        produto=produto,
        erro=erro,
        tipo=session.get("tipo"),
        permissoes=session.get("permissoes", [])
    )


@app.route("/editar_produto/<pid>")
def editar_produto_alias(pid):
    return redirect(url_for("editar_produto", pid=pid))


# ================== REMOVER PRODUTO ==================
@app.route("/estoque/remover/<pid>", methods=["POST"])
def remover_produto(pid):
    if not usuario_logado():
        return redirect(url_for("login"))

    if session.get("tipo") != "admin" and not tem_permissao("excluir_produto"):
        return redirect(url_for("estoque"))

    db.delete_product(pid)
    return redirect(url_for("estoque"))


# ================== LOTES DO PRODUTO ==================
@app.route("/estoque/produto/<pid>", methods=["GET", "POST"])
def estoque_produto(pid):
    if not usuario_logado():
        return redirect(url_for("login"))
    if not tem_permissao("ver_estoque"):
        return redirect(url_for("vendas"))

    produto = db.get_product(pid)
    if not produto:
        return redirect(url_for("estoque"))

    erro = ""

    if request.method == "POST":
        forma_entrada    = request.form.get("forma_entrada", "unidade")
        data             = (request.form.get("data") or "").strip()
        nome_lote        = (request.form.get("nome_lote") or "").strip() or None
        fornecedor_lote  = request.form.get("fornecedor") or None
        validade_lote    = request.form.get("validade") or None
        tipo_custo       = request.form.get("tipo_custo", "unitario")

        if forma_entrada == "caixa":
            qtd_caixas     = to_int(request.form.get("qtd_caixas"), 0)
            unid_por_caixa = to_int(request.form.get("unidades_por_caixa"), 0)
            qtd            = qtd_caixas * unid_por_caixa
            qtd_informada  = qtd_caixas
        else:
            qtd            = to_int(request.form.get("quantidade"), 0)
            qtd_informada  = qtd
            unid_por_caixa = None

        if tipo_custo == "total":
            custo_total_val = to_float(request.form.get("custo_total"), 0.0)
            custo_unit      = round(custo_total_val / qtd, 4) if qtd > 0 else 0.0
        else:
            custo_unit = to_float(request.form.get("custo_unit"), 0.0)

        if qtd <= 0:
            erro = "Quantidade inválida."
        elif custo_unit <= 0:
            erro = "Custo unitário inválido."
        else:
            conteudo_val  = to_float(request.form.get("conteudo_valor"), None) if request.form.get("conteudo_valor") else None
            conteudo_unit = request.form.get("conteudo_unidade") or None
            lote = {
                "data":           normalizar_data_str(data),
                "qtd":            qtd,
                "custo_unit":     custo_unit,
                "forma_entrada":  forma_entrada,
                "qtd_informada":  qtd_informada,
                "criado_por":     session.get("usuario", ""),
            }
            if unid_por_caixa:
                lote["unidades_por_caixa"] = unid_por_caixa
            if conteudo_val:
                lote["conteudo_valor"]  = conteudo_val
                lote["conteudo_unidade"] = conteudo_unit or "ml"
            if nome_lote:
                lote["nome_lote"] = nome_lote
            if fornecedor_lote:
                lote["fornecedor"] = fornecedor_lote
            if validade_lote:
                lote["validade"] = validade_lote
            produto.setdefault("lotes", []).append(lote)
            atualizar_campos_derivados_estoque(produto)
            if conteudo_val and not produto.get("conteudo_valor"):
                produto["conteudo_valor"]   = conteudo_val
                produto["conteudo_unidade"] = conteudo_unit or "ml"
            db.update_product(produto)
            return redirect(url_for("estoque_produto", pid=pid))

    lotes = produto.get("lotes", [])
    if isinstance(lotes, list):
        lotes = sorted(lotes, key=lambda l: str(l.get("data", "")))
    else:
        lotes = []

    pacotes_usando = db.bar_get_packages_using_product(pid)

    saidas = sorted(
        [s for s in db.get_sales_by_product(pid) if s.get("produto_tipo") == "estoque"],
        key=lambda s: s.get("data", ""),
        reverse=True
    )

    return render_template(
        "estoque_produto.html",
        produto=produto,
        lotes=lotes,
        saidas=saidas,
        erro=erro,
        hoje=today_brt().isoformat(),
        pacotes_usando=pacotes_usando,
        tipo=session.get("tipo"),
        permissoes=session.get("permissoes", [])
    )


@app.route("/estoque/produto/<pid>/lote/<int:idx>/remover", methods=["POST"])
def remover_lote(pid, idx):
    if not usuario_logado() or not tem_permissao("editar_estoque"):
        return redirect(url_for("login"))
    produto = db.get_product(pid)
    if produto:
        lotes = produto.get("lotes", [])
        if isinstance(lotes, list) and 0 <= idx < len(lotes):
            lotes_sorted = sorted(lotes, key=lambda l: str(l.get("data", "")))
            lote_alvo = lotes_sorted[idx]
            produto["lotes"] = [l for l in lotes if l is not lote_alvo]
            atualizar_campos_derivados_estoque(produto)
            db.update_product(produto)
    return redirect(url_for("estoque_produto", pid=pid))


@app.route("/estoque/produto/<pid>/lote/<int:idx>/editar", methods=["POST"])
def editar_lote(pid, idx):
    if not usuario_logado() or not tem_permissao("editar_estoque"):
        return redirect(url_for("login"))
    produto = db.get_product(pid)
    if produto:
        lotes = produto.get("lotes", [])
        if isinstance(lotes, list):
            lotes_sorted = sorted(lotes, key=lambda l: str(l.get("data", "")))
            if 0 <= idx < len(lotes_sorted):
                lote_alvo = lotes_sorted[idx]
                nova_data     = (request.form.get("data") or "").strip()
                nova_qtd      = to_int(request.form.get("quantidade"), 0)
                novo_custo    = to_float(request.form.get("custo_unit"), 0.0)
                novo_nome_lote = (request.form.get("nome_lote") or "").strip()
                if nova_qtd > 0 and novo_custo > 0:
                    for l in produto["lotes"]:
                        if l is lote_alvo:
                            l["data"]      = normalizar_data_str(nova_data)
                            l["qtd"]       = nova_qtd
                            l["custo_unit"] = novo_custo
                            if novo_nome_lote:
                                l["nome_lote"] = novo_nome_lote
                            else:
                                l.pop("nome_lote", None)
                            break
                    atualizar_campos_derivados_estoque(produto)
                    db.update_product(produto)
    return redirect(url_for("estoque_produto", pid=pid))


# ================== CADASTRAR VENDA ==================
@app.route("/cadastrar_venda", methods=["GET", "POST"])
def cadastrar_venda():
    if not usuario_logado():
        return redirect(url_for("login"))

    if not tem_permissao("criar_vendas"):
        return redirect(url_for("vendas"))

    estoque = db.get_all_products()
    erro = ""

    if request.method == "GET":
        hoje = now_brt().strftime("%Y-%m-%d")
        return render_template(
            "cadastrar_venda.html",
            estoque=estoque,
            erro="",
            hoje=hoje,
            tipo=session.get("tipo"),
            permissoes=session.get("permissoes", [])
        )

    try:
        caixa_aberto_cv = db.get_caixa_aberto()
    except Exception:
        caixa_aberto_cv = None

    if not caixa_aberto_cv:
        hoje = now_brt().strftime("%Y-%m-%d")
        return render_template(
            "cadastrar_venda.html",
            estoque=estoque,
            erro="⚠️ Nenhum caixa aberto. Abra o caixa antes de registrar vendas.",
            hoje=hoje,
            tipo=session.get("tipo"),
            permissoes=session.get("permissoes", [])
        )

    data = (request.form.get("data") or "").strip()
    forma_pagamento = (request.form.get("forma_pagamento") or "").strip()

    pids = request.form.getlist("produto_pid[]")
    quantidades = request.form.getlist("quantidade[]")

    if not pids or not quantidades:
        pid_unico = (request.form.get("produto_pid") or "").strip()
        qtd_unica = (request.form.get("quantidade") or "").strip()
        if pid_unico and qtd_unica:
            pids = [pid_unico]
            quantidades = [qtd_unica]

    if not pids or not quantidades or len(pids) != len(quantidades):
        erro = "Adicione pelo menos 1 item na venda."
    else:
        id_venda = f"V{int(time.time())}{uuid.uuid4().hex[:4]}"
        itens = []

        for pid, qtd_raw in zip(pids, quantidades):
            pid = (pid or "").strip()
            qtd = to_int(qtd_raw, 0)
            if not pid or qtd <= 0:
                continue
            produto = next((p for p in estoque if str(p.get("pid", "")) == pid), None)
            if not produto:
                erro = "Produto não encontrado no estoque."
                break
            itens.append((produto, qtd))

        if not erro and not itens:
            erro = "Adicione pelo menos 1 produto com quantidade válida."

        if not erro:
            novas_vendas = []
            produtos_modificados = []

            for produto, qtd in itens:
                try:
                    custo_total, consumo = consumir_fifo(produto, qtd)
                except ValueError as e:
                    erro = str(e)
                    break

                custo_unit_medio = (custo_total / qtd) if qtd else 0.0
                novas_vendas.append({
                    "uid": uuid.uuid4().hex,
                    "id_venda": id_venda,
                    "data": data,
                    "produto_pid": produto.get("pid"),
                    "nome": produto.get("nome", ""),
                    "valor_venda": to_float(produto.get("valor_venda", 0)),
                    "valor_investido": round(custo_unit_medio, 4),
                    "quantidade": qtd,
                    "forma_pagamento": forma_pagamento,
                    "consumo_lotes": consumo,
                    "hora": now_brt().strftime("%H:%M"),
                    "usuario": session.get("usuario", ""),
                })
                produtos_modificados.append(produto)

            if not erro:
                caixa_id_atual = caixa_aberto_cv["id"] if caixa_aberto_cv else None
                try:
                    for venda in novas_vendas:
                        venda["caixa_id"] = caixa_id_atual
                        db.insert_sale(venda)
                    for produto in produtos_modificados:
                        db.update_product(produto)
                except RuntimeError as e:
                    erro = str(e)
                else:
                    return redirect(url_for("cadastrar_venda_lote") + "?sucesso=1")

    hoje = now_brt().strftime("%Y-%m-%d")
    comps_por_produto_leg = db.bar_get_all_components_grouped()
    drinks = []
    for d in db.bar_get_all_products():
        if d["type"] != "composite":
            continue
        comps = comps_por_produto_leg.get(d["id"], [])
        d["componentes"] = [{"nome": c.get("component", {}).get("name", "?"),
                              "quantidade": c.get("quantity", 0),
                              "unidade_uso": c.get("unidade_uso") or "UN"} for c in comps]
        drinks.append(d)
    return render_template(
        "cadastrar_venda_lote.html",
        estoque=estoque,
        drinks=drinks,
        erro=erro,
        hoje=hoje,
        tipo=session.get("tipo"),
        permissoes=session.get("permissoes", [])
    )


# ================== CADASTRAR VENDA (LOTE) ==================
@app.route("/cadastrar_venda_lote", methods=["GET", "POST"])
def cadastrar_venda_lote():
    if not usuario_logado():
        return redirect(url_for("login"))

    if not tem_permissao("criar_vendas"):
        return redirect(url_for("vendas"))

    estoque = db.get_all_products()
    comps_por_produto = db.bar_get_all_components_grouped()
    drinks = []
    for d in db.bar_get_all_products():
        if d["type"] != "composite":
            continue
        comps = comps_por_produto.get(d["id"], [])
        d["componentes"] = [
            {"nome": c.get("component", {}).get("name", "?"),
             "quantidade": c.get("quantity", 0),
             "unidade_uso": c.get("unidade_uso") or "UN"}
            for c in comps
        ]
        drinks.append(d)
    try:
        caixa_aberto = db.get_caixa_aberto()
    except Exception:
        caixa_aberto = None
    erro = ""

    if request.method == "POST":
        if not caixa_aberto:
            erro = "⚠️ Nenhum caixa aberto. Abra o caixa antes de registrar vendas."

        data = request.form.get("data", "")
        forma_pagamento = request.form.get("forma_pagamento", "")

        pids   = request.form.getlist("produto_pid[]")
        qtds   = request.form.getlist("quantidade[]")
        tipos  = request.form.getlist("produto_tipo[]")
        vpcs   = request.form.getlist("vender_por_conteudo[]")
        precos = request.form.getlist("preco_venda[]")

        # garante que tipos, vpcs e precos têm o mesmo tamanho que pids
        while len(tipos) < len(pids):
            tipos.append("estoque")
        while len(vpcs) < len(pids):
            vpcs.append("0")
        while len(precos) < len(pids):
            precos.append("")

        # desconto/preço manual só vale se o usuário tiver a permissão — sem
        # ela, o preço submetido é ignorado e usamos sempre o preço de catálogo.
        pode_descontar = session.get("tipo") == "admin" or tem_permissao("aplicar_desconto")

        def _preco_final(preco_catalogo, preco_raw):
            if not pode_descontar:
                return preco_catalogo
            preco_submetido = to_float(preco_raw, 0)
            if 0 < preco_submetido <= preco_catalogo:
                return preco_submetido
            return preco_catalogo

        if not pids or not qtds or len(pids) != len(qtds):
            if not erro:
                erro = "Adicione pelo menos 1 item na venda."
        elif not erro:
            id_venda = f"V{int(time.time())}{uuid.uuid4().hex[:4]}"
            novas_vendas = []
            produtos_modificados = []

            for pid, qtd_raw, tipo, vpc_flag, preco_raw in zip(pids, qtds, tipos, vpcs, precos):
                pid = (pid or "").strip()
                qtd = to_int(qtd_raw, 0)
                vpc = vpc_flag == "1"
                if not pid or qtd <= 0:
                    continue

                if tipo == "bar":
                    bar_prod = db.bar_get_product(pid)
                    if not bar_prod:
                        erro = f"Drink '{pid}' não encontrado."
                        break
                    try:
                        custo_total, consumo = bar_calc_drink_cost(pid, qtd)
                    except Exception as e:
                        erro = str(e)
                        break
                    custo_unit = round(custo_total / qtd, 4) if qtd else 0.0
                    preco_catalogo = to_float(bar_prod.get("sale_price", 0))
                    novas_vendas.append({
                        "uid": uuid.uuid4().hex,
                        "id_venda": id_venda,
                        "data": data,
                        "produto_pid": pid,
                        "produto_tipo": "bar",
                        "nome": bar_prod["name"],
                        "valor_venda": _preco_final(preco_catalogo, preco_raw),
                        "valor_investido": custo_unit,
                        "quantidade": qtd,
                        "forma_pagamento": forma_pagamento,
                        "consumo_lotes": consumo,
                        "hora": now_brt().strftime("%H:%M"),
                        "usuario": session.get("usuario", ""),
                    })
                else:
                    produto = next((p for p in estoque if str(p.get("pid", "")) == pid), None)
                    if not produto:
                        erro = "Produto não encontrado no estoque."
                        break
                    cv = float((produto or {}).get("conteudo_valor") or 1)
                    try:
                        if vpc and cv > 1:
                            qtd_estoque = qtd / cv
                            custo_total, consumo = bar_fifo_consume(pid, qtd_estoque)
                            # bar_fifo_consume já salva internamente
                        else:
                            custo_total, consumo = consumir_fifo(produto, qtd)
                            produtos_modificados.append(produto)
                    except ValueError as e:
                        erro = str(e)
                        break
                    custo_unit = round(custo_total / qtd, 4) if qtd else 0.0
                    preco_catalogo = to_float(produto.get("valor_venda", 0)) / cv if (vpc and cv > 1) else to_float(produto.get("valor_venda", 0))
                    preco_unit = _preco_final(preco_catalogo, preco_raw)
                    novas_vendas.append({
                        "uid": uuid.uuid4().hex,
                        "id_venda": id_venda,
                        "data": data,
                        "produto_pid": produto.get("pid"),
                        "produto_tipo": "estoque",
                        "nome": produto.get("nome", ""),
                        "valor_venda": round(preco_unit, 4),
                        "valor_investido": custo_unit,
                        "quantidade": qtd,
                        "forma_pagamento": forma_pagamento,
                        "consumo_lotes": consumo,
                        "hora": now_brt().strftime("%H:%M"),
                        "usuario": session.get("usuario", ""),
                    })

            if not erro and not novas_vendas:
                erro = "Adicione pelo menos 1 produto com quantidade válida."

            if not erro:
                caixa_id_atual = caixa_aberto["id"] if caixa_aberto else None
                try:
                    for venda in novas_vendas:
                        venda["caixa_id"] = caixa_id_atual
                        db.insert_sale(venda)
                    for produto in produtos_modificados:
                        db.update_product(produto)
                except RuntimeError as e:
                    erro = str(e)
                else:
                    total_venda = sum(to_float(v.get("valor_venda", 0)) * to_int(v.get("quantidade", 0)) for v in novas_vendas)
                    desc = novas_vendas[0]["nome"] if len(novas_vendas) == 1 else f"{len(novas_vendas)} itens"
                    db.registrar_log(
                        session.get("usuario", ""), "venda_criada",
                        f"Criou venda: {desc} (R$ {total_venda:.2f})",
                        now_brt().strftime("%Y-%m-%d"), now_brt().strftime("%H:%M"),
                    )
                    return redirect(url_for("cadastrar_venda_lote") + "?sucesso=1")

    hoje = now_brt().strftime("%Y-%m-%d")
    return render_template(
        "cadastrar_venda_lote.html",
        estoque=estoque,
        drinks=drinks,
        erro=erro,
        hoje=hoje,
        caixa_aberto=caixa_aberto,
        usuario=session.get("usuario", ""),
        tipo=session.get("tipo"),
        permissoes=session.get("permissoes", [])
    )


# ================== COMANDAS ==================
@app.route("/comandas")
def comandas():
    if not usuario_logado():
        return redirect(url_for("login"))
    if session.get("tipo") != "admin" and not tem_permissao("ver_vendas"):
        return redirect(url_for("vendas"))

    hoje = now_brt().strftime("%Y-%m-%d")
    todas = db.get_all_commands()

    def total_comanda(c):
        return sum(
            to_float(i.get("valor_venda", 0)) * to_int(i.get("quantidade", 0))
            for i in c.get("itens", [])
        )

    def valor_pago(c):
        return sum(to_float(p.get("valor", 0)) for p in c.get("pagamentos", []))

    comandas_exibir = [c for c in todas if isinstance(c, dict)]
    for c in comandas_exibir:
        if c.get("status") == "agendado" and c.get("vencimento") and c["vencimento"] < hoje:
            c["_status_display"] = "atrasada"
        else:
            c["_status_display"] = c.get("status", "aberta")
        c["_total"] = round(total_comanda(c), 2)
        c["_valor_pago"] = round(valor_pago(c), 2)
        c["_saldo"] = round(max(c["_total"] - c["_valor_pago"], 0), 2)

    comandas_exibir.sort(
        key=lambda c: (c.get("data_abertura", "") + "T" + c.get("hora_abertura", "")),
        reverse=True
    )
    nomes_sugestoes = sorted(set(c.get("nome", "") for c in comandas_exibir if c.get("nome")))

    stats = {s: {"count": 0, "total": 0.0} for s in
             ("aberta", "aguardando_pagamento", "pagamento_parcial", "agendado", "quitada", "atrasada")}
    for c in comandas_exibir:
        s = c["_status_display"]
        if s in stats:
            stats[s]["count"] += 1
            stats[s]["total"] += c["_saldo"]

    erro_caixa = session.pop("erro_caixa", None)

    return render_template(
        "comandas.html",
        comandas=comandas_exibir,
        tipo=session.get("tipo"),
        permissoes=session.get("permissoes", []),
        usuario=session.get("usuario", "Administrador"),
        nomes_sugestoes=nomes_sugestoes,
        hoje=hoje,
        stats=stats,
        erro_caixa=erro_caixa,
    )


@app.route("/comandas/nova", methods=["POST"])
def comanda_nova():
    if not usuario_logado():
        return redirect(url_for("login"))
    if session.get("tipo") != "admin" and not tem_permissao("ver_vendas"):
        return redirect(url_for("vendas"))

    tipo = (request.form.get("tipo_atendimento") or "avulso").strip()
    nome = (request.form.get("nome") or "").strip()
    mesa = (request.form.get("mesa") or "").strip()
    cliente_id = (request.form.get("cliente_id") or "").strip() or None

    if tipo == "fiado" and cliente_id:
        cliente = db.get_cliente(cliente_id)
        if cliente:
            nome = cliente.get("nome", nome)

    if not nome:
        return redirect(url_for("comandas"))

    nova = {
        "cid": uuid.uuid4().hex,
        "nome": nome,
        "mesa": mesa,
        "tipo_atendimento": tipo,
        "cliente_id": cliente_id,
        "limite": None,
        "data_abertura": now_brt().strftime("%Y-%m-%d"),
        "hora_abertura": now_brt().strftime("%H:%M"),
        "status": "aberta",
        "itens": [],
        "pagamentos": [],
        "criado_por": session.get("usuario", ""),
    }
    db.insert_command(nova)
    db.registrar_log(
        session.get("usuario", ""), "comanda_criada", f"Criou comanda: {nome}",
        now_brt().strftime("%Y-%m-%d"), now_brt().strftime("%H:%M"),
    )
    return redirect(url_for("comandas"))


# ==================== CLIENTES ====================

@app.route("/clientes/buscar")
def clientes_buscar():
    if not usuario_logado():
        return jsonify([]), 401
    import unicodedata
    def _norm(s):
        return unicodedata.normalize("NFD", (s or "").lower()).encode("ascii", "ignore").decode()
    q = (request.args.get("q") or "").strip()
    clientes = db.get_all_clientes()
    if q:
        clientes = [
            c for c in clientes
            if _norm(q) in _norm(c.get("nome", ""))
            or q in (c.get("telefone", "") or "")
            or q in (c.get("cpf", "") or "")
        ]
    return jsonify(clientes[:10])


@app.route("/clientes/<cid>/info")
def cliente_info(cid):
    if not usuario_logado():
        return jsonify({}), 401
    c = db.get_cliente(cid)
    if not c:
        return jsonify({}), 404
    saldo = db.get_saldo_cliente(cid)
    limite = float(c.get("limite_credito") or 0)
    return jsonify({
        **c,
        "saldo_utilizado": saldo,
        "credito_disponivel": round(limite - saldo, 2)
    })


@app.route("/clientes/novo", methods=["POST"])
def cliente_novo():
    if not usuario_logado():
        return jsonify({"erro": "não autorizado"}), 401
    if session.get("tipo") != "admin" and not tem_permissao("ver_vendas"):
        return jsonify({"erro": "não autorizado"}), 403
    nome = (request.form.get("nome") or "").strip()
    if not nome:
        return jsonify({"erro": "Nome obrigatório"}), 400
    novo = {
        "id": uuid.uuid4().hex,
        "nome": nome,
        "apelido": (request.form.get("apelido") or "").strip(),
        "telefone": (request.form.get("telefone") or "").strip(),
        "cpf": (request.form.get("cpf") or "").strip(),
        "rg": (request.form.get("rg") or "").strip(),
        "endereco": (request.form.get("endereco") or "").strip(),
        "observacoes": (request.form.get("observacoes") or "").strip(),
        "limite_credito": to_float(request.form.get("limite_credito") or "0"),
        "dia_vencimento": to_int(request.form.get("dia_vencimento") or "0") or None,
        "proximo_vencimento": (request.form.get("proximo_vencimento") or "").strip(),
        "data_cadastro": now_brt().strftime("%Y-%m-%d"),
    }
    try:
        db.insert_cliente(novo)
    except RuntimeError as e:
        return jsonify({"erro": str(e)}), 500
    db.registrar_log(
        session.get("usuario", ""), "cliente_criado", f"Criou cliente: {nome}",
        now_brt().strftime("%Y-%m-%d"), now_brt().strftime("%H:%M"),
    )
    return jsonify(novo)


@app.route("/comandas/<cid>")
def comanda_detalhe(cid):
    if not usuario_logado():
        return redirect(url_for("login"))
    if session.get("tipo") != "admin" and not tem_permissao("ver_vendas"):
        return redirect(url_for("vendas"))

    comanda = db.get_command(cid)
    if not comanda:
        return redirect(url_for("comandas"))

    estoque = db.get_all_products()
    estoque_disponivel = [p for p in estoque if isinstance(p, dict)]
    estoque_disponivel.sort(key=lambda p: str(p.get("nome", "")).lower())

    bar_pacotes = [p for p in db.bar_get_all_products() if p.get("type") == "composite"]
    bar_pacotes.sort(key=lambda p: str(p.get("name", "")).lower())

    total = sum(
        to_float(i.get("valor_venda", 0)) * to_int(i.get("quantidade", 0))
        for i in comanda.get("itens", [])
    )

    return render_template(
        "comanda_detalhe.html",
        comanda=comanda,
        estoque=estoque_disponivel,
        bar_pacotes=bar_pacotes,
        total=round(total, 2),
        erro=session.pop("erro_comanda", ""),
        tipo=session.get("tipo"),
        permissoes=session.get("permissoes", []),
        usuario=session.get("usuario", "")
    )


@app.route("/comandas/<cid>/adicionar", methods=["POST"])
def comanda_adicionar(cid):
    if not usuario_logado():
        return redirect(url_for("login"))
    if session.get("tipo") != "admin" and not tem_permissao("ver_vendas"):
        return redirect(url_for("vendas"))

    comanda = db.get_command(cid)
    if not comanda or comanda.get("status") != "aberta":
        return redirect(url_for("comandas"))

    pid = (request.form.get("produto_pid") or "").strip()
    qtd = to_int(request.form.get("quantidade", "1"), 0)
    produto_tipo = (request.form.get("produto_tipo") or "estoque").strip()
    vender_por_conteudo = request.form.get("vender_por_conteudo") == "1"
    erro = ""

    if produto_tipo == "bar":
        bar_prod = db.bar_get_product(pid)
        if not bar_prod:
            erro = "Pacote não encontrado."
        elif qtd <= 0:
            erro = "Quantidade inválida."
        else:
            try:
                components = db.bar_get_components(pid)
                if not components:
                    raise ValueError("Este pacote não possui componentes cadastrados.")
                components_consumo = []
                total_cost = 0.0
                all_logs = []
                for comp in components:
                    unidade_uso = (comp.get("unidade_uso") or "UN").strip()
                    qtd_bruta = float(comp["quantity"]) * qtd
                    comp_pid = (comp.get("component_id") or "").replace("-", "")
                    produto_comp = db.get_product(comp_pid)
                    cv = float((produto_comp or {}).get("conteudo_valor") or 1)
                    needed = qtd_bruta / cv if (cv > 1 and unidade_uso.upper() != "UN") else qtd_bruta
                    cost, log = bar_fifo_consume(comp_pid, needed)
                    total_cost += cost
                    all_logs.extend(log)
                    components_consumo.append({"pid": comp_pid, "consumo": log})

                custo_unit = round(total_cost / qtd, 4) if qtd else 0.0
                hoje_item = now_brt().strftime("%Y-%m-%d")
                hora_item = now_brt().strftime("%H:%M")
                sale_uid = uuid.uuid4().hex
                db.insert_sale({
                    "uid": sale_uid,
                    "comanda_id": comanda["cid"],
                    "comanda_nome": comanda["nome"],
                    "data": hoje_item,
                    "hora": hora_item,
                    "produto_pid": pid,
                    "produto_tipo": "bar",
                    "nome": bar_prod["name"],
                    "valor_venda": to_float(bar_prod.get("sale_price", 0)),
                    "valor_investido": custo_unit,
                    "quantidade": qtd,
                    "status": "pendente",
                    "consumo_lotes": all_logs,
                    "usuario": session.get("usuario", ""),
                })
                comanda["itens"].append({
                    "item_id": uuid.uuid4().hex,
                    "sale_uid": sale_uid,
                    "produto_pid": pid,
                    "produto_tipo": "bar",
                    "nome": bar_prod["name"],
                    "quantidade": qtd,
                    "valor_venda": to_float(bar_prod.get("sale_price", 0)),
                    "valor_investido": custo_unit,
                    "consumo_lotes": all_logs,
                    "bar_consumo_components": components_consumo,
                    "data": hoje_item,
                    "hora": hora_item
                })
                db.update_command(comanda)
            except (ValueError, RuntimeError) as e:
                erro = str(e)
    else:
        produto = db.get_product(pid)
        if not produto:
            erro = "Produto não encontrado."
        elif qtd <= 0:
            erro = "Quantidade inválida."
        else:
            try:
                cv = float((produto or {}).get("conteudo_valor") or 1)
                if vender_por_conteudo and cv > 1:
                    qtd_estoque = qtd / cv
                    custo_total, consumo = bar_fifo_consume(pid, qtd_estoque)
                    custo_unit_medio = (custo_total / qtd) if qtd else 0.0
                    preco_unit = to_float(produto.get("valor_venda", 0)) / cv
                else:
                    custo_total, consumo = consumir_fifo(produto, qtd)
                    custo_unit_medio = (custo_total / qtd) if qtd else 0.0
                    preco_unit = to_float(produto.get("valor_venda", 0))
                    db.update_product(produto)

                hoje_item = now_brt().strftime("%Y-%m-%d")
                hora_item = now_brt().strftime("%H:%M")
                sale_uid = uuid.uuid4().hex
                db.insert_sale({
                    "uid": sale_uid,
                    "comanda_id": comanda["cid"],
                    "comanda_nome": comanda["nome"],
                    "data": hoje_item,
                    "hora": hora_item,
                    "produto_pid": produto.get("pid"),
                    "produto_tipo": "estoque",
                    "nome": produto.get("nome", ""),
                    "valor_venda": round(preco_unit, 4),
                    "valor_investido": round(custo_unit_medio, 4),
                    "quantidade": qtd,
                    "status": "pendente",
                    "consumo_lotes": consumo,
                    "usuario": session.get("usuario", ""),
                })
                comanda["itens"].append({
                    "item_id": uuid.uuid4().hex,
                    "sale_uid": sale_uid,
                    "produto_pid": produto.get("pid"),
                    "produto_tipo": "estoque",
                    "vender_por_conteudo": vender_por_conteudo and cv > 1,
                    "nome": produto.get("nome", ""),
                    "quantidade": qtd,
                    "valor_venda": round(preco_unit, 4),
                    "valor_investido": round(custo_unit_medio, 4),
                    "consumo_lotes": consumo,
                    "data": hoje_item,
                    "hora": hora_item
                })

                db.update_command(comanda)
            except (ValueError, RuntimeError) as e:
                erro = str(e)

    if erro:
        estoque = db.get_all_products()
        estoque_disponivel = [p for p in estoque if isinstance(p, dict)]
        estoque_disponivel.sort(key=lambda p: str(p.get("nome", "")).lower())
        bar_pacotes = [p for p in db.bar_get_all_products() if p.get("type") == "composite"]
        bar_pacotes.sort(key=lambda p: str(p.get("name", "")).lower())
        total = sum(
            to_float(i.get("valor_venda", 0)) * to_int(i.get("quantidade", 0))
            for i in comanda.get("itens", [])
        )
        return render_template(
            "comanda_detalhe.html",
            comanda=comanda,
            estoque=estoque_disponivel,
            bar_pacotes=bar_pacotes,
            total=round(total, 2),
            erro=erro,
            tipo=session.get("tipo"),
            permissoes=session.get("permissoes", []),
            usuario=session.get("usuario", "")
        )

    return redirect(url_for("comanda_detalhe", cid=cid))


@app.route("/comandas/<cid>/remover/<item_id>", methods=["POST"])
def comanda_remover_item(cid, item_id):
    if not usuario_logado():
        return redirect(url_for("login"))
    if session.get("tipo") != "admin" and not tem_permissao("ver_vendas"):
        return redirect(url_for("vendas"))

    comanda = db.get_command(cid)
    if not comanda or comanda.get("status") != "aberta":
        return redirect(url_for("comandas"))

    item = next((i for i in comanda.get("itens", []) if str(i.get("item_id")) == str(item_id)), None)
    if item:
        try:
            if item.get("produto_tipo") == "bar":
                devolver_bar_componentes(item.get("bar_consumo_components", []))
            elif item.get("vender_por_conteudo"):
                pid_dev = str(item.get("produto_pid", ""))
                devolver_bar_componentes([{"pid": pid_dev, "consumo": item.get("consumo_lotes", [])}])
            else:
                pid = str(item.get("produto_pid", ""))
                produto = db.get_product(pid)
                if produto and item.get("consumo_lotes"):
                    devolver_fifo(produto, item["consumo_lotes"])
                    db.update_product(produto)

            comanda["itens"] = [i for i in comanda["itens"] if str(i.get("item_id")) != str(item_id)]
            db.update_command(comanda)
            if item.get("sale_uid"):
                db.cancelar_sale(item["sale_uid"], session.get("usuario", ""), now_brt().strftime("%Y-%m-%d %H:%M"))
        except RuntimeError as e:
            session["erro_comanda"] = f"Não foi possível remover o item: {e}"

    return redirect(url_for("comanda_detalhe", cid=cid))


@app.route("/comandas/<cid>/remover", methods=["POST"])
def comanda_remover(cid):
    if not usuario_logado():
        return redirect(url_for("login"))
    if session.get("tipo") != "admin" and not tem_permissao("ver_vendas"):
        return redirect(url_for("vendas"))

    CANCELAVEIS = ("aberta", "aguardando_pagamento", "pagamento_parcial", "agendado", "atrasada", "em_pagamento")

    comanda = db.get_command(cid)
    if not comanda or comanda.get("status") not in CANCELAVEIS:
        return redirect(url_for("comandas"))

    try:
        for item in comanda.get("itens", []):
            if item.get("produto_tipo") == "bar":
                devolver_bar_componentes(item.get("bar_consumo_components", []))
            elif item.get("vender_por_conteudo"):
                pid_dev = str(item.get("produto_pid", ""))
                devolver_bar_componentes([{"pid": pid_dev, "consumo": item.get("consumo_lotes", [])}])
            else:
                pid = str(item.get("produto_pid", ""))
                produto = db.get_product(pid)
                if produto and item.get("consumo_lotes"):
                    devolver_fifo(produto, item["consumo_lotes"])
                    db.update_product(produto)

        db.delete_command(cid)
        for item in comanda.get("itens", []):
            if item.get("sale_uid"):
                db.cancelar_sale(item["sale_uid"], session.get("usuario", ""), now_brt().strftime("%Y-%m-%d %H:%M"))
    except RuntimeError as e:
        session["erro_comanda"] = f"Não foi possível cancelar a comanda: {e}"
        return redirect(url_for("comanda_detalhe", cid=cid))

    return redirect(url_for("comandas"))


def _registrar_historico(comanda: dict, status_anterior: str):
    hist = comanda.get("historico") or []
    hist.append({
        "de": status_anterior,
        "para": comanda["status"],
        "data": now_brt().strftime("%Y-%m-%d"),
        "hora": now_brt().strftime("%H:%M"),
        "usuario": session.get("usuario", ""),
    })
    comanda["historico"] = hist


# Mapa comanda.status -> sales.status — toda venda ligada a uma comanda
# reflete o status da comanda (pendente/parcial/quitada), nunca o valor total
# de uma vez só antes de ser efetivamente recebido.
STATUS_COMANDA_PARA_VENDA = {
    "aberta": "pendente",
    "aguardando_pagamento": "pendente",
    "agendado": "pendente",
    "atrasada": "pendente",
    "em_pagamento": "pendente",
    "pagamento_parcial": "parcial",
    "quitada": "quitada",
}


def _sincronizar_status_vendas_comanda(comanda: dict):
    novo_status = STATUS_COMANDA_PARA_VENDA.get(comanda.get("status"), "pendente")
    try:
        db.update_sales_status_by_comanda(comanda["cid"], novo_status)
    except RuntimeError:
        pass


@app.route("/comandas/<cid>/aguardar", methods=["POST"])
def comanda_aguardar(cid):
    if not usuario_logado():
        return redirect(url_for("login"))
    if session.get("tipo") != "admin" and not tem_permissao("ver_vendas"):
        return redirect(url_for("vendas"))

    comanda = db.get_command(cid)
    if comanda and comanda.get("status") in ("aberta",):
        status_anterior = comanda["status"]
        comanda["status"] = "aguardando_pagamento"
        _registrar_historico(comanda, status_anterior)
        db.update_command(comanda)
        _sincronizar_status_vendas_comanda(comanda)

    return redirect(url_for("comandas"))


@app.route("/comandas/<cid>/em_pagamento", methods=["POST"])
def comanda_em_pagamento(cid):
    return comanda_aguardar(cid)


@app.route("/comandas/<cid>/pagar", methods=["POST"])
def comanda_pagar(cid):
    if not usuario_logado():
        return redirect(url_for("login"))
    if session.get("tipo") != "admin" and not tem_permissao("ver_vendas"):
        return redirect(url_for("vendas"))

    try:
        caixa_pagar = db.get_caixa_aberto()
    except Exception:
        caixa_pagar = None
    if not caixa_pagar:
        session["erro_caixa"] = "⚠️ Nenhum caixa aberto. Abra o caixa antes de registrar pagamentos."
        return redirect(url_for("comandas"))

    comanda = db.get_command(cid)
    PAGAVEIS = ("aberta", "aguardando_pagamento", "pagamento_parcial", "agendado", "atrasada", "em_pagamento")
    if not comanda or comanda.get("status") not in PAGAVEIS:
        return redirect(url_for("comandas"))

    if not comanda.get("itens"):
        return redirect(url_for("comandas"))

    valor_raw = (request.form.get("valor") or "0").strip()
    valor = to_float(valor_raw)
    forma = (request.form.get("forma") or "dinheiro").strip()
    obs   = (request.form.get("obs") or "").strip()

    total = sum(
        to_float(i.get("valor_venda", 0)) * to_int(i.get("quantidade", 0))
        for i in comanda.get("itens", [])
    )
    pago_antes = sum(to_float(p.get("valor", 0)) for p in comanda.get("pagamentos", []))
    saldo = round(total - pago_antes, 2)

    if valor <= 0 or valor > saldo + 0.005:
        return redirect(url_for("comandas"))

    hoje = now_brt().strftime("%Y-%m-%d")
    hora = now_brt().strftime("%H:%M")
    usuario = session.get("usuario", "")

    pagamentos = comanda.get("pagamentos") or []
    pagamentos.append({
        "uid": uuid.uuid4().hex,
        "valor": round(valor, 2),
        "forma": forma,
        "data": hoje,
        "hora": hora,
        "usuario": usuario,
        "obs": obs,
        "caixa_id": caixa_pagar.get("id"),
    })
    comanda["pagamentos"] = pagamentos

    pago_total = round(pago_antes + valor, 2)
    status_anterior = comanda.get("status", "aberta")
    if pago_total >= total - 0.005:
        comanda["status"] = "quitada"
        comanda["data_quitada"] = hoje
    else:
        comanda["status"] = "pagamento_parcial"

    _registrar_historico(comanda, status_anterior)
    try:
        db.update_command(comanda)
        _sincronizar_status_vendas_comanda(comanda)
        desc = f"Quitou comanda: {comanda.get('nome', '')}" if comanda["status"] == "quitada" else f"Registrou pagamento na comanda {comanda.get('nome', '')} (R$ {valor:.2f})"
        db.registrar_log(usuario, "pagamento_comanda", desc, hoje, hora)
    except RuntimeError:
        pass
    return redirect(url_for("comandas"))


@app.route("/comandas/<cid>/agendar", methods=["POST"])
def comanda_agendar(cid):
    if not usuario_logado():
        return redirect(url_for("login"))
    if session.get("tipo") != "admin" and not tem_permissao("ver_vendas"):
        return redirect(url_for("vendas"))

    comanda = db.get_command(cid)
    AGENDAVEIS = ("aberta", "aguardando_pagamento", "pagamento_parcial", "em_pagamento")
    if not comanda or comanda.get("status") not in AGENDAVEIS:
        return redirect(url_for("comandas"))

    vencimento = (request.form.get("vencimento") or "").strip()
    observacao = (request.form.get("observacao") or "").strip()

    if not vencimento:
        return redirect(url_for("comandas"))

    status_anterior = comanda.get("status", "aberta")
    comanda["status"] = "agendado"
    comanda["vencimento"] = vencimento
    comanda["observacao"] = observacao
    _registrar_historico(comanda, status_anterior)
    db.update_command(comanda)
    _sincronizar_status_vendas_comanda(comanda)
    return redirect(url_for("comandas"))


@app.route("/comandas/<cid>/fechar", methods=["POST"])
def comanda_fechar(cid):
    if not usuario_logado():
        return redirect(url_for("login"))
    if session.get("tipo") != "admin" and not tem_permissao("ver_vendas"):
        return redirect(url_for("vendas"))

    try:
        caixa_fechar = db.get_caixa_aberto()
    except Exception:
        caixa_fechar = None
    if not caixa_fechar:
        session["erro_caixa"] = "⚠️ Nenhum caixa aberto. Abra o caixa antes de quitar comandas."
        return redirect(url_for("comandas"))

    comanda = db.get_command(cid)
    FECHAVEIS = ("aberta", "aguardando_pagamento", "em_pagamento", "pagamento_parcial", "agendado", "atrasada")
    if not comanda or comanda.get("status") not in FECHAVEIS:
        return redirect(url_for("comandas"))

    if not comanda.get("itens"):
        return redirect(url_for("comandas"))

    forma_pagamento = (request.form.get("forma_pagamento") or "dinheiro").strip()
    hoje = now_brt().strftime("%Y-%m-%d")
    hora = now_brt().strftime("%H:%M")
    usuario = session.get("usuario", "")

    total = sum(
        to_float(i.get("valor_venda", 0)) * to_int(i.get("quantidade", 0))
        for i in comanda.get("itens", [])
    )
    pago_antes = sum(to_float(p.get("valor", 0)) for p in comanda.get("pagamentos", []))
    restante = round(total - pago_antes, 2)

    if restante > 0.005:
        pagamentos = comanda.get("pagamentos") or []
        pagamentos.append({
            "uid": uuid.uuid4().hex,
            "valor": round(restante, 2),
            "forma": forma_pagamento,
            "data": hoje,
            "hora": hora,
            "usuario": usuario,
            "obs": "",
            "caixa_id": caixa_fechar.get("id"),
        })
        comanda["pagamentos"] = pagamentos

    status_anterior = comanda.get("status", "aberta")
    comanda["status"] = "quitada"
    comanda["data_quitada"] = hoje
    _registrar_historico(comanda, status_anterior)
    try:
        db.update_command(comanda)
        _sincronizar_status_vendas_comanda(comanda)
    except RuntimeError:
        pass
    return redirect(url_for("comandas"))


@app.route("/comandas/<cid>/reverter", methods=["POST"])
def comanda_reverter(cid):
    if not usuario_logado():
        return redirect(url_for("login"))
    if session.get("tipo") != "admin":
        return redirect(url_for("comandas"))

    comanda = db.get_command(cid)
    if not comanda:
        return redirect(url_for("comandas"))

    historico = comanda.get("historico") or []
    if not historico:
        return redirect(url_for("comandas"))

    ultima = historico[-1]
    comanda["status"] = ultima["de"]
    comanda["historico"] = historico[:-1]

    if ultima["para"] == "quitada":
        comanda["data_quitada"] = ""
    if ultima["para"] == "agendado":
        comanda["vencimento"] = ""
        comanda["observacao"] = ""

    try:
        db.update_command(comanda)
        _sincronizar_status_vendas_comanda(comanda)
    except RuntimeError:
        pass
    return redirect(url_for("comandas"))


# ================== CAIXA PDV ==================

def _auth_caixa():
    if not usuario_logado():
        return redirect(url_for("login"))
    if session.get("tipo") != "admin" and not tem_permissao("abrir_fechar_caixa"):
        return redirect(url_for("vendas"))
    return None


def _auth_caixa_ver():
    """Permite acesso com abrir_fechar_caixa OU ver_historico_caixa (somente visualização)."""
    if not usuario_logado():
        return redirect(url_for("login"))
    if session.get("tipo") != "admin" and not tem_permissao("abrir_fechar_caixa") and not tem_permissao("ver_historico_caixa"):
        return redirect(url_for("vendas"))
    return None


def _calcular_totais_caixa(caixa_id: str, vendas_ids: list | None = None) -> dict:
    """Soma as vendas diretas do caixa por forma de pagamento, mais os
    pagamentos de comanda feitos enquanto esse caixa estava aberto (cada
    pagamento de comanda já carrega o caixa_id de quando foi registrado,
    então isso funciona igual pra caixa aberto ou já fechado)."""
    if vendas_ids is not None:
        vendas = db.get_sales_by_uids(vendas_ids) if vendas_ids else []
    else:
        vendas = db.get_sales_open_by_caixa(caixa_id)

    totais = {"dinheiro": 0.0, "pix": 0.0, "credito": 0.0, "debito": 0.0, "fiado": 0.0, "total": 0.0, "qtd": 0}
    for v in vendas:
        valor = to_float(v.get("valor_venda", 0)) * to_int(v.get("quantidade", 1))
        forma = normalizar_pagamento(v.get("forma_pagamento", ""))
        totais[forma] = totais.get(forma, 0.0) + valor
        totais["total"] += valor
        totais["qtd"] += 1

    for p in db.get_pagamentos_comanda_por_caixa(caixa_id):
        valor = to_float(p.get("valor", 0))
        forma = normalizar_pagamento(p.get("forma", ""))
        totais[forma] = totais.get(forma, 0.0) + valor
        totais["total"] += valor
        totais["qtd"] += 1

    return totais


def _comandas_abertas_no_periodo(caixa: dict) -> list:
    """Comandas cuja abertura (data_abertura + hora_abertura) caiu dentro do
    período em que este caixa esteve aberto — do momento da abertura do
    caixa até agora (ou até o fechamento, se já fechado)."""
    inicio = f"{caixa.get('data_abertura','')} {caixa.get('hora_abertura','')}"
    if caixa.get("status") == "fechado" and caixa.get("data_fechamento"):
        fim = f"{caixa['data_fechamento']} {caixa.get('hora_fechamento','23:59')}"
    else:
        fim = now_brt().strftime("%Y-%m-%d %H:%M")

    resultado = []
    for c in db.get_all_commands():
        abertura_c = f"{c.get('data_abertura','')} {c.get('hora_abertura','')}"
        if inicio <= abertura_c <= fim:
            resultado.append(c)
    resultado.sort(key=lambda c: (c.get("data_abertura", ""), c.get("hora_abertura", "")))
    return resultado


def _tempo_operacao_str(caixa: dict) -> str:
    """Tempo decorrido desde a abertura do caixa até agora, formatado 'Xh Ymin'."""
    agora = now_brt()
    abertura_dt = datetime.strptime(
        f"{caixa['data_abertura']} {caixa['hora_abertura']}", "%Y-%m-%d %H:%M"
    )
    minutos = max(0, int((agora.replace(tzinfo=None) - abertura_dt).total_seconds() // 60))
    return f"{minutos // 60}h {minutos % 60}min"


@app.route("/caixa/abrir", methods=["GET", "POST"])
def caixa_abrir():
    redir = _auth_caixa()
    if redir: return redir

    caixa_aberto = db.get_caixa_aberto()
    erro = ""

    if request.method == "POST":
        if caixa_aberto:
            erro = "Já existe um caixa aberto. Feche-o antes de abrir outro."
        else:
            valor_raw = (request.form.get("valor_abertura") or "0").strip()
            valor = to_float(valor_raw)
            novo = {
                "id": uuid.uuid4().hex,
                "status": "aberto",
                "data_abertura": now_brt().strftime("%Y-%m-%d"),
                "hora_abertura": now_brt().strftime("%H:%M"),
                "operador_abertura": session.get("usuario", ""),
                "valor_abertura": valor,
            }
            try:
                db.insert_caixa(novo)
                db.registrar_log(
                    session.get("usuario", ""), "caixa_aberto",
                    f"Abriu caixa (valor inicial: R$ {valor:.2f})",
                    now_brt().strftime("%Y-%m-%d"), now_brt().strftime("%H:%M"),
                )
                return redirect(url_for("caixa_turno"))
            except RuntimeError as e:
                erro = str(e)

    return render_template("caixa_abrir.html",
        caixa_aberto=caixa_aberto, erro=erro,
        tipo=session.get("tipo"), permissoes=session.get("permissoes", []))


@app.route("/caixa/turno")
def caixa_turno():
    redir = _auth_caixa()
    if redir: return redir

    caixa = db.get_caixa_aberto()
    if not caixa:
        return redirect(url_for("caixa_abrir"))

    movs = db.get_movimentacoes_caixa(caixa["id"])
    total_sangrias   = sum(to_float(m["valor"]) for m in movs if m["tipo"] == "sangria")
    total_suprimentos = sum(to_float(m["valor"]) for m in movs if m["tipo"] == "suprimento")
    total_despesas   = sum(to_float(m["valor"]) for m in movs if m["tipo"] == "despesa")
    qtd_sangrias     = sum(1 for m in movs if m["tipo"] == "sangria")
    qtd_suprimentos  = sum(1 for m in movs if m["tipo"] == "suprimento")
    qtd_despesas     = sum(1 for m in movs if m["tipo"] == "despesa")
    ultima_mov = movs[-1] if movs else None

    # vendas diretas + pagamentos de comanda vinculados a este caixa
    totais = _calcular_totais_caixa(caixa["id"])

    dinheiro_esperado = round(
        to_float(caixa["valor_abertura"]) + totais["dinheiro"]
        + total_suprimentos - total_sangrias - total_despesas, 2)

    comandas_periodo = _comandas_abertas_no_periodo(caixa)
    tempo_operacao = _tempo_operacao_str(caixa)
    hoje = now_brt().strftime("%Y-%m-%d")

    return render_template("caixa_turno.html",
        caixa=caixa, movs=movs, totais=totais,
        total_sangrias=total_sangrias, total_suprimentos=total_suprimentos,
        total_despesas=total_despesas, dinheiro_esperado=dinheiro_esperado,
        qtd_sangrias=qtd_sangrias, qtd_suprimentos=qtd_suprimentos, qtd_despesas=qtd_despesas,
        ultima_mov=ultima_mov, tempo_operacao=tempo_operacao, hoje=hoje,
        comandas_periodo=comandas_periodo,
        tipo=session.get("tipo"), permissoes=session.get("permissoes", []))


@app.route("/caixa/movimentacao", methods=["POST"])
def caixa_movimentacao():
    redir = _auth_caixa()
    if redir: return redir

    caixa = db.get_caixa_aberto()
    if not caixa:
        return redirect(url_for("caixa_abrir"))

    tipo      = (request.form.get("tipo") or "").strip()
    valor_raw = (request.form.get("valor") or "0").strip()
    descricao = (request.form.get("descricao") or "").strip()

    if tipo not in ("sangria", "suprimento", "despesa"):
        return redirect(url_for("caixa_turno"))

    valor = to_float(valor_raw)
    if valor <= 0:
        return redirect(url_for("caixa_turno"))

    mov = {
        "id": uuid.uuid4().hex,
        "caixa_id": caixa["id"],
        "tipo": tipo,
        "valor": round(valor, 2),
        "descricao": descricao,
        "data": now_brt().strftime("%Y-%m-%d"),
        "hora": now_brt().strftime("%H:%M"),
        "usuario": session.get("usuario", ""),
    }
    try:
        db.insert_movimentacao(mov)
    except RuntimeError:
        pass
    return redirect(url_for("caixa_turno"))


@app.route("/caixa/fechar", methods=["GET", "POST"])
def caixa_fechar():
    redir = _auth_caixa()
    if redir: return redir

    caixa = db.get_caixa_aberto()
    if not caixa:
        return redirect(url_for("caixa_abrir"))

    movs = db.get_movimentacoes_caixa(caixa["id"])
    total_sangrias    = round(sum(to_float(m["valor"]) for m in movs if m["tipo"] == "sangria"), 2)
    total_suprimentos = round(sum(to_float(m["valor"]) for m in movs if m["tipo"] == "suprimento"), 2)
    total_despesas    = round(sum(to_float(m["valor"]) for m in movs if m["tipo"] == "despesa"), 2)

    # vendas diretas abertas deste caixa
    rows = db.get_sales_open_by_caixa(caixa["id"])
    totais = {"dinheiro": 0.0, "pix": 0.0, "credito": 0.0, "debito": 0.0, "fiado": 0.0, "total": 0.0, "qtd": 0}
    uids = []
    produtos_vendidos = 0
    for v in rows:
        qtd_item = to_int(v.get("quantidade", 1))
        valor = to_float(v.get("valor_venda", 0)) * qtd_item
        forma = normalizar_pagamento(v.get("forma_pagamento", ""))
        totais[forma] = totais.get(forma, 0.0) + valor
        totais["total"] += valor
        totais["qtd"] += 1
        produtos_vendidos += qtd_item
        uids.append(v["uid"])

    # + pagamentos de comanda registrados enquanto este caixa estava aberto
    for p in db.get_pagamentos_comanda_por_caixa(caixa["id"]):
        valor = to_float(p.get("valor", 0))
        forma = normalizar_pagamento(p.get("forma", ""))
        totais[forma] = totais.get(forma, 0.0) + valor
        totais["total"] += valor
        totais["qtd"] += 1

    dinheiro_esperado = round(
        to_float(caixa["valor_abertura"]) + totais["dinheiro"]
        + total_suprimentos - total_sangrias - total_despesas, 2)

    ticket_medio = round(totais["total"] / totais["qtd"], 2) if totais["qtd"] else 0.0

    LABEL_FORMA_PAGAMENTO_CAIXA = {
        "pix": "Pix", "dinheiro": "Dinheiro", "credito": "Cartão",
        "debito": "Débito", "fiado": "Fiado",
    }
    pagamentos = []
    for forma, total_forma in sorted(totais.items(), key=lambda x: x[1], reverse=True):
        if forma in ("total", "qtd") or total_forma <= 0:
            continue
        pct = round(total_forma / totais["total"] * 100) if totais["total"] > 0 else 0
        pagamentos.append({
            "forma": LABEL_FORMA_PAGAMENTO_CAIXA.get(forma, forma.capitalize()),
            "total": round(total_forma, 2),
            "pct": pct,
        })

    agora = now_brt()
    tempo_operacao = _tempo_operacao_str(caixa)

    if request.method == "POST":
        conf_dinheiro = to_float(request.form.get("conf_dinheiro") or "0")
        conf_pix      = to_float(request.form.get("conf_pix") or "0")
        conf_cartao   = to_float(request.form.get("conf_cartao") or "0")
        cartao_esperado = totais["credito"] + totais["debito"]

        conferencia = {
            "dinheiro": round(conf_dinheiro, 2),
            "pix":      round(conf_pix, 2),
            "credito":  round(conf_cartao, 2),
            "debito":   0.0,
        }
        diferencas = {
            "dinheiro": round(conf_dinheiro - dinheiro_esperado, 2),
            "pix":      round(conf_pix - totais["pix"], 2),
            "credito":  round(conf_cartao - cartao_esperado, 2),
            "debito":   0.0,
        }
        observacoes = (request.form.get("observacoes") or "").strip()[:200]

        agora = now_brt()
        caixa.update({
            "status":               "fechado",
            "data_fechamento":      agora.strftime("%Y-%m-%d"),
            "hora_fechamento":      agora.strftime("%H:%M"),
            "operador_fechamento":  session.get("usuario", ""),
            "total_dinheiro":       round(totais["dinheiro"], 2),
            "total_pix":            round(totais["pix"], 2),
            "total_cartao_cred":    round(totais["credito"] + totais["debito"], 2),
            "total_cartao_deb":     0,
            "total_fiado":          round(totais["fiado"], 2),
            "total_sangrias":       total_sangrias,
            "total_suprimentos":    total_suprimentos,
            "total_despesas":       total_despesas,
            "total_vendas":         round(totais["total"], 2),
            "qtd_vendas":           totais["qtd"],
            "conferencia":          conferencia,
            "diferencas":           diferencas,
            "vendas_uids":          uids,
            "observacoes":          observacoes,
        })

        fechado_em = agora.strftime("%Y-%m-%dT%H:%M")
        try:
            db.update_caixa(caixa)
            if uids:
                db.mark_sales_fechado(uids, agora.strftime("%Y-%m-%d"), fechado_em)
            db.registrar_log(
                session.get("usuario", ""), "caixa_fechado",
                f"Fechou caixa (total vendas: R$ {totais['total']:.2f})",
                agora.strftime("%Y-%m-%d"), agora.strftime("%H:%M"),
            )
        except RuntimeError:
            pass

        comandas_periodo = _comandas_abertas_no_periodo(caixa)

        return render_template("caixa_fechado_novo.html",
            caixa=caixa, totais=totais, conferencia=conferencia,
            diferencas=diferencas, dinheiro_esperado=dinheiro_esperado,
            movs=movs, total_sangrias=total_sangrias,
            total_suprimentos=total_suprimentos, total_despesas=total_despesas,
            observacoes=observacoes, comandas_periodo=comandas_periodo,
            tipo=session.get("tipo"), permissoes=session.get("permissoes", []))

    comandas_periodo = _comandas_abertas_no_periodo(caixa)

    return render_template("caixa_fechar.html",
        caixa=caixa, totais=totais, movs=movs,
        total_sangrias=total_sangrias, total_suprimentos=total_suprimentos,
        total_despesas=total_despesas, dinheiro_esperado=dinheiro_esperado,
        produtos_vendidos=produtos_vendidos, ticket_medio=ticket_medio,
        pagamentos=pagamentos, tempo_operacao=tempo_operacao, agora=agora,
        comandas_periodo=comandas_periodo,
        tipo=session.get("tipo"), permissoes=session.get("permissoes", []))


@app.route("/caixa/historico")
def caixa_historico_novo():
    redir = _auth_caixa_ver()
    if redir: return redir

    hoje = today_brt().isoformat()
    sessoes = db.get_all_caixas()
    pode_ver_despesas = session.get("tipo") == "admin" or tem_permissao("ver_despesas")

    for s in sessoes:
        if not pode_ver_despesas:
            s["total_despesas"] = 0
        s["data_str"] = str(s.get("data_abertura") or "")[:10]

        if s.get("hora_abertura") and s.get("hora_fechamento"):
            try:
                t1 = datetime.strptime(s["hora_abertura"], "%H:%M")
                t2 = datetime.strptime(s["hora_fechamento"], "%H:%M")
                delta = t2 - t1
                if delta.total_seconds() < 0:
                    delta += timedelta(days=1)
                h = int(delta.total_seconds() // 3600)
                m = int((delta.total_seconds() % 3600) // 60)
                s["tempo_aberto"] = f"{h}h {m}m"
            except Exception:
                s["tempo_aberto"] = "—"
        else:
            s["tempo_aberto"] = "—"

        s["diferenca_total"] = sum(
            v for v in s.get("diferencas", {}).values()
            if isinstance(v, (int, float))
        )

    sessoes_hoje = [s for s in sessoes if str(s.get("data_abertura", ""))[:10] == hoje]
    caixa_aberto = db.get_caixa_aberto()
    total_vendido_hoje  = sum(s.get("total_vendas", 0) or 0 for s in sessoes_hoje)
    total_dinheiro_hoje = sum(s.get("total_dinheiro", 0) or 0 for s in sessoes_hoje)
    diferenca_dia       = sum(s.get("diferenca_total", 0) for s in sessoes_hoje if s.get("status") == "fechado")
    fechamentos_hoje    = sum(1 for s in sessoes_hoje if s.get("status") == "fechado")

    all_movs = db.get_all_movimentacoes()
    movs_por_caixa: dict = {}
    for m in all_movs:
        cid = str(m.get("caixa_id", ""))
        if cid:
            movs_por_caixa.setdefault(cid, []).append(m)

    sessoes_by_id = {str(s["id"]): s for s in sessoes}

    return render_template("caixa_historico_novo.html",
        sessoes=sessoes,
        caixa_aberto=caixa_aberto,
        total_vendido_hoje=total_vendido_hoje,
        total_dinheiro_hoje=total_dinheiro_hoje,
        diferenca_dia=diferenca_dia,
        fechamentos_hoje=fechamentos_hoje,
        sessoes_by_id=sessoes_by_id,
        movs_por_caixa=movs_por_caixa,
        hoje=hoje,
        tipo=session.get("tipo"), permissoes=session.get("permissoes", []))


@app.route("/caixa/historico/<cid>")
def caixa_detalhe(cid):
    redir = _auth_caixa_ver()
    if redir: return redir

    sessao = db.get_caixa_sessao(cid)
    if not sessao:
        return redirect(url_for("caixa_historico_novo"))

    movs = db.get_movimentacoes_caixa(cid)
    return render_template("caixa_detalhe.html",
        sessao=sessao, movs=movs,
        tipo=session.get("tipo"), permissoes=session.get("permissoes", []))


# ================== FECHAR CAIXA (LEGADO) ==================
@app.route("/fechar_caixa", methods=["GET", "POST"])
def fechar_caixa():
    if not usuario_logado():
        return redirect(url_for("login"))

    if session.get("tipo") != "admin" and not tem_permissao("abrir_fechar_caixa"):
        return redirect(url_for("vendas"))

    if request.method == "GET":
        hoje = now_brt().strftime("%Y-%m-%d")
        return render_template("fechar_caixa.html", hoje=hoje, erro="")

    dia_raw = (request.form.get("dia") or "").strip()
    if not dia_raw:
        hoje = now_brt().strftime("%Y-%m-%d")
        return render_template("fechar_caixa.html", hoje=hoje, erro="Selecione uma data.")

    dia = normalizar_data_str(dia_raw)
    abertas_do_dia = db.get_sales_open_by_date(dia)

    pix = dinheiro = cartao = total = 0.0
    qtd_vendas = 0
    uids_fechados = []
    fechado_em = now_brt().strftime("%Y-%m-%d %H:%M:%S")

    for v in abertas_do_dia:
        qtd = to_int(v.get("quantidade", 0), 0)
        unit = to_float(v.get("valor_venda", 0), 0.0)
        valor = unit * qtd
        forma = normalizar_pagamento(v.get("forma_pagamento"))

        total += valor
        if forma == "pix":
            pix += valor
        elif forma == "dinheiro":
            dinheiro += valor
        else:
            cartao += valor

        uids_fechados.append(v["uid"])
        qtd_vendas += 1

    if qtd_vendas == 0:
        return render_template(
            "fechar_caixa.html",
            hoje=dia,
            erro=f"Não há vendas abertas para fechar em {dia}."
        )

    registro = {
        "id": uuid.uuid4().hex,
        "dia": dia,
        "data_fechamento": now_brt().strftime("%Y-%m-%d %H:%M"),
        "pix": round(pix, 2),
        "dinheiro": round(dinheiro, 2),
        "cartao": round(cartao, 2),
        "total": round(total, 2),
        "qtd_vendas": qtd_vendas,
        "vendas_uids": uids_fechados
    }

    db.mark_sales_fechado(uids_fechados, dia, fechado_em)
    db.insert_cash_register(registro)

    return render_template("caixa_fechado.html", r=registro)


@app.route("/fechar_caixa/previa", methods=["POST"])
def fechar_caixa_previa():
    if not usuario_logado():
        return redirect(url_for("login"))

    if session.get("tipo") != "admin" and not tem_permissao("abrir_fechar_caixa"):
        return redirect(url_for("vendas"))

    dia_raw = (request.form.get("dia") or "").strip()
    if not dia_raw:
        hoje = now_brt().strftime("%Y-%m-%d")
        return render_template("fechar_caixa.html", hoje=hoje, erro="Selecione uma data.")

    dia = normalizar_data_str(dia_raw)
    previa = montar_previa_caixa_por_dia(dia)

    if previa["qtd_vendas"] == 0:
        return render_template(
            "fechar_caixa.html",
            hoje=dia,
            erro=f"Não há vendas abertas para fechar em {dia}."
        )

    session["previa_caixa_dia"] = dia
    return render_template("caixa_previa.html", p=previa)


@app.route("/caixa")
def caixa_historico():
    if not usuario_logado():
        return redirect(url_for("login"))

    if session.get("tipo") != "admin" and not tem_permissao("ver_caixa"):
        return redirect(url_for("vendas"))

    caixa = db.get_all_cash_registers()
    caixa = sorted(caixa, key=lambda c: str(c.get("dia", "")), reverse=True)
    return render_template("caixa_historico.html", caixa=caixa)


@app.route("/caixa/<dia>")
def caixa_relatorio(dia):
    if not usuario_logado():
        return redirect(url_for("login"))

    if session.get("tipo") != "admin" and not tem_permissao("ver_caixa"):
        return redirect(url_for("vendas"))

    rel = montar_relatorio_caixa_por_dia(dia)
    if not rel:
        return redirect(url_for("caixa_historico"))

    return render_template("caixa_relatorio.html", rel=rel)


@app.route("/caixa/<dia>/pdf")
def caixa_relatorio_pdf(dia):
    if not usuario_logado():
        return redirect(url_for("login"))

    if session.get("tipo") != "admin" and not tem_permissao("ver_caixa"):
        return redirect(url_for("vendas"))

    rel = montar_relatorio_caixa_por_dia(dia)
    if not rel:
        return redirect(url_for("caixa_historico"))

    buffer = BytesIO()
    c = canvas.Canvas(buffer, pagesize=A4)
    width, height = A4
    y = height - 40

    c.setFont("Helvetica-Bold", 14)
    c.drawString(40, y, f"Fechamento de Caixa - {dia}")
    y -= 18

    c.setFont("Helvetica", 10)
    c.drawString(40, y, f"Fechado em: {rel['registro'].get('data_fechamento', '')}")
    y -= 20

    c.setFont("Helvetica-Bold", 11)
    c.drawString(40, y, "Produtos vendidos (ordem cadastrada):")
    y -= 14

    c.setFont("Helvetica", 10)
    for item in rel["itens"]:
        linha = f"- {item['nome']} (qtd: {item['qtd']} | unit: R$ {item['unit']:.2f})"
        if y < 80:
            c.showPage()
            y = height - 40
            c.setFont("Helvetica", 10)
        c.drawString(50, y, linha)
        y -= 14

    y -= 8
    c.setFont("Helvetica-Bold", 11)
    c.drawString(40, y, "Pagamentos:")
    y -= 14

    c.setFont("Helvetica", 10)
    for k, titulo in [("pix", "Pix"), ("dinheiro", "Dinheiro"), ("cartao", "Cartão")]:
        p = rel["pagamentos"][k]
        linha = f"{titulo}: quantidade (vendas) {p['qtd_vendas']} | valor pago R$ {p['valor']:.2f}"
        c.drawString(50, y, linha)
        y -= 14

    y -= 8
    c.setFont("Helvetica-Bold", 12)
    c.drawString(40, y, f"TOTAL DO DIA: R$ {rel['total']:.2f}")
    y -= 16
    c.drawString(40, y, f"LUCRO DO DIA: R$ {rel['lucro']:.2f}")

    c.save()
    buffer.seek(0)

    return send_file(
        buffer,
        as_attachment=True,
        download_name=f"caixa_{dia}.pdf",
        mimetype="application/pdf"
    )


# ================== FINANCEIRO ==================
def parse_date_input(s: str):
    if not s:
        return None
    s = str(s).strip()
    if not s:
        return None
    try:
        return datetime.strptime(s, "%Y-%m-%d").date()
    except ValueError:
        return None


@app.route("/financeiro")
def financeiro():
    if not usuario_logado():
        return redirect(url_for("login"))

    if session.get("tipo") != "admin" and not tem_permissao("ver_financeiro"):
        return redirect(url_for("vendas"))

    data_ini_raw = (request.args.get("data_ini") or "").strip()
    data_fim_raw = (request.args.get("data_fim") or "").strip()

    if not request.args:
        hoje = today_brt().strftime("%Y-%m-%d")
        data_ini_raw = hoje
        data_fim_raw = hoje

    data_de = parse_date_input(data_ini_raw)
    data_ate = parse_date_input(data_fim_raw)

    vendas = db.get_all_sales()  # já exclui canceladas (excluida=False)

    vendas_filtradas = []
    for v in vendas:
        if not isinstance(v, dict):
            continue
        d = parse_date_input(v.get("data", ""))
        if not d:
            continue
        if data_de and d < data_de:
            continue
        if data_ate and d > data_ate:
            continue
        vendas_filtradas.append(v)

    vendas_filtradas.sort(
        key=lambda x: parse_date_input(x.get("data", "")) or date.min,
        reverse=True
    )

    # Fração paga de cada comanda parcial (pago / total), pra reconhecer
    # recebido/lucro na proporção certa — nunca o valor cheio antes de entrar
    # de fato no caixa. Comandas quitadas/pendentes não precisam da fração
    # (100% e 0% respectivamente, já cobertos pelo status da venda).
    fracao_paga_comanda = {}
    for c in db.get_all_commands():
        if not isinstance(c, dict) or c.get("status") != "pagamento_parcial":
            continue
        total_c = sum(to_float(i.get("valor_venda", 0)) * to_int(i.get("quantidade", 0)) for i in c.get("itens", []))
        pago_c = sum(to_float(p.get("valor", 0)) for p in c.get("pagamentos", []))
        fracao_paga_comanda[c["cid"]] = min(pago_c / total_c, 1.0) if total_c > 0 else 0.0

    total_vendido = 0.0
    total_investido = 0.0
    valor_recebido = 0.0
    lucro_total = 0.0
    lucro_realizado = 0.0

    produto_stats = defaultdict(lambda: {"qtd": 0, "lucro": 0.0})

    for v in vendas_filtradas:
        qtd = to_int(v.get("quantidade", 0))
        valor_linha = to_float(v.get("valor_venda", 0)) * qtd
        custo_linha = to_float(v.get("valor_investido", 0)) * qtd
        lucro_linha = valor_linha - custo_linha
        total_vendido += valor_linha
        total_investido += custo_linha
        lucro_total += lucro_linha

        status_v = v.get("status") or "quitada"
        if status_v == "quitada":
            fracao = 1.0
        elif status_v == "parcial":
            fracao = fracao_paga_comanda.get(v.get("comanda_id"), 0.0)
        else:  # pendente
            fracao = 0.0

        recebido_linha = valor_linha * fracao
        lucro_realizado_linha = lucro_linha * fracao
        valor_recebido += recebido_linha
        lucro_realizado += lucro_realizado_linha

        nome = v.get("nome", "").strip() or "Sem nome"
        produto_stats[nome]["qtd"] += qtd
        produto_stats[nome]["lucro"] += lucro_realizado_linha

    total_vendido = round(total_vendido, 2)
    total_investido = round(total_investido, 2)
    lucro_total = round(lucro_total, 2)
    valor_recebido = round(valor_recebido, 2)
    lucro_realizado = round(lucro_realizado, 2)
    total_a_receber = round(total_vendido - valor_recebido, 2)
    lucro_pendente = round(lucro_total - lucro_realizado, 2)

    # Top produtos por lucro já efetivamente realizado (não conta lucro sobre
    # venda ainda não recebida no ranking).
    top_produtos = sorted(
        [{"nome": k, "quantidade": v["qtd"], "lucro": round(v["lucro"], 2)}
         for k, v in produto_stats.items()],
        key=lambda x: x["lucro"], reverse=True
    )[:5]

    # Pagamentos por forma — vendas diretas (sempre quitadas na hora) contam
    # pelo valor da venda; vendas de comanda contam pelo pagamento individual
    # de fato registrado no período (cada pagamento no seu próprio dia/forma).
    LABEL_FORMA_PAGAMENTO = {
        "pix": "Pix",
        "dinheiro": "Dinheiro",
        "credito": "Cartão",
        "debito": "Débito",
        "fiado": "Fiado",
    }
    pag_totais = defaultdict(float)
    for v in vendas_filtradas:
        if v.get("comanda_id"):
            continue
        forma = normalizar_pagamento(v.get("forma_pagamento", ""))
        pag_totais[forma] += to_float(v.get("valor_venda", 0)) * to_int(v.get("quantidade", 0))
    for c in db.get_all_commands():
        if not isinstance(c, dict):
            continue
        for p in c.get("pagamentos", []):
            d = parse_date_input(p.get("data", ""))
            if not d or (data_de and d < data_de) or (data_ate and d > data_ate):
                continue
            forma = normalizar_pagamento(p.get("forma", ""))
            pag_totais[forma] += to_float(p.get("valor", 0))
    pagamentos = []
    for forma, total in sorted(pag_totais.items(), key=lambda x: x[1], reverse=True):
        pct = round(total / valor_recebido * 100) if valor_recebido > 0 else 0
        pagamentos.append({"forma": LABEL_FORMA_PAGAMENTO.get(forma, forma.capitalize()), "total": round(total, 2), "pct": pct})

    # Contagem de comandas por status, no mesmo período (por data de abertura)
    num_comandas_abertas = 0
    num_comandas_parciais = 0
    num_comandas_quitadas = 0
    STATUS_ABERTA = ("aberta", "aguardando_pagamento", "agendado", "atrasada", "em_pagamento")
    for c in db.get_all_commands():
        if not isinstance(c, dict):
            continue
        d = parse_date_input(c.get("data_abertura", ""))
        if not d or (data_de and d < data_de) or (data_ate and d > data_ate):
            continue
        if c.get("status") in STATUS_ABERTA:
            num_comandas_abertas += 1
        elif c.get("status") == "pagamento_parcial":
            num_comandas_parciais += 1
        elif c.get("status") == "quitada":
            num_comandas_quitadas += 1

    # "venda" agora é 1 carrinho/comanda, não 1 linha de produto
    num_vendas_geral = len(set(_grupo_key(v) for v in vendas_filtradas))

    # Datas formatadas para exibição
    def fmt_br(s):
        d = parse_date_input(s)
        return d.strftime("%d/%m/%Y") if d else ""
    data_ini_br = fmt_br(data_ini_raw)
    data_fim_br = fmt_br(data_fim_raw)

    nomes_sugestoes = sorted(set(
        v.get("nome", "").strip() for v in vendas_filtradas
        if isinstance(v, dict) and v.get("nome", "").strip()
    ))

    return render_template(
        "financeiro.html",
        vendas=vendas_filtradas,
        total_vendido=total_vendido,
        total_investido=total_investido,
        lucro_total=lucro_total,
        lucro_realizado=lucro_realizado,
        lucro_pendente=lucro_pendente,
        num_vendas_geral=num_vendas_geral,
        total_a_receber=total_a_receber,
        num_comandas_abertas=num_comandas_abertas,
        num_comandas_parciais=num_comandas_parciais,
        num_comandas_quitadas=num_comandas_quitadas,
        valor_recebido=valor_recebido,
        num_vendas=len(vendas_filtradas),
        data_ini_raw=data_ini_raw,
        data_fim_raw=data_fim_raw,
        data_ini_br=data_ini_br,
        data_fim_br=data_fim_br,
        top_produtos=top_produtos,
        pagamentos=pagamentos,
        nomes_sugestoes=nomes_sugestoes,
        hoje_raw=today_brt().strftime("%Y-%m-%d"),
        usuario=session.get("usuario", ""),
        tipo=session.get("tipo"),
        permissoes=session.get("permissoes", []),
    )


# ================== BAR ==================

def bar_auth(permissao=None):
    if not usuario_logado():
        return redirect(url_for("login"))
    if permissao and session.get("tipo") != "admin" and not tem_permissao(permissao):
        return redirect(url_for("vendas"))
    return None


def _pid_to_uuid(pid: str) -> str:
    h = pid.replace("-", "")
    if len(h) == 32:
        return f"{h[:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:]}"
    return pid


def bar_fifo_consume(product_id: str, qty_needed: float) -> tuple[float, list]:
    """Consume qty_needed do estoque principal (products) via FIFO.
    Se o produto não existir no estoque principal, usa bar_stock_batches como fallback."""
    produto = db.get_product(product_id)
    if produto:
        lotes = produto.get("lotes", [])
        remaining = qty_needed
        total_cost = 0.0
        log = []
        i = 0
        while remaining > 0 and i < len(lotes):
            lote = lotes[i]
            avail = to_float(lote.get("qtd", 0))
            custo_unit = to_float(lote.get("custo_unit", 0))
            consume = min(avail, remaining)
            total_cost += consume * custo_unit
            log.append({"data_lote": lote.get("data"), "qty": round(consume, 6), "unit_cost": custo_unit})
            novo_qtd = round(avail - consume, 6)
            lote["qtd"] = novo_qtd
            if novo_qtd <= 0:
                lotes.pop(i)
            else:
                i += 1
            remaining = round(remaining - consume, 6)
        atualizar_campos_derivados_estoque(produto)
        db.update_product(produto)
        return round(total_cost, 4), log

    # Fallback: bar_stock_batches (ingredientes cadastrados apenas no bar)
    batches = db.bar_get_available_batches(product_id)
    remaining = qty_needed
    total_cost = 0.0
    log = []
    for b in batches:
        if remaining <= 0:
            break
        avail = float(b["quantity_remaining"])
        unit_cost = float(b["total_cost"]) / float(b["quantity_total"])
        consume = min(avail, remaining)
        total_cost += consume * unit_cost
        log.append({"batch_id": b["id"], "qty": consume, "unit_cost": unit_cost})
        db.bar_update_batch_remaining(b["id"], round(avail - consume, 6))
        remaining -= consume
    return round(total_cost, 4), log


def bar_calc_drink_cost(product_id: str, quantity: float) -> tuple[float, list]:
    """Calculate cost and consume stock for a composite or simple product."""
    product = db.bar_get_product(product_id)
    if not product:
        return 0.0, []

    if product["type"] == "composite":
        components = db.bar_get_components(product_id)
        total_cost = 0.0
        all_logs = []
        for comp in components:
            unidade_uso = (comp.get("unidade_uso") or "UN").strip()
            qtd_bruta = float(comp["quantity"]) * quantity
            comp_pid_inner = (comp["component_id"] or "").replace("-", "")
            produto_comp = db.get_product(comp_pid_inner)
            cv = float((produto_comp or {}).get("conteudo_valor") or 1)
            needed = qtd_bruta / cv if (cv > 1 and unidade_uso.upper() != "UN") else qtd_bruta
            cost, log = bar_fifo_consume(comp_pid_inner, needed)
            if cost == 0 and produto_comp:
                vi = float(produto_comp.get("valor_investido", 0) or 0)
                cost = needed * vi
            total_cost += cost
            all_logs.extend(log)
        return round(total_cost, 4), all_logs
    else:
        return bar_fifo_consume(product_id, quantity)


# ── Produtos (drinks) ──

@app.route("/bar/produtos")
def bar_produtos():
    redir = bar_auth("ver_estoque")
    if redir: return redir

    import unicodedata
    def _norm(s):
        return unicodedata.normalize("NFD", (s or "").lower()).encode("ascii", "ignore").decode()

    todos = db.bar_get_all_products()
    stock = db.bar_get_stock_summary()
    costs = db.bar_get_cost_summary()
    costs.update(db.bar_get_composite_cost_summary())

    q = request.args.get("q", "").strip()
    cat = request.args.get("cat", "").strip().lower()
    codigo = request.args.get("codigo", "").strip().upper()

    produtos = [
        p for p in todos
        if (not q or _norm(q) in _norm(p.get("name", "")))
        and (not cat or (p.get("category") or "").lower() == cat)
        and (not codigo or (p.get("codigo") or "").upper() == codigo)
    ]

    categorias = sorted(set(
        (p.get("category") or "").strip()
        for p in todos if p.get("category")
    ))
    nomes_sugestoes = sorted(set(
        p.get("name", "").strip() for p in todos if p.get("name")
    ))

    return render_template("bar_produtos.html",
        produtos=produtos, stock=stock, costs=costs,
        q=q, cat=cat, codigo=codigo,
        categorias=categorias, nomes_sugestoes=nomes_sugestoes,
        tipo=session.get("tipo"), usuario=session.get("usuario", ""), permissoes=session.get("permissoes", []))



@app.route("/bar/produtos/novo", methods=["GET", "POST"])
def bar_produto_novo():
    redir = bar_auth("cadastrar_produto")
    if redir: return redir

    erro = ""
    if request.method == "POST":
        name  = (request.form.get("name") or "").strip()
        price = to_float(request.form.get("sale_price", "0"))

        category = (request.form.get("category") or "combo").strip()
        if not name:
            erro = "Nome obrigatório."
        else:
            pid = db.bar_insert_product({
                "name": name, "category": category, "unit": "un",
                "type": "composite", "sale_price": price
            })
            comp_ids   = request.form.getlist("component_id[]")
            comp_qtys  = request.form.getlist("component_qty[]")
            comp_units = request.form.getlist("unidade_uso[]")
            while len(comp_units) < len(comp_ids):
                comp_units.append("UN")
            for cid, qty_s, unit in zip(comp_ids, comp_qtys, comp_units):
                qty = to_float(qty_s)
                if cid and qty > 0:
                    db.bar_insert_component({"product_id": pid, "component_id": _pid_to_uuid(cid), "quantity": qty, "unidade_uso": unit or "UN"})
            return redirect(url_for("bar_produtos"))

    insumos = [{"id": p["pid"], "name": p["nome"], "unit": p.get("unit", "UN"),
                "cost": p.get("valor_investido", 0), "stock": p.get("quantidade", 0),
                "conteudo_valor": p.get("conteudo_valor") or 0,
                "conteudo_unidade": p.get("conteudo_unidade") or ""}
               for p in db.get_all_products()]
    return render_template("bar_produto_form.html", produto=None, components=[], insumos=insumos, erro=erro,
        tipo=session.get("tipo"), usuario=session.get("usuario", ""), permissoes=session.get("permissoes", []))


@app.route("/bar/produtos/<pid>/editar", methods=["GET", "POST"])
def bar_produto_editar(pid):
    redir = bar_auth("editar_produto")
    if redir: return redir

    produto = db.bar_get_product(pid)
    if not produto:
        return redirect(url_for("bar_produtos"))

    erro = ""
    if request.method == "POST":
        name  = (request.form.get("name") or "").strip()
        price = to_float(request.form.get("sale_price", "0"))
        category = (request.form.get("category") or "combo").strip()
        if not name:
            erro = "Nome obrigatório."
        else:
            db.bar_update_product(pid, {"name": name, "sale_price": price, "category": category})
            db.bar_delete_all_components(pid)
            comp_ids   = request.form.getlist("component_id[]")
            comp_qtys  = request.form.getlist("component_qty[]")
            comp_units = request.form.getlist("unidade_uso[]")
            while len(comp_units) < len(comp_ids):
                comp_units.append("UN")
            for cid, qty_s, unit in zip(comp_ids, comp_qtys, comp_units):
                qty = to_float(qty_s)
                if cid and qty > 0:
                    db.bar_insert_component({"product_id": pid, "component_id": _pid_to_uuid(cid), "quantity": qty, "unidade_uso": unit or "UN"})
            return redirect(url_for("bar_produtos"))

    insumos    = [{"id": p["pid"], "name": p["nome"], "unit": p.get("unit", "UN"),
                   "cost": p.get("valor_investido", 0), "stock": p.get("quantidade", 0),
                   "conteudo_valor": p.get("conteudo_valor") or 0,
                   "conteudo_unidade": p.get("conteudo_unidade") or ""}
                  for p in db.get_all_products()]
    components = db.bar_get_components(pid)
    return render_template("bar_produto_form.html", produto=produto, components=components, insumos=insumos, erro=erro,
        tipo=session.get("tipo"), usuario=session.get("usuario", ""), permissoes=session.get("permissoes", []))


@app.route("/bar/produtos/<pid>/deletar", methods=["POST"])
def bar_produto_deletar(pid):
    redir = bar_auth("excluir_produto")
    if redir: return redir
    db.bar_delete_product(pid)
    return redirect(url_for("bar_produtos"))


# ── Lotes (pacotes) ──

@app.route("/bar/produtos/<pid>/lotes", methods=["GET", "POST"])
def bar_lotes(pid):
    redir = bar_auth("editar_produto")
    if redir: return redir

    produto = db.bar_get_product(pid)
    if not produto:
        return redirect(url_for("bar_produtos"))

    erro = ""
    if request.method == "POST":
        qty_packages      = to_float(request.form.get("quantity_packages", "0"))
        units_per_package = to_float(request.form.get("units_per_package", "1"))
        cost_per_package  = to_float(request.form.get("cost_per_package", "0"))
        prod_unit_value   = to_float(produto.get("unit_value") or 1) or 1.0
        qty_total  = round(qty_packages * units_per_package * prod_unit_value, 6)
        total_cost = round(qty_packages * cost_per_package, 2)

        if qty_packages <= 0:
            erro = "Quantidade de pacotes deve ser maior que zero."
        elif units_per_package <= 0:
            erro = "Unidades por pacote deve ser maior que zero."
        elif cost_per_package < 0:
            erro = "Custo não pode ser negativo."
        else:
            db.bar_insert_batch({
                "product_id": pid,
                "quantity_total": qty_total,
                "quantity_remaining": qty_total,
                "unit_value": prod_unit_value,
                "total_cost": total_cost
            })
            return redirect(url_for("bar_lotes", pid=pid))

    batches = db.bar_get_batches(pid)
    return render_template("bar_lotes.html",
        produto=produto, batches=batches, erro=erro,
        tipo=session.get("tipo"), usuario=session.get("usuario", ""), permissoes=session.get("permissoes", []))


# ================== PERMISSÕES ==================
@app.route("/permissoes", methods=["GET", "POST"])
def permissoes():
    if not usuario_logado():
        return redirect(url_for("login"))

    if session.get("tipo") != "admin":
        return redirect(url_for("vendas"))

    usuarios = db.get_all_users()

    def _log(tipo_evento, descricao, alvo=None, resultado="sucesso"):
        db.registrar_log(
            session.get("usuario", ""), tipo_evento, descricao,
            now_brt().strftime("%Y-%m-%d"), now_brt().strftime("%H:%M"),
            resultado=resultado, detalhes={"usuario_alvo": alvo} if alvo else None,
        )

    if request.method == "POST":
        acao = (request.form.get("acao") or "").strip()

        if acao == "toggle_ativo":
            usuario_alvo = (request.form.get("usuario_alvo") or "").strip()
            if (usuario_alvo in usuarios
                    and usuario_alvo != "admin"
                    and normalizar_tipo(usuarios[usuario_alvo].get("tipo")) != "admin"):
                ativo_atual = usuarios[usuario_alvo].get("ativo", True)
                db.set_user_ativo(usuario_alvo, not ativo_atual)
                if ativo_atual:
                    db.encerrar_sessoes_usuario(usuario_alvo, session.get("usuario", ""), quando=now_brt().strftime("%Y-%m-%d %H:%M:%S"))
                _log("usuario_bloqueado" if ativo_atual else "usuario_ativado",
                     f"Usuário {'bloqueado' if ativo_atual else 'ativado'}: {usuario_alvo}", usuario_alvo)
            return redirect(url_for("permissoes") + "?salvo=1")

        if acao == "arquivar_usuario":
            usuario_alvo = (request.form.get("usuario_alvo") or "").strip()
            if (usuario_alvo in usuarios
                    and usuario_alvo != "admin"
                    and normalizar_tipo(usuarios[usuario_alvo].get("tipo")) != "admin"):
                arquivado_atual = usuarios[usuario_alvo].get("arquivado", False)
                db.set_user_arquivado(usuario_alvo, not arquivado_atual)
                if not arquivado_atual:
                    db.encerrar_sessoes_usuario(usuario_alvo, session.get("usuario", ""), quando=now_brt().strftime("%Y-%m-%d %H:%M:%S"))
                _log("usuario_arquivado" if not arquivado_atual else "usuario_desarquivado",
                     f"Usuário {'arquivado' if not arquivado_atual else 'desarquivado'}: {usuario_alvo}", usuario_alvo)
            return redirect(url_for("permissoes") + "?salvo=1")

        if acao == "criar_usuario":
            novo_login = (request.form.get("novo_login") or "").strip()
            novo_nome = (request.form.get("novo_nome") or "").strip()
            nova_senha = request.form.get("nova_senha") or ""
            novo_cargo = (request.form.get("novo_cargo") or "funcionario").strip()
            if novo_cargo not in CARGOS or novo_cargo == "admin":
                novo_cargo = "funcionario"
            erro_criar = ""
            if not novo_login or not nova_senha:
                erro_criar = "Login e senha são obrigatórios."
            elif novo_login in usuarios:
                erro_criar = "Já existe um usuário com esse login."
            if not erro_criar:
                db.insert_user({
                    "username": novo_login,
                    "nome": novo_nome,
                    "senha": db.hash_senha(nova_senha),
                    "tipo": "funcionario",
                    "cargo": novo_cargo,
                    "permissoes": CARGO_PERMISSOES_PADRAO.get(novo_cargo, []),
                    "ativo": True,
                    "arquivado": False,
                    "data_criacao": now_brt().strftime("%Y-%m-%d"),
                })
                _log("usuario_criado", f"Usuário criado: {novo_login} (cargo {novo_cargo})", novo_login)
                return redirect(url_for("permissoes") + "?salvo=1")
            return redirect(url_for("permissoes") + "?erro=" + erro_criar)

        if acao == "trocar_senha":
            usuario_alvo = (request.form.get("usuario_alvo") or "").strip()
            nova_senha = request.form.get("nova_senha") or ""
            if usuario_alvo in usuarios and nova_senha:
                db.update_user_senha(usuario_alvo, db.hash_senha(nova_senha))
                db.encerrar_sessoes_usuario(usuario_alvo, session.get("usuario", ""), quando=now_brt().strftime("%Y-%m-%d %H:%M:%S"))
                _log("senha_alterada", f"Senha redefinida para: {usuario_alvo}", usuario_alvo)
            return redirect(url_for("permissoes") + "?salvo=1")

        if acao == "trocar_pin":
            usuario_alvo = (request.form.get("usuario_alvo") or "").strip()
            novo_pin = (request.form.get("novo_pin") or "").strip()
            if usuario_alvo in usuarios and novo_pin.isdigit() and len(novo_pin) in (4, 6):
                db.update_user_pin(usuario_alvo, db.hash_pin(novo_pin))
                _log("pin_alterado", f"PIN redefinido para: {usuario_alvo}", usuario_alvo)
            return redirect(url_for("permissoes") + "?salvo=1")

        if acao == "encerrar_sessoes":
            usuario_alvo = (request.form.get("usuario_alvo") or "").strip()
            if usuario_alvo in usuarios:
                db.encerrar_sessoes_usuario(usuario_alvo, session.get("usuario", ""), quando=now_brt().strftime("%Y-%m-%d %H:%M:%S"))
                _log("sessao_encerrada", f"Sessões encerradas para: {usuario_alvo}", usuario_alvo)
            return redirect(url_for("permissoes") + "?salvo=1")

        usuario_alvo = (request.form.get("usuario_alvo") or "").strip()
        permissoes_marcadas = request.form.getlist("permissoes")
        permissoes_marcadas = [p for p in permissoes_marcadas if p in PERMISSOES_TODAS]
        cargo_novo = (request.form.get("cargo") or "funcionario").strip()
        if cargo_novo not in CARGOS:
            cargo_novo = "funcionario"

        if usuario_alvo in usuarios and normalizar_tipo(usuarios[usuario_alvo].get("tipo")) != "admin":
            db.update_user_permissions(usuario_alvo, permissoes_marcadas)
            try:
                db.update_user_cargo(usuario_alvo, cargo_novo)
            except Exception:
                pass
            _log("permissoes_alteradas", f"Permissões/cargo atualizados para: {usuario_alvo}", usuario_alvo)

        return redirect(url_for("permissoes") + "?salvo=1")

    sessoes_ativas = {s["username"] for s in db.listar_sessoes_ativas()}

    return render_template(
        "permissoes.html",
        usuarios=usuarios,
        sessoes_ativas=sessoes_ativas,
        permissoes_disponiveis=PERMISSOES_TODAS,
        cargos=CARGOS,
        cargo_permissoes_padrao=CARGO_PERMISSOES_PADRAO,
        usuario=session.get("usuario", ""),
        tipo=session.get("tipo"),
        permissoes=session.get("permissoes", [])
    )


# ================== USUÁRIOS ONLINE (painel de monitoramento) ==================
ICONE_EVENTO = {
    "login": "🔓", "logout": "🚪", "venda_criada": "🛒", "venda_cancelada": "❌",
    "produto_criado": "📦", "produto_editado": "✏️", "estoque_alterado": "📦",
    "caixa_aberto": "💰", "caixa_fechado": "💰", "comanda_criada": "📋",
    "pagamento_comanda": "💳", "cliente_criado": "🧑", "autorizacao_pin": "🔑",
    "sessao_encerrada": "🚪",
}


def _parse_user_agent(ua: str) -> str:
    ua = ua or ""
    if "Edg/" in ua:
        navegador = "Edge"
    elif "Chrome/" in ua and "Chromium" not in ua:
        navegador = "Chrome"
    elif "Firefox/" in ua:
        navegador = "Firefox"
    elif "Safari/" in ua and "Chrome/" not in ua:
        navegador = "Safari"
    else:
        navegador = "Navegador"

    if "Windows" in ua:
        so = "Windows"
    elif "Mac OS" in ua or "Macintosh" in ua:
        so = "macOS"
    elif "Android" in ua:
        so = "Android"
    elif "iPhone" in ua or "iPad" in ua:
        so = "iOS"
    elif "Linux" in ua:
        so = "Linux"
    else:
        so = ""

    return f"{navegador} no {so}" if so else navegador


def _parse_dt(s):
    if not s:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M"):
        try:
            return datetime.strptime(s, fmt)
        except (ValueError, TypeError):
            continue
    return None


def _fmt_tempo(minutos: float) -> str:
    minutos = max(0, int(round(minutos)))
    h, m = divmod(minutos, 60)
    return f"{h}h{m:02d}min" if h else f"{m}min"


@app.route("/usuarios/online")
def usuarios_online():
    if not usuario_logado():
        return redirect(url_for("login"))
    if session.get("tipo") != "admin":
        return redirect(url_for("vendas"))

    usuarios = db.get_all_users()
    hoje = now_brt().strftime("%Y-%m-%d")
    agora = now_brt().replace(tzinfo=None)

    sessoes_hoje = db.listar_sessoes_do_dia_todos(hoje)
    sessoes_por_usuario = defaultdict(list)
    for s in sessoes_hoje:
        sessoes_por_usuario[s["username"]].append(s)

    logs_hoje = db.get_logs(data_ini=hoje, data_fim=hoje)
    logs_por_usuario = defaultdict(list)
    for l in logs_hoje:
        logs_por_usuario[l["usuario"]].append(l)

    linhas = []
    tempos_trabalhados = []
    contagem_status = {"em_expediente": 0, "ausente": 0, "em_pausa": 0, "encerrado": 0, "bloqueado": 0}

    for username, info in usuarios.items():
        if info.get("arquivado"):
            continue

        sessoes_usr = sorted(sessoes_por_usuario.get(username, []), key=lambda s: s.get("criado_em") or "")
        ativas = [s for s in sessoes_usr if s.get("ativa")]

        inicio_dt = _parse_dt(sessoes_usr[0]["criado_em"]) if sessoes_usr else None
        if ativas:
            fim_dt = None
            ultima_atividade_dt = max((_parse_dt(s.get("ultima_atividade")) for s in ativas if _parse_dt(s.get("ultima_atividade"))), default=None)
        else:
            encerradas_em = [_parse_dt(s.get("encerrada_em")) or _parse_dt(s.get("ultima_atividade")) for s in sessoes_usr]
            encerradas_em = [d for d in encerradas_em if d]
            fim_dt = max(encerradas_em) if encerradas_em else None
            ultima_atividade_dt = fim_dt

        if not info.get("ativo", True):
            status = "bloqueado"
        elif ativas:
            minutos_inativo = (agora - ultima_atividade_dt).total_seconds() / 60 if ultima_atividade_dt else 999
            status = "em_expediente" if minutos_inativo <= 5 else "ausente"
        else:
            status = "encerrado"
        contagem_status[status] += 1

        tempo_min = None
        if inicio_dt:
            fim_calc = fim_dt or (agora if ativas else None)
            if fim_calc:
                tempo_min = (fim_calc - inicio_dt).total_seconds() / 60
                tempos_trabalhados.append(tempo_min)

        logs_usr = sorted(logs_por_usuario.get(username, []), key=lambda l: l.get("hora", ""), reverse=True)
        ultima_acao = None
        if logs_usr:
            l0 = logs_usr[0]
            ultima_acao = {"icone": ICONE_EVENTO.get(l0["tipo_evento"], "•"), "descricao": l0["descricao"], "hora": l0["hora"]}

        sessoes_detalhe = [{
            "token": s["token"],
            "dispositivo": _parse_user_agent(s.get("user_agent")),
            "ultima_atividade": s.get("ultima_atividade"),
        } for s in ativas]

        resumo_dia = {
            "vendas": sum(1 for l in logs_usr if l["tipo_evento"] == "venda_criada"),
            "cancelamentos": sum(1 for l in logs_usr if l["tipo_evento"] == "venda_cancelada"),
            "produtos_criados": sum(1 for l in logs_usr if l["tipo_evento"] == "produto_criado"),
            "produtos_editados": sum(1 for l in logs_usr if l["tipo_evento"] == "produto_editado"),
            "autorizacoes_pin": sum(1 for l in logs_usr if l["tipo_evento"] == "autorizacao_pin"),
            "caixas_fechados": sum(1 for l in logs_usr if l["tipo_evento"] == "caixa_fechado"),
        }

        linhas.append({
            "username": username,
            "nome": info.get("nome") or username,
            "cargo": info.get("cargo") or "funcionario",
            "status": status,
            "inicio_expediente": inicio_dt.strftime("%H:%M") if inicio_dt else "—",
            "fim_expediente": fim_dt.strftime("%H:%M") if fim_dt else "—",
            "tempo_trabalhando": _fmt_tempo(tempo_min) if tempo_min is not None else "—",
            "ultima_atividade": ultima_atividade_dt.strftime("%Y-%m-%d %H:%M:%S") if ultima_atividade_dt else "",
            "tela_atual": (ativas[-1].get("tela_atual") or "—") if ativas else "—",
            "ultima_acao": ultima_acao,
            "sessoes": sessoes_detalhe,
            "resumo_dia": resumo_dia,
            "permissoes": info.get("permissoes") or [],
            "ultimo_login": info.get("ultimo_login") or "—",
        })

    linhas.sort(key=lambda x: (["bloqueado", "em_expediente", "ausente", "em_pausa", "encerrado"].index(x["status"]), x["username"]))

    tempo_medio = _fmt_tempo(sum(tempos_trabalhados) / len(tempos_trabalhados)) if tempos_trabalhados else "—"

    dashboard = {
        "cadastrados": len(linhas),
        "em_expediente": contagem_status["em_expediente"],
        "em_pausa": contagem_status["em_pausa"],
        "encerrado": contagem_status["encerrado"],
        "ausente": contagem_status["ausente"],
        "bloqueado": contagem_status["bloqueado"],
        "tempo_medio": tempo_medio,
    }

    return render_template(
        "usuarios_online.html",
        linhas=linhas,
        dashboard=dashboard,
        cargos=CARGOS,
        usuario=session.get("usuario", ""),
        tipo=session.get("tipo"),
        permissoes=session.get("permissoes", [])
    )


@app.route("/usuarios/online/encerrar/<token>", methods=["POST"])
def usuarios_online_encerrar(token):
    if not usuario_logado():
        return redirect(url_for("login"))
    if session.get("tipo") != "admin":
        return redirect(url_for("vendas"))

    db.encerrar_sessao(token, session.get("usuario", ""), quando=now_brt().strftime("%Y-%m-%d %H:%M:%S"))
    db.registrar_log(
        session.get("usuario", ""), "sessao_encerrada", f"Sessão encerrada manualmente (token {token[:8]}…)",
        now_brt().strftime("%Y-%m-%d"), now_brt().strftime("%H:%M"),
    )
    return redirect(url_for("usuarios_online"))


@app.route("/usuarios/online/encerrar_todas/<username>", methods=["POST"])
def usuarios_online_encerrar_todas(username):
    if not usuario_logado():
        return redirect(url_for("login"))
    if session.get("tipo") != "admin":
        return redirect(url_for("vendas"))

    db.encerrar_sessoes_usuario(username, session.get("usuario", ""), quando=now_brt().strftime("%Y-%m-%d %H:%M:%S"))
    db.registrar_log(
        session.get("usuario", ""), "sessao_encerrada", f"Todas as sessões de {username} foram encerradas manualmente",
        now_brt().strftime("%Y-%m-%d"), now_brt().strftime("%H:%M"),
    )
    return redirect(url_for("usuarios_online"))


# ================== LOGS / AUDITORIA ==================
@app.route("/logs")
def logs():
    if not usuario_logado():
        return redirect(url_for("login"))
    if session.get("tipo") != "admin":
        return redirect(url_for("vendas"))

    data_ini = (request.args.get("data_ini") or "").strip()
    data_fim = (request.args.get("data_fim") or "").strip()
    usuario_filtro = (request.args.get("usuario") or "").strip()
    tipo_evento_filtro = (request.args.get("tipo_evento") or "").strip()
    resultado_filtro = (request.args.get("resultado") or "").strip()

    registros = db.get_logs(
        data_ini=data_ini, data_fim=data_fim, usuario=usuario_filtro,
        tipo_evento=tipo_evento_filtro, resultado=resultado_filtro,
    )

    usuarios_conhecidos = sorted(db.get_all_users().keys())
    tipos_conhecidos = sorted(set(r.get("tipo_evento", "") for r in registros) | {
        "login", "logout", "usuario_criado", "usuario_bloqueado", "usuario_ativado",
        "usuario_arquivado", "usuario_desarquivado", "senha_alterada", "pin_alterado",
        "permissoes_alteradas", "sessao_encerrada", "autorizacao_pin",
    })

    return render_template(
        "logs.html",
        registros=registros,
        data_ini=data_ini, data_fim=data_fim,
        usuario_filtro=usuario_filtro, tipo_evento_filtro=tipo_evento_filtro, resultado_filtro=resultado_filtro,
        usuarios_conhecidos=usuarios_conhecidos, tipos_conhecidos=sorted(tipos_conhecidos),
        usuario=session.get("usuario", ""),
        tipo=session.get("tipo"),
        permissoes=session.get("permissoes", [])
    )


# ================== AUTORIZAÇÃO POR PIN ==================
def autorizar_acao(permissao):
    """Retorna None se a ação pode prosseguir (usuário tem a permissão, ou um
    autorizador válido cobriu via PIN — em ambos os casos já registra o log).
    Caso contrário, retorna um redirect e registra o log como negado."""
    usuario_atual = session.get("usuario", "")
    hoje = now_brt().strftime("%Y-%m-%d")
    hora = now_brt().strftime("%H:%M")

    if session.get("tipo") == "admin" or tem_permissao(permissao):
        db.registrar_log(usuario_atual, "autorizacao_pin", f"Ação permitida ({permissao})", hoje, hora)
        return None

    autorizador = (request.form.get("autorizador_usuario") or "").strip()
    pin = (request.form.get("autorizador_pin") or "").strip()

    if autorizador and pin:
        autorizador_data = db.get_user(autorizador)
        autorizador_ok = (
            autorizador_data
            and autorizador_data.get("ativo", True)
            and not autorizador_data.get("arquivado")
            and db.verificar_pin(autorizador_data.get("pin_hash"), pin)
            and (normalizar_tipo(autorizador_data.get("tipo")) == "admin"
                 or permissao in (autorizador_data.get("permissoes") or []))
        )
        if autorizador_ok:
            db.registrar_log(
                usuario_atual, "autorizacao_pin", f"Ação autorizada por PIN ({permissao})",
                hoje, hora, autorizado_por=autorizador,
            )
            return None
        db.registrar_log(
            usuario_atual, "autorizacao_pin", f"PIN inválido ou sem permissão ({permissao}, autorizador: {autorizador})",
            hoje, hora, resultado="negado",
        )
        flash_erro = "PIN inválido ou usuário autorizador sem permissão para esta ação."
    else:
        db.registrar_log(usuario_atual, "autorizacao_pin", f"Ação negada, sem permissão ({permissao})", hoje, hora, resultado="negado")
        flash_erro = "Você não possui permissão para executar esta operação."

    session["_erro_autorizacao"] = flash_erro
    return redirect(request.referrer or url_for("vendas"))


# ================== RUN ==================
if __name__ == "__main__":
    app.run(debug=True, port=5003)
