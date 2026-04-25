from flask import Flask, render_template, request, redirect, session, url_for, send_file
import os
import time
import uuid
from collections import defaultdict
from datetime import datetime, date
from io import BytesIO

from dotenv import load_dotenv
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas

import db

load_dotenv()

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "isa_modas_secreto")


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


PERMISSOES_TODAS = ["estoque", "vendas", "financeiro", "permissoes"]


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
        return datetime.now().strftime("%Y-%m-%d")
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
    return datetime.now().strftime("%Y-%m-%d")


def normalizar_pagamento(forma):
    f = (forma or "").strip().lower()
    if f in ["pix", "dinheiro"]:
        return f
    if f in ["cartao", "cartão", "card"]:
        return "cartao"
    return "cartao"


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
    produto["valor_investido"] = round((valor / qtd), 4) if qtd > 0 else 0.0


def entrada_estoque_lote(produto, data, qtd, custo_unit):
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
        "custo_unit": custo_unit
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


# ================== ATUALIZA SESSÃO ==================
@app.before_request
def atualizar_permissoes_da_sessao():
    if "usuario" not in session:
        return

    usuario = session.get("usuario", "")
    try:
        user_data = db.get_user(usuario)
    except Exception:
        return  # falha de rede transitória — mantém sessão atual

    if not user_data:
        session.clear()
        return

    tipo_atual = normalizar_tipo(user_data.get("tipo", "funcionario"))

    if usuario == "admin" or tipo_atual == "admin":
        session["tipo"] = "admin"
        session["permissoes"] = PERMISSOES_TODAS[:]
        return

    session["tipo"] = tipo_atual
    permissoes = user_data.get("permissoes", [])
    if not isinstance(permissoes, list):
        permissoes = []
    permissoes = [p for p in permissoes if p in PERMISSOES_TODAS]
    session["permissoes"] = permissoes


# ================== LOGIN ==================
@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        usuario = request.form.get("usuario", "").strip()
        senha = request.form.get("senha", "").strip()

        user_data = db.get_user(usuario)

        if user_data and user_data.get("senha") == senha:
            session.clear()
            session["usuario"] = usuario

            tipo = normalizar_tipo(user_data.get("tipo", "funcionario"))

            if usuario == "admin" or tipo == "admin":
                session["tipo"] = "admin"
                session["permissoes"] = PERMISSOES_TODAS[:]
            else:
                session["tipo"] = tipo
                perms = user_data.get("permissoes", [])
                if not isinstance(perms, list):
                    perms = []
                perms = [p for p in perms if p in PERMISSOES_TODAS]
                session["permissoes"] = perms

            return redirect(url_for("vendas"))

        return render_template("login.html", erro="Usuário ou senha inválidos")

    return render_template("login.html")


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


# ================== VENDAS ==================
@app.route("/")
@app.route("/vendas")
def vendas():
    if not usuario_logado():
        return redirect(url_for("login"))

    if session.get("tipo") != "admin" and not tem_permissao("vendas"):
        return redirect(url_for("login"))

    vendas_data = db.get_all_sales()

    data_ini_raw = request.args.get("data_ini", "").strip()
    data_fim_raw = request.args.get("data_fim", "").strip()
    busca_produto_raw = request.args.get("busca_produto", "").strip()
    forma_pagamento_raw = request.args.get("forma_pagamento", "").strip()
    margem_min_raw = request.args.get("margem_min", "").strip()
    ordenar_raw = request.args.get("ordenar", "data_desc").strip()

    # quick date shortcuts
    hoje_date = date.today()
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
        data_fim_raw = data_ini_raw

    data_ini = parse_date_yyyy_mm_dd(data_ini_raw)
    data_fim = parse_date_yyyy_mm_dd(data_fim_raw)
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
                if margem_calc(v) < float(margem_min_raw):
                    ok = False
            except ValueError:
                pass
        if ok:
            vendas_filtradas.append(v)

    def chave_data(v):
        d = parse_date_yyyy_mm_dd(v.get("data", ""))
        return d or datetime.min.date()

    if ordenar_raw == "margem_asc":
        vendas_filtradas.sort(key=margem_calc)
    elif ordenar_raw == "margem_desc":
        vendas_filtradas.sort(key=margem_calc, reverse=True)
    elif ordenar_raw == "valor_desc":
        vendas_filtradas.sort(
            key=lambda v: float(v.get("valor_venda") or 0) * float(v.get("quantidade") or 1),
            reverse=True
        )
    elif ordenar_raw == "nome_asc":
        vendas_filtradas.sort(key=lambda v: str(v.get("nome", "")).lower())
    else:
        vendas_filtradas.sort(key=chave_data, reverse=True)

    # stats calculados sobre lista filtrada completa
    total_vendido = sum(
        float(v.get("valor_venda") or 0) * float(v.get("quantidade") or 1)
        for v in vendas_filtradas
    )
    num_vendas = len(vendas_filtradas)
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
    total_registros = len(vendas_filtradas)
    total_paginas = (total_registros + por_pagina - 1) // por_pagina

    if total_paginas >= 1 and pagina > total_paginas:
        pagina = total_paginas

    inicio = (pagina - 1) * por_pagina
    fim = inicio + por_pagina
    vendas_pagina = vendas_filtradas[inicio:fim]
    inicio_reg = inicio + 1 if vendas_filtradas else 0
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
        forma_pagamento_raw=forma_pagamento_raw,
        margem_min_raw=margem_min_raw,
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
    if session.get("tipo") != "admin" and not tem_permissao("vendas"):
        return redirect(url_for("login"))

    import csv, io as _io
    from datetime import timedelta

    vendas_data = db.get_all_sales()

    data_ini_raw = request.args.get("data_ini", "").strip()
    data_fim_raw = request.args.get("data_fim", "").strip()
    busca_produto_raw = request.args.get("busca_produto", "").strip()
    forma_pagamento_raw = request.args.get("forma_pagamento", "").strip()
    margem_min_raw = request.args.get("margem_min", "").strip()
    ordenar_raw = request.args.get("ordenar", "data_desc").strip()

    hoje_date = date.today()
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

    if session.get("tipo") != "admin" and not tem_permissao("vendas"):
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


# ================== EXCLUIR VENDA ==================
@app.route("/vendas/excluir/<uid>")
def excluir_venda(uid):
    if not usuario_logado():
        return redirect(url_for("login"))

    if session.get("tipo") != "admin" and not tem_permissao("vendas"):
        return redirect(url_for("vendas"))

    venda = db.get_sale(uid)

    if venda:
        if venda.get("fechado"):
            return redirect(url_for("vendas"))

        pid = str(venda.get("produto_pid", "")).strip()
        produto = db.get_product(pid)
        consumo_lotes = venda.get("consumo_lotes", [])

        if produto and consumo_lotes:
            devolver_fifo(produto, consumo_lotes)
            db.update_product(produto)

        db.delete_sale(uid)

    return redirect(url_for("vendas"))


@app.route("/vendas/ver/<uid>")
def ver_venda(uid):
    if not usuario_logado():
        return redirect(url_for("login"))

    if session.get("tipo") != "admin" and not tem_permissao("vendas"):
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


# ================== ESTOQUE ==================
@app.route("/estoque", methods=["GET"])
def estoque():
    if not usuario_logado():
        return redirect(url_for("login"))

    if not tem_permissao("estoque"):
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

    q            = (request.args.get("q", "")         or "").strip()
    cat_filtro   = (request.args.get("categoria", "")  or "").strip()
    status_filtro = (request.args.get("status", "")    or "").strip()

    estoque_filtrado = estoque_data
    if q:
        q_low = q.lower()
        estoque_filtrado = [p for p in estoque_filtrado if q_low in str(p.get("nome", "")).lower()]
    if cat_filtro:
        estoque_filtrado = [p for p in estoque_filtrado if (p.get("categoria") or "") == cat_filtro]
    if status_filtro:
        estoque_filtrado = [p for p in estoque_filtrado if p["_status"] == status_filtro]

    nomes_sugestoes = sorted(set(
        p.get("nome", "").strip() for p in estoque_data
        if p.get("nome", "").strip()
    ))

    return render_template(
        "estoque.html",
        estoque=estoque_filtrado,
        q=q,
        cat_filtro=cat_filtro,
        status_filtro=status_filtro,
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
    if not tem_permissao("estoque"):
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

    if not tem_permissao("estoque"):
        return redirect(url_for("vendas"))

    erro = ""

    if request.method == "POST":
        nome          = (request.form.get("nome", "") or "").strip()
        data          = (request.form.get("data", "") or "").strip()
        valor_venda   = to_float(request.form.get("valor_venda", "0"))
        forma_entrada = request.form.get("forma_entrada", "unidade")
        tipo_custo    = request.form.get("tipo_custo", "total")
        data_entrada  = request.form.get("data_entrada") or data or date.today().isoformat()

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
                    entrada_estoque_lote(existente, data_entrada, quantidade, valor_investido)
                    db.update_product(existente)
                except ValueError as e:
                    erro = str(e)
            else:
                novo = {
                    "pid": uuid.uuid4().hex,
                    "data": data,
                    "nome": nome,
                    "valor_venda": valor_venda,
                    "lotes": [],
                    **extras,
                }
                try:
                    entrada_estoque_lote(novo, data_entrada, quantidade, valor_investido)
                except ValueError as e:
                    erro = str(e)

                if not erro:
                    db.insert_product(novo)

            if not erro:
                return redirect(url_for("estoque"))

    return render_template(
        "estoque_novo.html",
        erro=erro,
        hoje=date.today().isoformat(),
        fornecedores=[],
        tipo=session.get("tipo"),
        permissoes=session.get("permissoes", []),
    )


# ================== EDITAR PRODUTO ==================
@app.route("/estoque/editar/<pid>", methods=["GET", "POST"])
def editar_produto(pid):
    if not usuario_logado():
        return redirect(url_for("login"))

    if not tem_permissao("estoque"):
        return redirect(url_for("vendas"))

    produto = db.get_product(pid)
    if not produto:
        return redirect(url_for("estoque"))

    erro = ""

    if request.method == "POST":
        nome            = (request.form.get("nome") or "").strip()
        data            = (request.form.get("data") or "").strip()
        valor_venda     = to_float(request.form.get("valor_venda"), 0.0)
        valor_investido = to_float(request.form.get("valor_investido"), 0.0)

        if not nome:
            erro = "Informe o nome do produto."
        elif valor_venda <= 0:
            erro = "Preço de venda inválido."
        else:
            produto["nome"]        = nome
            produto["data"]        = data
            produto["valor_venda"] = valor_venda
            if valor_investido > 0:
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

    if session.get("tipo") != "admin":
        return redirect(url_for("estoque"))

    db.delete_product(pid)
    return redirect(url_for("estoque"))


# ================== LOTES DO PRODUTO ==================
@app.route("/estoque/produto/<pid>", methods=["GET", "POST"])
def estoque_produto(pid):
    if not usuario_logado():
        return redirect(url_for("login"))
    if not tem_permissao("estoque"):
        return redirect(url_for("vendas"))

    produto = db.get_product(pid)
    if not produto:
        return redirect(url_for("estoque"))

    erro = ""

    if request.method == "POST":
        forma_entrada    = request.form.get("forma_entrada", "unidade")
        data             = (request.form.get("data") or "").strip()
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
            }
            if unid_por_caixa:
                lote["unidades_por_caixa"] = unid_por_caixa
            if conteudo_val:
                lote["conteudo_valor"]  = conteudo_val
                lote["conteudo_unidade"] = conteudo_unit or "ml"
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

    all_sales = db.get_all_sales()
    saidas = sorted(
        [s for s in all_sales
         if s.get("produto_tipo") == "estoque" and s.get("produto_pid") == pid],
        key=lambda s: s.get("data", ""),
        reverse=True
    )

    return render_template(
        "estoque_produto.html",
        produto=produto,
        lotes=lotes,
        saidas=saidas,
        erro=erro,
        hoje=date.today().isoformat(),
        pacotes_usando=pacotes_usando,
        tipo=session.get("tipo"),
        permissoes=session.get("permissoes", [])
    )


@app.route("/estoque/produto/<pid>/lote/<int:idx>/remover", methods=["POST"])
def remover_lote(pid, idx):
    if not usuario_logado() or not tem_permissao("estoque"):
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
    if not usuario_logado() or not tem_permissao("estoque"):
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
                if nova_qtd > 0 and novo_custo > 0:
                    for l in produto["lotes"]:
                        if l is lote_alvo:
                            l["data"]      = normalizar_data_str(nova_data)
                            l["qtd"]       = nova_qtd
                            l["custo_unit"] = novo_custo
                            break
                    atualizar_campos_derivados_estoque(produto)
                    db.update_product(produto)
    return redirect(url_for("estoque_produto", pid=pid))


# ================== CADASTRAR VENDA ==================
@app.route("/cadastrar_venda", methods=["GET", "POST"])
def cadastrar_venda():
    if not usuario_logado():
        return redirect(url_for("login"))

    if not tem_permissao("vendas"):
        return redirect(url_for("vendas"))

    estoque = db.get_all_products()
    erro = ""

    if request.method == "GET":
        hoje = datetime.now().strftime("%Y-%m-%d")
        return render_template(
            "cadastrar_venda.html",
            estoque=estoque,
            erro="",
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
        id_venda = f"V{int(time.time())}"
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
                    "consumo_lotes": consumo
                })
                produtos_modificados.append(produto)

            if not erro:
                for venda in novas_vendas:
                    db.insert_sale(venda)
                for produto in produtos_modificados:
                    db.update_product(produto)
                return redirect(url_for("cadastrar_venda_lote") + "?sucesso=1")

    hoje = datetime.now().strftime("%Y-%m-%d")
    drinks = []
    for d in db.bar_get_all_products():
        if d["type"] != "composite":
            continue
        comps = db.bar_get_components(d["id"])
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

    if not tem_permissao("vendas"):
        return redirect(url_for("vendas"))

    estoque = db.get_all_products()
    drinks = []
    for d in db.bar_get_all_products():
        if d["type"] != "composite":
            continue
        comps = db.bar_get_components(d["id"])
        d["componentes"] = [
            {"nome": c.get("component", {}).get("name", "?"),
             "quantidade": c.get("quantity", 0),
             "unidade_uso": c.get("unidade_uso") or "UN"}
            for c in comps
        ]
        drinks.append(d)
    erro = ""

    if request.method == "POST":
        data = request.form.get("data", "")
        forma_pagamento = request.form.get("forma_pagamento", "")

        pids   = request.form.getlist("produto_pid[]")
        qtds   = request.form.getlist("quantidade[]")
        tipos  = request.form.getlist("produto_tipo[]")
        vpcs   = request.form.getlist("vender_por_conteudo[]")

        # garante que tipos e vpcs têm o mesmo tamanho que pids
        while len(tipos) < len(pids):
            tipos.append("estoque")
        while len(vpcs) < len(pids):
            vpcs.append("0")

        if not pids or not qtds or len(pids) != len(qtds):
            erro = "Adicione pelo menos 1 item na venda."
        else:
            id_venda = f"V{int(time.time())}"
            novas_vendas = []
            produtos_modificados = []

            for pid, qtd_raw, tipo, vpc_flag in zip(pids, qtds, tipos, vpcs):
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
                    novas_vendas.append({
                        "uid": uuid.uuid4().hex,
                        "id_venda": id_venda,
                        "data": data,
                        "produto_pid": pid,
                        "produto_tipo": "bar",
                        "nome": bar_prod["name"],
                        "valor_venda": to_float(bar_prod.get("sale_price", 0)),
                        "valor_investido": custo_unit,
                        "quantidade": qtd,
                        "forma_pagamento": forma_pagamento,
                        "consumo_lotes": consumo
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
                    preco_unit = to_float(produto.get("valor_venda", 0)) / cv if (vpc and cv > 1) else to_float(produto.get("valor_venda", 0))
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
                        "consumo_lotes": consumo
                    })

            if not erro and not novas_vendas:
                erro = "Adicione pelo menos 1 produto com quantidade válida."

            if not erro:
                for venda in novas_vendas:
                    db.insert_sale(venda)
                for produto in produtos_modificados:
                    db.update_product(produto)
                return redirect(url_for("cadastrar_venda_lote") + "?sucesso=1")

    hoje = datetime.now().strftime("%Y-%m-%d")
    return render_template(
        "cadastrar_venda_lote.html",
        estoque=estoque,
        drinks=drinks,
        erro=erro,
        hoje=hoje,
        usuario=session.get("usuario", ""),
        tipo=session.get("tipo"),
        permissoes=session.get("permissoes", [])
    )


# ================== COMANDAS ==================
@app.route("/comandas")
def comandas():
    if not usuario_logado():
        return redirect(url_for("login"))
    if session.get("tipo") != "admin" and not tem_permissao("vendas"):
        return redirect(url_for("vendas"))

    hoje = datetime.now().strftime("%Y-%m-%d")
    todas = db.get_all_commands()

    comandas_exibir = [c for c in todas if isinstance(c, dict)]
    comandas_exibir.sort(
        key=lambda c: (c.get("data_abertura", "") + "T" + c.get("hora_abertura", "")),
        reverse=True
    )
    nomes_sugestoes = sorted(set(c.get("nome", "") for c in comandas_exibir if c.get("nome")))

    def total_comanda(c):
        return sum(
            to_float(i.get("valor_venda", 0)) * to_int(i.get("quantidade", 0))
            for i in c.get("itens", [])
        )

    abertas       = [c for c in todas if isinstance(c, dict) and c.get("status") == "aberta"]
    em_pagamento  = [c for c in todas if isinstance(c, dict) and c.get("status") == "em_pagamento"]
    fechadas_hoje = [c for c in todas if isinstance(c, dict) and c.get("status") == "fechada" and c.get("data_fechamento") == hoje]

    stat_abertas_count     = len(abertas)
    stat_abertas_total     = sum(total_comanda(c) for c in abertas)
    stat_em_pag_count      = len(em_pagamento)
    stat_em_pag_total      = sum(total_comanda(c) for c in em_pagamento)
    stat_fechadas_count    = len(fechadas_hoje)
    stat_fechadas_total    = sum(total_comanda(c) for c in fechadas_hoje)
    stat_faturamento_hoje  = stat_fechadas_total

    return render_template(
        "comandas.html",
        comandas=comandas_exibir,
        tipo=session.get("tipo"),
        permissoes=session.get("permissoes", []),
        usuario=session.get("usuario", "Administrador"),
        nomes_sugestoes=nomes_sugestoes,
        stat_abertas_count=stat_abertas_count,
        stat_abertas_total=round(stat_abertas_total, 2),
        stat_em_pag_count=stat_em_pag_count,
        stat_em_pag_total=round(stat_em_pag_total, 2),
        stat_fechadas_count=stat_fechadas_count,
        stat_fechadas_total=round(stat_fechadas_total, 2),
        stat_faturamento_hoje=round(stat_faturamento_hoje, 2),
    )


@app.route("/comandas/nova", methods=["POST"])
def comanda_nova():
    if not usuario_logado():
        return redirect(url_for("login"))
    if session.get("tipo") != "admin" and not tem_permissao("vendas"):
        return redirect(url_for("vendas"))

    nome = (request.form.get("nome") or "").strip()
    if not nome:
        return redirect(url_for("comandas"))

    nova = {
        "cid": uuid.uuid4().hex,
        "nome": nome,
        "data_abertura": datetime.now().strftime("%Y-%m-%d"),
        "hora_abertura": datetime.now().strftime("%H:%M"),
        "status": "aberta",
        "itens": []
    }
    db.insert_command(nova)
    return redirect(url_for("comandas"))


@app.route("/comandas/<cid>")
def comanda_detalhe(cid):
    if not usuario_logado():
        return redirect(url_for("login"))
    if session.get("tipo") != "admin" and not tem_permissao("vendas"):
        return redirect(url_for("vendas"))

    comanda = db.get_command(cid)
    if not comanda:
        return redirect(url_for("comandas"))

    estoque = db.get_all_products()
    estoque_disponivel = [p for p in estoque if isinstance(p, dict) and estoque_qtd_total(p) > 0]
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
        erro="",
        tipo=session.get("tipo"),
        permissoes=session.get("permissoes", []),
        usuario=session.get("usuario", "")
    )


@app.route("/comandas/<cid>/adicionar", methods=["POST"])
def comanda_adicionar(cid):
    if not usuario_logado():
        return redirect(url_for("login"))
    if session.get("tipo") != "admin" and not tem_permissao("vendas"):
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
                comanda["itens"].append({
                    "item_id": uuid.uuid4().hex,
                    "produto_pid": pid,
                    "produto_tipo": "bar",
                    "nome": bar_prod["name"],
                    "quantidade": qtd,
                    "valor_venda": to_float(bar_prod.get("sale_price", 0)),
                    "valor_investido": custo_unit,
                    "consumo_lotes": all_logs,
                    "bar_consumo_components": components_consumo,
                    "hora": datetime.now().strftime("%H:%M")
                })
                db.update_command(comanda)
            except ValueError as e:
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

                comanda["itens"].append({
                    "item_id": uuid.uuid4().hex,
                    "produto_pid": produto.get("pid"),
                    "produto_tipo": "estoque",
                    "vender_por_conteudo": vender_por_conteudo and cv > 1,
                    "nome": produto.get("nome", ""),
                    "quantidade": qtd,
                    "valor_venda": round(preco_unit, 4),
                    "valor_investido": round(custo_unit_medio, 4),
                    "consumo_lotes": consumo,
                    "hora": datetime.now().strftime("%H:%M")
                })

                db.update_command(comanda)
            except ValueError as e:
                erro = str(e)

    if erro:
        estoque = db.get_all_products()
        estoque_disponivel = [p for p in estoque if isinstance(p, dict) and estoque_qtd_total(p) > 0]
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
    if session.get("tipo") != "admin" and not tem_permissao("vendas"):
        return redirect(url_for("vendas"))

    comanda = db.get_command(cid)
    if not comanda or comanda.get("status") != "aberta":
        return redirect(url_for("comandas"))

    item = next((i for i in comanda.get("itens", []) if str(i.get("item_id")) == str(item_id)), None)
    if item:
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

    return redirect(url_for("comanda_detalhe", cid=cid))


@app.route("/comandas/<cid>/remover", methods=["POST"])
def comanda_remover(cid):
    if not usuario_logado():
        return redirect(url_for("login"))
    if session.get("tipo") != "admin" and not tem_permissao("vendas"):
        return redirect(url_for("vendas"))

    comanda = db.get_command(cid)
    if not comanda or comanda.get("status") not in ("aberta", "em_pagamento"):
        return redirect(url_for("comandas"))

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

    comanda["status"] = "cancelada"
    db.update_command(comanda)

    return redirect(url_for("comandas"))


@app.route("/comandas/<cid>/em_pagamento", methods=["POST"])
def comanda_em_pagamento(cid):
    if not usuario_logado():
        return redirect(url_for("login"))
    if session.get("tipo") != "admin" and not tem_permissao("vendas"):
        return redirect(url_for("vendas"))

    comanda = db.get_command(cid)
    if comanda and comanda.get("status") == "aberta":
        comanda["status"] = "em_pagamento"
        db.update_command(comanda)

    return redirect(url_for("comandas"))


@app.route("/comandas/<cid>/fechar", methods=["POST"])
def comanda_fechar(cid):
    if not usuario_logado():
        return redirect(url_for("login"))
    if session.get("tipo") != "admin" and not tem_permissao("vendas"):
        return redirect(url_for("vendas"))

    comanda = db.get_command(cid)
    if not comanda or comanda.get("status") not in ("aberta", "em_pagamento"):
        return redirect(url_for("comandas"))

    if not comanda.get("itens"):
        return redirect(url_for("comandas"))

    forma_pagamento = (request.form.get("forma_pagamento") or "cartao").strip()

    hoje = datetime.now().strftime("%Y-%m-%d")
    id_venda = f"V{int(time.time())}"

    for item in comanda["itens"]:
        db.insert_sale({
            "uid": uuid.uuid4().hex,
            "id_venda": id_venda,
            "comanda_id": comanda["cid"],
            "comanda_nome": comanda["nome"],
            "data": hoje,
            "produto_pid": item.get("produto_pid"),
            "produto_tipo": item.get("produto_tipo", "estoque"),
            "nome": item.get("nome", ""),
            "valor_venda": to_float(item.get("valor_venda", 0)),
            "valor_investido": to_float(item.get("valor_investido", 0)),
            "quantidade": to_int(item.get("quantidade", 0)),
            "forma_pagamento": forma_pagamento,
            "consumo_lotes": item.get("consumo_lotes", [])
        })

    comanda["status"] = "fechada"
    comanda["data_fechamento"] = hoje
    comanda["hora_fechamento"] = datetime.now().strftime("%H:%M")
    comanda["forma_pagamento"] = forma_pagamento

    db.update_command(comanda)
    return redirect(url_for("comandas"))


# ================== FECHAR CAIXA ==================
@app.route("/fechar_caixa", methods=["GET", "POST"])
def fechar_caixa():
    if not usuario_logado():
        return redirect(url_for("login"))

    if session.get("tipo") != "admin" and not tem_permissao("vendas"):
        return redirect(url_for("vendas"))

    if request.method == "GET":
        hoje = datetime.now().strftime("%Y-%m-%d")
        return render_template("fechar_caixa.html", hoje=hoje, erro="")

    dia_raw = (request.form.get("dia") or "").strip()
    if not dia_raw:
        hoje = datetime.now().strftime("%Y-%m-%d")
        return render_template("fechar_caixa.html", hoje=hoje, erro="Selecione uma data.")

    dia = normalizar_data_str(dia_raw)
    abertas_do_dia = db.get_sales_open_by_date(dia)

    pix = dinheiro = cartao = total = 0.0
    qtd_vendas = 0
    uids_fechados = []
    fechado_em = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

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
        "data_fechamento": datetime.now().strftime("%Y-%m-%d %H:%M"),
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

    if session.get("tipo") != "admin" and not tem_permissao("vendas"):
        return redirect(url_for("vendas"))

    dia_raw = (request.form.get("dia") or "").strip()
    if not dia_raw:
        hoje = datetime.now().strftime("%Y-%m-%d")
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

    if session.get("tipo") != "admin" and not tem_permissao("financeiro"):
        return redirect(url_for("vendas"))

    caixa = db.get_all_cash_registers()
    caixa = sorted(caixa, key=lambda c: str(c.get("dia", "")), reverse=True)
    return render_template("caixa_historico.html", caixa=caixa)


@app.route("/caixa/<dia>")
def caixa_relatorio(dia):
    if not usuario_logado():
        return redirect(url_for("login"))

    if session.get("tipo") != "admin" and not tem_permissao("financeiro"):
        return redirect(url_for("vendas"))

    rel = montar_relatorio_caixa_por_dia(dia)
    if not rel:
        return redirect(url_for("caixa_historico"))

    return render_template("caixa_relatorio.html", rel=rel)


@app.route("/caixa/<dia>/pdf")
def caixa_relatorio_pdf(dia):
    if not usuario_logado():
        return redirect(url_for("login"))

    if session.get("tipo") != "admin" and not tem_permissao("financeiro"):
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

    if session.get("tipo") != "admin" and not tem_permissao("financeiro"):
        return redirect(url_for("vendas"))

    data_ini_raw = (request.args.get("data_ini") or "").strip()
    data_fim_raw = (request.args.get("data_fim") or "").strip()

    if not request.args:
        hoje = date.today().strftime("%Y-%m-%d")
        data_ini_raw = hoje
        data_fim_raw = hoje

    data_de = parse_date_input(data_ini_raw)
    data_ate = parse_date_input(data_fim_raw)

    vendas = db.get_all_sales()

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

    total_vendido = 0.0
    total_investido = 0.0

    for v in vendas_filtradas:
        qtd = to_int(v.get("quantidade", 0))
        venda_unit = to_float(v.get("valor_venda", 0))
        inv_unit = to_float(v.get("valor_investido", 0))
        total_vendido += venda_unit * qtd
        total_investido += inv_unit * qtd

    lucro_total = round(total_vendido - total_investido, 2)

    # Top produtos por lucro
    produto_stats = defaultdict(lambda: {"qtd": 0, "lucro": 0.0})
    for v in vendas_filtradas:
        nome = v.get("nome", "").strip() or "Sem nome"
        qtd = to_int(v.get("quantidade", 0))
        lucro_unit = to_float(v.get("valor_venda", 0)) - to_float(v.get("valor_investido", 0))
        produto_stats[nome]["qtd"] += qtd
        produto_stats[nome]["lucro"] += lucro_unit * qtd
    top_produtos = sorted(
        [{"nome": k, "quantidade": v["qtd"], "lucro": round(v["lucro"], 2)}
         for k, v in produto_stats.items()],
        key=lambda x: x["lucro"], reverse=True
    )[:5]

    # Pagamentos por forma
    pag_totais = defaultdict(float)
    for v in vendas_filtradas:
        forma = v.get("forma_pagamento", "Outros").strip() or "Outros"
        pag_totais[forma] += to_float(v.get("valor_venda", 0)) * to_int(v.get("quantidade", 0))
    pagamentos = []
    for forma, total in sorted(pag_totais.items(), key=lambda x: x[1], reverse=True):
        pct = round(total / total_vendido * 100) if total_vendido > 0 else 0
        pagamentos.append({"forma": forma, "total": round(total, 2), "pct": pct})

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
        total_vendido=round(total_vendido, 2),
        total_investido=round(total_investido, 2),
        lucro_total=lucro_total,
        num_vendas=len(vendas_filtradas),
        data_ini_raw=data_ini_raw,
        data_fim_raw=data_fim_raw,
        data_ini_br=data_ini_br,
        data_fim_br=data_fim_br,
        top_produtos=top_produtos,
        pagamentos=pagamentos,
        nomes_sugestoes=nomes_sugestoes,
        usuario=session.get("usuario", ""),
        tipo=session.get("tipo"),
        permissoes=session.get("permissoes", []),
    )


# ================== BAR ==================

def bar_auth():
    if not usuario_logado():
        return redirect(url_for("login"))
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
            total_cost += cost
            all_logs.extend(log)
        return round(total_cost, 4), all_logs
    else:
        return bar_fifo_consume(product_id, quantity)


# ── Produtos (drinks) ──

@app.route("/bar/produtos")
def bar_produtos():
    redir = bar_auth()
    if redir: return redir

    produtos = db.bar_get_all_products()
    stock = db.bar_get_stock_summary()
    return render_template("bar_produtos.html",
        produtos=produtos, stock=stock,
        tipo=session.get("tipo"), usuario=session.get("usuario", ""), permissoes=session.get("permissoes", []))


@app.route("/bar/produtos/novo", methods=["GET", "POST"])
def bar_produto_novo():
    redir = bar_auth()
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
    redir = bar_auth()
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
    redir = bar_auth()
    if redir: return redir
    db.bar_delete_product(pid)
    return redirect(url_for("bar_produtos"))


# ── Lotes (pacotes) ──

@app.route("/bar/produtos/<pid>/lotes", methods=["GET", "POST"])
def bar_lotes(pid):
    redir = bar_auth()
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

    if request.method == "POST":
        usuario_alvo = (request.form.get("usuario_alvo") or "").strip()
        permissoes_marcadas = request.form.getlist("permissoes")
        permissoes_marcadas = [p for p in permissoes_marcadas if p in PERMISSOES_TODAS]

        if usuario_alvo in usuarios and normalizar_tipo(usuarios[usuario_alvo].get("tipo")) != "admin":
            db.update_user_permissions(usuario_alvo, permissoes_marcadas)

        return redirect(url_for("permissoes"))

    return render_template(
        "permissoes.html",
        usuarios=usuarios,
        permissoes_disponiveis=PERMISSOES_TODAS,
        usuario=session.get("usuario", ""),
        tipo=session.get("tipo"),
        permissoes=session.get("permissoes", [])
    )


# ================== RUN ==================
if __name__ == "__main__":
    app.run(debug=True, port=5003)
