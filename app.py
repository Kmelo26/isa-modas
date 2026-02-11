from flask import Flask, render_template, request, redirect, session, url_for, send_file
import json
import os
import time
import uuid
from json import JSONDecodeError
from datetime import datetime, date
from io import BytesIO

from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas

app = Flask(__name__)
app.secret_key = "isa_modas_secreto"


# ================== FILTRO DATA BR (JINJA) ==================
def br_date(value):
    """
    Aceita: '2026-02-05', '2026/02/05', datetime/date, None
    Retorna: '05/02/2026' ou '' se vazio
    """
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


# ================== ARQUIVOS ==================
ARQUIVO_ESTOQUE = "estoque.json"
ARQUIVO_VENDAS = "vendas.json"
ARQUIVO_USUARIOS = "usuarios.json"
ARQUIVO_CAIXA = "caixa.json"


# ================== HELPERS JSON ==================
def carregar_json(arquivo):
    if not os.path.exists(arquivo):
        return {} if arquivo == ARQUIVO_USUARIOS else []
    try:
        with open(arquivo, "r", encoding="utf-8") as f:
            conteudo = f.read().strip()
            if not conteudo:
                return {} if arquivo == ARQUIVO_USUARIOS else []
            return json.loads(conteudo)
    except (JSONDecodeError, OSError):
        return {} if arquivo == ARQUIVO_USUARIOS else []


def salvar_json(arquivo, dados):
    with open(arquivo, "w", encoding="utf-8") as f:
        json.dump(dados, f, ensure_ascii=False, indent=4)


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


# ================== MIGRAÇÕES / CONSISTÊNCIA ==================
def garantir_uid_vendas():
    vendas = carregar_json(ARQUIVO_VENDAS)
    if not isinstance(vendas, list):
        return
    alterou = False
    for v in vendas:
        if isinstance(v, dict) and not v.get("uid"):
            v["uid"] = uuid.uuid4().hex
            alterou = True
    if alterou:
        salvar_json(ARQUIVO_VENDAS, vendas)


def migrar_vendas_remover_codigo():
    vendas = carregar_json(ARQUIVO_VENDAS)
    if not isinstance(vendas, list):
        vendas = []

    alterou = False
    for v in vendas:
        if not isinstance(v, dict):
            continue

        if not v.get("uid"):
            v["uid"] = uuid.uuid4().hex
            alterou = True

        if "codigo" in v:
            v.pop("codigo", None)
            alterou = True

        v["nome"] = str(v.get("nome", "")).strip()
        v["data"] = str(v.get("data", "")).strip()
        v["quantidade"] = to_int(v.get("quantidade", 0))
        v["valor_venda"] = to_float(v.get("valor_venda", 0))
        v["valor_investido"] = to_float(v.get("valor_investido", 0))
        v["forma_pagamento"] = str(v.get("forma_pagamento", "")).strip()

        if "consumo_lotes" in v and not isinstance(v.get("consumo_lotes"), list):
            v["consumo_lotes"] = []
            alterou = True

    if alterou:
        salvar_json(ARQUIVO_VENDAS, vendas)


# ================== FIFO / LOTES ==================
def estoque_qtd_total(produto):
    lotes = produto.get("lotes", [])
    if not isinstance(lotes, list):
        return 0
    return sum(to_int(l.get("qtd", 0)) for l in lotes)


def estoque_valor_total(produto):
    lotes = produto.get("lotes", [])
    if not isinstance(lotes, list):
        return 0.0
    return sum(to_int(l.get("qtd", 0)) * to_float(l.get("custo_unit", 0)) for l in lotes)


def atualizar_campos_derivados_estoque(produto):
    qtd = estoque_qtd_total(produto)
    valor = estoque_valor_total(produto)
    produto["quantidade"] = qtd
    produto["valor_investido"] = round((valor / qtd), 4) if qtd > 0 else 0.0


def garantir_lotes_fifo_estoque():
    estoque = carregar_json(ARQUIVO_ESTOQUE)
    if not isinstance(estoque, list):
        estoque = []

    alterou = False

    for p in estoque:
        if not isinstance(p, dict):
            continue

        if "codigo" in p:
            p.pop("codigo", None)
            alterou = True

        if not p.get("pid"):
            p["pid"] = uuid.uuid4().hex
            alterou = True

        p["nome"] = str(p.get("nome", "")).strip()
        if not p["nome"]:
            p["nome"] = "SEM NOME"
            alterou = True

        if "data" not in p:
            p["data"] = ""
            alterou = True

        p["valor_venda"] = to_float(p.get("valor_venda", 0))

        if "lotes" not in p or not isinstance(p.get("lotes"), list):
            qtd_antiga = to_int(p.get("quantidade", 0))
            custo_antigo = to_float(p.get("valor_investido", 0))
            data_lote = normalizar_data_str(p.get("data"))

            p["lotes"] = []
            if qtd_antiga > 0:
                p["lotes"].append({
                    "data": data_lote,
                    "qtd": qtd_antiga,
                    "custo_unit": custo_antigo
                })
            alterou = True

        lotes_ok = []
        for l in p.get("lotes", []):
            if not isinstance(l, dict):
                continue
            qtd = to_int(l.get("qtd", 0))
            if qtd <= 0:
                continue
            lotes_ok.append({
                "data": normalizar_data_str(l.get("data")),
                "qtd": qtd,
                "custo_unit": to_float(l.get("custo_unit", 0))
            })

        if lotes_ok != p.get("lotes"):
            p["lotes"] = lotes_ok
            alterou = True

        antes_qtd = p.get("quantidade")
        antes_inv = p.get("valor_investido")
        atualizar_campos_derivados_estoque(p)
        if antes_qtd != p.get("quantidade") or antes_inv != p.get("valor_investido"):
            alterou = True

    if alterou:
        salvar_json(ARQUIVO_ESTOQUE, estoque)


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

    estoque_total = sum(to_int(l.get("qtd", 0)) for l in lotes)
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
        consumo.append({
            "data_lote": data_lote,
            "qtd": usar,
            "custo_unit": custo_unit
        })

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


# ================== CAIXA ==================
def montar_relatorio_caixa_por_dia(dia):
    caixa = carregar_json(ARQUIVO_CAIXA)
    vendas = carregar_json(ARQUIVO_VENDAS)

    if not isinstance(caixa, list):
        caixa = []
    if not isinstance(vendas, list):
        vendas = []

    registro = next((c for c in reversed(caixa) if str(c.get("dia", "")) == str(dia)), None)
    if not registro:
        return None

    uids = registro.get("vendas_uids", [])

    if not uids:
        vendas_dia = [v for v in vendas if str(v.get("caixa_dia", "")) == str(dia) and v.get("fechado")]
    else:
        uids_set = set(uids)
        vendas_dia = [v for v in vendas if v.get("uid") in uids_set]

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
    vendas = carregar_json(ARQUIVO_VENDAS)
    if not isinstance(vendas, list):
        vendas = []

    dia = normalizar_data_str(dia)

    vendas_dia = [
        v for v in vendas
        if isinstance(v, dict)
        and normalizar_data_str(v.get("data", "")) == dia
        and not v.get("fechado")
    ]

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


# ================== USUÁRIOS INICIAIS ==================
if not os.path.exists(ARQUIVO_USUARIOS):
    salvar_json(ARQUIVO_USUARIOS, {
        "admin": {"senha": "2803", "tipo": "admin", "permissoes": []},
        "isa": {"senha": "isa2026", "tipo": "funcionario", "permissoes": ["vendas"]}
    })


# ================== ATUALIZA SESSÃO + MIGRAÇÕES ==================
@app.before_request
def atualizar_permissoes_da_sessao():
    # Mantém seu comportamento original (migrar sempre)
    garantir_lotes_fifo_estoque()
    migrar_vendas_remover_codigo()

    if "usuario" not in session:
        return

    usuario = session.get("usuario", "")
    usuarios = carregar_json(ARQUIVO_USUARIOS)

    if usuario not in usuarios:
        session.clear()
        return

    tipo_atual = normalizar_tipo(usuarios[usuario].get("tipo", "funcionario"))

    if usuario == "admin" or tipo_atual == "admin":
        session["tipo"] = "admin"
        session["permissoes"] = PERMISSOES_TODAS[:]
        return

    session["tipo"] = tipo_atual
    permissoes = usuarios[usuario].get("permissoes", [])
    if not isinstance(permissoes, list):
        permissoes = []
    permissoes = [p for p in permissoes if p in PERMISSOES_TODAS]
    session["permissoes"] = permissoes


# ================== LOGIN ==================
@app.route("/login", methods=["GET", "POST"])
def login():
    usuarios = carregar_json(ARQUIVO_USUARIOS)

    if request.method == "POST":
        usuario = request.form.get("usuario", "").strip()
        senha = request.form.get("senha", "").strip()

        if usuario in usuarios and usuarios[usuario].get("senha") == senha:
            session.clear()
            session["usuario"] = usuario

            tipo = normalizar_tipo(usuarios[usuario].get("tipo", "funcionario"))

            if usuario == "admin" or tipo == "admin":
                session["tipo"] = "admin"
                session["permissoes"] = PERMISSOES_TODAS[:]
            else:
                session["tipo"] = tipo
                perms = usuarios[usuario].get("permissoes", [])
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

    garantir_uid_vendas()
    vendas_data = carregar_json(ARQUIVO_VENDAS)
    if not isinstance(vendas_data, list):
        vendas_data = []

    data_ini_raw = request.args.get("data_ini", "").strip()
    data_fim_raw = request.args.get("data_fim", "").strip()
    busca_produto_raw = request.args.get("busca_produto", "").strip()

    data_ini = parse_date_yyyy_mm_dd(data_ini_raw)
    data_fim = parse_date_yyyy_mm_dd(data_fim_raw)
    busca_produto = busca_produto_raw.lower()

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

        if ok:
            vendas_filtradas.append(v)

    def chave_data(v):
        d = parse_date_yyyy_mm_dd(v.get("data", ""))
        return d or datetime.min.date()

    vendas_filtradas.sort(key=chave_data, reverse=True)

    # ===== PAGINAÇÃO (20 por página) - só o necessário =====
    try:
        pagina = int(request.args.get("pagina", "1"))
    except ValueError:
        pagina = 1
    if pagina < 1:
        pagina = 1

    por_pagina = 20
    total = len(vendas_filtradas)
    total_paginas = (total + por_pagina - 1) // por_pagina

    if total_paginas >= 1 and pagina > total_paginas:
        pagina = total_paginas

    inicio = (pagina - 1) * por_pagina
    fim = inicio + por_pagina
    vendas_pagina = vendas_filtradas[inicio:fim]
    # ================================================

    return render_template(
        "vendas.html",
        vendas=vendas_pagina,
        usuario=session.get("usuario", ""),
        tipo=session.get("tipo"),
        permissoes=session.get("permissoes", []),
        data_ini_raw=data_ini_raw,
        data_fim_raw=data_fim_raw,
        busca_produto=busca_produto_raw,

        # só para o template desenhar a paginação
        pagina=pagina,
        total_paginas=total_paginas
    )


# ================== EDITAR VENDA (CORRIGIDO + FIFO) ==================
@app.route("/vendas/editar/<uid>", methods=["GET", "POST"])
def editar_venda(uid):
    if not usuario_logado():
        return redirect(url_for("login"))

    if session.get("tipo") != "admin" and not tem_permissao("vendas"):
        return redirect(url_for("vendas"))

    vendas = carregar_json(ARQUIVO_VENDAS)
    if not isinstance(vendas, list):
        vendas = []

    estoque = carregar_json(ARQUIVO_ESTOQUE)
    if not isinstance(estoque, list):
        estoque = []

    venda = next((v for v in vendas if isinstance(v, dict) and str(v.get("uid")) == str(uid)), None)
    if not venda:
        return redirect(url_for("vendas"))

    # Não deixa alterar venda já fechada (evita bagunçar caixa)
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

        # manter forma de pagamento caso venha vazio (opção "manter como está")
        fp_form = (request.form.get("forma_pagamento") or "").strip()
        if fp_form:
            nova_forma_pagamento = normalizar_pagamento(fp_form)
        else:
            nova_forma_pagamento = venda.get("forma_pagamento", "cartao")

        novo_pid = (request.form.get("produto_pid") or "").strip() or str(venda.get("produto_pid", "")).strip()
        nova_qtd = to_int(request.form.get("quantidade", venda.get("quantidade", 1)), 0)

        if nova_qtd <= 0:
            erro = "Quantidade inválida."

        produto_novo = next((p for p in estoque if str(p.get("pid", "")) == str(novo_pid)), None)
        if not erro and not produto_novo:
            erro = "Produto não encontrado no estoque."

        if not erro:
            # devolve consumo antigo no produto antigo
            pid_antigo = str(venda.get("produto_pid", "")).strip()
            produto_antigo = next((p for p in estoque if str(p.get("pid", "")) == pid_antigo), None)
            consumo_antigo = venda.get("consumo_lotes", [])

            if produto_antigo and consumo_antigo:
                devolver_fifo(produto_antigo, consumo_antigo)

            # consome novamente no produto novo
            try:
                custo_total, consumo_novo = consumir_fifo(produto_novo, nova_qtd)
            except ValueError as e:
                # se falhar, tenta reverter (melhor esforço)
                if produto_antigo and consumo_antigo:
                    try:
                        consumir_fifo(produto_antigo, sum(to_int(c.get("qtd", 0)) for c in consumo_antigo))
                    except Exception:
                        pass
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

            salvar_json(ARQUIVO_VENDAS, vendas)
            salvar_json(ARQUIVO_ESTOQUE, estoque)
            return redirect(url_for("vendas"))

    return render_template(
        "editar_vendas.html",
        venda=venda,
        estoque=estoque,
        erro=erro,
        tipo=session.get("tipo"),
        permissoes=session.get("permissoes", [])
    )

##############-ESTOQUE-######################
@app.route("/estoque", methods=["GET"])
def estoque():
    if not usuario_logado():
        return redirect(url_for("login"))

    if not tem_permissao("estoque"):
        return redirect(url_for("vendas"))

    estoque_data = carregar_json(ARQUIVO_ESTOQUE)
    if not isinstance(estoque_data, list):
        estoque_data = []

    # ordenar
    estoque_data.sort(key=lambda p: str(p.get("nome", "")).strip().lower())

    # ===== FILTRO (PESQUISA) =====
    q = (request.args.get("q", "") or "").strip()
    if q:
        q_low = q.lower()
        estoque_filtrado = [
            p for p in estoque_data
            if q_low in str(p.get("nome", "")).lower()
        ]
    else:
        estoque_filtrado = estoque_data
    # =============================

    # ===== PAGINAÇÃO (20 por página) =====
    try:
        pagina = int(request.args.get("pagina", "1"))
    except ValueError:
        pagina = 1
    if pagina < 1:
        pagina = 1

    por_pagina = 20
    total = len(estoque_filtrado)
    total_paginas = (total + por_pagina - 1) // por_pagina

    if total_paginas >= 1 and pagina > total_paginas:
        pagina = total_paginas

    inicio = (pagina - 1) * por_pagina
    fim = inicio + por_pagina
    estoque_pagina = estoque_filtrado[inicio:fim]
    # ====================================

    return render_template(
        "estoque.html",
        estoque=estoque_pagina,
        estoque_lista=estoque_filtrado,  # para o select (somente do filtro atual)
        q=q,
        erro="",
        tipo=session.get("tipo"),
        permissoes=session.get("permissoes", []),
        pagina=pagina,
        total_paginas=total_paginas
    )

######### CADASTRAR PRODUTO ##########

@app.route("/estoque/novo", methods=["GET", "POST"])
def estoque_novo():
    if not usuario_logado():
        return redirect(url_for("login"))

    if not tem_permissao("estoque"):
        return redirect(url_for("vendas"))

    estoque_data = carregar_json(ARQUIVO_ESTOQUE)
    if not isinstance(estoque_data, list):
        estoque_data = []

    erro = ""

    if request.method == "POST":
        nome = (request.form.get("nome", "") or "").strip()
        data = (request.form.get("data", "") or "").strip()
        valor_investido = to_float(request.form.get("valor_investido", "0"))
        valor_venda = to_float(request.form.get("valor_venda", "0"))
        quantidade = to_int(request.form.get("quantidade", "0"))

        if not nome:
            erro = "Informe o nome do produto."
        elif quantidade <= 0:
            erro = "Quantidade inválida."
        else:
            nome_key = nome.lower()
            existente = next((p for p in estoque_data if str(p.get("nome", "")).strip().lower() == nome_key), None)

            if existente:
                existente["data"] = data
                existente["valor_venda"] = valor_venda
                try:
                    entrada_estoque_lote(existente, data, quantidade, valor_investido)
                except ValueError as e:
                    erro = str(e)
            else:
                novo = {
                    "pid": uuid.uuid4().hex,
                    "data": data,
                    "nome": nome,
                    "valor_venda": valor_venda,
                    "lotes": []
                }
                try:
                    entrada_estoque_lote(novo, data, quantidade, valor_investido)
                except ValueError as e:
                    erro = str(e)

                if not erro:
                    estoque_data.append(novo)

            if not erro:
                salvar_json(ARQUIVO_ESTOQUE, estoque_data)
                return redirect(url_for("estoque"))

    return render_template(
        "estoque_novo.html",
        erro=erro,
        tipo=session.get("tipo"),
        permissoes=session.get("permissoes", []),
    )

##### EDITAR ESTOQUE #####

@app.route("/estoque/editar/<pid>", methods=["GET", "POST"])
def editar_produto(pid):
    if not usuario_logado():
        return redirect(url_for("login"))

    if not tem_permissao("estoque"):
        return redirect(url_for("vendas"))

    estoque_data = carregar_json(ARQUIVO_ESTOQUE)
    if not isinstance(estoque_data, list):
        estoque_data = []

    produto = next((p for p in estoque_data if str(p.get("pid", "")) == str(pid)), None)
    if not produto:
        return redirect(url_for("estoque"))

    erro = ""

    if request.method == "POST":
        nome = (request.form.get("nome") or "").strip()
        data = (request.form.get("data") or "").strip()
        valor_venda = to_float(request.form.get("valor_venda"), 0.0)

        if not nome:
            erro = "Informe o nome do produto."
        elif valor_venda <= 0:
            erro = "Preço de venda inválido."
        else:
            produto["nome"] = nome
            produto["data"] = data
            produto["valor_venda"] = valor_venda

            # mantém campos derivados consistentes (quantidade e valor investido médio)
            atualizar_campos_derivados_estoque(produto)

            salvar_json(ARQUIVO_ESTOQUE, estoque_data)
            return redirect(url_for("estoque"))

    return render_template(
        "editar_produto.html",
        produto=produto,
        erro=erro,
        tipo=session.get("tipo"),
        permissoes=session.get("permissoes", [])
    )


# Alias: se algum template antigo chamar url_for('editar_produto', pid=...)
# e você quiser linkar também por /editar_produto/<pid> sem quebrar.
@app.route("/editar_produto/<pid>")
def editar_produto_alias(pid):
    return redirect(url_for("editar_produto", pid=pid))


# ================== VER LOTES DO PRODUTO ==================
@app.route("/estoque/produto/<pid>", methods=["GET", "POST"])
def estoque_produto(pid):
    if not usuario_logado():
        return redirect(url_for("login"))
    if not tem_permissao("estoque"):
        return redirect(url_for("vendas"))

    estoque_data = carregar_json(ARQUIVO_ESTOQUE)
    if not isinstance(estoque_data, list):
        estoque_data = []

    produto = next((p for p in estoque_data if str(p.get("pid", "")) == str(pid)), None)
    if not produto:
        return redirect(url_for("estoque"))

    erro = ""

    if request.method == "POST":
        data = (request.form.get("data", "") or "").strip()
        qtd = to_int(request.form.get("quantidade", "0"), 0)
        custo_unit = to_float(request.form.get("custo_unit", "0"), 0.0)

        if qtd <= 0:
            erro = "Quantidade inválida."
        elif custo_unit <= 0:
            erro = "Custo unitário inválido."
        else:
            try:
                entrada_estoque_lote(produto, data, qtd, custo_unit)
                salvar_json(ARQUIVO_ESTOQUE, estoque_data)
                return redirect(url_for("estoque_produto", pid=pid))
            except ValueError as e:
                erro = str(e)

    lotes = produto.get("lotes", [])
    if isinstance(lotes, list):
        lotes = sorted(lotes, key=lambda l: str(l.get("data", "")))
    else:
        lotes = []

    return render_template(
        "estoque_produto.html",
        produto=produto,
        lotes=lotes,
        erro=erro,
        tipo=session.get("tipo"),
        permissoes=session.get("permissoes", [])
    )


# ================== CADASTRAR VENDA ==================
@app.route("/cadastrar_venda", methods=["GET", "POST"])
def cadastrar_venda():
    if not usuario_logado():
        return redirect(url_for("login"))

    if not tem_permissao("vendas"):
        return redirect(url_for("vendas"))

    estoque = carregar_json(ARQUIVO_ESTOQUE)
    if not isinstance(estoque, list):
        estoque = []

    vendas = carregar_json(ARQUIVO_VENDAS)
    if not isinstance(vendas, list):
        vendas = []

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
            for produto, qtd in itens:
                try:
                    custo_total, consumo = consumir_fifo(produto, qtd)
                except ValueError as e:
                    erro = str(e)
                    break

                custo_unit_medio = (custo_total / qtd) if qtd else 0.0

                vendas.append({
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

            if not erro:
                salvar_json(ARQUIVO_VENDAS, vendas)
                salvar_json(ARQUIVO_ESTOQUE, estoque)
                return redirect(url_for("vendas"))

    hoje = datetime.now().strftime("%Y-%m-%d")
    return render_template(
        "cadastrar_venda.html",
        estoque=estoque,
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

    estoque = carregar_json(ARQUIVO_ESTOQUE)
    if not isinstance(estoque, list):
        estoque = []

    vendas = carregar_json(ARQUIVO_VENDAS)
    if not isinstance(vendas, list):
        vendas = []

    erro = ""

    if request.method == "POST":
        data = request.form.get("data", "")
        forma_pagamento = request.form.get("forma_pagamento", "")

        pids = request.form.getlist("produto_pid[]")
        quantidades = request.form.getlist("quantidade[]")

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
                for produto, qtd in itens:
                    try:
                        custo_total, consumo = consumir_fifo(produto, qtd)
                    except ValueError as e:
                        erro = str(e)
                        break

                    custo_unit_medio = (custo_total / qtd) if qtd else 0.0

                    vendas.append({
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

            if not erro:
                salvar_json(ARQUIVO_VENDAS, vendas)
                salvar_json(ARQUIVO_ESTOQUE, estoque)
                return redirect(url_for("vendas"))

    return render_template(
        "cadastrar_venda_lote.html",
        estoque=estoque,
        erro=erro,
        usuario=session.get("usuario", ""),
        tipo=session.get("tipo"),
        permissoes=session.get("permissoes", [])
    )


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

    vendas = carregar_json(ARQUIVO_VENDAS)
    if not isinstance(vendas, list):
        vendas = []

    caixa = carregar_json(ARQUIVO_CAIXA)
    if not isinstance(caixa, list):
        caixa = []

    abertas_do_dia = []
    for v in vendas:
        if not isinstance(v, dict):
            continue
        data_v = normalizar_data_str(v.get("data", ""))
        if data_v == dia and not v.get("fechado"):
            abertas_do_dia.append(v)

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

        v["fechado"] = True
        v["fechado_em"] = fechado_em
        v["caixa_dia"] = dia

        if not v.get("uid"):
            v["uid"] = uuid.uuid4().hex

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

    caixa.append(registro)
    salvar_json(ARQUIVO_VENDAS, vendas)
    salvar_json(ARQUIVO_CAIXA, caixa)

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

    caixa = carregar_json(ARQUIVO_CAIXA)
    if not isinstance(caixa, list):
        caixa = []

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

    data_de = parse_date_input(data_ini_raw)
    data_ate = parse_date_input(data_fim_raw)

    vendas = carregar_json(ARQUIVO_VENDAS)
    if not isinstance(vendas, list):
        vendas = []

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

    return render_template(
        "financeiro.html",
        vendas=vendas_filtradas,
        total_vendido=round(total_vendido, 2),
        total_investido=round(total_investido, 2),
        num_vendas=len(vendas_filtradas),
        data_ini_raw=data_ini_raw,
        data_fim_raw=data_fim_raw,
        usuario=session.get("usuario", ""),
        tipo=session.get("tipo"),
        permissoes=session.get("permissoes", [])
    )


# ================== PERMISSÕES ==================
@app.route("/permissoes", methods=["GET", "POST"])
def permissoes():
    if not usuario_logado():
        return redirect(url_for("login"))

    if session.get("tipo") != "admin":
        return redirect(url_for("vendas"))

    usuarios = carregar_json(ARQUIVO_USUARIOS)
    if not isinstance(usuarios, dict):
        usuarios = {}

    if request.method == "POST":
        usuario_alvo = (request.form.get("usuario_alvo") or "").strip()
        permissoes_marcadas = request.form.getlist("permissoes")
        permissoes_marcadas = [p for p in permissoes_marcadas if p in PERMISSOES_TODAS]

        if usuario_alvo in usuarios and normalizar_tipo(usuarios[usuario_alvo].get("tipo")) != "admin":
            usuarios[usuario_alvo]["permissoes"] = permissoes_marcadas
            salvar_json(ARQUIVO_USUARIOS, usuarios)

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
