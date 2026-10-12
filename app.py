from flask import Flask, render_template, request, redirect, session, url_for, send_file, jsonify
import os
import time
import uuid
import json
import copy
import calendar
from concurrent.futures import ThreadPoolExecutor
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


def qtd_br(valor, casas: int = 3) -> str:
    """Quantidade no formato brasileiro, sem zeros sobrando: 12 / 1,5 / 0,75."""
    try:
        v = round(float(valor or 0), casas)
    except (TypeError, ValueError):
        return "—"
    txt = f"{v:.{casas}f}".rstrip("0").rstrip(".")
    return (txt if txt not in ("", "-0") else "0").replace(".", ",")


app.jinja_env.filters["qtd_br"] = qtd_br


_NOMES_FORMA_PAGAMENTO = {
    "pix": "Pix", "dinheiro": "Dinheiro", "cartao": "Cartão", "credito": "Cartão",
    "debito": "Débito", "fiado": "Faturado", "assumido": "Assumido (prejuízo)",
}


def forma_pagamento_label(value):
    """Nome de exibição da forma de pagamento. O valor gravado continua
    "fiado"; na tela aparece "Faturado"."""
    v = str(value or "").strip()
    if not v:
        return "—"
    return _NOMES_FORMA_PAGAMENTO.get(v.lower(), v.capitalize())


app.jinja_env.filters["forma_pagamento"] = forma_pagamento_label


# Compacta (gzip) páginas e JSON antes de mandar pro navegador — o HTML das
# telas tem 100–400 KB e cai pra ~1/8, o que pesa muito em celular/4G.
import gzip as _gzip


@app.after_request
def compactar_resposta(resposta):
    try:
        if (resposta.status_code != 200 or resposta.direct_passthrough
                or "gzip" not in (request.headers.get("Accept-Encoding") or "").lower()
                or resposta.headers.get("Content-Encoding")
                or resposta.mimetype not in ("text/html", "application/json", "text/css",
                                             "application/javascript", "text/javascript")):
            return resposta
        dados = resposta.get_data()
        if len(dados) < 1500:
            return resposta
        resposta.set_data(_gzip.compress(dados, compresslevel=5))
        resposta.headers["Content-Encoding"] = "gzip"
        resposta.headers["Content-Length"] = str(len(resposta.get_data()))
        resposta.headers.add("Vary", "Accept-Encoding")
    except Exception:
        pass
    return resposta


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
    "aplicar_desconto", "editar_desconto", "ver_valor_comandas", "alterar_preco_venda_realizada",
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
    "ver_vendas_por_usuario", "ver_lotes_periodo",
    # Fornecedores
    "ver_fornecedores", "gerenciar_fornecedores",
    # Críticas
    "excluir_vendas", "alterar_preco_venda", "alterar_custo_produto", "assumir_prejuizo_comanda",
    "gerenciar_usuarios", "alterar_permissoes",
    "ver_vendas_canceladas", "editar_pagamento_comanda",
    # Investidores
    "ver_investidores", "editar_investidores",
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
    if "assumid" in f:
        return "assumido"  # restante de comanda assumido pela casa — não é dinheiro que entrou
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


def _distribuir_pagamentos(total_venda, valor_dinheiro, valor_pix, valor_cartao):
    """Divide o total da venda entre as formas informadas, na ordem
    dinheiro/pix/cartao, nunca somando mais do que o total (protege contra
    troco em dinheiro ou valores adulterados nos campos ocultos)."""
    restante = round(total_venda, 2)
    pagamentos = []
    for forma, valor in (("dinheiro", valor_dinheiro), ("pix", valor_pix), ("cartao", valor_cartao)):
        if valor <= 0 or restante <= 0:
            continue
        usado = round(min(valor, restante), 2)
        pagamentos.append({"forma": forma, "valor": usado})
        restante = round(restante - usado, 2)
    return pagamentos


# ================== FIFO / LOTES ==================
SEM_USUARIO = "__sem__"  # chave do filtro de usuário do Financeiro p/ vendas sem usuário gravado
LOCAL_PADRAO = "loja"
# Lote com saldo abaixo disso é resto de arredondamento (venda por dose/ml)
# e conta como zerado — senão fica no produto pra sempre (ex.: 0,000003).
RESTO_LOTE_MINIMO = 0.001  # local assumido pra lotes/vendas antigos, sem local_id gravado


def preco_por_local(item: dict, local_id: str | None, campo: str = "valor_venda") -> float:
    """Preço de venda de um produto (campo "valor_venda") ou pacote ("sale_price")
    no local informado: o preço próprio do local, se houver; senão, o padrão."""
    precos = (item or {}).get("precos_local") or {}
    if isinstance(precos, dict) and local_id:
        preco_local = to_float(precos.get(local_id), 0.0)
        if preco_local > 0:
            return preco_local
    return to_float((item or {}).get(campo, 0))


app.jinja_env.globals["preco_por_local"] = preco_por_local
app.jinja_env.globals["qtd_no_local"] = lambda produto, local_id: _qtd_por_local(produto).get(local_id, 0.0)


MSG_SQL_PRECOS_LOCAL = ("Para salvar o preço do Escritório, falta rodar o arquivo schema_precos_local.sql "
                        "no SQL Editor do Supabase. O produto não foi salvo.")


def ler_precos_local_form(form, preco_padrao: float, permitidos=None) -> dict:
    """Lê os campos preco_local_<id> (locais que não são o padrão, ex.:
    Escritório). Guarda só preço > 0 e diferente do padrão (vazio = usa o
    padrão). permitidos: se informado, só aceita esses locais."""
    precos = {}
    for l in db.get_all_locais_estoque():
        if l["id"] == LOCAL_PADRAO or (permitidos is not None and l["id"] not in permitidos):
            continue
        v = to_float(form.get(f"preco_local_{l['id']}"), 0.0)
        if v > 0 and abs(v - preco_padrao) > 0.004:
            precos[l["id"]] = round(v, 2)
    return precos


def locais_com_lote(produto: dict) -> set:
    """Locais em que o produto tem (ou teve) lote registrado."""
    return {loc for l in (produto or {}).get("lotes") or [] for loc in _dist_lote(l)}


def _dist_lote(lote: dict) -> dict:
    """Distribuição do lote por local de estoque. Lotes que ainda não têm
    esse campo (cadastrados antes dessa feature) são tratados como 100% no
    local padrão — não precisa de migração pra continuar funcionando."""
    dist = lote.get("distribuicao")
    if isinstance(dist, dict) and dist:
        return dist
    return {LOCAL_PADRAO: to_float(lote.get("qtd", 0))}


def _ajustar_dist_para_qtd(lote: dict) -> bool:
    """Faz o saldo por local do lote somar exatamente a quantidade do lote.
    Sobra: tira primeiro do local principal (de onde saíam as vendas sem
    local e de pacote), depois dos outros, sem deixar negativo. Falta: põe
    no local com maior saldo (ou no principal). Devolve True se mudou."""
    qtd = round(to_float(lote.get("qtd", 0)), 6)
    dist = {k: round(to_float(v), 6) for k, v in _dist_lote(lote).items()}
    diferenca = round(sum(dist.values()) - qtd, 6)
    if abs(diferenca) <= 0.0001:
        return False
    if diferenca > 0:
        ordem = sorted(dist, key=lambda k: (k != LOCAL_PADRAO, -dist[k]))
        for loc in ordem:
            tirar = min(dist[loc], diferenca)
            dist[loc] = round(dist[loc] - tirar, 6)
            diferenca = round(diferenca - tirar, 6)
            if diferenca <= 0:
                break
    else:
        alvo = max(dist, key=lambda k: dist[k]) if dist else LOCAL_PADRAO
        dist[alvo] = round(dist.get(alvo, 0) - diferenca, 6)
    lote["distribuicao"] = dist
    return True


def _reparar_lotes_produto(produto: dict) -> dict:
    """Arruma os lotes de um produto (só na memória; quem chama grava):
    1) junta lotes repetidos (mesmo id — vinham da devolução antiga), somando
       quantidade e saldo por local no primeiro;
    2) faz o saldo por local de cada lote bater com a quantidade.
    3) remove lotes que só têm resto de arredondamento (< RESTO_LOTE_MINIMO);
    4) dá um id a lote antigo sem id (sem ele a devolução não acha o lote).
    Devolve o que mudou: {"duplicados", "ajustados", "restos", "sem_id"}."""
    lotes = produto.get("lotes") or []
    resumo = {"duplicados": 0, "ajustados": 0, "restos": 0, "sem_id": 0}
    for l in lotes:
        if not l.get("id"):
            l["id"] = uuid.uuid4().hex
            resumo["sem_id"] += 1
    unidos, por_id = [], {}
    for l in lotes:
        lid = l.get("id")
        if lid and lid in por_id:
            alvo = por_id[lid]
            dist_alvo = dict(_dist_lote(alvo))
            for loc, q in _dist_lote(l).items():
                dist_alvo[loc] = round(to_float(dist_alvo.get(loc, 0)) + to_float(q), 6)
            alvo["distribuicao"] = dist_alvo
            alvo["qtd"] = round(to_float(alvo.get("qtd", 0)) + to_float(l.get("qtd", 0)), 6)
            resumo["duplicados"] += 1
            continue
        if lid:
            por_id[lid] = l
        unidos.append(l)
    restos = [l for l in unidos if to_float(l.get("qtd", 0)) < RESTO_LOTE_MINIMO]
    resumo["restos"] = len(restos)
    unidos = [l for l in unidos if to_float(l.get("qtd", 0)) >= RESTO_LOTE_MINIMO]
    for l in unidos:
        if _ajustar_dist_para_qtd(l):
            resumo["ajustados"] += 1
    produto["lotes"] = unidos
    if any(resumo.values()):
        atualizar_campos_derivados_estoque(produto)
    return resumo


_GIRO_CACHE = {"em": 0.0, "vendido": None}


def _alerta_giro(produtos: list, dias_limite: int = 60) -> list:
    """Produtos cujo saldo daria pra mais de `dias_limite` dias no ritmo de
    venda dos últimos 30 dias. O ritmo vem do que de fato saiu dos lotes nas
    vendas (avulsas, por dose e em pacotes). Saldo muito acima do que vende
    costuma ser entrada lançada a mais ou saída não lançada. O consumo de 30
    dias fica guardado 10 min (o alerta é só informativo)."""
    if _GIRO_CACHE["vendido"] is None or time.time() - _GIRO_CACHE["em"] > 600:
        hoje = today_brt()
        vendido = defaultdict(float)
        for v in db.get_consumo_vendas_periodo((hoje - timedelta(days=30)).isoformat(), hoje.isoformat()):
            for c in v.get("consumo_lotes") or []:
                pid_c = c.get("component_id") or (v.get("produto_pid") if v.get("produto_tipo") != "bar" else None)
                if pid_c:
                    vendido[pid_c] += to_float(c.get("qtd", c.get("qty", 0)))
        _GIRO_CACHE.update({"em": time.time(), "vendido": dict(vendido)})
    vendido = _GIRO_CACHE["vendido"]
    alerta = []
    for p in produtos:
        saldo = estoque_qtd_total(p)
        vend = vendido.get(p["pid"], 0.0)
        if saldo > 0 and vend > 0:
            dias = saldo / (vend / 30)
            if dias > dias_limite:
                excesso = saldo - vend / 30 * dias_limite  # unidades além de `dias_limite` dias de venda
                alerta.append({"pid": p["pid"], "nome": p.get("nome", ""), "saldo": round(saldo, 2),
                               "vendido_30d": round(vend, 2), "dias": round(dias),
                               "excesso": round(excesso, 1),
                               "valor_excesso": round(excesso * _custo_medio_produto(p), 2)})
    # mais dinheiro parado além do normal primeiro (é onde a conferência vale mais)
    return sorted(alerta, key=lambda a: -a["valor_excesso"])


def _diagnostico_lotes(produtos: list) -> dict:
    """Conta (sem alterar nada) produtos com lote repetido ou com saldo por
    local diferente da quantidade."""
    diag = {"produtos": 0, "duplicados": 0, "ajustados": 0, "restos": 0, "sem_id": 0}
    for p in produtos:
        r = _reparar_lotes_produto(copy.deepcopy(p))
        if any(r.values()):
            diag["produtos"] += 1
            for k in ("duplicados", "ajustados", "restos", "sem_id"):
                diag[k] += r[k]
    return diag


def _qtd_por_local(produto: dict) -> dict:
    """Soma a quantidade de todos os lotes do produto, por local de estoque."""
    totais = {}
    for l in produto.get("lotes", []):
        for loc, q in _dist_lote(l).items():
            totais[loc] = totais.get(loc, 0.0) + to_float(q)
    return totais


def _com_qtd_por_local(produtos: list) -> list:
    """Anexa o saldo por local em cada produto (usado pela tela de vendas,
    que confere o estoque do local escolhido)."""
    for p in produtos:
        p["qtd_por_local"] = _qtd_por_local(p)
    return produtos


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


def _categorias_existentes(produtos: list, campo: str = "categoria") -> list:
    return sorted(set(
        (p.get(campo) or "").strip() for p in produtos
        if (p.get(campo) or "").strip()
    ))


def atualizar_campos_derivados_estoque(produto):
    qtd = estoque_qtd_total(produto)
    valor = estoque_valor_total(produto)
    produto["quantidade"] = qtd
    if qtd > 0:
        produto["valor_investido"] = round(valor / qtd, 4)
    # qtd == 0: mantém o último valor_investido; só atualiza ao entrar novo lote


def _log_lote(tipo_evento: str, produto: dict, acao: str, detalhes: dict | None = None):
    """Registra no histórico o que foi feito num lote (antes não ficava
    registro de quem adicionou/editou/removeu lote)."""
    db.registrar_log(
        session.get("usuario", ""), tipo_evento, f"{produto.get('nome', '')}: {acao}",
        now_brt().strftime("%Y-%m-%d"), now_brt().strftime("%H:%M"),
        detalhes={"pid": produto.get("pid"), **(detalhes or {})},
    )


MSG_LOTE_REPETIDO = ("Esse mesmo lote acabou de ser lançado (mesma quantidade, custo e data, há menos de 1 minuto). "
                     "Não foi lançado de novo — confira a lista de lotes do produto.")


def _lote_repetido_recente(produto: dict, qtd, custo_unit, data, usuario: str, segundos: int = 60) -> bool:
    """Evita lote em dobro por clique duplo no "Salvar" (já aconteceu: o mesmo
    lote lançado 6x no mesmo minuto). Compara com lotes deste produto criados
    há menos de `segundos` pelo mesmo usuário."""
    agora = now_brt()
    data_n = normalizar_data_str(data)
    for l in produto.get("lotes") or []:
        try:
            criado = datetime.fromisoformat(l.get("criado_em") or "")
        except (TypeError, ValueError):
            continue
        if criado.tzinfo is None:
            criado = criado.replace(tzinfo=BRT)
        if ((agora - criado).total_seconds() <= segundos
                and l.get("criado_por") == usuario
                and l.get("data") == data_n
                and abs(to_float(l.get("qtd_entrada", l.get("qtd"))) - to_float(qtd)) < 1e-6
                and abs(to_float(l.get("custo_unit")) - to_float(custo_unit)) < 1e-6):
            return True
    return False


def entrada_estoque_lote(produto, data, qtd, custo_unit, usuario="", local_id=None):
    qtd = to_int(qtd, 0)
    custo_unit = to_float(custo_unit, 0)
    if qtd <= 0:
        raise ValueError("Quantidade inválida para entrada de estoque.")
    if custo_unit <= 0:
        raise ValueError("Custo unitário inválido.")
    if _lote_repetido_recente(produto, qtd, custo_unit, data, usuario):
        raise ValueError(MSG_LOTE_REPETIDO)

    novo_lote = {
        "id": uuid.uuid4().hex,
        "data": normalizar_data_str(data),
        "qtd": qtd,
        "qtd_entrada": qtd,
        "custo_unit": custo_unit,
        "criado_por": usuario,
        "criado_em": now_brt().isoformat(timespec="seconds"),
    }
    if local_id:
        novo_lote["distribuicao"] = {local_id: qtd}
    produto.setdefault("lotes", [])
    produto["lotes"].append(novo_lote)
    atualizar_campos_derivados_estoque(produto)


def calcular_margem(venda, custo) -> float:
    """Markup %: quanto o preço de venda está acima do custo —
    (venda-custo)/custo*100. Sem limite superior (pode passar de 100%) nem
    inferior (negativo = vendeu no prejuízo). Usada em toda exibição/filtro
    de margem (Vendas, Estoque, Pacotes, Comandas) — não usar pra rateio
    proporcional de lucro (isso é fração da receita, não markup sobre custo)."""
    custo = to_float(custo)
    if custo <= 0:
        return 0.0
    return ((to_float(venda) - custo) / custo) * 100


def consumir_fifo(produto, qtd_vender):
    """Baixa de venda sem local (vendas antigas/tela antiga = Loja). Usa a
    versão por local pra baixar também o saldo do local — antes baixava só a
    quantidade do lote e o saldo por local ficava errado."""
    return consumir_fifo_local(produto, qtd_vender, LOCAL_PADRAO)


def devolver_fifo(produto, consumo_lotes):
    """Devolução de venda sem local gravado (vendas antigas = Loja). Usa a
    mesma regra da versão por local, pra manter o saldo por local certo e
    não duplicar lote."""
    devolver_fifo_local(produto, consumo_lotes, LOCAL_PADRAO)


def consumir_fifo_local(produto, qtd_vender, local_id, ignorar_falta=False):
    """Igual a consumir_fifo, mas só consome de lotes com saldo disponível
    no local informado — usado nos fluxos que já sabem de qual local a
    venda saiu (comandas e venda avulsa de produtos de Estoque). Função
    irmã, separada: consumir_fifo original não é tocado.

    ignorar_falta: igual ao parâmetro homônimo de bar_fifo_consume — quando
    True, deixa vender mesmo sem estoque suficiente nesse local (usuário
    confirmou "continuar mesmo assim" na tela)."""
    qtd_vender = to_float(qtd_vender, 0)
    if qtd_vender <= 0:
        raise ValueError("Quantidade inválida.")
    local_id = local_id or LOCAL_PADRAO

    lotes = produto.get("lotes", [])
    if not isinstance(lotes, list):
        lotes = []
        produto["lotes"] = lotes
    lotes.sort(key=lambda l: str(l.get("data", "")))

    disponivel_local = sum(to_float(_dist_lote(l).get(local_id, 0)) for l in lotes)
    # Fora do local principal (ex.: Escritório) não existe "continuar mesmo
    # assim": só vende o que tem lote registrado lá.
    if local_id != LOCAL_PADRAO:
        ignorar_falta = False
    if qtd_vender > disponivel_local and not ignorar_falta:
        nome_local = next((l["nome"] for l in db.get_all_locais_estoque() if l["id"] == local_id), local_id)
        fmt = lambda q: f"{q:g}".replace(".", ",")
        raise ValueError(f"{produto.get('nome', 'Produto')}: sem estoque suficiente no(a) {nome_local} "
                         f"(tem {fmt(disponivel_local)}, pediu {fmt(qtd_vender)}).")

    restante = qtd_vender
    custo_total = 0.0
    consumo = []

    i = 0
    while restante > 0 and i < len(lotes):
        lote = lotes[i]
        dist = _dist_lote(lote)
        qtd_lote_local = to_float(dist.get(local_id, 0))
        if qtd_lote_local <= 0:
            i += 1
            continue

        custo_unit = to_float(lote.get("custo_unit", 0))
        data_lote = normalizar_data_str(lote.get("data"))
        usar = min(restante, qtd_lote_local)
        custo_total += usar * custo_unit
        consumo.append({
            "data_lote": data_lote, "qtd": usar, "custo_unit": custo_unit,
            "lote_id": lote.get("id"), "local_id": local_id,
        })

        dist = dict(dist)
        dist[local_id] = round(qtd_lote_local - usar, 4)
        lote["distribuicao"] = dist
        lote["qtd"] = round(to_float(lote.get("qtd", 0)) - usar, 4)
        restante = round(restante - usar, 6)

        if lote["qtd"] < RESTO_LOTE_MINIMO:
            lotes.pop(i)
        else:
            i += 1

    atualizar_campos_derivados_estoque(produto)
    return custo_total, consumo


def devolver_fifo_local(produto, consumo_lotes, local_id):
    """Espelho de devolver_fifo, mas devolve a quantidade pro local de onde
    ela saiu (gravado em cada item de consumo). Função irmã, separada:
    devolver_fifo original não é tocado."""
    if not consumo_lotes or not isinstance(consumo_lotes, list):
        return
    local_id = local_id or LOCAL_PADRAO

    produto.setdefault("lotes", [])

    for c in reversed(consumo_lotes):
        _devolver_ao_lote(produto, c.get("lote_id"), c.get("data_lote"),
                          to_float(c.get("qtd", 0)), to_float(c.get("custo_unit", 0)),
                          c.get("local_id") or local_id)

    atualizar_campos_derivados_estoque(produto)


def _devolver_ao_lote(produto: dict, lote_id, data_lote, qtd: float, custo_unit: float, local_id: str):
    """Devolve qtd ao lote de onde saiu: se o lote ainda existe no produto,
    soma nele (quantidade e saldo do local); só recria o lote se ele já
    tinha zerado e sido removido. Antes sempre criava uma linha nova, e o
    mesmo lote aparecia repetido."""
    if qtd <= 0:
        return
    local_id = local_id or LOCAL_PADRAO
    lotes = produto.setdefault("lotes", [])
    existente = next((l for l in lotes if lote_id and l.get("id") == lote_id), None)
    if existente:
        dist = dict(_dist_lote(existente))
        dist[local_id] = round(to_float(dist.get(local_id, 0)) + qtd, 6)
        existente["distribuicao"] = dist
        existente["qtd"] = round(to_float(existente.get("qtd", 0)) + qtd, 6)
        return
    lotes.insert(0, {
        "id": lote_id,
        "data": normalizar_data_str(data_lote),
        "qtd": qtd,
        "custo_unit": custo_unit,
        "distribuicao": {local_id: qtd},
    })


def _aplicar_devolucao_bar_componente(produto: dict, consumo: list):
    """Lógica pura (sem acesso a banco) de devolver 1 componente ao estoque.
    Extraída de devolver_bar_componentes pra poder ser reaproveitada em lote
    (ver comanda_remover), sem duplicar a regra."""
    produto.setdefault("lotes", [])
    for c in reversed(consumo or []):
        # consumo de pacote antigo não tem local gravado: volta pro principal
        _devolver_ao_lote(produto, c.get("lote_id"), c.get("data_lote"),
                          to_float(c.get("qty", c.get("qtd", 0))),
                          to_float(c.get("unit_cost", c.get("custo_unit", 0))),
                          c.get("local_id") or LOCAL_PADRAO)
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
        _aplicar_devolucao_bar_componente(produto, consumo)
        db.update_product(produto)


def _resolver_participantes_lote(form) -> tuple[list, str]:
    """Lê investidor_id[]/percentual[] do formulário de lote e valida a soma.
    Se nada foi informado, o dono do sistema fica com 100% automaticamente.
    Retorna (participantes, erro) — erro vazio quando está tudo certo."""
    ids = form.getlist("investidor_id[]")
    pcts = form.getlist("percentual[]")
    participantes = []
    for iid, pct_raw in zip(ids, pcts):
        iid = (iid or "").strip()
        pct = to_float(pct_raw, 0)
        if iid and pct > 0:
            participantes.append({"investidor_id": iid, "percentual": pct})
    if not participantes:
        dono = db.get_or_create_dono_investidor()
        return [{"investidor_id": dono["id"], "percentual": 100.0}], ""
    soma = sum(p["percentual"] for p in participantes)
    if abs(soma - 100.0) > 0.01:
        return [], f"A soma dos percentuais dos participantes precisa ser 100% (está em {soma:.2f}%)."
    return participantes, ""


def _registrar_lote_investimento(lote_id, pid, produto_nome, data_entrada, quantidade, custo_unit, fornecedor, participantes):
    db.insert_lote_investimento({
        "id": lote_id,
        "pid": pid,
        "produto_nome": produto_nome,
        "data_entrada": normalizar_data_str(data_entrada),
        "quantidade": to_float(quantidade, 0),
        "custo_unit": to_float(custo_unit, 0),
        "valor_total": round(to_float(quantidade, 0) * to_float(custo_unit, 0), 2),
        "fornecedor": fornecedor or "",
        "participantes": participantes,
        "criado_por": session.get("usuario", ""),
        "criado_em": now_brt().strftime("%Y-%m-%d %H:%M"),
        "removido": False,
    })


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
# Janela de tolerância pra reconferir permissões/tipo do usuário no banco.
# Dentro desse intervalo, requests repetidos (inclusive AJAX) reaproveitam o
# que já está na sessão Flask em vez de refazer a mesma consulta. Efeito
# colateral aceito: bloquear/mudar permissão de alguém pode levar até esse
# tempo pra valer, em vez de ser instantâneo (a checagem de sessão encerrada
# remotamente, logo abaixo, continua rodando em toda requisição).
_PERM_CACHE_TTL_SEGUNDOS = 20
_SESSAO_CHECK_SEGUNDOS = 30

# Intervalo mínimo entre gravações de "última atividade" da sessão. Era
# gravado a cada requisição (inclusive AJAX leve); passa a gravar no máximo
# 1x por minuto. Efeito colateral aceito: a tela de usuários online pode
# mostrar a última atividade/tela com até esse atraso.
_ATIVIDADE_MIN_INTERVALO_SEGUNDOS = 60


@app.before_request
def atualizar_permissoes_da_sessao():
    if "usuario" not in session:
        return

    usuario = session.get("usuario", "")
    agora = time.time()

    # Sessão foi encerrada remotamente (admin clicou "Encerrar Sessão") ou o
    # usuário foi bloqueado/arquivado desde o último login. Continua sendo
    # checado em toda requisição — não entra no cache abaixo.
    token = session.get("_token")
    # Conferida no banco a cada _SESSAO_CHECK_SEGUNDOS (antes: toda página,
    # ~0,2 s cada). "Encerrar sessão" pelo admin derruba o usuário em até 30 s.
    sessao_verificada_em = session.get("_sessao_verificada_em", 0)
    if token and (agora - sessao_verificada_em) >= _SESSAO_CHECK_SEGUNDOS:
        try:
            if not db.sessao_ativa(token):
                session.clear()
                session["_sessao_encerrada"] = True
                return
            session["_sessao_verificada_em"] = agora
        except Exception:
            pass

    perm_verificada_em = session.get("_perm_verificada_em", 0)
    perm_em_cache = (
        "permissoes" in session
        and (agora - perm_verificada_em) < _PERM_CACHE_TTL_SEGUNDOS
    )

    if not perm_em_cache:
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

        session["_perm_verificada_em"] = agora

    if token:
        atividade_gravada_em = session.get("_atividade_gravada_em", 0)
        if (agora - atividade_gravada_em) >= _ATIVIDADE_MIN_INTERVALO_SEGUNDOS:
            try:
                db.atualizar_atividade_sessao(token, now_brt().strftime("%Y-%m-%d %H:%M:%S"), tela=_tela_atual_da_request())
                session["_atividade_gravada_em"] = agora
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


@app.route("/recuperar-senha")
def recuperar_senha():
    """Tela de 'Esqueci minha senha' — só a página, sem envio de e-mail de
    verdade (o sistema não tem nenhum serviço de e-mail configurado ainda).
    Reset de senha continua sendo feito manualmente pelo admin no painel de
    Permissões."""
    return render_template("recuperar_senha.html")


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
    """Identificador de venda exibido pro usuário (mesmo pra toda linha do
    mesmo carrinho/comanda): o código de rastreio compartilhado (V-XXXXX)
    quando já existe — venda avulsa sempre tem, comanda só depois de
    quitada. Sem código ainda (comanda aberta/pendente, ou venda antiga de
    antes dessa mudança), cai pro identificador interno de sempre:
    CMD-XXXXXXXX pra comanda, id_venda cru pra balcão."""
    if v.get("codigo"):
        return v["codigo"]
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
    usuario_raw = request.args.get("usuario", "").strip()
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
        return calcular_margem(preco, custo)

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
        if forma_pagamento_raw and forma_pagamento_raw != "__multiplas__":
            pgto = str(v.get("forma_pagamento", "")).lower()
            if forma_pagamento_raw.lower() not in pgto:
                ok = False
        if status_raw and (v.get("status") or "quitada") != status_raw:
            ok = False
        if usuario_raw:
            valor_usuario = v.get("usuario") or ""
            if usuario_raw == "__sem__":
                ok = ok and not valor_usuario
            elif valor_usuario != usuario_raw:
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

    ids_venda_lote = [itens[0].get("id_venda") for k, itens in grupos_map.items() if k.startswith("iv:") and itens[0].get("id_venda")]
    cids_comanda = [itens[0].get("comanda_id") for k, itens in grupos_map.items() if k.startswith("cmd:") and itens[0].get("comanda_id")]

    # As 3 buscas abaixo são independentes entre si — rodam em paralelo em
    # vez de uma atrás da outra (mesma ideia já aplicada em financeiro()).
    # commands_by_ids busca só as comandas que aparecem nas vendas já
    # filtradas desta página, em vez de db.get_all_commands() (a tabela
    # inteira — ~1.7s sozinha, contra uma fração disso pra um punhado de ids).
    tarefas_vendas = {
        "pagamentos_venda": lambda: db.get_pagamentos_por_id_vendas(ids_venda_lote),
        "comandas": lambda: db.get_commands_by_ids(cids_comanda),
        "usuarios_map": db.get_all_users,
    }
    with ThreadPoolExecutor(max_workers=len(tarefas_vendas)) as executor:
        futuros_vendas = {nome: executor.submit(fn) for nome, fn in tarefas_vendas.items()}
        resultados_vendas = {nome: fut.result() for nome, fut in futuros_vendas.items()}

    pagamentos_map = defaultdict(list)
    for p in resultados_vendas["pagamentos_venda"]:
        pagamentos_map[p["id_venda"]].append(p)

    # Pagamentos reais de comanda (forma + valor, podendo ter vários
    # registros — ex: 2x Pix) — usados em _montar_grupo pra mostrar a forma
    # de pagamento de verdade em "Visualizar Venda", em vez do
    # forma_pagamento bruto de cada item (que hoje é sempre "fiado" até a
    # comanda ser paga, e nunca é atualizado depois).
    pagamentos_comanda_map = {
        c["cid"]: (c.get("pagamentos") or [])
        for c in resultados_vendas["comandas"] if isinstance(c, dict)
    }

    usuarios_map = resultados_vendas["usuarios_map"]

    def _soma_por_forma(lista_pagamentos):
        soma = defaultdict(float)
        for p in lista_pagamentos:
            f = normalizar_pagamento(p.get("forma", ""))
            soma[f] += to_float(p.get("valor", 0))
        return sorted(soma.items(), key=lambda x: x[1], reverse=True)

    def _montar_grupo(chave, itens_grupo):
        itens_ordenados = sorted(itens_grupo, key=lambda v: (v.get("data") or "", v.get("hora") or ""))
        primeiro = itens_ordenados[0]
        valor_total = sum(float(v.get("valor_venda") or 0) * float(v.get("quantidade") or 1) for v in itens_grupo)
        custo_total = sum(float(v.get("valor_investido") or 0) * float(v.get("quantidade") or 1) for v in itens_grupo)
        margem_grupo = calcular_margem(valor_total, custo_total)

        comanda_id_grupo = primeiro.get("comanda_id")
        pagamentos_venda = pagamentos_map.get(primeiro.get("id_venda"), [])
        pagamentos_comanda = pagamentos_comanda_map.get(comanda_id_grupo, []) if comanda_id_grupo else []

        soma = _soma_por_forma(pagamentos_comanda) if pagamentos_comanda \
            else _soma_por_forma(pagamentos_venda) if pagamentos_venda else None

        if soma is not None:
            formas = [f for f, _ in soma]
            pagamentos_breakdown = [{"forma": f, "total": round(v, 2)} for f, v in soma]
        else:
            # Sem pagamento registrado ainda (comanda pendente, ou venda
            # antiga sem split) — mesmo comportamento de sempre: pega o
            # forma_pagamento bruto de cada item, sem valores.
            formas = []
            for v in itens_grupo:
                f = normalizar_pagamento(v.get("forma_pagamento", "")) if v.get("forma_pagamento") else None
                if f and f not in formas:
                    formas.append(f)
            pagamentos_breakdown = []

        statuses = set((v.get("status") or "quitada") for v in itens_grupo)
        if "pendente" in statuses:
            status_grupo = "pendente"
        elif "parcial" in statuses:
            status_grupo = "parcial"
        elif statuses == {"cancelada"}:
            status_grupo = "cancelada"
        else:
            status_grupo = "quitada"

        id_display = _id_display(primeiro)

        return {
            "grupo_key": chave,
            "id_display": id_display,
            "comanda_id": comanda_id_grupo,
            "comanda_nome": primeiro.get("comanda_nome"),
            "data": primeiro.get("data"),
            "hora": primeiro.get("hora"),
            "qtd_itens": len(itens_grupo),
            "qtd_unidades": sum(int(float(v.get("quantidade") or 1)) for v in itens_grupo),
            "valor_total": round(valor_total, 2),
            "margem": round(margem_grupo, 2),
            "formas_pagamento": formas,
            "pagamentos_breakdown": pagamentos_breakdown,
            "usuario": primeiro.get("usuario") or "",
            "status": status_grupo,
            "pode_cancelar_inteira": status_grupo in ("quitada", "cancelada"),
            "itens": itens_ordenados,
        }

    vendas_agrupadas = [_montar_grupo(k, itens) for k, itens in grupos_map.items()]

    # "Múltiplas formas" só existe depois de agrupar (cada linha crua de
    # sales.forma_pagamento é sempre uma forma só) — filtra aqui em cima dos
    # grupos já montados, e reconstrói vendas_filtradas a partir dos itens
    # dos grupos que sobraram, pra estatísticas do topo baterem com a tabela.
    if forma_pagamento_raw == "__multiplas__":
        vendas_agrupadas = [g for g in vendas_agrupadas if len(g.get("formas_pagamento") or []) > 1]
        vendas_filtradas = [v for g in vendas_agrupadas for v in g["itens"]]

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

    # Paginação inteira no navegador (igual à tela de Estoque) — o servidor só
    # manda um lote com limite de segurança (não a lista inteira sem fim, já
    # que vendas é um histórico que só cresce, diferente do catálogo de
    # produtos), e o JS pagina por dentro desse lote sem recarregar a página.
    LIMITE_VENDAS_TELA = 1000
    total_registros = len(vendas_agrupadas)
    vendas_truncadas = total_registros > LIMITE_VENDAS_TELA
    vendas_pagina = vendas_agrupadas[:LIMITE_VENDAS_TELA]

    nomes_sugestoes = sorted(set(
        v.get("nome", "").strip() for v in vendas_data
        if isinstance(v, dict) and v.get("nome", "").strip()
    ))

    usuarios_conhecidos = [
        (u, (dados.get("nome") or u)) for u, dados in sorted(usuarios_map.items())
    ]

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
        usuario_raw=usuario_raw,
        usuarios_conhecidos=usuarios_conhecidos,
        margem_min_raw=margem_min_raw,
        margem_max_raw=margem_max_raw,
        ordenar_raw=ordenar_raw,
        total_registros=total_registros,
        vendas_truncadas=vendas_truncadas,
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

    # Quando as duas pontas do período estão definidas (inclusive depois de
    # resolver os atalhos "hoje"/"semana"/"mês" acima), pede só esse
    # intervalo pro banco em vez da tabela de vendas inteira — mesma regra
    # de segurança já usada em financeiro(). Se faltar alguma ponta, cai de
    # volta pra busca completa, igual ao comportamento de sempre.
    if data_ini_raw and data_fim_raw:
        vendas_data = db.get_sales_by_date_range(data_ini_raw, data_fim_raw)
    else:
        vendas_data = db.get_all_sales()

    data_ini = parse_date_yyyy_mm_dd(data_ini_raw)
    data_fim = parse_date_yyyy_mm_dd(data_fim_raw)
    busca_produto = busca_produto_raw.lower()

    def margem_calc_exp(v):
        preco = float(v.get("valor_venda") or 0)
        custo = float(v.get("valor_investido") or 0)
        return calcular_margem(preco, custo)

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
        margem = round(calcular_margem(preco, custo), 1)
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

    venda = db.get_sale(uid)
    if not venda:
        return redirect(url_for("vendas"))

    # Produto travado: não dá mais pra trocar por outro aqui, só ajustar
    # data/quantidade/forma de pagamento do mesmo produto/pacote já vendido.
    # Resolve pelo tipo gravado na própria venda (bar_products não entra em
    # db.get_all_products(), por isso é preciso ramificar).
    produto_tipo_venda = venda.get("produto_tipo") or "estoque"
    pid_venda = str(venda.get("produto_pid", "")).strip()
    if produto_tipo_venda == "bar":
        produto_atual = db.bar_get_product(pid_venda)
        nome_produto_atual = (produto_atual or {}).get("name") or venda.get("nome", "")
        preco_produto_atual = to_float((produto_atual or {}).get("sale_price", venda.get("valor_venda", 0)))
        qtd_disponivel_atual = None  # pacote não tem um "disponível" único, depende de todos os componentes
    else:
        produto_atual = db.get_product(pid_venda)
        nome_produto_atual = (produto_atual or {}).get("nome") or venda.get("nome", "")
        preco_produto_atual = to_float((produto_atual or {}).get("valor_venda", venda.get("valor_venda", 0)))
        qtd_disponivel_atual = (produto_atual or {}).get("quantidade")

    # A edição mantém o preço que a venda já tinha (antes trocava pelo preço
    # atual do produto). Quem tem "alterar_preco_venda_realizada" pode mudar;
    # item de comanda não, porque o valor dele é controlado pela comanda.
    preco_venda_original = to_float(venda.get("valor_venda", 0)) or preco_produto_atual
    pode_alterar_preco = (session.get("tipo") == "admin" or tem_permissao("alterar_preco_venda_realizada")) \
        and not venda.get("comanda_id")

    id_venda = venda.get("id_venda") or ""
    irmaos = [s for s in db.get_sales_by_id_venda(id_venda) if s["uid"] != uid] if id_venda else []
    total_outros_itens = sum(to_float(s.get("valor_venda", 0)) * to_int(s.get("quantidade", 1)) for s in irmaos)
    pagamentos_atuais = db.get_pagamentos_venda(id_venda) if id_venda else []

    if venda.get("fechado"):
        return render_template(
            "editar_vendas.html",
            venda=venda,
            nome_produto_atual=nome_produto_atual,
            preco_produto_atual=preco_venda_original,
            pode_alterar_preco=pode_alterar_preco,
            produto_tipo_venda=produto_tipo_venda,
            qtd_disponivel_atual=qtd_disponivel_atual,
            total_outros_itens=total_outros_itens,
            pagamentos_atuais=pagamentos_atuais,
            erro="Esta venda já foi fechada no caixa e não pode ser editada.",
            tipo=session.get("tipo"),
            permissoes=session.get("permissoes", [])
        )

    # Item de comanda ainda em aberto: a forma de pagamento de cada linha
    # fica "fiado" até a comanda ser paga de verdade (fluxo próprio em
    # Comandas) — editar aqui deixaria essa linha com uma forma de
    # pagamento "real" sem nenhum pagamento ter sido registrado, causando
    # o "Cartão + Fiado" confuso na lista de Vendas.
    comanda_vinculada = db.get_command(venda.get("comanda_id")) if venda.get("comanda_id") else None
    if comanda_vinculada and comanda_vinculada.get("status") != "quitada":
        return render_template(
            "editar_vendas.html",
            venda=venda,
            nome_produto_atual=nome_produto_atual,
            preco_produto_atual=preco_venda_original,
            pode_alterar_preco=pode_alterar_preco,
            produto_tipo_venda=produto_tipo_venda,
            qtd_disponivel_atual=qtd_disponivel_atual,
            total_outros_itens=total_outros_itens,
            pagamentos_atuais=pagamentos_atuais,
            erro="Este item pertence a uma comanda ainda em aberto — a forma de pagamento só é definida quitando a comanda (tela de Comandas), não por aqui.",
            tipo=session.get("tipo"),
            permissoes=session.get("permissoes", [])
        )

    erro = ""

    if request.method == "POST":
        redir = autorizar_acao("editar_vendas")
        if redir:
            return redir

        nova_data = normalizar_data_str(request.form.get("data", venda.get("data", "")))

        valor_dinheiro = to_float(request.form.get("valor_dinheiro", "0"))
        valor_pix = to_float(request.form.get("valor_pix", "0"))
        valor_cartao = to_float(request.form.get("valor_cartao", "0"))

        nova_qtd = to_int(request.form.get("quantidade", venda.get("quantidade", 1)), 0)
        qtd_antiga = to_int(venda.get("quantidade", 0))

        novo_preco = preco_venda_original
        if pode_alterar_preco and request.form.get("preco_unit") not in (None, ""):
            novo_preco = round(to_float(request.form.get("preco_unit"), 0.0), 2)

        if nova_qtd <= 0:
            erro = "Quantidade inválida."
        elif novo_preco <= 0:
            erro = "Preço de venda inválido."
        elif not produto_atual:
            erro = "Pacote não encontrado." if produto_tipo_venda == "bar" else "Produto não encontrado no estoque."

        total_venda_atual = 0.0
        if not erro:
            total_venda_atual = round(total_outros_itens + novo_preco * nova_qtd, 2)
            if round(valor_dinheiro + valor_pix + valor_cartao, 2) + 0.01 < total_venda_atual:
                erro = "A soma dos valores de pagamento é menor que o total da venda."

        # Quantidade não mudou: não mexe em estoque nenhum, só nos campos
        # abaixo (data/forma). Evita churn desnecessário no FIFO e resolve
        # de graça o caso mais comum (só corrigir a data).
        custo_unit_medio = to_float(venda.get("valor_investido", 0))
        consumo_novo = venda.get("consumo_lotes", [])

        if not erro and nova_qtd != qtd_antiga:
            if produto_tipo_venda == "bar":
                consumo_antigo = venda.get("consumo_lotes") or []
                # Venda de pacote gravada antes desta atualização não tem o
                # componente de cada item de consumo — não dá pra devolver
                # com segurança, então só bloqueia a troca de quantidade
                # (data/forma continuam editáveis, não passam por aqui).
                sem_detalhe = any(not c.get("component_id") for c in consumo_antigo)
                if consumo_antigo and sem_detalhe:
                    erro = ("Esta venda de pacote é anterior a esta atualização e não tem o "
                            "detalhamento necessário pra alterar a quantidade com segurança — "
                            "edite apenas data e forma de pagamento.")
                else:
                    try:
                        components = db.bar_get_components(pid_venda)
                        if not components:
                            raise ValueError("Este pacote não possui componentes cadastrados.")

                        if consumo_antigo:
                            por_componente = defaultdict(list)
                            for c in consumo_antigo:
                                por_componente[c.get("component_id")].append(c)
                            devolver_bar_componentes([
                                {"pid": comp_id, "consumo": lst} for comp_id, lst in por_componente.items()
                            ])

                        # Confere disponibilidade de cada componente pra nova
                        # quantidade ANTES de consumir de verdade — bar_fifo_consume
                        # não valida isso sozinho, então a checagem é feita aqui.
                        faltando = []
                        necessidades = []
                        for comp in components:
                            unidade_uso = (comp.get("unidade_uso") or "UN").strip()
                            qtd_bruta = float(comp["quantity"]) * nova_qtd
                            comp_pid = (comp.get("component_id") or "").replace("-", "")
                            produto_comp = db.get_product(comp_pid)
                            cv = float((produto_comp or {}).get("conteudo_valor") or 1)
                            needed = qtd_bruta / cv if (cv > 1 and unidade_uso.upper() != "UN") else qtd_bruta
                            necessidades.append((comp_pid, needed, produto_comp))
                            disponivel = sum(to_float(l.get("qtd", 0)) for l in (produto_comp or {}).get("lotes", []))
                            if needed > disponivel + 0.0001:
                                faltando.append((comp.get("component") or {}).get("name") or comp_pid)

                        if faltando:
                            raise ValueError("Estoque insuficiente pra essa quantidade: " + ", ".join(faltando) + ".")

                        total_cost = 0.0
                        all_logs = []
                        for comp_pid, needed, produto_comp in necessidades:
                            cost, log = bar_fifo_consume(comp_pid, needed, produto_prefetched=produto_comp,
                                                         local_id=venda.get("local_id") or LOCAL_PADRAO)
                            total_cost += cost
                            all_logs.extend(log)

                        custo_unit_medio = round(total_cost / nova_qtd, 4) if nova_qtd else 0.0
                        consumo_novo = all_logs
                    except ValueError as e:
                        erro = str(e)
            else:
                consumo_antigo = venda.get("consumo_lotes", [])
                local_da_venda = venda.get("local_id")

                if consumo_antigo:
                    if local_da_venda:
                        devolver_fifo_local(produto_atual, consumo_antigo, local_da_venda)
                    else:
                        devolver_fifo(produto_atual, consumo_antigo)

                try:
                    if local_da_venda:
                        custo_total, consumo_novo = consumir_fifo_local(produto_atual, nova_qtd, local_da_venda)
                    else:
                        custo_total, consumo_novo = consumir_fifo(produto_atual, nova_qtd)
                    custo_unit_medio = (custo_total / nova_qtd) if nova_qtd else 0.0
                except ValueError as e:
                    erro = str(e)

        if not erro:
            nomes_forma = {"dinheiro": "Dinheiro", "pix": "Pix", "cartao": "Cartão"}
            pagamentos = _distribuir_pagamentos(total_venda_atual, valor_dinheiro, valor_pix, valor_cartao)
            rotulo = "+".join(nomes_forma[p["forma"]] for p in pagamentos) or venda.get("forma_pagamento", "cartao")

            venda["data"] = nova_data
            venda["forma_pagamento"] = rotulo
            venda["nome"] = nome_produto_atual
            venda["quantidade"] = nova_qtd
            venda["valor_venda"] = novo_preco
            venda["valor_investido"] = round(custo_unit_medio, 4)
            venda["consumo_lotes"] = consumo_novo

            db.update_sale(venda)
            if abs(novo_preco - preco_venda_original) > 0.004:
                db.registrar_log(
                    session.get("usuario", ""), "venda_preco_alterado",
                    f"Alterou o preço da venda de {nome_produto_atual} ({venda.get('data', '')}): "
                    f"R$ {preco_venda_original:.2f} → R$ {novo_preco:.2f}",
                    now_brt().strftime("%Y-%m-%d"), now_brt().strftime("%H:%M"),
                )
            if produto_tipo_venda != "bar" and nova_qtd != qtd_antiga:
                db.update_product(produto_atual)

            if id_venda:
                db.update_sales_forma_pagamento_by_id_venda(id_venda, rotulo, exclude_uid=uid)
                db.delete_pagamentos_venda(id_venda)
                agora_pg = now_brt()
                for p in pagamentos:
                    p.update({
                        "id": uuid.uuid4().hex, "id_venda": id_venda,
                        "data": agora_pg.strftime("%Y-%m-%d"), "hora": agora_pg.strftime("%H:%M"),
                        "usuario": session.get("usuario", ""),
                    })
                db.insert_pagamentos_venda(pagamentos)

            return redirect(url_for("vendas"))

    return render_template(
        "editar_vendas.html",
        venda=venda,
        nome_produto_atual=nome_produto_atual,
        preco_produto_atual=preco_venda_original,
        pode_alterar_preco=pode_alterar_preco,
        produto_tipo_venda=produto_tipo_venda,
        qtd_disponivel_atual=qtd_disponivel_atual,
        total_outros_itens=total_outros_itens,
        pagamentos_atuais=pagamentos_atuais,
        erro=erro,
        tipo=session.get("tipo"),
        permissoes=session.get("permissoes", [])
    )


@app.route("/vendas/produtos_troca")
def vendas_produtos_troca():
    """Lista leve de produtos (pid/nome/valor_venda) pro seletor do modal
    'Trocar Produto' — buscada sob demanda (fetch) só quando o modal é
    aberto, em vez de carregar o estoque inteiro em toda visita a /vendas."""
    if not usuario_logado():
        return jsonify([]), 401
    if session.get("tipo") != "admin" and not tem_permissao("editar_vendas"):
        return jsonify([]), 403
    produtos = [
        {"pid": p.get("pid"), "nome": p.get("nome"), "valor_venda": to_float(p.get("valor_venda", 0))}
        for p in db.get_all_products()
    ]
    return jsonify(produtos)


@app.route("/vendas/trocar/<uid>", methods=["POST"])
def trocar_produto_venda(uid):
    if not usuario_logado():
        return redirect(url_for("login"))
    if session.get("tipo") != "admin" and not tem_permissao("editar_vendas"):
        return redirect(url_for("vendas"))

    venda = db.get_sale(uid)
    if not venda or venda.get("excluida"):
        return redirect(url_for("vendas"))
    if venda.get("fechado"):
        session["_erro_autorizacao"] = "Esta venda já foi fechada no caixa e não pode ser trocada."
        return redirect(url_for("vendas"))

    # Mesmo bloqueio de editar_venda(): item de comanda ainda em aberto não
    # pode ter forma de pagamento definida aqui, só quitando a comanda.
    comanda_vinculada = db.get_command(venda.get("comanda_id")) if venda.get("comanda_id") else None
    if comanda_vinculada and comanda_vinculada.get("status") != "quitada":
        session["_erro_autorizacao"] = "Este item pertence a uma comanda ainda em aberto — não pode trocar produto por aqui até a comanda ser quitada."
        return redirect(url_for("vendas"))

    novo_pid = (request.form.get("novo_pid") or "").strip()
    forma_diferenca = (request.form.get("forma_pagamento_diferenca") or "").strip()

    produto_novo = db.get_product(novo_pid)
    if not produto_novo:
        session["_erro_autorizacao"] = "Produto novo não encontrado."
        return redirect(url_for("vendas"))

    pid_antigo = str(venda.get("produto_pid", "")).strip()
    nome_antigo = venda.get("nome", "")
    preco_antigo = to_float(venda.get("valor_venda", 0))
    qtd = to_int(venda.get("quantidade", 1), 1)

    if pid_antigo == str(produto_novo.get("pid", "")):
        produto_antigo = produto_novo
    else:
        produto_antigo = db.get_product(pid_antigo)

    consumo_antigo = venda.get("consumo_lotes", [])
    local_da_venda = venda.get("local_id")
    if produto_antigo and consumo_antigo:
        if local_da_venda:
            devolver_fifo_local(produto_antigo, consumo_antigo, local_da_venda)
        else:
            devolver_fifo(produto_antigo, consumo_antigo)

    try:
        if local_da_venda:
            custo_total, consumo_novo = consumir_fifo_local(produto_novo, qtd, local_da_venda)
        else:
            custo_total, consumo_novo = consumir_fifo(produto_novo, qtd)
    except ValueError as e:
        # como nenhum db.update_product() é chamado neste caminho, a
        # devolução acima fica só na memória e nunca é salva — nada muda
        # de verdade no estoque quando a troca falha.
        session["_erro_autorizacao"] = str(e)
        return redirect(url_for("vendas"))

    preco_novo = preco_por_local(produto_novo, local_da_venda or LOCAL_PADRAO)
    diferenca = round((preco_novo - preco_antigo) * qtd, 2)
    custo_unit_medio = (custo_total / qtd) if qtd else 0.0

    venda["produto_pid"] = produto_novo.get("pid")
    venda["nome"] = produto_novo.get("nome", venda.get("nome", ""))
    venda["valor_venda"] = preco_novo
    venda["valor_investido"] = round(custo_unit_medio, 4)
    venda["consumo_lotes"] = consumo_novo
    if forma_diferenca:
        venda["forma_pagamento"] = normalizar_pagamento(forma_diferenca)

    db.update_sale(venda)
    db.update_product(produto_novo)
    if produto_antigo and produto_antigo is not produto_novo:
        db.update_product(produto_antigo)

    db.registrar_log(
        session.get("usuario", ""), "venda_trocada",
        f"Trocou produto da venda: {nome_antigo} → {produto_novo.get('nome','')} (diferença: R$ {diferenca:+.2f})",
        now_brt().strftime("%Y-%m-%d"), now_brt().strftime("%H:%M"),
    )
    return redirect(url_for("vendas"))


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
        local_da_venda = venda.get("local_id")
        if local_da_venda:
            devolver_fifo_local(produto, consumo_lotes, local_da_venda)
        else:
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
    margem = calcular_margem(valor_venda, custo)

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
    diag_lotes = _diagnostico_lotes(estoque_data) if session.get("tipo") == "admin" else None
    try:
        giro_alerta = _alerta_giro(estoque_data) if session.get("tipo") == "admin" else []
    except Exception:
        giro_alerta = []  # alerta é só informativo — nunca derruba a tela
    estoque_data.sort(key=lambda p: (
        (p.get("categoria") or "").strip().lower() or "zzz_sem_categoria",
        str(p.get("nome", "")).strip().lower(),
    ))

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
        p["_qtd_por_local"] = _qtd_por_local(p)

    # KPIs sobre todo o estoque
    total_produtos       = len(estoque_data)
    total_estoque_un     = int(sum(float(p.get("quantidade", 0) or 0) for p in estoque_data))
    estoque_baixo_count  = sum(1 for p in estoque_data if p["_status"] in ("BAIXO", "CRÍTICO"))
    valor_total_estoque  = round(sum(
        float(p.get("valor_investido", 0) or 0) * float(p.get("quantidade", 0) or 0)
        for p in estoque_data
    ), 2)

    locais_estoque = db.get_all_locais_estoque()
    kpis_local = {
        l["id"]: sum(p["_qtd_por_local"].get(l["id"], 0.0) for p in estoque_data)
        for l in locais_estoque
    }

    categorias = _categorias_existentes(estoque_data)

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
            return calcular_margem(venda, custo)
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

    # Versão enxuta dos produtos, só com os campos que o modal "Visualizar"
    # (JS de estoque.html) de fato lê — em particular, sem o campo "lotes"
    # (o pesado: todos os lotes de todos os produtos), que esse modal nunca
    # usa. Reduz bastante o JSON embutido na página sem mudar nada visível.
    CAMPOS_MODAL_VISUALIZAR_PRODUTO = (
        "pid", "data", "nome", "categoria", "tipo_controle", "unidade_venda",
        "quantidade", "estoque_minimo", "valor_investido", "valor_venda",
        "conteudo_valor", "conteudo_unidade", "validade", "fornecedor",
        "tipo_conversao", "unidades_por_caixa", "observacoes", "_qtd_por_local",
    )
    estoque_modal = [
        {campo: p.get(campo) for campo in CAMPOS_MODAL_VISUALIZAR_PRODUTO}
        for p in estoque_filtrado
    ]

    return render_template(
        "estoque.html",
        estoque=estoque_filtrado,
        estoque_modal=estoque_modal,
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
        locais_estoque=locais_estoque,
        kpis_local=kpis_local,
        diag_lotes=diag_lotes,
        giro_alerta=giro_alerta,
        msg=request.args.get("msg", ""),
        erro="",
        tipo=session.get("tipo"),
        permissoes=session.get("permissoes", []),
        nomes_sugestoes=nomes_sugestoes,
    )


@app.route("/estoque/transferencia/produto_lotes")
def estoque_transferencia_produto_lotes():
    """Lotes com saldo no local de origem escolhido, pro select lazy-loaded
    da tela de Transferência (mesmo padrão de /vendas/produtos_troca)."""
    if not usuario_logado():
        return jsonify([]), 401
    if not tem_permissao("ver_estoque"):
        return jsonify([]), 403

    pid = (request.args.get("pid") or "").strip()
    origem = (request.args.get("origem") or "").strip()
    produto = db.get_product(pid)
    if not produto or not origem:
        return jsonify([])

    lotes = []
    for l in produto.get("lotes", []):
        qtd_disp = to_float(_dist_lote(l).get(origem, 0))
        if qtd_disp > 0:
            lotes.append({
                "lote_id": l.get("id"),
                "data": normalizar_data_str(l.get("data")),
                "qtd_disponivel": qtd_disp,
                "nome_lote": l.get("nome_lote") or "",
            })
    return jsonify(lotes)


@app.route("/estoque/transferencia", methods=["GET", "POST"])
def estoque_transferencia():
    if not usuario_logado():
        return redirect(url_for("login"))
    if not tem_permissao("ver_estoque"):
        return redirect(url_for("vendas"))

    erro = ""
    sucesso = request.args.get("sucesso") == "1"

    if request.method == "POST":
        pid = (request.form.get("produto_pid") or "").strip()
        lote_id = (request.form.get("lote_id") or "").strip()
        origem = (request.form.get("origem") or "").strip()
        destino = (request.form.get("destino") or "").strip()
        quantidade = to_float(request.form.get("quantidade"), 0)

        produto = db.get_product(pid)
        if not produto:
            erro = "Produto não encontrado."
        elif not origem or not destino:
            erro = "Selecione origem e destino."
        elif origem == destino:
            erro = "Origem e destino não podem ser o mesmo local."
        elif quantidade <= 0:
            erro = "Quantidade inválida."
        else:
            lote = next((l for l in produto.get("lotes", []) if str(l.get("id")) == lote_id), None)
            if not lote:
                erro = "Lote não encontrado."
            else:
                dist = dict(_dist_lote(lote))
                disponivel = to_float(dist.get(origem, 0))
                if quantidade > disponivel:
                    erro = f"Quantidade insuficiente no local de origem. Disponível: {disponivel}."
                else:
                    dist[origem] = round(disponivel - quantidade, 4)
                    dist[destino] = round(to_float(dist.get(destino, 0)) + quantidade, 4)
                    lote["distribuicao"] = dist
                    db.update_product(produto)

                    hoje = now_brt().strftime("%Y-%m-%d")
                    hora = now_brt().strftime("%H:%M")
                    usuario = session.get("usuario", "")
                    db.insert_transferencia_estoque({
                        "id": uuid.uuid4().hex,
                        "data": hoje,
                        "hora": hora,
                        "usuario": usuario,
                        "pid": pid,
                        "produto_nome": produto.get("nome", ""),
                        "lote_id": lote_id,
                        "origem": origem,
                        "destino": destino,
                        "quantidade": quantidade,
                        "criado_em": f"{hoje} {hora}",
                    })
                    db.registrar_log(
                        usuario, "transferencia_estoque",
                        f"Transferiu {quantidade} de '{produto.get('nome','')}' de {origem} para {destino}",
                        hoje, hora,
                    )
                    return redirect(url_for("estoque_transferencia") + "?sucesso=1")

    estoque_data = db.get_all_products()
    estoque_data.sort(key=lambda p: str(p.get("nome", "")).strip().lower())
    transferencias = db.get_all_transferencias_estoque()
    locais_estoque = db.get_all_locais_estoque()
    locais_nomes_map = {l["id"]: l["nome"] for l in locais_estoque}

    pode_ver_custo = session.get("tipo") == "admin" or "ver_custo_produtos" in session.get("permissoes", [])
    produtos_busca = []
    for p in estoque_data:
        item = {
            "pid": p.get("pid"),
            "nome": p.get("nome", ""),
            "codigo": p.get("codigo", ""),
            "categoria": p.get("categoria", ""),
            "quantidade": to_float(p.get("quantidade", 0)),
            "qtd_por_local": _qtd_por_local(p),
        }
        if pode_ver_custo:
            item["valor_investido"] = to_float(p.get("valor_investido", 0))
            item["valor_venda"] = to_float(p.get("valor_venda", 0))
        produtos_busca.append(item)

    return render_template(
        "estoque_transferencia.html",
        estoque=estoque_data,
        produtos_busca=produtos_busca,
        locais_estoque=locais_estoque,
        locais_nomes_map=locais_nomes_map,
        local_padrao=LOCAL_PADRAO,
        transferencias=transferencias,
        erro=erro,
        sucesso=sucesso,
        tipo=session.get("tipo"),
        permissoes=session.get("permissoes", []),
        usuario=session.get("usuario", ""),
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
        local_id      = (request.form.get("local_id") or LOCAL_PADRAO).strip()

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

        permite_investidores = session.get("tipo") == "admin" or tem_permissao("editar_investidores")

        if not nome:
            erro = "Informe o nome do produto."
        elif quantidade <= 0:
            erro = "Quantidade inválida."
        elif valor_investido <= 0:
            erro = "Informe o custo do produto."
        else:
            if permite_investidores:
                participantes_lote, erro = _resolver_participantes_lote(request.form)
            else:
                participantes_lote = [{"investidor_id": db.get_or_create_dono_investidor()["id"], "percentual": 100.0}]

        if not erro:
            nome_key = nome.lower()
            estoque_data = db.get_all_products()
            existente = next(
                (p for p in estoque_data if str(p.get("nome", "")).strip().lower() == nome_key), None
            )

            precos_local_form = ler_precos_local_form(request.form, valor_venda, {local_id})
            if precos_local_form and not db.precos_local_disponivel():
                erro = MSG_SQL_PRECOS_LOCAL
        if not erro:
            if existente:
                existente["data"] = data
                existente["valor_venda"] = valor_venda
                if precos_local_form:
                    existente["precos_local"] = precos_local_form
                existente.update(extras)
                try:
                    entrada_estoque_lote(existente, data_entrada, quantidade, valor_investido, usuario=session.get("usuario", ""), local_id=local_id)
                    db.update_product(existente)
                    db.registrar_log(
                        session.get("usuario", ""), "estoque_alterado",
                        f"Alterou estoque: {nome} (+{quantidade})",
                        now_brt().strftime("%Y-%m-%d"), now_brt().strftime("%H:%M"),
                    )
                    _registrar_lote_investimento(
                        existente["lotes"][-1]["id"], existente["pid"], nome, data_entrada,
                        quantidade, valor_investido, extras.get("fornecedor"), participantes_lote,
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
                if precos_local_form:
                    novo["precos_local"] = precos_local_form
                try:
                    entrada_estoque_lote(novo, data_entrada, quantidade, valor_investido, usuario=session.get("usuario", ""), local_id=local_id)
                except ValueError as e:
                    erro = str(e)

                if not erro:
                    db.insert_product(novo)
                    db.registrar_log(
                        session.get("usuario", ""), "produto_criado", f"Cadastrou produto: {nome}",
                        now_brt().strftime("%Y-%m-%d"), now_brt().strftime("%H:%M"),
                    )
                    _registrar_lote_investimento(
                        novo["lotes"][-1]["id"], novo["pid"], nome, data_entrada,
                        quantidade, valor_investido, extras.get("fornecedor"), participantes_lote,
                    )

            if not erro:
                return redirect(url_for("estoque"))

    categorias = db.get_all_categorias()
    permite_investidores = session.get("tipo") == "admin" or tem_permissao("editar_investidores")
    investidores_ativos = [i for i in db.get_all_investidores() if i.get("status") == "ativo"] if permite_investidores else []

    return render_template(
        "estoque_novo.html",
        erro=erro,
        hoje=today_brt().isoformat(),
        fornecedores=_fornecedores_para_sugestao(),
        pode_cadastrar_fornecedor=_pode_gerenciar_fornecedores(),
        categorias=categorias,
        permite_investidores=permite_investidores,
        investidores_ativos=investidores_ativos,
        locais_estoque=db.get_all_locais_estoque(),
        local_padrao=LOCAL_PADRAO,
        tipo=session.get("tipo"),
        permissoes=session.get("permissoes", []),
    )


# ================== CATEGORIAS DE PRODUTO ==================
TIPOS_CATEGORIA = ("bebida", "comida", "insumo")
TIPOS_CATEGORIA_LABEL = {"bebida": "Bebida", "comida": "Comida", "insumo": "Insumo"}


@app.route("/estoque/categorias", methods=["GET", "POST"])
def estoque_categorias():
    if not usuario_logado():
        return redirect(url_for("login"))
    if not tem_permissao("cadastrar_produto"):
        return redirect(url_for("vendas"))

    erro = ""

    if request.method == "POST":
        acao = (request.form.get("acao") or "").strip()
        nome = (request.form.get("nome") or "").strip()
        tipo = (request.form.get("tipo") or "").strip()
        hoje = now_brt().strftime("%Y-%m-%d")
        hora = now_brt().strftime("%H:%M")

        categorias_atuais = db.get_all_categorias()

        if acao == "criar":
            if not nome or tipo not in TIPOS_CATEGORIA:
                erro = "Preencha o nome e escolha um tipo válido."
            elif any(c.get("nome", "").strip().lower() == nome.lower() for c in categorias_atuais):
                erro = "Já existe uma categoria com esse nome."
            else:
                try:
                    db.insert_categoria(nome, tipo)
                    db.registrar_log(
                        session.get("usuario", ""), "categoria_criada",
                        f"Criou categoria: {nome} ({TIPOS_CATEGORIA_LABEL.get(tipo, tipo)})", hoje, hora,
                    )
                except RuntimeError as e:
                    erro = str(e)

        elif acao == "editar_tipo":
            if not nome or tipo not in TIPOS_CATEGORIA:
                erro = "Dados inválidos para atualizar a categoria."
            else:
                try:
                    db.update_categoria_tipo(nome, tipo)
                    db.registrar_log(
                        session.get("usuario", ""), "categoria_editada",
                        f"Alterou tipo da categoria {nome} para {TIPOS_CATEGORIA_LABEL.get(tipo, tipo)}", hoje, hora,
                    )
                except RuntimeError as e:
                    erro = str(e)

        elif acao == "excluir":
            if not nome:
                erro = "Categoria inválida."
            else:
                try:
                    db.delete_categoria(nome)
                    db.registrar_log(
                        session.get("usuario", ""), "categoria_excluida",
                        f"Excluiu categoria: {nome}", hoje, hora,
                    )
                except RuntimeError as e:
                    erro = str(e)

        if not erro:
            return redirect(url_for("estoque_categorias") + "?salvo=1")

    categorias = sorted(db.get_all_categorias(), key=lambda c: (c.get("nome") or "").lower())
    contagem = defaultdict(int)
    for p in db.get_all_products():
        c = (p.get("categoria") or "").strip()
        if c:
            contagem[c] += 1

    return render_template(
        "estoque_categorias.html",
        categorias=categorias,
        contagem=contagem,
        tipos_categoria=TIPOS_CATEGORIA,
        tipos_categoria_label=TIPOS_CATEGORIA_LABEL,
        erro=erro,
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

        # Preço do Escritório: só pra local onde o produto tem lote registrado.
        precos_local_novo = ler_precos_local_form(request.form, valor_venda, locais_com_lote(produto)) if pode_preco else None

        if not nome:
            erro = "Informe o nome do produto."
        elif valor_venda <= 0:
            erro = "Preço de venda inválido."
        elif precos_local_novo and not db.precos_local_disponivel():
            erro = MSG_SQL_PRECOS_LOCAL
        else:
            produto["nome"]        = nome
            produto["data"]        = data
            produto["valor_venda"] = valor_venda
            if precos_local_novo is not None:
                produto["precos_local"] = precos_local_novo
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

    categorias = db.get_all_categorias()
    fornecedores_lista = _fornecedores_para_sugestao()

    return render_template(
        "editar_produto.html",
        locais_estoque=db.get_all_locais_estoque(),
        locais_lote=locais_com_lote(produto),
        local_padrao=LOCAL_PADRAO,
        fornecedores=fornecedores_lista,
        fornecedor_atual=_fornecedor_cadastrado(produto.get("fornecedor"), fornecedores_lista),
        pode_cadastrar_fornecedor=_pode_gerenciar_fornecedores(),
        produto=produto,
        erro=erro,
        categorias=categorias,
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

    erro = request.args.get("erro", "") if request.method == "GET" else ""

    if request.method == "POST":
        forma_entrada    = request.form.get("forma_entrada", "unidade")
        data             = (request.form.get("data") or "").strip()
        nome_lote        = (request.form.get("nome_lote") or "").strip() or None
        fornecedor_lote  = request.form.get("fornecedor") or None
        validade_lote    = request.form.get("validade") or None
        tipo_custo       = request.form.get("tipo_custo", "unitario")
        local_id         = (request.form.get("local_id") or LOCAL_PADRAO).strip()

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

        permite_investidores = session.get("tipo") == "admin" or tem_permissao("editar_investidores")

        if qtd <= 0:
            erro = "Quantidade inválida."
        elif custo_unit <= 0:
            erro = "Custo unitário inválido."
        else:
            if permite_investidores:
                participantes_lote, erro = _resolver_participantes_lote(request.form)
            else:
                participantes_lote = [{"investidor_id": db.get_or_create_dono_investidor()["id"], "percentual": 100.0}]

        if not erro and _lote_repetido_recente(produto, qtd, custo_unit, data, session.get("usuario", "")):
            erro = MSG_LOTE_REPETIDO

        if not erro:
            conteudo_val  = to_float(request.form.get("conteudo_valor"), None) if request.form.get("conteudo_valor") else None
            conteudo_unit = request.form.get("conteudo_unidade") or None
            lote_id = uuid.uuid4().hex
            lote = {
                "id":             lote_id,
                "data":           normalizar_data_str(data),
                "qtd":            qtd,
                "qtd_entrada":    qtd,
                "custo_unit":     custo_unit,
                "forma_entrada":  forma_entrada,
                "qtd_informada":  qtd_informada,
                "criado_por":     session.get("usuario", ""),
                "criado_em":      now_brt().isoformat(timespec="seconds"),
                "distribuicao":   {local_id: qtd},
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
            _registrar_lote_investimento(
                lote_id, pid, produto.get("nome", ""), data, qtd, custo_unit,
                fornecedor_lote, participantes_lote,
            )
            _log_lote("lote_adicionado", produto, f"Adicionou lote de {qtd:g} un. a R$ {custo_unit:.2f} "
                      f"(entrada {normalizar_data_str(data)}, {local_id})",
                      {"lote_id": lote_id, "qtd": qtd, "custo_unit": custo_unit, "local_id": local_id,
                       "data": normalizar_data_str(data), "fornecedor": fornecedor_lote})
            return redirect(url_for("estoque_produto", pid=pid))

    # As buscas abaixo não dependem uma da outra — rodam ao mesmo tempo
    # (cada ida ao Supabase custa ~0,2 s; em sequência somavam ~1,5 s).
    permite_investidores = session.get("tipo") == "admin" or tem_permissao("editar_investidores")
    tarefas_produto = {
        "fornecedores": _fornecedores_para_sugestao,
        "pacotes_usando": lambda: db.bar_get_packages_using_product(pid),
        "vendas_produto": lambda: db.get_sales_by_product(pid),
        "investidores": db.get_all_investidores if permite_investidores else (lambda: []),
        "locais_estoque": db.get_all_locais_estoque,
        "entradas": db.get_all_lote_investimentos,
        "ajustes": lambda: db.get_ajustes_estoque(pid),
    }
    with ThreadPoolExecutor(max_workers=len(tarefas_produto)) as executor:
        futuros_produto = {nome: executor.submit(fn) for nome, fn in tarefas_produto.items()}
        res_produto = {nome: fut.result() for nome, fut in futuros_produto.items()}

    fornecedores_lista = res_produto["fornecedores"]
    lotes = produto.get("lotes", [])
    if isinstance(lotes, list):
        lotes = sorted(lotes, key=lambda l: str(l.get("data", "")))
    else:
        lotes = []
    # Quanto entrou em cada lote (registro de entrada); lote antigo sem
    # registro usa a quantidade informada no próprio lote.
    entrada_por_lote = {li["id"]: to_float(li.get("quantidade")) for li in res_produto["entradas"] if li.get("pid") == pid}
    proximo_por_local = {}  # PEPS: lote mais antigo com saldo em cada local = o próximo a sair
    for l in lotes:
        l["_distribuicao_resolvida"] = _dist_lote(l)
        if l.get("id") in entrada_por_lote:
            l["_entrou"] = entrada_por_lote[l["id"]]
        else:
            qi = to_float(l.get("qtd_informada", l.get("qtd", 0)))
            l["_entrou"] = qi * to_int(l.get("unidades_por_caixa") or 1) if l.get("forma_entrada") == "caixa" else qi
        l["_entrou"] = max(l["_entrou"], to_float(l.get("qtd", 0)))
        l["_pct_vendido"] = round((1 - to_float(l.get("qtd", 0)) / l["_entrou"]) * 100) if l["_entrou"] > 0 else 0
        l["_saindo_em"] = []
        for loc, q in l["_distribuicao_resolvida"].items():
            if to_float(q) > 0 and loc not in proximo_por_local:
                proximo_por_local[loc] = True
                l["_saindo_em"].append(loc)
        # fornecedor do lote resolvido pro cadastro (pré-seleção na edição)
        l["_forn_sel"], l["_forn_extra"] = _fornecedor_cadastrado(l.get("fornecedor"), fornecedores_lista)

    pacotes_usando = res_produto["pacotes_usando"]

    saidas = sorted(
        [s for s in res_produto["vendas_produto"] if s.get("produto_tipo") == "estoque"],
        key=lambda s: s.get("data", ""),
        reverse=True
    )

    investidores_ativos = [i for i in res_produto["investidores"] if i.get("status") == "ativo"]
    locais_estoque = res_produto["locais_estoque"]
    locais_nomes_map = {l["id"]: l["nome"] for l in locais_estoque}

    return render_template(
        "estoque_produto.html",
        ajustes_estoque=res_produto["ajustes"],
        motivos_ajuste=MOTIVOS_AJUSTE,
        saldo_por_local=_qtd_por_local(produto),
        pode_ajustar=tem_permissao("editar_estoque"),
        msg=request.args.get("msg", ""),
        fornecedores_sugestao=fornecedores_lista,
        fornecedor_padrao_lote=_fornecedor_cadastrado(produto.get("fornecedor"), fornecedores_lista)[0],
        pode_cadastrar_fornecedor=_pode_gerenciar_fornecedores(),
        produto=produto,
        lotes=lotes,
        saidas=saidas,
        erro=erro,
        hoje=today_brt().isoformat(),
        pacotes_usando=pacotes_usando,
        permite_investidores=permite_investidores,
        investidores_ativos=investidores_ativos,
        locais_estoque=locais_estoque,
        locais_nomes_map=locais_nomes_map,
        local_padrao=LOCAL_PADRAO,
        tipo=session.get("tipo"),
        permissoes=session.get("permissoes", [])
    )


MOTIVOS_AJUSTE = {
    "quebra": "Quebra / perda",
    "consumo": "Consumo interno",
    "venda_nao_registrada": "Venda não registrada",
    "entrada_a_mais": "Entrada lançada a mais",
    "contagem": "Contagem / correção",
}


def _custo_medio_produto(produto: dict) -> float:
    lotes = [l for l in produto.get("lotes") or [] if to_float(l.get("qtd")) > 0]
    qtd = sum(to_float(l.get("qtd")) for l in lotes)
    if qtd > 0:
        return round(sum(to_float(l.get("qtd")) * to_float(l.get("custo_unit")) for l in lotes) / qtd, 4)
    return to_float(produto.get("valor_investido"), 0.0)


@app.route("/estoque/produto/<pid>/ajuste", methods=["POST"])
def estoque_ajuste_contagem(pid):
    """Acerta o estoque com a contagem física, local por local. Contou menos:
    baixa a diferença por PEPS (lotes mais antigos primeiro, como uma venda).
    Contou mais: cria um lote de ajuste com o custo médio. Tudo vai pro
    histórico com motivo — não conta como venda nem como compra."""
    if not usuario_logado():
        return redirect(url_for("login"))
    if not tem_permissao("editar_estoque"):
        return redirect(url_for("estoque_produto", pid=pid, erro="Você não tem permissão para ajustar o estoque."))
    produto = db.get_product(pid)
    if not produto:
        return redirect(url_for("estoque"))

    motivo = (request.form.get("motivo") or "").strip()
    if motivo not in MOTIVOS_AJUSTE:
        return redirect(url_for("estoque_produto", pid=pid, erro="Escolha o motivo do ajuste."))
    obs = (request.form.get("observacao") or "").strip()[:300]
    locais = db.get_all_locais_estoque()
    saldo = _qtd_por_local(produto)
    usuario = session.get("usuario", "")
    hoje = now_brt().strftime("%Y-%m-%d")
    ajustes = []
    try:
        for l in locais:
            bruto = (request.form.get(f"contado_{l['id']}") or "").strip().replace(",", ".")
            if bruto == "":
                continue  # local não contado
            contado = to_float(bruto, -1)
            if contado < 0:
                raise ValueError(f"Quantidade contada inválida no(a) {l['nome']}.")
            antes = round(to_float(saldo.get(l["id"], 0)), 4)
            diferenca = round(contado - antes, 4)
            if abs(diferenca) < 0.0005:
                continue
            custo_total, lotes_afetados = 0.0, []
            if diferenca < 0:
                custo_total, consumo = consumir_fifo_local(produto, -diferenca, l["id"])
                lotes_afetados = [{"lote_id": c.get("lote_id"), "data_lote": c.get("data_lote"), "qtd": c.get("qtd")} for c in consumo]
            else:
                custo_unit = _custo_medio_produto(produto) or 0.01
                lote_id = uuid.uuid4().hex
                produto.setdefault("lotes", []).append({
                    "id": lote_id, "data": hoje, "qtd": diferenca, "qtd_entrada": diferenca,
                    "custo_unit": custo_unit, "criado_por": usuario,
                    "criado_em": now_brt().isoformat(timespec="seconds"),
                    "distribuicao": {l["id"]: diferenca}, "nome_lote": "Ajuste de contagem", "ajuste": True,
                })
                custo_total = round(diferenca * custo_unit, 2)
                lotes_afetados = [{"lote_id": lote_id, "data_lote": hoje, "qtd": diferenca}]
            ajustes.append({"local": l, "antes": antes, "contado": contado, "diferenca": diferenca,
                            "custo": round(custo_total, 2), "lotes": lotes_afetados})
    except ValueError as e:
        return redirect(url_for("estoque_produto", pid=pid, erro=str(e)))

    if not ajustes:
        return redirect(url_for("estoque_produto", pid=pid, msg="Nada a ajustar: a contagem bate com o sistema."))

    atualizar_campos_derivados_estoque(produto)
    db.update_product(produto)
    for a in ajustes:
        sinal = "−" if a["diferenca"] < 0 else "+"
        db.registrar_log(
            usuario, "estoque_ajuste",
            f"{produto.get('nome', '')}: ajuste no(a) {a['local']['nome']} {a['antes']:g} → {a['contado']:g} "
            f"({sinal}{abs(a['diferenca']):g} un., R$ {a['custo']:.2f}) — {MOTIVOS_AJUSTE[motivo]}"
            + (f" — {obs}" if obs else ""),
            now_brt().strftime("%Y-%m-%d"), now_brt().strftime("%H:%M"),
            detalhes={"pid": pid, "local_id": a["local"]["id"], "antes": a["antes"], "contado": a["contado"],
                      "diferenca": a["diferenca"], "custo": a["custo"], "motivo": motivo, "observacao": obs,
                      "lotes": a["lotes"]},
        )
    resumo = "; ".join(f"{a['local']['nome']}: {a['antes']:g} → {a['contado']:g}" for a in ajustes)
    return redirect(url_for("estoque_produto", pid=pid, msg=f"Estoque ajustado ({resumo})."))


@app.route("/estoque/reparar-lotes", methods=["POST"])
def estoque_reparar_lotes():
    """Correção única (admin): junta lotes repetidos e acerta o saldo por
    local de cada lote pra bater com a quantidade."""
    if not usuario_logado() or session.get("tipo") != "admin":
        return redirect(url_for("estoque"))
    total = {"produtos": 0, "duplicados": 0, "ajustados": 0, "restos": 0, "sem_id": 0}
    nomes = []
    for produto in db.get_all_products():
        r = _reparar_lotes_produto(produto)
        if any(r.values()):
            db.update_product(produto)
            total["produtos"] += 1
            for k in ("duplicados", "ajustados", "restos", "sem_id"):
                total[k] += r[k]
            nomes.append(produto.get("nome", ""))
    db.registrar_log(
        session.get("usuario", ""), "estoque_lotes_reparados",
        f"Corrigiu lotes de {total['produtos']} produto(s): {total['duplicados']} lote(s) repetido(s) unido(s), "
        f"{total['ajustados']} saldo(s) por local ajustado(s), {total['restos']} resto(s) de arredondamento removido(s), "
        f"{total['sem_id']} lote(s) sem identificação identificado(s). "
        f"Produtos: {', '.join(nomes)[:900]}",
        now_brt().strftime("%Y-%m-%d"), now_brt().strftime("%H:%M"),
    )
    return redirect(url_for("estoque", msg=f"Lotes corrigidos em {total['produtos']} produto(s): "
                                            f"{total['duplicados']} repetido(s) unido(s), {total['ajustados']} saldo(s) por local ajustado(s) "
                                            f", {total['restos']} resto(s) de arredondamento removido(s) "
                                            f"e {total['sem_id']} lote(s) antigo(s) identificado(s)."))


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
            lote_id_removido = lote_alvo.get("id")
            produto["lotes"] = [l for l in lotes if l is not lote_alvo]
            atualizar_campos_derivados_estoque(produto)
            db.update_product(produto)
            _log_lote("lote_removido", produto,
                      f"Removeu lote de {normalizar_data_str(lote_alvo.get('data'))} com {to_float(lote_alvo.get('qtd')):g} un. "
                      f"a R$ {to_float(lote_alvo.get('custo_unit')):.2f}",
                      {"lote_id": lote_id_removido, "qtd": to_float(lote_alvo.get("qtd")),
                       "custo_unit": to_float(lote_alvo.get("custo_unit")), "distribuicao": _dist_lote(lote_alvo)})
            if lote_id_removido:
                try:
                    db.marcar_lote_investimento_removido(lote_id_removido)
                except RuntimeError:
                    pass
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
                # só mexe no fornecedor se o campo veio no formulário
                muda_fornecedor = "fornecedor" in request.form
                novo_fornecedor = (request.form.get("fornecedor") or "").strip()
                if nova_qtd > 0 and novo_custo > 0:
                    antes_lote = {"data": lote_alvo.get("data"), "qtd": to_float(lote_alvo.get("qtd")),
                                  "custo_unit": to_float(lote_alvo.get("custo_unit")), "fornecedor": lote_alvo.get("fornecedor")}
                    for l in produto["lotes"]:
                        if l is lote_alvo:
                            l["data"]      = normalizar_data_str(nova_data)
                            l["qtd"]       = nova_qtd
                            _ajustar_dist_para_qtd(l)  # saldo por local acompanha a nova quantidade
                            l["custo_unit"] = novo_custo
                            if novo_nome_lote:
                                l["nome_lote"] = novo_nome_lote
                            else:
                                l.pop("nome_lote", None)
                            if muda_fornecedor:
                                if novo_fornecedor:
                                    l["fornecedor"] = novo_fornecedor
                                else:
                                    l.pop("fornecedor", None)
                            break
                    atualizar_campos_derivados_estoque(produto)
                    db.update_product(produto)
                    depois_lote = {"data": lote_alvo.get("data"), "qtd": to_float(lote_alvo.get("qtd")),
                                   "custo_unit": to_float(lote_alvo.get("custo_unit")), "fornecedor": lote_alvo.get("fornecedor")}
                    mudancas = [f"{k}: {antes_lote[k]} → {depois_lote[k]}" for k in antes_lote if antes_lote[k] != depois_lote[k]]
                    if mudancas:
                        _log_lote("lote_editado", produto,
                                  f"Editou lote de {normalizar_data_str(antes_lote['data'])} ({'; '.join(mudancas)})",
                                  {"lote_id": lote_alvo.get("id"), "antes": antes_lote, "depois": depois_lote})
                    # Mantém o ledger de investimento (Financeiro, Investidores,
                    # Fornecedores) igual ao lote editado — antes ficava com os
                    # valores da criação.
                    if lote_alvo.get("id"):
                        try:
                            campos = {
                                "data_entrada": lote_alvo["data"],
                                "quantidade": float(nova_qtd),
                                "custo_unit": novo_custo,
                                "valor_total": round(nova_qtd * novo_custo, 2),
                            }
                            if muda_fornecedor:
                                campos["fornecedor"] = novo_fornecedor
                            db.update_lote_investimento(lote_alvo["id"], campos)
                        except RuntimeError:
                            pass
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
        caixa_aberto_cv = db.get_caixa_aberto(LOCAL_PADRAO)
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
                    "valor_venda": preco_por_local(produto, LOCAL_PADRAO),
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
                    codigo_venda = db.gerar_codigo_rastreio("V", data or now_brt().strftime("%Y-%m-%d"))
                    for venda in novas_vendas:
                        venda["caixa_id"] = caixa_id_atual
                        venda["codigo"] = codigo_venda
                    db.insert_sales(novas_vendas)
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
        estoque=_com_qtd_por_local(estoque),
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

    # As 6 buscas abaixo não dependem uma da outra — rodam em paralelo em vez
    # de uma atrás da outra (mesma ideia já aplicada em vendas()/comandas()/
    # financeiro()). Essa é a tela mais usada durante o expediente.
    tarefas_cvl = {
        "estoque": db.get_all_products,
        "comps_por_produto": db.bar_get_all_components_grouped,
        "bar_produtos": db.bar_get_all_products,
        "caixa_aberto_padrao": lambda: db.get_caixa_aberto(LOCAL_PADRAO),
        "comandas_abertas_raw": lambda: db.get_comandas_abertas_resumo(["aberta", "parcial"]),
        "locais_estoque": db.get_all_locais_estoque,
    }
    with ThreadPoolExecutor(max_workers=len(tarefas_cvl)) as executor:
        futuros_cvl = {nome: executor.submit(fn) for nome, fn in tarefas_cvl.items()}
        estoque = futuros_cvl["estoque"].result()
        comps_por_produto = futuros_cvl["comps_por_produto"].result()
        bar_produtos_raw = futuros_cvl["bar_produtos"].result()
        try:
            caixa_aberto = futuros_cvl["caixa_aberto_padrao"].result()
        except Exception:
            caixa_aberto = None
        comandas_abertas_raw = futuros_cvl["comandas_abertas_raw"].result()
        locais_estoque_cvl = futuros_cvl["locais_estoque"].result()

    # Status do caixa de cada local pro menu de engrenagem — sem isso o menu
    # só enxergava o caixa da Loja e o "Fechar Caixa" nunca chegava no do
    # Escritório.
    def _caixa_do_local(lid):
        try:
            return db.get_caixa_aberto(lid)
        except Exception:
            return None
    with ThreadPoolExecutor(max_workers=max(1, len(locais_estoque_cvl))) as executor:
        caixas_menu = list(executor.map(
            lambda l: {"id": l["id"], "nome": l["nome"], "aberto": bool(_caixa_do_local(l["id"]))},
            locais_estoque_cvl))

    drinks = []
    for d in bar_produtos_raw:
        if d["type"] != "composite":
            continue
        comps = comps_por_produto.get(d["id"], [])
        d["componentes"] = [
            {"component_id": c.get("component", {}).get("id", ""),
             "nome": c.get("component", {}).get("name", "?"),
             "quantidade": c.get("quantity", 0),
             "unidade_uso": c.get("unidade_uso") or "UN"}
            for c in comps
        ]
        drinks.append(d)
    erro = ""

    if request.method == "POST":
        data = request.form.get("data", "")
        forma_pagamento = request.form.get("forma_pagamento", "")
        valor_dinheiro = to_float(request.form.get("valor_dinheiro", "0"))
        valor_pix = to_float(request.form.get("valor_pix", "0"))
        valor_cartao = to_float(request.form.get("valor_cartao", "0"))
        local_id = (request.form.get("local_id") or LOCAL_PADRAO).strip()

        try:
            caixa_aberto = db.get_caixa_aberto(local_id)
        except Exception:
            caixa_aberto = None
        if not caixa_aberto:
            nome_local = next((l["nome"] for l in db.get_all_locais_estoque() if l["id"] == local_id), local_id)
            erro = f"⚠️ Nenhum caixa aberto em {nome_local}. Abra o caixa antes de registrar vendas."

        pids            = request.form.getlist("produto_pid[]")
        qtds            = request.form.getlist("quantidade[]")
        tipos           = request.form.getlist("produto_tipo[]")
        vpcs            = request.form.getlist("vender_por_conteudo[]")
        precos          = request.form.getlist("preco_venda[]")
        substituicoes   = request.form.getlist("substituicoes[]")
        ignorar_estoque = request.form.getlist("ignorar_estoque[]")

        # garante que tipos, vpcs, precos, substituicoes e ignorar_estoque têm o mesmo tamanho que pids
        while len(tipos) < len(pids):
            tipos.append("estoque")
        while len(vpcs) < len(pids):
            vpcs.append("0")
        while len(precos) < len(pids):
            precos.append("")
        while len(substituicoes) < len(pids):
            substituicoes.append("")
        while len(ignorar_estoque) < len(pids):
            ignorar_estoque.append("0")

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
            # Um único objeto por produto pra venda inteira: ingrediente de
            # pacote e o mesmo produto vendido avulso no carrinho baixam do
            # mesmo saldo, e cada produto é gravado uma vez só no fim (antes
            # o pacote gravava na hora e a gravação final apagava essa baixa).
            produtos_cache = {str(p.get("pid", "")): p for p in estoque}

            for pid, qtd_raw, tipo, vpc_flag, preco_raw, subst_raw, ignorar_raw in zip(pids, qtds, tipos, vpcs, precos, substituicoes, ignorar_estoque):
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
                    # Receita customizada só nessa venda (ex: trocou o gin do
                    # combo) — vem como JSON de [{component_id, quantity,
                    # unidade_uso}], montado em cadastrar_venda_lote.html.
                    componentes_override = None
                    if subst_raw:
                        try:
                            componentes_override = json.loads(subst_raw)
                        except (ValueError, TypeError):
                            componentes_override = None
                    try:
                        custo_total, consumo = bar_calc_drink_cost(
                            pid, qtd, componentes_override=componentes_override,
                            ignorar_falta=(ignorar_raw == "1"), local_id=local_id,
                            produtos_cache=produtos_cache, salvar=False,
                        )
                    except Exception as e:
                        erro = str(e)
                        break
                    for comp_id in {c.get("component_id") for c in consumo if c.get("lote_id") is not None or c.get("data_lote")}:
                        comp_prod = produtos_cache.get(comp_id)
                        if comp_prod is not None and not any(m is comp_prod for m in produtos_modificados):
                            produtos_modificados.append(comp_prod)
                    custo_unit = round(custo_total / qtd, 4) if qtd else 0.0
                    preco_catalogo = preco_por_local(bar_prod, local_id, "sale_price")
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
                    ignorar_falta = ignorar_raw == "1"
                    try:
                        if vpc and cv > 1:
                            qtd_estoque = qtd / cv
                            custo_total, consumo = consumir_fifo_local(produto, qtd_estoque, local_id, ignorar_falta=ignorar_falta)
                        else:
                            custo_total, consumo = consumir_fifo_local(produto, qtd, local_id, ignorar_falta=ignorar_falta)
                        if not any(m is produto for m in produtos_modificados):
                            produtos_modificados.append(produto)
                    except ValueError as e:
                        erro = str(e)
                        break
                    custo_unit = round(custo_total / qtd, 4) if qtd else 0.0
                    preco_base = preco_por_local(produto, local_id)
                    preco_catalogo = preco_base / cv if (vpc and cv > 1) else preco_base
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
                        "local_id": local_id,
                    })

            if not erro and not novas_vendas:
                erro = "Adicione pelo menos 1 produto com quantidade válida."

            total_venda = 0.0
            if not erro:
                total_venda = sum(to_float(v.get("valor_venda", 0)) * to_int(v.get("quantidade", 0)) for v in novas_vendas)
                if round(valor_dinheiro + valor_pix + valor_cartao, 2) + 0.01 < total_venda:
                    erro = "A soma dos valores de pagamento é menor que o total da venda."

            if not erro:
                caixa_id_atual = caixa_aberto["id"] if caixa_aberto else None
                try:
                    codigo_venda = db.gerar_codigo_rastreio("V", now_brt().strftime("%Y-%m-%d"))
                    for venda in novas_vendas:
                        venda["caixa_id"] = caixa_id_atual
                        venda["codigo"] = codigo_venda
                    db.insert_sales(novas_vendas)
                    for produto in produtos_modificados:
                        db.update_product(produto)

                    pagamentos = _distribuir_pagamentos(total_venda, valor_dinheiro, valor_pix, valor_cartao)
                    agora_pg = now_brt()
                    for p in pagamentos:
                        p.update({
                            "id": uuid.uuid4().hex, "id_venda": id_venda,
                            "data": agora_pg.strftime("%Y-%m-%d"), "hora": agora_pg.strftime("%H:%M"),
                            "usuario": session.get("usuario", ""),
                        })
                    db.insert_pagamentos_venda(pagamentos)
                except RuntimeError as e:
                    erro = str(e)
                else:
                    desc = novas_vendas[0]["nome"] if len(novas_vendas) == 1 else f"{len(novas_vendas)} itens"
                    db.registrar_log(
                        session.get("usuario", ""), "venda_criada",
                        f"Criou venda: {desc} (R$ {total_venda:.2f})",
                        now_brt().strftime("%Y-%m-%d"), now_brt().strftime("%H:%M"),
                    )
                    return redirect(url_for("cadastrar_venda_lote") + "?sucesso=1")

    hoje = now_brt().strftime("%Y-%m-%d")
    categorias = sorted(set(_categorias_existentes(estoque)) | {
        (d.get("category") or "").strip() for d in drinks if d.get("category")
    })
    comandas_abertas = [
        {"cid": c["cid"], "nome": c.get("nome", ""), "mesa": c.get("mesa", ""),
         "status": c.get("status"), "local_id": c.get("local_id") or LOCAL_PADRAO}
        for c in comandas_abertas_raw
    ]
    return render_template(
        "cadastrar_venda_lote.html",
        estoque=_com_qtd_por_local(estoque),
        drinks=drinks,
        categorias=categorias,
        comandas_abertas=comandas_abertas,
        erro=erro,
        hoje=hoje,
        caixa_aberto=caixa_aberto,
        usuario=session.get("usuario", ""),
        tipo=session.get("tipo"),
        permissoes=session.get("permissoes", []),
        locais_estoque=locais_estoque_cvl,
        local_padrao=LOCAL_PADRAO,
        caixas_menu=caixas_menu,
    )


# ================== COMANDAS ==================
def _total_comanda(comanda: dict) -> float:
    """Total que o cliente deve na comanda: itens consumidos + empréstimos
    (dinheiro emprestado, incluindo juros lançados à parte). NÃO usar em
    cálculos de faturamento/lucro (Financeiro) — lá o total é só de itens,
    já que empréstimo não é venda de mercadoria."""
    total_itens = sum(
        to_float(i.get("valor_venda", 0)) * to_int(i.get("quantidade", 0))
        for i in comanda.get("itens", [])
    )
    total_emprestimos = sum(to_float(e.get("valor", 0)) for e in comanda.get("emprestimos", []))
    return total_itens + total_emprestimos


def _margem_comanda(comanda: dict) -> float:
    """Margem % atual da comanda, só sobre itens (mesma fórmula markup usada
    em Vendas/Estoque/Pacotes: calcular_margem). Empréstimo não entra aqui,
    igual não entra no cálculo de lucro do Financeiro."""
    total_venda = sum(
        to_float(i.get("valor_venda", 0)) * to_int(i.get("quantidade", 0))
        for i in comanda.get("itens", [])
    )
    total_custo = sum(
        to_float(i.get("valor_investido", 0)) * to_int(i.get("quantidade", 0))
        for i in comanda.get("itens", [])
    )
    return round(calcular_margem(total_venda, total_custo), 2)


def _preco_por_margem(custo: float, margem_pct: float) -> float:
    """Preço de venda necessário pra bater a margem % desejada sobre esse
    custo, invertendo calcular_margem: margem% = (venda-custo)/custo*100."""
    if custo <= 0:
        return 0.0
    return custo * (1 + margem_pct / 100)


@app.route("/comandas")
def comandas():
    if not usuario_logado():
        return redirect(url_for("login"))
    if session.get("tipo") != "admin" and not tem_permissao("ver_vendas"):
        return redirect(url_for("vendas"))

    hoje = now_brt().strftime("%Y-%m-%d")

    # As 3 buscas abaixo são independentes entre si — rodam em paralelo em
    # vez de uma atrás da outra (mesma ideia já aplicada em financeiro()).
    tarefas_comandas = {
        "todas": db.get_all_commands,
        "usuarios_map": db.get_all_users,
        "locais_estoque": db.get_all_locais_estoque,
    }
    with ThreadPoolExecutor(max_workers=len(tarefas_comandas)) as executor:
        futuros_comandas = {nome: executor.submit(fn) for nome, fn in tarefas_comandas.items()}
        resultados_comandas = {nome: fut.result() for nome, fut in futuros_comandas.items()}

    todas = resultados_comandas["todas"]

    def valor_pago(c):
        return sum(to_float(p.get("valor", 0)) for p in c.get("pagamentos", []))

    comandas_exibir = [c for c in todas if isinstance(c, dict)]
    for c in comandas_exibir:
        if c.get("status") == "agendado" and c.get("vencimento") and c["vencimento"] < hoje:
            c["_status_display"] = "atrasada"
        else:
            c["_status_display"] = c.get("status", "aberta")
        c["_total"] = round(_total_comanda(c), 2)
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

    usuarios_map = resultados_comandas["usuarios_map"]
    usuarios_conhecidos = [
        (u, (dados.get("nome") or u)) for u, dados in sorted(usuarios_map.items())
    ]

    return render_template(
        "comandas.html",
        comandas=comandas_exibir,
        tipo=session.get("tipo"),
        permissoes=session.get("permissoes", []),
        usuario=session.get("usuario", "Administrador"),
        nomes_sugestoes=nomes_sugestoes,
        usuarios_conhecidos=usuarios_conhecidos,
        hoje=hoje,
        stats=stats,
        erro_caixa=erro_caixa,
        locais_estoque=resultados_comandas["locais_estoque"],
        local_padrao=LOCAL_PADRAO,
    )


@app.route("/comandas/nova", methods=["POST"])
def comanda_nova():
    if not usuario_logado():
        return redirect(url_for("login"))
    if session.get("tipo") != "admin" and not (tem_permissao("ver_vendas") or tem_permissao("criar_vendas")):
        return redirect(url_for("vendas"))

    # ajax=1: chamado via fetch (ex: modal "Nova comanda" dentro de
    # "Enviar para comanda" em cadastrar_venda_lote.html) — responde em JSON
    # em vez de redirecionar, pra não perder o carrinho em memória daquela
    # tela. Sem o flag, comportamento idêntico ao form tradicional de comandas.html.
    ajax = request.form.get("ajax") == "1"

    tipo = (request.form.get("tipo_atendimento") or "avulso").strip()
    nome = (request.form.get("nome") or "").strip()
    mesa = (request.form.get("mesa") or "").strip()
    cliente_id = (request.form.get("cliente_id") or "").strip() or None
    local_id = (request.form.get("local_id") or LOCAL_PADRAO).strip()

    if tipo == "fiado" and cliente_id:
        cliente = db.get_cliente(cliente_id)
        if cliente:
            nome = cliente.get("nome", nome)
            # comanda fiado abre sempre no local do cliente (o do formulário
            # só vale pra cliente que ainda não tem local definido)
            if cliente.get("local_id"):
                local_id = cliente["local_id"]

    if not nome:
        if ajax:
            return jsonify({"erro": "Nome obrigatório."}), 400
        return redirect(url_for("comandas"))

    nova = {
        "cid": uuid.uuid4().hex,
        "nome": nome,
        "mesa": mesa,
        "tipo_atendimento": tipo,
        "cliente_id": cliente_id,
        "local_id": local_id,
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

    if ajax:
        return jsonify({
            "cid": nova["cid"],
            "nome": nova["nome"],
            "mesa": nova["mesa"],
            "status": nova["status"],
            "local_id": nova["local_id"],
        })
    return redirect(url_for("comandas"))


@app.route("/comandas/<cid>/editar", methods=["POST"])
def comanda_editar(cid):
    if not usuario_logado():
        return redirect(url_for("login"))
    if session.get("tipo") != "admin" and not tem_permissao("ver_vendas"):
        return redirect(url_for("comandas"))

    comanda = db.get_command(cid)
    if not comanda:
        return redirect(url_for("comandas"))

    novo_nome = (request.form.get("nome") or "").strip()
    if novo_nome and novo_nome != comanda.get("nome"):
        nome_antigo = comanda.get("nome", "")
        comanda["nome"] = novo_nome
        db.update_command(comanda)
        db.update_sales_comanda_nome(cid, novo_nome)
        db.registrar_log(
            session.get("usuario", ""), "comanda_editada",
            f"Renomeou comanda: {nome_antigo} → {novo_nome}",
            now_brt().strftime("%Y-%m-%d"), now_brt().strftime("%H:%M"),
        )
    return redirect(url_for("comandas"))


# ==================== CLIENTES ====================

def _local_cliente_valido(local_id: str) -> str:
    """Devolve o local se ele existe nos locais de estoque, senão ""."""
    local_id = (local_id or "").strip()
    return local_id if local_id in {l["id"] for l in db.get_all_locais_estoque()} else ""


@app.route("/clientes")
def clientes_lista():
    if not usuario_logado():
        return redirect(url_for("login"))
    if session.get("tipo") != "admin" and not tem_permissao("ver_vendas"):
        return redirect(url_for("comandas"))

    clientes = db.get_all_clientes()
    saldos = db.get_saldos_clientes([c["id"] for c in clientes])
    for c in clientes:
        saldo = saldos.get(c["id"], 0.0)
        limite = to_float(c.get("limite_credito") or 0)
        c["saldo_utilizado"] = saldo
        c["credito_disponivel"] = round(limite - saldo, 2)

    locais_estoque = db.get_all_locais_estoque()
    return render_template(
        "clientes.html",
        clientes=clientes,
        locais_estoque=locais_estoque,
        locais_nomes_map={l["id"]: l["nome"] for l in locais_estoque},
        tipo=session.get("tipo"),
        permissoes=session.get("permissoes", []),
    )


@app.route("/clientes/buscar")
def clientes_buscar():
    if not usuario_logado():
        return jsonify([]), 401
    import unicodedata
    def _norm(s):
        return unicodedata.normalize("NFD", (s or "").lower()).encode("ascii", "ignore").decode()
    q = (request.args.get("q") or "").strip()
    local = (request.args.get("local") or "").strip()
    clientes = db.get_all_clientes()
    if local:
        # só os clientes daquele local (e os que ainda não têm local definido)
        clientes = [c for c in clientes if c.get("local_id") in ("", local)]
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
    if session.get("tipo") != "admin" and not (tem_permissao("ver_vendas") or tem_permissao("criar_vendas")):
        return jsonify({"erro": "não autorizado"}), 403
    nome = (request.form.get("nome") or "").strip()
    if not nome:
        return jsonify({"erro": "Nome obrigatório"}), 400
    local_id = _local_cliente_valido(request.form.get("local_id"))
    if not local_id:
        return jsonify({"erro": "Escolha o local do cliente (Loja ou Escritório)."}), 400
    novo = {
        "id": uuid.uuid4().hex,
        "nome": nome,
        "local_id": local_id,
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


@app.route("/clientes/<cid>/editar", methods=["POST"])
def cliente_editar(cid):
    if not usuario_logado():
        return jsonify({"erro": "não autorizado"}), 401
    if session.get("tipo") != "admin" and not tem_permissao("ver_vendas"):
        return jsonify({"erro": "não autorizado"}), 403

    cliente = db.get_cliente(cid)
    if not cliente:
        return jsonify({"erro": "Cliente não encontrado"}), 404

    nome = (request.form.get("nome") or "").strip()
    telefone = (request.form.get("telefone") or "").strip()
    if not nome:
        return jsonify({"erro": "Nome obrigatório"}), 400
    if not telefone:
        return jsonify({"erro": "Telefone obrigatório"}), 400
    local_id = _local_cliente_valido(request.form.get("local_id"))
    if not local_id:
        return jsonify({"erro": "Escolha o local do cliente (Loja ou Escritório)."}), 400

    cliente.update({
        "nome": nome,
        "local_id": local_id,
        "apelido": (request.form.get("apelido") or "").strip(),
        "telefone": telefone,
        "cpf": (request.form.get("cpf") or "").strip(),
        "rg": (request.form.get("rg") or "").strip(),
        "endereco": (request.form.get("endereco") or "").strip(),
        "observacoes": (request.form.get("observacoes") or "").strip(),
        "limite_credito": to_float(request.form.get("limite_credito") or "0"),
        "dia_vencimento": to_int(request.form.get("dia_vencimento") or "0") or None,
        "proximo_vencimento": (request.form.get("proximo_vencimento") or "").strip(),
    })
    try:
        db.update_cliente(cliente)
    except RuntimeError as e:
        return jsonify({"erro": str(e)}), 500
    db.registrar_log(
        session.get("usuario", ""), "cliente_editado", f"Editou cliente: {nome}",
        now_brt().strftime("%Y-%m-%d"), now_brt().strftime("%H:%M"),
    )
    return jsonify(cliente)


@app.route("/clientes/<cid>/excluir", methods=["POST"])
def cliente_excluir(cid):
    if not usuario_logado():
        return jsonify({"erro": "não autorizado"}), 401
    if session.get("tipo") != "admin" and not tem_permissao("ver_vendas"):
        return jsonify({"erro": "não autorizado"}), 403

    cliente = db.get_cliente(cid)
    if not cliente:
        return jsonify({"erro": "Cliente não encontrado"}), 404

    if db.tem_comandas_abertas_cliente(cid):
        return jsonify({"erro": "Esse cliente tem comanda em aberto. Feche ou quite antes de excluir."}), 400

    try:
        db.delete_cliente(cid)
    except RuntimeError as e:
        return jsonify({"erro": str(e)}), 500
    db.registrar_log(
        session.get("usuario", ""), "cliente_excluido", f"Excluiu cliente: {cliente.get('nome', '')}",
        now_brt().strftime("%Y-%m-%d"), now_brt().strftime("%H:%M"),
    )
    return jsonify({"ok": True})


@app.route("/comandas/<cid>/detalhe_json")
def comanda_detalhe_json(cid):
    """Itens/empréstimos/pagamentos de 1 comanda, sob demanda — carregado só
    quando o painel de detalhe dela é aberto em /comandas, em vez de vir
    embutido no JSON de TODAS as comandas já na carga inicial da página
    (mesmo padrão já usado em /estoque/transferencia/produto_lotes e
    /clientes/<cid>/info)."""
    if not usuario_logado():
        return jsonify({}), 401
    if session.get("tipo") != "admin" and not tem_permissao("ver_vendas"):
        return jsonify({}), 403
    c = db.get_command(cid)
    if not c:
        return jsonify({}), 404
    return jsonify({
        "itens": c.get("itens", []),
        "emprestimos": c.get("emprestimos", []),
        "pagamentos": c.get("pagamentos", []),
    })


def _bar_pacotes_com_componentes():
    """Pacotes do bar (type=='composite') com a receita (ingredientes)
    anexada em cada um, igual ao que cadastrar_venda_lote() já monta pra
    COMPONENTES_MAP — usado pra tela de comandas poder checar estoque de
    ingrediente no cliente antes de enviar o form."""
    comps_por_produto = db.bar_get_all_components_grouped()
    pacotes = [p for p in db.bar_get_all_products() if p.get("type") == "composite"]
    for p in pacotes:
        comps = comps_por_produto.get(p["id"], [])
        p["componentes"] = [
            {"component_id": c.get("component", {}).get("id", ""),
             "nome": c.get("component", {}).get("name", "?"),
             "quantidade": c.get("quantity", 0),
             "unidade_uso": c.get("unidade_uso") or "UN"}
            for c in comps
        ]
    pacotes.sort(key=lambda p: str(p.get("name", "")).lower())
    return pacotes


@app.route("/comandas/<cid>")
def comanda_detalhe(cid):
    if not usuario_logado():
        return redirect(url_for("login"))
    if session.get("tipo") != "admin" and not tem_permissao("ver_vendas"):
        return redirect(url_for("vendas"))

    # Comanda, produtos, pacotes e locais não dependem um do outro — buscados
    # ao mesmo tempo em vez de um atrás do outro.
    with ThreadPoolExecutor(max_workers=4) as executor:
        fut_comanda = executor.submit(db.get_command, cid)
        fut_estoque = executor.submit(db.get_all_products)
        fut_pacotes = executor.submit(_bar_pacotes_com_componentes)
        fut_locais = executor.submit(db.get_all_locais_estoque)
        comanda = fut_comanda.result()
        if not comanda:
            return redirect(url_for("comandas"))
        estoque = fut_estoque.result()
        bar_pacotes = fut_pacotes.result()
        locais_estoque_cd = fut_locais.result()

    estoque_disponivel = [p for p in estoque if isinstance(p, dict)]
    estoque_disponivel.sort(key=lambda p: str(p.get("nome", "")).lower())

    total = _total_comanda(comanda)
    locais_nomes_map = {l["id"]: l["nome"] for l in locais_estoque_cd}

    return render_template(
        "comanda_detalhe.html",
        comanda=comanda,
        estoque=estoque_disponivel,
        bar_pacotes=bar_pacotes,
        total=round(total, 2),
        margem_atual=_margem_comanda(comanda),
        permite_margem=(session.get("tipo") == "admin" or tem_permissao("aplicar_desconto")),
        erro=session.pop("erro_comanda", ""),
        tipo=session.get("tipo"),
        permissoes=session.get("permissoes", []),
        usuario=session.get("usuario", ""),
        locais_nomes_map=locais_nomes_map,
        local_padrao=LOCAL_PADRAO,
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
    ignorar_estoque = request.form.get("ignorar_estoque") == "1"
    erro = ""
    local_id = comanda.get("local_id") or LOCAL_PADRAO

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
                    cost, log = bar_fifo_consume(comp_pid, needed, ignorar_falta=ignorar_estoque, produto_prefetched=produto_comp,
                                                 local_id=local_id)
                    total_cost += cost
                    all_logs.extend(log)
                    components_consumo.append({"pid": comp_pid, "consumo": log})

                custo_unit = round(total_cost / qtd, 4) if qtd else 0.0
                margem_ativa = comanda.get("margem_customizada")
                local_id = comanda.get("local_id") or LOCAL_PADRAO
                preco_unit = (
                    _preco_por_margem(custo_unit, margem_ativa)
                    if margem_ativa is not None
                    else preco_por_local(bar_prod, local_id, "sale_price")
                )
                hoje_item = now_brt().strftime("%Y-%m-%d")
                hora_item = now_brt().strftime("%H:%M")
                sale_uid = uuid.uuid4().hex
                caixa_aberto_item = db.get_caixa_aberto(local_id)
                db.insert_sale({
                    "uid": sale_uid,
                    "comanda_id": comanda["cid"],
                    "comanda_nome": comanda["nome"],
                    "data": hoje_item,
                    "hora": hora_item,
                    "produto_pid": pid,
                    "produto_tipo": "bar",
                    "nome": bar_prod["name"],
                    "valor_venda": round(preco_unit, 4),
                    "valor_investido": custo_unit,
                    "quantidade": qtd,
                    "status": "pendente",
                    "forma_pagamento": "fiado",
                    "consumo_lotes": all_logs,
                    "usuario": session.get("usuario", ""),
                    "local_id": local_id,
                    "caixa_id": caixa_aberto_item["id"] if caixa_aberto_item else None,
                })
                comanda["itens"].append({
                    "item_id": uuid.uuid4().hex,
                    "sale_uid": sale_uid,
                    "produto_pid": pid,
                    "produto_tipo": "bar",
                    "nome": bar_prod["name"],
                    "quantidade": qtd,
                    "valor_venda": round(preco_unit, 4),
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
                margem_ativa = comanda.get("margem_customizada")
                local_id = comanda.get("local_id") or LOCAL_PADRAO
                if vender_por_conteudo and cv > 1:
                    qtd_estoque = qtd / cv
                    custo_total, consumo = consumir_fifo_local(produto, qtd_estoque, local_id, ignorar_falta=ignorar_estoque)
                    custo_unit_medio = (custo_total / qtd) if qtd else 0.0
                    preco_unit = (
                        _preco_por_margem(custo_unit_medio, margem_ativa)
                        if margem_ativa is not None
                        else preco_por_local(produto, local_id) / cv
                    )
                    db.update_product(produto)
                else:
                    custo_total, consumo = consumir_fifo_local(produto, qtd, local_id, ignorar_falta=ignorar_estoque)
                    custo_unit_medio = (custo_total / qtd) if qtd else 0.0
                    preco_unit = (
                        _preco_por_margem(custo_unit_medio, margem_ativa)
                        if margem_ativa is not None
                        else preco_por_local(produto, local_id)
                    )
                    db.update_product(produto)

                hoje_item = now_brt().strftime("%Y-%m-%d")
                hora_item = now_brt().strftime("%H:%M")
                sale_uid = uuid.uuid4().hex
                caixa_aberto_item = db.get_caixa_aberto(local_id)
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
                    "forma_pagamento": "fiado",
                    "consumo_lotes": consumo,
                    "usuario": session.get("usuario", ""),
                    "local_id": local_id,
                    "caixa_id": caixa_aberto_item["id"] if caixa_aberto_item else None,
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
                    "local_id": local_id,
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
        bar_pacotes = _bar_pacotes_com_componentes()
        total = _total_comanda(comanda)
        return render_template(
            "comanda_detalhe.html",
            comanda=comanda,
            estoque=estoque_disponivel,
            bar_pacotes=bar_pacotes,
            total=round(total, 2),
            margem_atual=_margem_comanda(comanda),
            permite_margem=(session.get("tipo") == "admin" or tem_permissao("aplicar_desconto")),
            erro=erro,
            tipo=session.get("tipo"),
            permissoes=session.get("permissoes", []),
            usuario=session.get("usuario", ""),
            locais_nomes_map={l["id"]: l["nome"] for l in db.get_all_locais_estoque()},
            local_padrao=LOCAL_PADRAO,
        )

    return redirect(url_for("comanda_detalhe", cid=cid))


@app.route("/comandas/<cid>/adicionar_lote", methods=["POST"])
def comanda_adicionar_lote(cid):
    """Envia o carrinho inteiro montado em /cadastrar_venda_lote pra uma
    comanda de uma vez só (em vez de 1 item por request, como
    comanda_adicionar acima). Mesma lógica de consumo/custo daquela rota,
    só que em lote e sempre usando o local_id da própria comanda."""
    if not usuario_logado():
        return redirect(url_for("login"))
    if not tem_permissao("criar_vendas"):
        return redirect(url_for("vendas"))

    comanda = db.get_command(cid)
    if not comanda or comanda.get("status") not in ("aberta", "parcial"):
        return redirect(url_for("cadastrar_venda_lote", comanda_erro="Essa comanda não está mais disponível para receber itens."))

    pids            = request.form.getlist("produto_pid[]")
    qtds            = request.form.getlist("quantidade[]")
    tipos           = request.form.getlist("produto_tipo[]")
    vpcs            = request.form.getlist("vender_por_conteudo[]")
    substituicoes   = request.form.getlist("substituicoes[]")
    ignorar_estoque = request.form.getlist("ignorar_estoque[]")
    while len(tipos) < len(pids):
        tipos.append("estoque")
    while len(vpcs) < len(pids):
        vpcs.append("0")
    while len(substituicoes) < len(pids):
        substituicoes.append("")
    while len(ignorar_estoque) < len(pids):
        ignorar_estoque.append("0")

    if not pids or not qtds or len(pids) != len(qtds):
        return redirect(url_for("cadastrar_venda_lote", comanda_erro="Carrinho vazio — nada para enviar."))

    local_id = comanda.get("local_id") or LOCAL_PADRAO
    margem_ativa = comanda.get("margem_customizada")
    caixa_aberto_item = db.get_caixa_aberto(local_id)
    hoje_item = now_brt().strftime("%Y-%m-%d")
    hora_item = now_brt().strftime("%H:%M")

    erro = ""
    novos_itens = []
    novas_vendas = []
    produtos_modificados = []
    # Um único objeto por produto pro envio inteiro (mesmo motivo de
    # cadastrar_venda_lote): 2 linhas do mesmo produto, ou o produto avulso
    # + como ingrediente de pacote, baixam do mesmo saldo e são gravados
    # uma vez só no fim. Antes cada item relia o produto do banco e a
    # gravação de um apagava a baixa do outro.
    produtos_cache = {}

    def _produto_do_envio(pid_):
        if pid_ not in produtos_cache:
            produtos_cache[pid_] = db.get_product(pid_)
        return produtos_cache[pid_]

    def _marcar_modificado(prod):
        if prod is not None and not any(m is prod for m in produtos_modificados):
            produtos_modificados.append(prod)

    for pid, qtd_raw, tipo, vpc_flag, subst_raw, ignorar_raw in zip(pids, qtds, tipos, vpcs, substituicoes, ignorar_estoque):
        pid = (pid or "").strip()
        qtd = to_int(qtd_raw, 0)
        vpc = vpc_flag == "1"
        if not pid or qtd <= 0:
            continue

        if tipo == "bar":
            bar_prod = db.bar_get_product(pid)
            if not bar_prod:
                erro = "Pacote não encontrado."
                break
            try:
                # Receita customizada só nessa venda (trocou ingrediente/qtd
                # no "Ver itens" antes de enviar pra comanda) — mesmo formato
                # usado em cadastrar_venda_lote().
                components = None
                if subst_raw:
                    try:
                        components = json.loads(subst_raw)
                    except (ValueError, TypeError):
                        components = None
                if not components:
                    components = db.bar_get_components(pid)
                if not components:
                    raise ValueError(f"O pacote '{bar_prod.get('name', '')}' não possui componentes cadastrados.")
                ignorar_falta = ignorar_raw == "1"
                components_consumo = []
                total_cost = 0.0
                all_logs = []
                for comp in components:
                    comp_pid = (comp.get("component_id") or "").replace("-", "")
                    qtd_por_unidade = to_float(comp.get("quantity"), 0)
                    if not comp_pid or qtd_por_unidade <= 0:
                        continue
                    unidade_uso = (comp.get("unidade_uso") or "UN").strip()
                    qtd_bruta = qtd_por_unidade * qtd
                    produto_comp = _produto_do_envio(comp_pid)
                    cv = float((produto_comp or {}).get("conteudo_valor") or 1)
                    needed = qtd_bruta / cv if (cv > 1 and unidade_uso.upper() != "UN") else qtd_bruta
                    cost, log = bar_fifo_consume(comp_pid, needed, ignorar_falta=ignorar_falta, produto_prefetched=produto_comp,
                                                 local_id=local_id, salvar=False)
                    _marcar_modificado(produto_comp)
                    total_cost += cost
                    all_logs.extend(log)
                    components_consumo.append({"pid": comp_pid, "consumo": log})
            except (ValueError, RuntimeError) as e:
                erro = str(e)
                break

            custo_unit = round(total_cost / qtd, 4) if qtd else 0.0
            preco_unit = (
                _preco_por_margem(custo_unit, margem_ativa)
                if margem_ativa is not None
                else preco_por_local(bar_prod, local_id, "sale_price")
            )
            sale_uid = uuid.uuid4().hex
            novas_vendas.append({
                "uid": sale_uid, "comanda_id": comanda["cid"], "comanda_nome": comanda["nome"],
                "data": hoje_item, "hora": hora_item, "produto_pid": pid, "produto_tipo": "bar",
                "nome": bar_prod["name"], "valor_venda": round(preco_unit, 4),
                "valor_investido": custo_unit, "quantidade": qtd, "status": "pendente",
                "forma_pagamento": "fiado", "consumo_lotes": all_logs,
                "usuario": session.get("usuario", ""), "local_id": local_id,
                "caixa_id": caixa_aberto_item["id"] if caixa_aberto_item else None,
            })
            novos_itens.append({
                "item_id": uuid.uuid4().hex, "sale_uid": sale_uid, "produto_pid": pid,
                "produto_tipo": "bar", "nome": bar_prod["name"], "quantidade": qtd,
                "valor_venda": round(preco_unit, 4), "valor_investido": custo_unit,
                "consumo_lotes": all_logs, "bar_consumo_components": components_consumo,
                "data": hoje_item, "hora": hora_item,
            })
        else:
            produto = _produto_do_envio(pid)
            if not produto:
                erro = "Produto não encontrado no estoque."
                break
            try:
                cv = float((produto or {}).get("conteudo_valor") or 1)
                ignorar_falta = ignorar_raw == "1"
                if vpc and cv > 1:
                    qtd_estoque = qtd / cv
                    custo_total, consumo = consumir_fifo_local(produto, qtd_estoque, local_id, ignorar_falta=ignorar_falta)
                    custo_unit_medio = (custo_total / qtd) if qtd else 0.0
                    preco_catalogo = preco_por_local(produto, local_id) / cv
                else:
                    custo_total, consumo = consumir_fifo_local(produto, qtd, local_id, ignorar_falta=ignorar_falta)
                    custo_unit_medio = (custo_total / qtd) if qtd else 0.0
                    preco_catalogo = preco_por_local(produto, local_id)
                _marcar_modificado(produto)
            except ValueError as e:
                erro = str(e)
                break

            preco_unit = (
                _preco_por_margem(custo_unit_medio, margem_ativa)
                if margem_ativa is not None
                else preco_catalogo
            )
            sale_uid = uuid.uuid4().hex
            novas_vendas.append({
                "uid": sale_uid, "comanda_id": comanda["cid"], "comanda_nome": comanda["nome"],
                "data": hoje_item, "hora": hora_item, "produto_pid": produto.get("pid"),
                "produto_tipo": "estoque", "nome": produto.get("nome", ""),
                "valor_venda": round(preco_unit, 4), "valor_investido": round(custo_unit_medio, 4),
                "quantidade": qtd, "status": "pendente", "forma_pagamento": "fiado",
                "consumo_lotes": consumo, "usuario": session.get("usuario", ""),
                "local_id": local_id, "caixa_id": caixa_aberto_item["id"] if caixa_aberto_item else None,
            })
            novos_itens.append({
                "item_id": uuid.uuid4().hex, "sale_uid": sale_uid, "produto_pid": produto.get("pid"),
                "produto_tipo": "estoque", "vender_por_conteudo": vpc and cv > 1,
                "nome": produto.get("nome", ""), "quantidade": qtd,
                "valor_venda": round(preco_unit, 4), "valor_investido": round(custo_unit_medio, 4),
                "consumo_lotes": consumo, "local_id": local_id,
                "data": hoje_item, "hora": hora_item,
            })

    if not erro and not novos_itens:
        erro = "Nenhum item válido para enviar."

    if erro:
        return redirect(url_for("cadastrar_venda_lote", comanda_erro=erro))

    # Só grava depois que o envio inteiro validou: produtos avulsos e
    # ingredientes de pacote ficam só em memória (produtos_cache /
    # produtos_modificados) até aqui, e cada produto é gravado uma vez.
    for produto in produtos_modificados:
        db.update_product(produto)
    db.insert_sales(novas_vendas)
    comanda["itens"].extend(novos_itens)
    db.update_command(comanda)

    total_enviado = sum(to_float(i["valor_venda"]) * to_int(i["quantidade"]) for i in novos_itens)
    db.registrar_log(
        session.get("usuario", ""), "comanda_itens_recebidos",
        f"Enviou {len(novos_itens)} item(ns) (R$ {total_enviado:.2f}) da tela de venda pra comanda: {comanda.get('nome', '')}",
        hoje_item, hora_item,
    )
    label = comanda.get("nome") or "comanda"
    if comanda.get("mesa"):
        label += f" — Mesa {comanda['mesa']}"
    return redirect(url_for("cadastrar_venda_lote", comanda_ok=1, comanda_label=label, comanda_qtd=len(novos_itens)))


@app.route("/comandas/<cid>/margem", methods=["POST"])
def comanda_margem(cid):
    if not usuario_logado():
        return redirect(url_for("login"))
    if session.get("tipo") != "admin" and not tem_permissao("aplicar_desconto"):
        session["_erro_autorizacao"] = "Você não tem permissão pra ajustar a margem da comanda."
        return redirect(url_for("comanda_detalhe", cid=cid))

    comanda = db.get_command(cid)
    if not comanda or comanda.get("status") != "aberta":
        return redirect(url_for("comandas"))

    if (request.form.get("acao") or "").strip() == "resetar":
        comanda["margem_customizada"] = None
        db.update_command(comanda)
        return redirect(url_for("comanda_detalhe", cid=cid))

    margem_raw = (request.form.get("margem") or "").strip()
    try:
        margem = float(margem_raw.replace(",", "."))
    except ValueError:
        session["_erro_autorizacao"] = "Margem inválida."
        return redirect(url_for("comanda_detalhe", cid=cid))

    if margem <= -100:
        session["_erro_autorizacao"] = "Margem inválida (precisa ser maior que -100%)."
        return redirect(url_for("comanda_detalhe", cid=cid))

    comanda["margem_customizada"] = margem
    itens_pulados = 0
    for item in comanda.get("itens", []):
        venda = db.get_sale(item.get("sale_uid", "")) if item.get("sale_uid") else None
        if venda and venda.get("fechado"):
            itens_pulados += 1
            continue
        novo_valor = round(_preco_por_margem(to_float(item.get("valor_investido", 0)), margem), 4)
        item["valor_venda"] = novo_valor
        if venda:
            venda["valor_venda"] = novo_valor
            db.update_sale(venda)

    db.update_command(comanda)

    if itens_pulados:
        session["_erro_autorizacao"] = (
            f"Margem aplicada, mas {itens_pulados} item(ns) não foram atualizados "
            "porque já estão em um caixa fechado."
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
                consumo_dev = item.get("consumo_lotes", [])
                if consumo_dev and isinstance(consumo_dev, list) and "qtd" in consumo_dev[0]:
                    produto = db.get_product(pid_dev)
                    if produto:
                        devolver_fifo_local(produto, consumo_dev, item.get("local_id") or comanda.get("local_id") or LOCAL_PADRAO)
                        db.update_product(produto)
                else:
                    devolver_bar_componentes([{"pid": pid_dev, "consumo": consumo_dev}])
            else:
                pid = str(item.get("produto_pid", ""))
                produto = db.get_product(pid)
                if produto and item.get("consumo_lotes"):
                    devolver_fifo_local(produto, item["consumo_lotes"], item.get("local_id") or comanda.get("local_id") or LOCAL_PADRAO)
                    db.update_product(produto)

            comanda["itens"] = [i for i in comanda["itens"] if str(i.get("item_id")) != str(item_id)]
            db.update_command(comanda)
            if item.get("sale_uid"):
                db.cancelar_sale(item["sale_uid"], session.get("usuario", ""), now_brt().strftime("%Y-%m-%d %H:%M"))
        except RuntimeError as e:
            session["erro_comanda"] = f"Não foi possível remover o item: {e}"

    return redirect(url_for("comanda_detalhe", cid=cid))


@app.route("/comandas/<cid>/emprestimo", methods=["POST"])
def comanda_emprestimo(cid):
    if not usuario_logado():
        return redirect(url_for("login"))
    if session.get("tipo") != "admin" and not tem_permissao("ver_vendas"):
        return redirect(url_for("vendas"))

    comanda = db.get_command(cid)
    if not comanda or comanda.get("status") != "aberta":
        return redirect(url_for("comandas"))

    motivo = (request.form.get("motivo") or "").strip()
    valor = to_float(request.form.get("valor"))
    vencimento = (request.form.get("vencimento") or "").strip()

    if not motivo or valor <= 0:
        session["erro_comanda"] = "Informe o motivo e um valor válido para o empréstimo."
        return redirect(url_for("comanda_detalhe", cid=cid))

    hoje = now_brt().strftime("%Y-%m-%d")
    hora = now_brt().strftime("%H:%M")
    usuario = session.get("usuario", "")

    emprestimos = comanda.get("emprestimos") or []
    emprestimos.append({
        "id": uuid.uuid4().hex,
        "motivo": motivo,
        "valor": round(valor, 2),
        "vencimento": vencimento,
        "data": hoje,
        "hora": hora,
        "criado_por": usuario,
    })
    comanda["emprestimos"] = emprestimos
    db.update_command(comanda)
    venc_txt = f" (devolução prevista: {vencimento})" if vencimento else ""
    db.registrar_log(
        usuario, "emprestimo_comanda",
        f"Lançou empréstimo de R$ {valor:.2f} na comanda {comanda.get('nome', '')}: {motivo}{venc_txt}",
        hoje, hora,
    )
    return redirect(url_for("comanda_detalhe", cid=cid))


@app.route("/comandas/<cid>/emprestimo/<emp_id>/remover", methods=["POST"])
def comanda_emprestimo_remover(cid, emp_id):
    if not usuario_logado():
        return redirect(url_for("login"))
    if session.get("tipo") != "admin" and not tem_permissao("ver_vendas"):
        return redirect(url_for("vendas"))

    comanda = db.get_command(cid)
    if not comanda or comanda.get("status") != "aberta":
        return redirect(url_for("comandas"))

    comanda["emprestimos"] = [
        e for e in comanda.get("emprestimos", []) if str(e.get("id")) != str(emp_id)
    ]
    db.update_command(comanda)
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
        itens = comanda.get("itens", [])

        # 1ª passada (sem consulta ao banco): descobre de antemão quais
        # produtos serão tocados, pra buscar todos numa única chamada em vez
        # de 1 chamada por item/componente (mesmos critérios da 2ª passada
        # abaixo, só adiantados).
        pids_necessarios = set()
        for item in itens:
            if item.get("produto_tipo") == "bar":
                for comp in (item.get("bar_consumo_components", []) or []):
                    if comp.get("pid") and comp.get("consumo"):
                        pids_necessarios.add(str(comp["pid"]))
            elif item.get("vender_por_conteudo"):
                pid_dev = str(item.get("produto_pid", ""))
                if pid_dev and item.get("consumo_lotes"):
                    pids_necessarios.add(pid_dev)
            else:
                pid = str(item.get("produto_pid", ""))
                if pid and item.get("consumo_lotes"):
                    pids_necessarios.add(pid)

        produtos_por_pid = {
            p["pid"]: p for p in db.get_products_by_pids(list(pids_necessarios))
        }
        produtos_alterados = set()

        # 2ª passada: aplica exatamente a mesma regra de devolução de estoque
        # de antes, mas em memória sobre produtos_por_pid — sem 1 leitura e
        # 1 gravação por item da comanda.
        for item in itens:
            if item.get("produto_tipo") == "bar":
                for comp in (item.get("bar_consumo_components", []) or []):
                    pid = comp.get("pid", "")
                    consumo = comp.get("consumo", [])
                    if not pid or not consumo:
                        continue
                    produto = produtos_por_pid.get(str(pid))
                    if not produto:
                        continue
                    _aplicar_devolucao_bar_componente(produto, consumo)
                    produtos_alterados.add(produto["pid"])
            elif item.get("vender_por_conteudo"):
                pid_dev = str(item.get("produto_pid", ""))
                consumo_dev = item.get("consumo_lotes", [])
                if consumo_dev and isinstance(consumo_dev, list) and "qtd" in consumo_dev[0]:
                    produto = produtos_por_pid.get(pid_dev)
                    if produto:
                        devolver_fifo_local(produto, consumo_dev, item.get("local_id") or comanda.get("local_id") or LOCAL_PADRAO)
                        produtos_alterados.add(produto["pid"])
                elif pid_dev and consumo_dev:
                    produto = produtos_por_pid.get(pid_dev)
                    if produto:
                        _aplicar_devolucao_bar_componente(produto, consumo_dev)
                        produtos_alterados.add(produto["pid"])
            else:
                pid = str(item.get("produto_pid", ""))
                produto = produtos_por_pid.get(pid)
                if produto and item.get("consumo_lotes"):
                    devolver_fifo_local(produto, item["consumo_lotes"], item.get("local_id") or comanda.get("local_id") or LOCAL_PADRAO)
                    produtos_alterados.add(produto["pid"])

        for pid in produtos_alterados:
            db.update_product(produtos_por_pid[pid])

        db.delete_command(cid)

        sale_uids = [item["sale_uid"] for item in itens if item.get("sale_uid")]
        if sale_uids:
            db.cancelar_sales_em_lote(sale_uids, session.get("usuario", ""), now_brt().strftime("%Y-%m-%d %H:%M"))
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

    if novo_status == "quitada" and not comanda.get("codigo"):
        try:
            codigo = db.gerar_codigo_rastreio("V", now_brt().strftime("%Y-%m-%d"))
            comanda["codigo"] = codigo
            db.update_command(comanda)
            db.update_sales_codigo_by_comanda(comanda["cid"], codigo)
        except RuntimeError:
            pass


def _e_assumido(pagamento: dict) -> bool:
    return normalizar_pagamento(pagamento.get("forma")) == "assumido"


def _pago_pelo_cliente(comanda: dict) -> float:
    """Só o que o cliente pagou de verdade (sem o restante assumido pela casa)."""
    return sum(to_float(p.get("valor", 0)) for p in comanda.get("pagamentos") or [] if not _e_assumido(p))


@app.route("/comandas/<cid>/assumir", methods=["POST"])
def comanda_assumir(cid):
    """Cliente não vai pagar (ou pagou só uma parte): a casa assume o
    restante como prejuízo. Opcionalmente reprecifica os itens a preço de
    custo antes (sem lucro). O restante vira um pagamento "assumido" — sem
    caixa, não conta como dinheiro recebido — e a comanda fica quitada."""
    if not usuario_logado():
        return redirect(url_for("login"))
    if session.get("tipo") != "admin" and not tem_permissao("assumir_prejuizo_comanda"):
        session["erro_caixa"] = "⚠️ Você não tem permissão para assumir o prejuízo de uma comanda."
        return redirect(url_for("comandas"))

    comanda = db.get_command(cid)
    PAGAVEIS = ("aberta", "aguardando_pagamento", "pagamento_parcial", "agendado", "atrasada", "em_pagamento")
    if not comanda or comanda.get("status") not in PAGAVEIS:
        session["erro_caixa"] = "⚠️ Essa comanda não está em aberto."
        return redirect(url_for("comandas"))

    a_custo = request.form.get("a_custo") == "1"
    motivo = (request.form.get("motivo") or "").strip()[:200]
    usuario = session.get("usuario", "")
    hoje = now_brt().strftime("%Y-%m-%d")
    hora = now_brt().strftime("%H:%M")

    total_antes = _total_comanda(comanda)
    if a_custo:
        # mesma regra de "margem 0%" (comanda_margem): item vale o custo
        for item in comanda.get("itens", []):
            venda = db.get_sale(item.get("sale_uid", "")) if item.get("sale_uid") else None
            if venda and venda.get("fechado"):
                continue  # já está num caixa fechado: mantém o preço
            novo_valor = round(to_float(item.get("valor_investido", 0)), 4)
            item["valor_venda"] = novo_valor
            if venda:
                venda["valor_venda"] = novo_valor
                db.update_sale(venda)
        comanda["margem_customizada"] = 0

    total = _total_comanda(comanda)
    pago = sum(to_float(p.get("valor", 0)) for p in comanda.get("pagamentos") or [])
    restante = round(total - pago, 2)

    if restante > 0.005:
        comanda.setdefault("pagamentos", []).append({
            "uid": uuid.uuid4().hex, "valor": restante, "forma": "assumido",
            "data": hoje, "hora": hora, "usuario": usuario,
            "obs": motivo or "Restante assumido pela casa",
            "caixa_id": None, "assumido_por": usuario,
        })
    status_anterior = comanda.get("status", "aberta")
    comanda["status"] = "quitada"
    comanda["data_quitada"] = hoje
    _registrar_historico(comanda, status_anterior)
    try:
        db.update_command(comanda)
        _sincronizar_status_vendas_comanda(comanda)
        detalhe = (f" (itens a preço de custo: R$ {total_antes:.2f} → R$ {total:.2f})" if a_custo else "")
        db.registrar_log(
            usuario, "comanda_prejuizo_assumido",
            f"Assumiu R$ {max(restante, 0):.2f} da comanda {comanda.get('nome', '')}{detalhe}"
            + (f" — {motivo}" if motivo else ""),
            hoje, hora,
            detalhes={"cid": cid, "assumido": max(restante, 0), "a_custo": a_custo,
                      "total_antes": round(total_antes, 2), "total": round(total, 2), "pago_cliente": round(pago, 2)},
        )
    except RuntimeError as e:
        session["erro_caixa"] = f"⚠️ Não foi possível assumir a comanda: {e}"
    return redirect(url_for("comandas"))


@app.route("/comandas/<cid>/desfazer_assumido", methods=["POST"])
def comanda_desfazer_assumido(cid):
    """Tira o pagamento "assumido": a comanda volta a dever o restante.
    (Os preços continuam como estão — se foi a custo, ajuste a margem.)"""
    if not usuario_logado():
        return redirect(url_for("login"))
    if session.get("tipo") != "admin" and not tem_permissao("assumir_prejuizo_comanda"):
        session["erro_caixa"] = "⚠️ Você não tem permissão para isso."
        return redirect(url_for("comandas"))
    comanda = db.get_command(cid)
    if not comanda or not any(_e_assumido(p) for p in comanda.get("pagamentos") or []):
        return redirect(url_for("comandas"))
    valor = sum(to_float(p.get("valor", 0)) for p in comanda["pagamentos"] if _e_assumido(p))
    comanda["pagamentos"] = [p for p in comanda["pagamentos"] if not _e_assumido(p)]
    status_anterior = comanda.get("status")
    comanda["status"] = "pagamento_parcial" if comanda["pagamentos"] else "aberta"
    comanda["data_quitada"] = None
    _registrar_historico(comanda, status_anterior)
    try:
        db.update_command(comanda)
        _sincronizar_status_vendas_comanda(comanda)
        db.registrar_log(session.get("usuario", ""), "comanda_prejuizo_desfeito",
                         f"Desfez o prejuízo assumido (R$ {valor:.2f}) da comanda {comanda.get('nome', '')}",
                         now_brt().strftime("%Y-%m-%d"), now_brt().strftime("%H:%M"))
    except RuntimeError as e:
        session["erro_caixa"] = f"⚠️ Não foi possível desfazer: {e}"
    return redirect(url_for("comandas"))


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

    comanda = db.get_command(cid)
    PAGAVEIS = ("aberta", "aguardando_pagamento", "pagamento_parcial", "agendado", "atrasada", "em_pagamento")
    if not comanda or comanda.get("status") not in PAGAVEIS:
        session["erro_caixa"] = "⚠️ Essa comanda não está mais disponível para pagamento."
        return redirect(url_for("comandas"))

    local_id = comanda.get("local_id") or LOCAL_PADRAO
    try:
        caixa_pagar = db.get_caixa_aberto(local_id)
    except Exception:
        caixa_pagar = None
    if not caixa_pagar:
        nome_local = next((l["nome"] for l in db.get_all_locais_estoque() if l["id"] == local_id), local_id)
        session["erro_caixa"] = f"⚠️ Nenhum caixa aberto em {nome_local}. Abra o caixa antes de registrar pagamentos."
        return redirect(url_for("comandas"))

    if not comanda.get("itens") and not comanda.get("emprestimos"):
        session["erro_caixa"] = "⚠️ Essa comanda não tem itens para pagar."
        return redirect(url_for("comandas"))

    valor_raw = (request.form.get("valor") or "0").strip()
    valor = to_float(valor_raw)
    forma = (request.form.get("forma") or "dinheiro").strip()
    obs   = (request.form.get("obs") or "").strip()

    total = _total_comanda(comanda)
    pago_antes = sum(to_float(p.get("valor", 0)) for p in comanda.get("pagamentos", []))
    saldo = round(total - pago_antes, 2)

    if valor <= 0 or valor > saldo + 0.005:
        session["erro_caixa"] = f"⚠️ Valor inválido: informe um valor entre R$ 0,01 e R$ {saldo:.2f}".replace(".", ",") + " (saldo devedor da comanda)."
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
    except RuntimeError as e:
        session["erro_caixa"] = f"⚠️ Não foi possível registrar o pagamento: {e}"
    return redirect(url_for("comandas"))


@app.route("/comandas/<cid>/pagamento/<pid>/editar", methods=["POST"])
def comanda_editar_pagamento(cid, pid):
    if not usuario_logado():
        return redirect(url_for("login"))

    redir = autorizar_acao("editar_pagamento_comanda")
    if redir:
        return redir

    comanda = db.get_command(cid)
    if not comanda or comanda.get("status") == "cancelada":
        session["erro_caixa"] = "⚠️ Essa comanda não está disponível para edição de pagamentos."
        return redirect(url_for("comandas"))

    pagamentos = comanda.get("pagamentos") or []
    pagamento = next((p for p in pagamentos if str(p.get("uid")) == str(pid)), None)
    if not pagamento:
        session["erro_caixa"] = "⚠️ Pagamento não encontrado."
        return redirect(url_for("comandas"))

    novo_valor = to_float((request.form.get("valor") or "0").strip())
    nova_obs = (request.form.get("obs") or "").strip()

    total = _total_comanda(comanda)
    outros_pagos = sum(to_float(p.get("valor", 0)) for p in pagamentos if p is not pagamento)
    saldo_max = round(total - outros_pagos, 2)

    if novo_valor <= 0 or novo_valor > saldo_max + 0.005:
        session["erro_caixa"] = f"⚠️ Valor inválido: informe um valor entre R$ 0,01 e R$ {saldo_max:.2f}".replace(".", ",") + " (considerando os outros pagamentos já registrados)."
        return redirect(url_for("comandas"))

    valor_anterior = to_float(pagamento.get("valor"))
    pagamento["valor"] = round(novo_valor, 2)
    pagamento["obs"] = nova_obs
    pagamento["editado_por"] = session.get("usuario", "")
    pagamento["editado_em"] = now_brt().strftime("%Y-%m-%d %H:%M")

    pago_total = round(outros_pagos + novo_valor, 2)
    status_anterior = comanda.get("status", "aberta")
    if pago_total >= total - 0.005:
        comanda["status"] = "quitada"
        if not comanda.get("data_quitada"):
            comanda["data_quitada"] = now_brt().strftime("%Y-%m-%d")
    else:
        comanda["status"] = "pagamento_parcial"
    _registrar_historico(comanda, status_anterior)

    try:
        db.update_command(comanda)
        _sincronizar_status_vendas_comanda(comanda)
        db.registrar_log(
            session.get("usuario", ""), "pagamento_comanda_editado",
            f"Editou pagamento da comanda {comanda.get('nome', '')}: R$ {valor_anterior:.2f} para R$ {novo_valor:.2f}",
            now_brt().strftime("%Y-%m-%d"), now_brt().strftime("%H:%M"),
        )
    except RuntimeError as e:
        session["erro_caixa"] = f"⚠️ Não foi possível salvar a edição do pagamento: {e}"

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

    comanda = db.get_command(cid)
    FECHAVEIS = ("aberta", "aguardando_pagamento", "em_pagamento", "pagamento_parcial", "agendado", "atrasada")
    if not comanda or comanda.get("status") not in FECHAVEIS:
        return redirect(url_for("comandas"))

    local_id = comanda.get("local_id") or LOCAL_PADRAO
    try:
        caixa_fechar = db.get_caixa_aberto(local_id)
    except Exception:
        caixa_fechar = None
    if not caixa_fechar:
        nome_local = next((l["nome"] for l in db.get_all_locais_estoque() if l["id"] == local_id), local_id)
        session["erro_caixa"] = f"⚠️ Nenhum caixa aberto em {nome_local}. Abra o caixa antes de quitar comandas."
        return redirect(url_for("comandas"))

    if not comanda.get("itens") and not comanda.get("emprestimos"):
        return redirect(url_for("comandas"))

    forma_pagamento = (request.form.get("forma_pagamento") or "dinheiro").strip()
    hoje = now_brt().strftime("%Y-%m-%d")
    hora = now_brt().strftime("%H:%M")
    usuario = session.get("usuario", "")

    total = _total_comanda(comanda)
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


# ================== INVESTIDORES ==================

def _fracao_paga_por_comanda() -> dict:
    """Réplica da mesma fórmula já usada em financeiro() pra saber quanto de
    cada comanda em pagamento parcial já foi efetivamente pago pelo cliente.
    Implementação própria (não chama financeiro()) pra não alterar nada lá."""
    fracoes = {}
    for c in db.get_all_commands():
        if not isinstance(c, dict) or c.get("status") != "pagamento_parcial":
            continue
        total_c = sum(to_float(i.get("valor_venda", 0)) * to_int(i.get("quantidade", 0)) for i in c.get("itens", []))
        pago_c = _pago_pelo_cliente(c)  # restante "assumido" pela casa não é receita
        fracoes[c["cid"]] = min(pago_c / total_c, 1.0) if total_c > 0 else 0.0
    return fracoes


_RESULTADO_LOTE_VAZIO = {
    "receita": 0.0, "custo": 0.0, "lucro": 0.0, "qtd_vendida": 0.0,
    "receita_pendente": 0.0, "lucro_pendente": 0.0,
}

# _calcular_resultados_lotes() varre sales + commands inteiras — caro, e
# tanto o dashboard de Investidores quanto a página de cada investidor
# pedem esse mesmo cálculo, um logo depois do outro, na mesma navegação.
# Cache curto (60s) evita refazer a varredura inteira a cada clique; dado
# não muda perceptivelmente entre 2 cliques do mesmo usuário no módulo.
_CACHE_RESULTADOS_LOTES_TTL = 60
_cache_resultados_lotes = {"dados": None, "ts": 0.0}


def _calcular_resultados_lotes_todos(data_ini: str | None = None, data_fim: str | None = None) -> dict:
    """Varre as vendas não canceladas e calcula, por lote_id (de TODOS os
    lotes ativos), custo/qtd_vendida (baixa de estoque) e
    receita_pendente/lucro_pendente (fração de cada venda ainda não paga,
    pelo status atual — não depende de quando o pagamento vai acontecer).
    Quando uma venda consumiu de mais de um lote (ex: pacote do bar com
    vários componentes), rateia proporcionalmente ao custo que cada lote
    contribuiu pra aquela venda. Também monta, por lote, o histórico
    "vendas" (todo consumo daquele lote, independente de pagamento).

    receita/lucro (as colunas REALIZADAS) usam regime de caixa — contam na
    data em que o dinheiro de fato entrou, não na data de consumo: venda
    avulsa é sempre paga na hora (mesma data), então sai direto da mesma
    passada acima; venda de comanda vem de uma 2ª passada sobre os
    pagamentos de cada comanda (cada um com sua própria data), ratando o
    valor de cada pagamento entre os lotes que a comanda inteira consumiu,
    proporcional ao custo de cada lote no custo total da comanda — mesmo
    princípio de rateio por custo usado na 1ª passada, só agregado na
    comanda inteira em vez de por venda.

    Se data_ini/data_fim forem informados (strings "YYYY-MM-DD"), a 1ª
    passada considera só vendas cuja própria data cai nesse período (mesmo
    comportamento de sempre) e a 2ª passada considera só pagamentos cuja
    própria data cai nesse período — usado por investidor_detalhe quando há
    filtro de período ativo. Sem argumentos, comportamento idêntico ao de
    sempre (usado pelo cache global de _obter_todos_resultados_lotes)."""
    # As 3 buscas abaixo são independentes entre si — rodam em paralelo em
    # vez de uma atrás da outra (mesma ideia já aplicada no Financeiro).
    # todas_comandas substitui a busca própria que _fracao_paga_por_comanda
    # fazia (mesma tabela, evita buscar commands duas vezes) — também
    # alimenta a 2ª passada (pagamentos por data).
    with ThreadPoolExecutor(max_workers=3) as executor:
        fut_lotes = executor.submit(db.get_all_lote_investimentos)
        fut_comandas = executor.submit(db.get_all_commands)
        fut_vendas = executor.submit(db.get_all_sales)
        lote_ids = {l["id"] for l in fut_lotes.result()}
        todas_comandas = fut_comandas.result()
        vendas = fut_vendas.result()

    fracao_paga_comanda = {}
    for c in todas_comandas:
        if not isinstance(c, dict) or c.get("status") != "pagamento_parcial":
            continue
        total_c = sum(to_float(i.get("valor_venda", 0)) * to_int(i.get("quantidade", 0)) for i in c.get("itens", []))
        pago_c = _pago_pelo_cliente(c)  # restante "assumido" pela casa não é receita
        fracao_paga_comanda[c["cid"]] = min(pago_c / total_c, 1.0) if total_c > 0 else 0.0

    resultado = {lid: {**_RESULTADO_LOTE_VAZIO, "vendas": []} for lid in lote_ids}

    # Pré-agrupa vendas por comanda_id — usado na 2ª passada pra não varrer
    # a lista de vendas inteira de novo pra cada comanda.
    vendas_por_comanda = defaultdict(list)
    for v in vendas:
        cid_v = v.get("comanda_id")
        if cid_v:
            vendas_por_comanda[cid_v].append(v)

    # 1ª passada: custo/qtd_vendida/receita_pendente/lucro_pendente/vendas
    # (histórico de consumo) de toda venda — e receita/lucro realizados só
    # das vendas AVULSAS (comanda usa a 2ª passada, abaixo).
    for v in vendas:
        if data_ini or data_fim:
            d = str(v.get("data") or "")
            if data_ini and d < data_ini:
                continue
            if data_fim and d > data_fim:
                continue
        consumo = v.get("consumo_lotes") or []
        if not consumo:
            continue
        custo_total_venda = sum(
            to_float(c.get("custo_unit", c.get("unit_cost", 0))) * to_float(c.get("qtd", c.get("qty", 0)))
            for c in consumo
        )
        if custo_total_venda <= 0:
            continue

        qtd_venda = to_float(v.get("quantidade", 0))
        valor_unit = to_float(v.get("valor_venda", 0))
        receita_total_venda = valor_unit * qtd_venda
        lucro_total_venda = receita_total_venda - custo_total_venda

        comanda_id = v.get("comanda_id")
        if comanda_id:
            status_v = v.get("status")
            if status_v == "quitada":
                fracao = 1.0
            elif status_v == "parcial":
                fracao = fracao_paga_comanda.get(comanda_id, 0.0)
            else:
                fracao = 0.0
        else:
            fracao = 1.0  # venda avulsa (fora de comanda) já é recebida na hora

        for c in consumo:
            lid = c.get("lote_id")
            if lid not in resultado:
                continue
            qtd_c = to_float(c.get("qtd", c.get("qty", 0)))
            custo_c = to_float(c.get("custo_unit", c.get("unit_cost", 0))) * qtd_c
            fatia = custo_c / custo_total_venda
            resultado[lid]["custo"]   += custo_c
            resultado[lid]["qtd_vendida"] += qtd_c
            resultado[lid]["receita_pendente"] += receita_total_venda * fatia * (1 - fracao)
            resultado[lid]["lucro_pendente"]   += lucro_total_venda * fatia * (1 - fracao)
            resultado[lid]["vendas"].append({
                "uid": v.get("uid"), "data": v.get("data"), "hora": v.get("hora"),
                "qtd": qtd_c, "receita": receita_total_venda * fatia,
                "lucro": lucro_total_venda * fatia, "status": v.get("status") or "quitada",
                "comanda_id": v.get("comanda_id"),
                "comanda_nome": v.get("comanda_nome"),
                "id_display": _id_display(v),
                "usuario": v.get("usuario"),
            })
            if not comanda_id:
                resultado[lid]["receita"] += receita_total_venda * fatia
                resultado[lid]["lucro"]   += lucro_total_venda * fatia

    # 2ª passada: receita/lucro realizados de vendas de COMANDA — cada
    # pagamento conta na sua própria data, ratando entre os lotes que a
    # comanda inteira consumiu.
    for c in todas_comandas:
        if not isinstance(c, dict):
            continue
        pagamentos_c = c.get("pagamentos") or []
        if not pagamentos_c:
            continue
        vendas_c = vendas_por_comanda.get(c.get("cid"), [])
        if not vendas_c:
            continue

        custo_por_lote = defaultdict(float)
        custo_total_comanda = 0.0
        receita_total_comanda = 0.0
        for v in vendas_c:
            qtd_venda = to_float(v.get("quantidade", 0))
            valor_unit = to_float(v.get("valor_venda", 0))
            receita_total_comanda += valor_unit * qtd_venda
            for cc in (v.get("consumo_lotes") or []):
                lid = cc.get("lote_id")
                custo_c = to_float(cc.get("custo_unit", cc.get("unit_cost", 0))) * to_float(cc.get("qtd", cc.get("qty", 0)))
                custo_por_lote[lid] += custo_c
                custo_total_comanda += custo_c

        if custo_total_comanda <= 0 or receita_total_comanda <= 0:
            continue
        margem_comanda = (receita_total_comanda - custo_total_comanda) / receita_total_comanda

        for p in pagamentos_c:
            if _e_assumido(p):
                continue  # prejuízo assumido: não é dinheiro que entrou
            d = str(p.get("data") or "")
            if data_ini and d < data_ini:
                continue
            if data_fim and d > data_fim:
                continue
            valor_p = to_float(p.get("valor", 0))
            for lid, custo_l in custo_por_lote.items():
                if lid not in resultado:
                    continue
                fatia_l = custo_l / custo_total_comanda
                resultado[lid]["receita"] += valor_p * fatia_l
                resultado[lid]["lucro"]   += valor_p * fatia_l * margem_comanda

    return resultado


def _obter_todos_resultados_lotes() -> dict:
    """Cálculo completo (todos os lotes) respeitando o cache de
    _CACHE_RESULTADOS_LOTES_TTL segundos — usado tanto por
    _calcular_resultados_lotes() quanto diretamente pelas rotas, quando
    fazem sentido disparar esse cálculo em paralelo com outras buscas
    independentes (ver investidores_home/investidor_detalhe)."""
    agora = time.monotonic()
    cache = _cache_resultados_lotes
    if cache["dados"] is None or (agora - cache["ts"]) >= _CACHE_RESULTADOS_LOTES_TTL:
        cache["dados"] = _calcular_resultados_lotes_todos()
        cache["ts"] = agora
    return cache["dados"]


def _calcular_resultados_lotes(lote_ids: set) -> dict:
    """Devolve os resultados (receita/custo/lucro realizado e pendente) só
    dos lote_ids pedidos — mesmo contrato de sempre (mesmas chaves, mesmos
    valores de antes). Por baixo, reaproveita um cálculo cacheado por até
    _CACHE_RESULTADOS_LOTES_TTL segundos em vez de varrer vendas e comandas
    de novo a cada chamada — dashboard de Investidores e página de um
    investidor específico pediam exatamente o mesmo cálculo, um logo depois
    do outro."""
    todos = _obter_todos_resultados_lotes()
    return {lid: todos.get(lid, dict(_RESULTADO_LOTE_VAZIO)) for lid in lote_ids}


@app.route("/investidores")
def investidores_home():
    if not usuario_logado():
        return redirect(url_for("login"))
    if session.get("tipo") != "admin" and not tem_permissao("ver_investidores"):
        return redirect(url_for("comandas"))

    editar = session.get("tipo") == "admin" or tem_permissao("editar_investidores")

    # As 4 buscas abaixo são independentes entre si (o cálculo de resultados
    # busca seu próprio universo de lotes por baixo — não depende de
    # "lotes" buscado aqui) — rodam em paralelo em vez de uma atrás da
    # outra, mesma ideia já aplicada no Financeiro.
    with ThreadPoolExecutor(max_workers=4) as executor:
        fut_investidores = executor.submit(db.get_all_investidores)
        fut_lotes = executor.submit(db.get_all_lote_investimentos)
        fut_produtos = executor.submit(db.get_all_products)
        fut_todos_resultados = executor.submit(_obter_todos_resultados_lotes)
        investidores = fut_investidores.result()
        lotes = fut_lotes.result()
        produtos_map = {p["pid"]: p for p in fut_produtos.result()}
        todos_resultados = fut_todos_resultados.result()

    resultados = {
        lid: todos_resultados.get(lid, dict(_RESULTADO_LOTE_VAZIO))
        for lid in {l["id"] for l in lotes}
    }
    investidores_map = {i["id"]: i for i in investidores}

    f_data_ini = (request.args.get("data_ini") or "").strip()
    f_data_fim = (request.args.get("data_fim") or "").strip()
    f_investidor = (request.args.get("investidor_id") or "").strip()
    f_pid = (request.args.get("pid") or "").strip()
    f_categoria = (request.args.get("categoria") or "").strip()
    f_fornecedor = (request.args.get("fornecedor") or "").strip()

    def _fornecedor_efetivo(li):
        return li.get("fornecedor") or produtos_map.get(li["pid"], {}).get("fornecedor") or ""

    def passa_filtro(li):
        if f_data_ini and li["data_entrada"] < f_data_ini:
            return False
        if f_data_fim and li["data_entrada"] > f_data_fim:
            return False
        if f_investidor and not any(p.get("investidor_id") == f_investidor for p in li.get("participantes", [])):
            return False
        if f_pid and li["pid"] != f_pid:
            return False
        if f_categoria and (produtos_map.get(li["pid"], {}).get("categoria") or "") != f_categoria:
            return False
        if f_fornecedor and _fornecedor_efetivo(li) != f_fornecedor:
            return False
        return True

    lotes_filtrados = [li for li in lotes if passa_filtro(li)]
    for li in lotes_filtrados:
        li["_participantes_nomes"] = [
            f"{investidores_map.get(p['investidor_id'], {}).get('nome', '?')} - {to_float(p['percentual']):g}%"
            for p in li.get("participantes", [])
        ]

    participantes_distintos = {p["investidor_id"] for li in lotes_filtrados for p in li.get("participantes", [])}

    total_investido_geral = sum(li["valor_total"] for li in lotes)
    total_lucro_geral = sum(r["lucro"] for r in resultados.values())
    pagamentos_por_investidor = db.get_totais_pagamentos_investidores([i["id"] for i in investidores])
    total_pago_geral = sum(pagamentos_por_investidor.values())
    total_pendente_geral = max(total_lucro_geral - total_pago_geral, 0.0)
    valor_pendente_vendas_geral = sum(r["receita_pendente"] for r in resultados.values())
    lucro_pendente_vendas_geral = sum(r["lucro_pendente"] for r in resultados.values())

    investido_por_investidor = {i["id"]: 0.0 for i in investidores}
    lucro_por_investidor = {i["id"]: 0.0 for i in investidores}
    for li in lotes:
        r = resultados.get(li["id"], {"lucro": 0.0})
        for p in li.get("participantes", []):
            iid = p["investidor_id"]
            if iid not in investido_por_investidor:
                continue
            fatia = to_float(p["percentual"]) / 100.0
            investido_por_investidor[iid] += li["valor_total"] * fatia
            lucro_por_investidor[iid] += r["lucro"] * fatia

    maior_investidor_id = max(investido_por_investidor, key=investido_por_investidor.get, default=None)
    maior_lucro_investidor_id = max(lucro_por_investidor, key=lucro_por_investidor.get, default=None)

    categorias_disponiveis = sorted({(p.get("categoria") or "").strip() for p in produtos_map.values() if p.get("categoria")})
    fornecedores_disponiveis = sorted({_fornecedor_efetivo(li).strip() for li in lotes if _fornecedor_efetivo(li)})
    produtos_com_lote = sorted(
        {(li["pid"], li["produto_nome"]) for li in lotes},
        key=lambda t: t[1].lower(),
    )

    return render_template(
        "investidores.html",
        editar=editar,
        investidores=investidores,
        lotes=lotes_filtrados,
        produtos_map=produtos_map,
        filtros={
            "data_ini": f_data_ini, "data_fim": f_data_fim, "investidor_id": f_investidor,
            "pid": f_pid, "categoria": f_categoria, "fornecedor": f_fornecedor,
        },
        categorias_disponiveis=categorias_disponiveis,
        fornecedores_disponiveis=fornecedores_disponiveis,
        produtos_com_lote=produtos_com_lote,
        total_investido_filtrado=round(sum(li["valor_total"] for li in lotes_filtrados), 2),
        qtd_lotes_filtrado=len(lotes_filtrados),
        participantes_distintos=len(participantes_distintos),
        dash_total_investido=round(total_investido_geral, 2),
        dash_total_lucro=round(total_lucro_geral, 2),
        dash_total_pago=round(total_pago_geral, 2),
        dash_total_pendente=round(total_pendente_geral, 2),
        dash_valor_pendente_vendas=round(valor_pendente_vendas_geral, 2),
        dash_lucro_pendente_vendas=round(lucro_pendente_vendas_geral, 2),
        dash_num_investidores=len(investidores),
        dash_maior_investidor=investidores_map.get(maior_investidor_id),
        dash_maior_lucro_investidor=investidores_map.get(maior_lucro_investidor_id),
        tipo=session.get("tipo"),
        permissoes=session.get("permissoes", []),
    )


@app.route("/investidores/novo", methods=["POST"])
def investidor_novo():
    if not usuario_logado():
        return jsonify({"erro": "não autorizado"}), 401
    if session.get("tipo") != "admin" and not tem_permissao("editar_investidores"):
        return jsonify({"erro": "não autorizado"}), 403
    nome = (request.form.get("nome") or "").strip()
    if not nome:
        return jsonify({"erro": "Nome obrigatório"}), 400
    novo = {
        "id": uuid.uuid4().hex,
        "nome": nome,
        "documento": (request.form.get("documento") or "").strip(),
        "telefone": (request.form.get("telefone") or "").strip(),
        "email": (request.form.get("email") or "").strip(),
        "observacoes": (request.form.get("observacoes") or "").strip(),
        "status": (request.form.get("status") or "ativo").strip(),
        "data_cadastro": now_brt().strftime("%Y-%m-%d"),
    }
    try:
        db.insert_investidor(novo)
    except RuntimeError as e:
        return jsonify({"erro": str(e)}), 500
    db.registrar_log(
        session.get("usuario", ""), "investidor_criado", f"Cadastrou investidor: {nome}",
        now_brt().strftime("%Y-%m-%d"), now_brt().strftime("%H:%M"),
    )
    return jsonify(novo)


@app.route("/investidores/<iid>/editar", methods=["POST"])
def investidor_editar(iid):
    if not usuario_logado():
        return jsonify({"erro": "não autorizado"}), 401
    if session.get("tipo") != "admin" and not tem_permissao("editar_investidores"):
        return jsonify({"erro": "não autorizado"}), 403
    investidor = db.get_investidor(iid)
    if not investidor:
        return jsonify({"erro": "Investidor não encontrado"}), 404
    nome = (request.form.get("nome") or "").strip()
    if not nome:
        return jsonify({"erro": "Nome obrigatório"}), 400
    investidor.update({
        "nome": nome,
        "documento": (request.form.get("documento") or "").strip(),
        "telefone": (request.form.get("telefone") or "").strip(),
        "email": (request.form.get("email") or "").strip(),
        "observacoes": (request.form.get("observacoes") or "").strip(),
        "status": (request.form.get("status") or "ativo").strip(),
    })
    try:
        db.update_investidor(investidor)
    except RuntimeError as e:
        return jsonify({"erro": str(e)}), 500
    db.registrar_log(
        session.get("usuario", ""), "investidor_editado", f"Editou investidor: {nome}",
        now_brt().strftime("%Y-%m-%d"), now_brt().strftime("%H:%M"),
    )
    return jsonify(investidor)


@app.route("/investidores/<iid>/excluir", methods=["POST"])
def investidor_excluir(iid):
    if not usuario_logado():
        return jsonify({"erro": "não autorizado"}), 401
    if session.get("tipo") != "admin" and not tem_permissao("editar_investidores"):
        return jsonify({"erro": "não autorizado"}), 403
    investidor = db.get_investidor(iid)
    if not investidor:
        return jsonify({"erro": "Investidor não encontrado"}), 404
    if iid == db.DONO_INVESTIDOR_ID:
        return jsonify({"erro": "O investidor 'dono' é gerenciado automaticamente pelo sistema e não pode ser excluído."}), 400
    if db.tem_vinculos_investidor(iid):
        return jsonify({"erro": "Esse investidor tem lotes ou pagamentos vinculados. Não é possível excluir."}), 400
    try:
        db.delete_investidor(iid)
    except RuntimeError as e:
        return jsonify({"erro": str(e)}), 500
    db.registrar_log(
        session.get("usuario", ""), "investidor_excluido", f"Excluiu investidor: {investidor.get('nome', '')}",
        now_brt().strftime("%Y-%m-%d"), now_brt().strftime("%H:%M"),
    )
    return jsonify({"ok": True})


@app.route("/investidores/<iid>")
def investidor_detalhe(iid):
    if not usuario_logado():
        return redirect(url_for("login"))
    if session.get("tipo") != "admin" and not tem_permissao("ver_investidores"):
        return redirect(url_for("comandas"))

    investidor = db.get_investidor(iid)
    if not investidor:
        return redirect(url_for("investidores_home"))

    editar = session.get("tipo") == "admin" or tem_permissao("editar_investidores")

    data_ini_raw = (request.args.get("data_ini") or "").strip()
    data_fim_raw = (request.args.get("data_fim") or "").strip()
    tem_filtro = bool(data_ini_raw or data_fim_raw)

    # As 4 buscas abaixo são independentes entre si (o cálculo de resultados
    # busca seu próprio universo de lotes por baixo) — rodam em paralelo em
    # vez de uma atrás da outra, mesma ideia já aplicada no Financeiro.
    with ThreadPoolExecutor(max_workers=4) as executor:
        fut_lotes = executor.submit(db.get_all_lote_investimentos)
        fut_produtos = executor.submit(db.get_all_products)
        fut_todos_resultados = (
            executor.submit(_calcular_resultados_lotes_todos, data_ini_raw, data_fim_raw)
            if tem_filtro else executor.submit(_obter_todos_resultados_lotes)
        )
        fut_pagamentos = executor.submit(db.get_pagamentos_investidor, iid)

        lotes = [
            li for li in fut_lotes.result()
            if any(p.get("investidor_id") == iid for p in li.get("participantes", []))
        ]
        if data_ini_raw:
            lotes = [li for li in lotes if str(li.get("data_entrada") or "") >= data_ini_raw]
        if data_fim_raw:
            lotes = [li for li in lotes if str(li.get("data_entrada") or "") <= data_fim_raw]

        produtos_map = {p["pid"]: p for p in fut_produtos.result()}
        todos_resultados = fut_todos_resultados.result()

        pagamentos = fut_pagamentos.result()
        if data_ini_raw:
            pagamentos = [p for p in pagamentos if str(p.get("data") or "") >= data_ini_raw]
        if data_fim_raw:
            pagamentos = [p for p in pagamentos if str(p.get("data") or "") <= data_fim_raw]

    resultados = {
        lid: todos_resultados.get(lid, {**_RESULTADO_LOTE_VAZIO, "vendas": []})
        for lid in {l["id"] for l in lotes}
    }

    total_investido = 0.0
    valor_em_estoque = 0.0
    receita_gerada = 0.0
    lucro_acumulado = 0.0
    receita_pendente_investidor = 0.0
    lucro_pendente_investidor = 0.0
    linhas = []
    for li in lotes:
        pct = next((to_float(p["percentual"]) for p in li["participantes"] if p["investidor_id"] == iid), 0.0) / 100.0
        r = resultados.get(li["id"], {"receita": 0.0, "lucro": 0.0, "qtd_vendida": 0.0, "receita_pendente": 0.0, "lucro_pendente": 0.0, "vendas": []})
        produto = produtos_map.get(li["pid"], {})
        qtd_restante = sum(to_float(l.get("qtd", 0)) for l in produto.get("lotes", []) if l.get("id") == li["id"])
        valor_restante = qtd_restante * to_float(li["custo_unit"])

        total_investido += li["valor_total"] * pct
        valor_em_estoque += valor_restante * pct
        receita_gerada += r["receita"] * pct
        lucro_acumulado += r["lucro"] * pct
        receita_pendente_investidor += r["receita_pendente"] * pct
        lucro_pendente_investidor += r["lucro_pendente"] * pct

        vendas_lote = [
            {
                "uid": vd.get("uid"), "data": vd.get("data"), "hora": vd.get("hora"),
                "qtd": vd.get("qtd", 0), "receita": vd.get("receita", 0) * pct,
                "lucro": vd.get("lucro", 0) * pct, "status": vd.get("status"),
                "comanda_id": vd.get("comanda_id"), "comanda_nome": vd.get("comanda_nome"),
                "id_display": vd.get("id_display"), "usuario": vd.get("usuario"),
            }
            for vd in r.get("vendas", [])
        ]
        linhas.append({
            "lote": li, "produto_nome": li["produto_nome"], "percentual": pct * 100,
            "valor_lote": li["valor_total"], "sua_fatia": li["valor_total"] * pct,
            "qtd_restante": qtd_restante, "lucro_gerado": r["lucro"] * pct,
            "lucro_pendente": r["lucro_pendente"] * pct,
            "vendas": vendas_lote,
        })

    valor_recebido = sum(to_float(p["valor"]) for p in pagamentos)
    saldo_a_receber = round(lucro_acumulado - valor_recebido, 2)

    return render_template(
        "investidor_detalhe.html",
        investidor=investidor,
        editar=editar,
        linhas=linhas,
        pagamentos=pagamentos,
        total_investido=round(total_investido, 2),
        qtd_lotes=len(lotes),
        valor_em_estoque=round(valor_em_estoque, 2),
        receita_gerada=round(receita_gerada, 2),
        lucro_acumulado=round(lucro_acumulado, 2),
        valor_recebido=round(valor_recebido, 2),
        saldo_a_receber=saldo_a_receber,
        receita_pendente=round(receita_pendente_investidor, 2),
        lucro_pendente=round(lucro_pendente_investidor, 2),
        hoje=today_brt().isoformat(),
        data_ini_raw=data_ini_raw,
        data_fim_raw=data_fim_raw,
        tipo=session.get("tipo"),
        permissoes=session.get("permissoes", []),
    )


@app.route("/investidores/<iid>/pagamento", methods=["POST"])
def investidor_pagamento(iid):
    if not usuario_logado():
        return redirect(url_for("login"))
    if session.get("tipo") != "admin" and not tem_permissao("editar_investidores"):
        return redirect(url_for("investidor_detalhe", iid=iid))
    investidor = db.get_investidor(iid)
    if not investidor:
        return redirect(url_for("investidores_home"))

    data = (request.form.get("data") or "").strip() or today_brt().isoformat()
    valor = to_float(request.form.get("valor"), 0.0)
    observacao = (request.form.get("observacao") or "").strip()
    if valor > 0:
        db.insert_pagamento_investidor({
            "id": uuid.uuid4().hex,
            "investidor_id": iid,
            "data": normalizar_data_str(data),
            "valor": valor,
            "observacao": observacao,
            "usuario": session.get("usuario", ""),
            "criado_em": now_brt().strftime("%Y-%m-%d %H:%M"),
        })
        db.registrar_log(
            session.get("usuario", ""), "pagamento_investidor",
            f"Registrou pagamento de R$ {valor:.2f} pro investidor {investidor.get('nome', '')}",
            now_brt().strftime("%Y-%m-%d"), now_brt().strftime("%H:%M"),
        )
    return redirect(url_for("investidor_detalhe", iid=iid))


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


def _calcular_totais_caixa(caixa_id: str, vendas_ids: list | None = None, todas_comandas: list | None = None,
                           vendas: list | None = None, pagamentos_vendas: list | None = None) -> dict:
    """Soma as vendas diretas do caixa por forma de pagamento, mais os
    pagamentos de comanda feitos enquanto esse caixa estava aberto (cada
    pagamento de comanda já carrega o caixa_id de quando foi registrado,
    então isso funciona igual pra caixa aberto ou já fechado).

    `todas_comandas`, se informado, é repassado pra
    db.get_pagamentos_comanda_por_caixa() em vez dela buscar de novo — evita
    varredura repetida da tabela commands quando o chamador já buscou essa
    lista pra outra coisa na mesma requisição (ver caixa_turno())."""
    if vendas is not None:
        pass  # já buscadas por quem chamou (ex.: caixa_turno) — evita repetir a consulta
    elif vendas_ids is not None:
        vendas = db.get_sales_by_uids(vendas_ids) if vendas_ids else []
    else:
        vendas = db.get_sales_open_by_caixa(caixa_id)

    ids_venda = list({v["id_venda"] for v in vendas if v.get("id_venda")})
    pagamentos_map = defaultdict(list)
    if pagamentos_vendas is None:
        pagamentos_vendas = db.get_pagamentos_por_id_vendas(ids_venda)
    for p in pagamentos_vendas:
        pagamentos_map[p["id_venda"]].append(p)

    totais = {"dinheiro": 0.0, "pix": 0.0, "credito": 0.0, "debito": 0.0, "fiado": 0.0, "total": 0.0, "qtd": 0}
    vendas_ja_contadas = set()  # evita somar 2x quando vários itens da mesma id_venda têm pagamentos na tabela nova
    for v in vendas:
        valor = to_float(v.get("valor_venda", 0)) * to_int(v.get("quantidade", 1))
        totais["total"] += valor
        totais["qtd"] += 1
        id_venda = v.get("id_venda")
        if id_venda and id_venda in pagamentos_map:
            if id_venda not in vendas_ja_contadas:
                vendas_ja_contadas.add(id_venda)
                for p in pagamentos_map[id_venda]:
                    forma = normalizar_pagamento(p.get("forma", ""))
                    totais[forma] = totais.get(forma, 0.0) + to_float(p.get("valor", 0))
        else:
            forma = normalizar_pagamento(v.get("forma_pagamento", ""))  # fallback: dado antigo, sem venda_pagamentos
            totais[forma] = totais.get(forma, 0.0) + valor

    for p in db.get_pagamentos_comanda_por_caixa(caixa_id, todas_comandas=todas_comandas):
        valor = to_float(p.get("valor", 0))
        forma = normalizar_pagamento(p.get("forma", ""))
        totais[forma] = totais.get(forma, 0.0) + valor
        totais["total"] += valor
        totais["qtd"] += 1

    return totais


def _comandas_abertas_no_periodo(caixa: dict, todas_comandas: list | None = None) -> list:
    """Comandas cuja abertura (data_abertura + hora_abertura) caiu dentro do
    período em que este caixa esteve aberto — do momento da abertura do
    caixa até agora (ou até o fechamento, se já fechado).

    `todas_comandas`, se informado, é usado em vez de buscar a tabela de
    novo (ver caixa_turno())."""
    inicio = f"{caixa.get('data_abertura','')} {caixa.get('hora_abertura','')}"
    if caixa.get("status") == "fechado" and caixa.get("data_fechamento"):
        fim = f"{caixa['data_fechamento']} {caixa.get('hora_fechamento','23:59')}"
    else:
        fim = now_brt().strftime("%Y-%m-%d %H:%M")

    caixa_local = caixa.get("local_id") or LOCAL_PADRAO
    resultado = []
    for c in (todas_comandas if todas_comandas is not None else db.get_all_commands()):
        if (c.get("local_id") or LOCAL_PADRAO) != caixa_local:
            continue
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

    locais_estoque = db.get_all_locais_estoque()
    local_id = (request.values.get("local") or request.form.get("local_id") or LOCAL_PADRAO).strip()
    caixa_aberto = db.get_caixa_aberto(local_id)
    caixas_por_local = {l["id"]: db.get_caixa_aberto(l["id"]) for l in locais_estoque}
    locais_disponiveis = [l for l in locais_estoque if not caixas_por_local.get(l["id"])]
    local_sugerido = local_id if not caixas_por_local.get(local_id) else (
        locais_disponiveis[0]["id"] if locais_disponiveis else local_id)
    erro = ""

    if request.method == "POST":
        local_id = (request.form.get("local_id") or LOCAL_PADRAO).strip()
        caixa_aberto = db.get_caixa_aberto(local_id)
        nome_local = next((l["nome"] for l in locais_estoque if l["id"] == local_id), local_id)
        if caixa_aberto:
            erro = f"Já existe um caixa aberto em {nome_local}. Feche-o antes de abrir outro."
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
                "local_id": local_id,
            }
            try:
                db.insert_caixa(novo)
                db.registrar_log(
                    session.get("usuario", ""), "caixa_aberto",
                    f"Abriu caixa em {nome_local} (valor inicial: R$ {valor:.2f})",
                    now_brt().strftime("%Y-%m-%d"), now_brt().strftime("%H:%M"),
                )
                return redirect(url_for("caixa_turno", local=local_id))
            except RuntimeError as e:
                erro = str(e)

    return render_template("caixa_abrir.html",
        caixa_aberto=caixa_aberto, erro=erro,
        locais_estoque=locais_estoque, caixas_por_local=caixas_por_local,
        locais_disponiveis=locais_disponiveis, local_sugerido=local_sugerido,
        local_padrao=LOCAL_PADRAO, local_atual=local_id,
        tipo=session.get("tipo"), permissoes=session.get("permissoes", []))


@app.route("/caixa/turno")
def caixa_turno():
    redir = _auth_caixa()
    if redir: return redir

    local_id = (request.args.get("local") or LOCAL_PADRAO).strip()

    # Buscas independentes rodam ao mesmo tempo. Comandas/locais/usuários não
    # dependem do caixa, então já começam enquanto o caixa aberto é procurado;
    # movimentações e vendas dependem do id do caixa e entram logo em seguida.
    # todas_comandas é buscada uma vez só e repassada pra
    # _calcular_totais_caixa/_comandas_abertas_no_periodo/pagamentos_comanda_caixa.
    with ThreadPoolExecutor(max_workers=6) as executor:
        fut_comandas = executor.submit(db.get_all_commands)
        fut_locais = executor.submit(db.get_all_locais_estoque)
        fut_usuarios = executor.submit(db.get_all_users)
        caixa = db.get_caixa_aberto(local_id)
        if not caixa:
            return redirect(url_for("caixa_abrir", local=local_id))
        fut_movs = executor.submit(db.get_movimentacoes_caixa, caixa["id"])
        fut_vendas = executor.submit(db.get_sales_open_by_caixa, caixa["id"])
        vendas_caixa = fut_vendas.result()
        # pagamentos das vendas dependem só das vendas: já buscados enquanto as comandas chegam
        fut_pag_vendas = executor.submit(
            db.get_pagamentos_por_id_vendas, list({v["id_venda"] for v in vendas_caixa if v.get("id_venda")}))
        movs = fut_movs.result()
        todas_comandas = fut_comandas.result()
        locais_estoque = fut_locais.result()
        usuarios_map = fut_usuarios.result()
        pagamentos_vendas_caixa = fut_pag_vendas.result()

    total_sangrias   = sum(to_float(m["valor"]) for m in movs if m["tipo"] == "sangria")
    total_suprimentos = sum(to_float(m["valor"]) for m in movs if m["tipo"] == "suprimento")
    total_despesas   = sum(to_float(m["valor"]) for m in movs if m["tipo"] == "despesa")
    qtd_sangrias     = sum(1 for m in movs if m["tipo"] == "sangria")
    qtd_suprimentos  = sum(1 for m in movs if m["tipo"] == "suprimento")
    qtd_despesas     = sum(1 for m in movs if m["tipo"] == "despesa")
    ultima_mov = movs[-1] if movs else None

    # vendas diretas + pagamentos de comanda vinculados a este caixa
    totais = _calcular_totais_caixa(caixa["id"], todas_comandas=todas_comandas, vendas=vendas_caixa,
                                    pagamentos_vendas=pagamentos_vendas_caixa)

    dinheiro_esperado = round(
        to_float(caixa["valor_abertura"]) + totais["dinheiro"]
        + total_suprimentos - total_sangrias - total_despesas, 2)

    comandas_periodo = _comandas_abertas_no_periodo(caixa, todas_comandas=todas_comandas)
    tempo_operacao = _tempo_operacao_str(caixa)
    hoje = now_brt().strftime("%Y-%m-%d")

    # Pagamentos de comanda feitos com este caixa aberto (total ou parcial) —
    # mesma função já usada em _calcular_totais_caixa pra somar o total, só
    # que aqui guardamos a lista pra exibir linha a linha.
    pagamentos_comanda_caixa = sorted(
        db.get_pagamentos_comanda_por_caixa(caixa["id"], todas_comandas=todas_comandas),
        key=lambda p: (p.get("data") or "", p.get("hora") or ""),
        reverse=True,
    )

    # vendas por usuário (quem lançou cada venda deste caixa) — agrupa por
    # "venda" (carrinho/comanda), mesmo critério de _grupo_key usado em /vendas,
    # não por linha de item, senão um carrinho de 3 produtos contaria como 3 vendas.
    grupos_caixa = defaultdict(list)
    for v in vendas_caixa:
        grupos_caixa[_grupo_key(v)].append(v)
    agrup_usuario = defaultdict(lambda: {"qtd": 0, "total": 0.0, "vendas": []})
    for chave_grupo, itens in grupos_caixa.items():
        itens_ordenados = sorted(itens, key=lambda v: (v.get("data") or "", v.get("hora") or ""))
        primeiro = itens_ordenados[0]
        valor_total_grupo = sum(to_float(v.get("valor_venda", 0)) * to_int(v.get("quantidade", 1)) for v in itens)
        formas = []
        for v in itens:
            f = normalizar_pagamento(v.get("forma_pagamento", "")) if v.get("forma_pagamento") else None
            if f and f not in formas:
                formas.append(f)
        statuses = set((v.get("status") or "quitada") for v in itens)
        if "pendente" in statuses:
            status_grupo = "pendente"
        elif "parcial" in statuses:
            status_grupo = "parcial"
        elif statuses == {"cancelada"}:
            status_grupo = "cancelada"
        else:
            status_grupo = "quitada"

        chave_usuario = primeiro.get("usuario") or "__sem__"
        agrup_usuario[chave_usuario]["qtd"] += 1
        agrup_usuario[chave_usuario]["total"] += valor_total_grupo
        agrup_usuario[chave_usuario]["vendas"].append({
            "id_display": _id_display(primeiro),
            "data": primeiro.get("data"),
            "hora": primeiro.get("hora"),
            "qtd_itens": len(itens),
            "formas_pagamento": formas,
            "valor_total": round(valor_total_grupo, 2),
            "status": status_grupo,
        })

    vendas_por_usuario = []
    for chave, dados in agrup_usuario.items():
        nome = "Usuário não identificado" if chave == "__sem__" else (usuarios_map.get(chave, {}).get("nome") or chave)
        vendas_do_usuario = sorted(dados["vendas"], key=lambda v: (v.get("data") or "", v.get("hora") or ""), reverse=True)
        vendas_por_usuario.append({
            "chave": chave, "nome": nome,
            "qtd": dados["qtd"], "total": round(dados["total"], 2),
            "vendas": vendas_do_usuario,
        })
    vendas_por_usuario.sort(key=lambda u: u["total"], reverse=True)

    return render_template("caixa_turno.html",
        caixa=caixa, movs=movs, totais=totais,
        total_sangrias=total_sangrias, total_suprimentos=total_suprimentos,
        total_despesas=total_despesas, dinheiro_esperado=dinheiro_esperado,
        qtd_sangrias=qtd_sangrias, qtd_suprimentos=qtd_suprimentos, qtd_despesas=qtd_despesas,
        ultima_mov=ultima_mov, tempo_operacao=tempo_operacao, hoje=hoje,
        comandas_periodo=comandas_periodo,
        pagamentos_comanda_caixa=pagamentos_comanda_caixa,
        vendas_por_usuario=vendas_por_usuario,
        locais_estoque=locais_estoque,
        locais_nomes_map={l["id"]: l["nome"] for l in locais_estoque},
        local_atual=local_id, local_padrao=LOCAL_PADRAO,
        tipo=session.get("tipo"), permissoes=session.get("permissoes", []))


@app.route("/caixa/movimentacao", methods=["POST"])
def caixa_movimentacao():
    redir = _auth_caixa()
    if redir: return redir

    local_id = (request.form.get("local_id") or LOCAL_PADRAO).strip()
    caixa = db.get_caixa_aberto(local_id)
    if not caixa:
        return redirect(url_for("caixa_abrir", local=local_id))

    tipo      = (request.form.get("tipo") or "").strip()
    valor_raw = (request.form.get("valor") or "0").strip()
    descricao = (request.form.get("descricao") or "").strip()

    if tipo not in ("sangria", "suprimento", "despesa"):
        return redirect(url_for("caixa_turno", local=local_id))

    valor = to_float(valor_raw)
    if valor <= 0:
        return redirect(url_for("caixa_turno", local=local_id))

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
    return redirect(url_for("caixa_turno", local=local_id))


@app.route("/caixa/fechar", methods=["GET", "POST"])
def caixa_fechar():
    redir = _auth_caixa()
    if redir: return redir

    local_id = (request.values.get("local") or request.values.get("local_id") or LOCAL_PADRAO).strip()
    caixa = db.get_caixa_aberto(local_id)
    if not caixa:
        return redirect(url_for("caixa_abrir", local=local_id))

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
    pagamentos_comanda_caixa = sorted(
        db.get_pagamentos_comanda_por_caixa(caixa["id"]),
        key=lambda p: (p.get("data") or "", p.get("hora") or ""),
        reverse=True,
    )
    for p in pagamentos_comanda_caixa:
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
        "debito": "Débito", "fiado": "Faturado",
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
        except RuntimeError as e:
            # Não mostra a tela de "caixa fechado" se o banco recusou o update —
            # antes o erro era engolido e o caixa continuava aberto sem aviso.
            app.logger.error("Falha ao fechar caixa %s: %s", caixa["id"], e)
            caixa = db.get_caixa_aberto(local_id) or caixa
            return render_template("caixa_fechar.html",
                caixa=caixa, totais=totais, movs=movs,
                total_sangrias=total_sangrias, total_suprimentos=total_suprimentos,
                total_despesas=total_despesas, dinheiro_esperado=dinheiro_esperado,
                produtos_vendidos=produtos_vendidos, ticket_medio=ticket_medio,
                pagamentos=pagamentos, tempo_operacao=tempo_operacao, agora=agora,
                comandas_periodo=_comandas_abertas_no_periodo(caixa),
                pagamentos_comanda_caixa=pagamentos_comanda_caixa,
                locais_nomes_map={l["id"]: l["nome"] for l in db.get_all_locais_estoque()},
                local_padrao=LOCAL_PADRAO, erro=f"Não foi possível fechar o caixa: {e}",
                tipo=session.get("tipo"), permissoes=session.get("permissoes", []))
        nome_local = next((l["nome"] for l in db.get_all_locais_estoque() if l["id"] == local_id), local_id)
        try:
            if uids:
                db.mark_sales_fechado(uids, agora.strftime("%Y-%m-%d"), fechado_em)
            db.registrar_log(
                session.get("usuario", ""), "caixa_fechado",
                f"Fechou caixa em {nome_local} (total vendas: R$ {totais['total']:.2f})",
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
            pagamentos_comanda_caixa=pagamentos_comanda_caixa,
            locais_nomes_map={l["id"]: l["nome"] for l in db.get_all_locais_estoque()},
            local_padrao=LOCAL_PADRAO,
            tipo=session.get("tipo"), permissoes=session.get("permissoes", []))

    comandas_periodo = _comandas_abertas_no_periodo(caixa)

    return render_template("caixa_fechar.html",
        caixa=caixa, totais=totais, movs=movs,
        total_sangrias=total_sangrias, total_suprimentos=total_suprimentos,
        total_despesas=total_despesas, dinheiro_esperado=dinheiro_esperado,
        produtos_vendidos=produtos_vendidos, ticket_medio=ticket_medio,
        pagamentos=pagamentos, tempo_operacao=tempo_operacao, agora=agora,
        comandas_periodo=comandas_periodo,
        pagamentos_comanda_caixa=pagamentos_comanda_caixa,
        locais_nomes_map={l["id"]: l["nome"] for l in db.get_all_locais_estoque()},
        local_padrao=LOCAL_PADRAO,
        tipo=session.get("tipo"), permissoes=session.get("permissoes", []))


@app.route("/caixa/historico")
def caixa_historico_novo():
    redir = _auth_caixa_ver()
    if redir: return redir

    hoje = today_brt().isoformat()
    # caixas, movimentações e locais: buscados ao mesmo tempo
    with ThreadPoolExecutor(max_workers=3) as executor:
        fut_caixas = executor.submit(db.get_all_caixas)
        fut_movs = executor.submit(db.get_all_movimentacoes)
        fut_locais = executor.submit(db.get_all_locais_estoque)
        sessoes = fut_caixas.result()
        all_movs_prefetch = fut_movs.result()
        locais_estoque = fut_locais.result()
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
    # caixa aberto de cada local já vem na lista de caixas (sem consultar de novo)
    caixas_por_local = {l["id"]: next((c for c in sessoes if c.get("status") == "aberto"
                                       and (c.get("local_id") or LOCAL_PADRAO) == l["id"]), None)
                        for l in locais_estoque}
    caixa_aberto = next((c for c in caixas_por_local.values() if c), None)
    total_vendido_hoje  = sum(s.get("total_vendas", 0) or 0 for s in sessoes_hoje)
    total_dinheiro_hoje = sum(s.get("total_dinheiro", 0) or 0 for s in sessoes_hoje)
    diferenca_dia       = sum(s.get("diferenca_total", 0) for s in sessoes_hoje if s.get("status") == "fechado")
    fechamentos_hoje    = sum(1 for s in sessoes_hoje if s.get("status") == "fechado")

    all_movs = all_movs_prefetch
    movs_por_caixa: dict = {}
    for m in all_movs:
        cid = str(m.get("caixa_id", ""))
        if cid:
            movs_por_caixa.setdefault(cid, []).append(m)

    sessoes_by_id = {str(s["id"]): s for s in sessoes}

    locais_nomes_map = {l["id"]: l["nome"] for l in locais_estoque}

    return render_template("caixa_historico_novo.html",
        sessoes=sessoes,
        caixa_aberto=caixa_aberto,
        locais_estoque=locais_estoque,
        caixas_por_local=caixas_por_local,
        locais_nomes_map=locais_nomes_map,
        local_padrao=LOCAL_PADRAO,
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
    pagamentos_comanda_caixa = sorted(
        db.get_pagamentos_comanda_por_caixa(cid),
        key=lambda p: (p.get("data") or "", p.get("hora") or ""),
        reverse=True,
    )
    return render_template("caixa_detalhe.html",
        sessao=sessao, movs=movs, pagamentos_comanda_caixa=pagamentos_comanda_caixa,
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

    pode_ver_despesas = session.get("tipo") == "admin" or tem_permissao("ver_despesas")

    data_ini_raw = (request.args.get("data_ini") or "").strip()
    data_fim_raw = (request.args.get("data_fim") or "").strip()

    if not request.args:
        hoje = today_brt().strftime("%Y-%m-%d")
        data_ini_raw = hoje
        data_fim_raw = hoje

    data_de = parse_date_input(data_ini_raw)
    data_ate = parse_date_input(data_fim_raw)

    def _buscar_vendas():
        # Quando o período tem as duas pontas definidas (caso normal,
        # inclusive o padrão "hoje"), pede só esse intervalo pro banco em vez
        # da tabela de vendas inteira. Se faltar alguma ponta (filtro
        # limpo/parcial), cai de volta pra busca completa — igual ao
        # comportamento de antes — pra não arriscar mudar o resultado
        # exibido nesse caso raro.
        if data_ini_raw and data_fim_raw:
            return db.get_sales_by_date_range(data_ini_raw, data_fim_raw)
        return db.get_all_sales()  # já exclui canceladas (excluida=False)

    # As 7 buscas abaixo são todas independentes entre si (nenhuma usa o
    # resultado da outra) — antes rodavam uma atrás da outra, cada uma
    # pagando sua própria viagem de rede até o Supabase (a API REST não tem
    # pool de conexão nem faz nada em paralelo sozinha). Agora rodam ao
    # mesmo tempo em threads: o tempo total passa a ser o da busca mais
    # lenta, não a soma de todas. Nenhum cálculo ou resultado exibido muda,
    # só a ordem/paralelismo das leituras.
    tarefas = {
        "vendas": _buscar_vendas,
        "comandas": db.get_all_commands,
        "despesas": (lambda: db.get_all_despesas()) if pode_ver_despesas else (lambda: []),
        "despesas_categorias": (lambda: db.get_all_despesas_categorias()) if pode_ver_despesas else (lambda: []),
        "despesas_recorrentes": (lambda: db.get_all_despesas_recorrentes()) if pode_ver_despesas else (lambda: []),
        "caixas": db.get_all_caixas,
        "lote_investimentos": db.get_all_lote_investimentos,
        "usuarios": db.get_all_users,
    }
    with ThreadPoolExecutor(max_workers=len(tarefas)) as executor:
        futuros = {nome: executor.submit(fn) for nome, fn in tarefas.items()}
        resultados = {nome: fut.result() for nome, fut in futuros.items()}

    vendas = resultados["vendas"]
    todas_comandas = resultados["comandas"]
    todas_despesas = resultados["despesas"]
    despesas_categorias = resultados["despesas_categorias"]
    despesas_recorrentes = resultados["despesas_recorrentes"]
    todas_caixas = resultados["caixas"]
    todos_lotes_investimento = resultados["lote_investimentos"]
    usuarios_map = resultados["usuarios"]
    pode_ver_por_usuario = session.get("tipo") == "admin" or tem_permissao("ver_vendas_por_usuario")
    # Sem a permissão, ignora ?usuario= na URL — não dá pra filtrar "na mão".
    filtro_usuario = (request.args.get("usuario") or "").strip() if pode_ver_por_usuario else ""

    if pode_ver_despesas:
        try:
            # Se alguma despesa recorrente automática for gerada agora
            # (dia de vencimento chegou e ainda não tinha instância este
            # mês), soma na lista já buscada em memória — sem precisar
            # buscar despesas de novo no banco pra ela aparecer nesta mesma
            # tela (mesmo resultado de antes, sem custo extra).
            novas_despesas = _gerar_despesas_recorrentes_pendentes(todas_despesas, despesas_recorrentes)
            if novas_despesas:
                todas_despesas = todas_despesas + novas_despesas
        except Exception:
            pass  # nunca deve derrubar o carregamento do financeiro

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

    # Vendas por usuário (quem lançou) — sempre sobre o período inteiro, sem
    # o filtro de usuário, pra dar pra comparar os vendedores entre si.
    # "Venda" = carrinho/comanda (_grupo_key), mesmo critério de num_vendas_geral.
    agrup_usuario = defaultdict(lambda: {"grupos": set(), "faturamento": 0.0, "lucro": 0.0})
    for v in vendas_filtradas:
        qtd = to_int(v.get("quantidade", 0))
        valor_linha = to_float(v.get("valor_venda", 0)) * qtd
        custo_linha = to_float(v.get("valor_investido", 0)) * qtd
        dados_u = agrup_usuario[v.get("usuario") or SEM_USUARIO]
        dados_u["grupos"].add(_grupo_key(v))
        dados_u["faturamento"] += valor_linha
        dados_u["lucro"] += valor_linha - custo_linha

    def _nome_usuario(u):
        if not u or u == SEM_USUARIO:
            return "Usuário não identificado"
        return usuarios_map.get(u, {}).get("nome") or u

    vendas_por_usuario = sorted(
        [{"usuario": u, "nome": _nome_usuario(u), "qtd": len(d["grupos"]),
          "faturamento": round(d["faturamento"], 2), "lucro": round(d["lucro"], 2)}
         for u, d in agrup_usuario.items()],
        key=lambda x: x["faturamento"], reverse=True,
    )
    # Opções do filtro: usuários não arquivados + quem vendeu no período
    # (cobre usuário arquivado/excluído que ainda tem venda registrada).
    usuarios_opcoes = [u for u, d in usuarios_map.items() if not d.get("arquivado")]
    usuarios_opcoes += [u for u in agrup_usuario if u != SEM_USUARIO]
    if filtro_usuario and filtro_usuario != SEM_USUARIO:
        usuarios_opcoes.append(filtro_usuario)
    usuarios_filtro = sorted(
        {u: _nome_usuario(u) for u in usuarios_opcoes}.items(),
        key=lambda x: x[1].lower(),
    )
    # Vendas/itens antigos sem usuário gravado — opção própria no fim, pra
    # soma dos usuários sempre fechar com o total.
    usuarios_filtro.append((SEM_USUARIO, _nome_usuario(SEM_USUARIO)))
    filtro_usuario_nome = _nome_usuario(filtro_usuario) if filtro_usuario else ""

    # Filtro de usuário: daqui pra frente todos os números que vêm de venda
    # (faturamento, lucro, top produtos, lista...) consideram só as vendas dele.
    if filtro_usuario:
        vendas_filtradas = [v for v in vendas_filtradas if (v.get("usuario") or SEM_USUARIO) == filtro_usuario]

    # todas_comandas já foi buscada no lote paralelo acima e é reaproveitada
    # aqui nos 3 lugares que precisam dela (fração paga, pagamentos por
    # forma, contagem por status) — antes eram 3 buscas completas de
    # commands na mesma requisição.

    # Fração paga de cada comanda parcial (pago / total), pra reconhecer
    # recebido/lucro na proporção certa — nunca o valor cheio antes de entrar
    # de fato no caixa. Comandas quitadas/pendentes não precisam da fração
    # (100% e 0% respectivamente, já cobertos pelo status da venda).
    fracao_paga_comanda = {}
    for c in todas_comandas:
        if not isinstance(c, dict) or c.get("status") != "pagamento_parcial":
            continue
        total_c = sum(to_float(i.get("valor_venda", 0)) * to_int(i.get("quantidade", 0)) for i in c.get("itens", []))
        pago_c = _pago_pelo_cliente(c)  # restante "assumido" pela casa não é receita
        fracao_paga_comanda[c["cid"]] = min(pago_c / total_c, 1.0) if total_c > 0 else 0.0

    total_vendido = 0.0
    total_vendido_comandas = 0.0
    total_investido = 0.0
    lucro_total = 0.0
    total_a_receber = 0.0
    lucro_pendente = 0.0
    investimento_pendente_periodo = 0.0
    qtd_itens_nao_pagos = 0

    produto_stats = defaultdict(lambda: {"qtd": 0, "lucro": 0.0})

    for v in vendas_filtradas:
        qtd = to_int(v.get("quantidade", 0))
        valor_linha = to_float(v.get("valor_venda", 0)) * qtd
        custo_linha = to_float(v.get("valor_investido", 0)) * qtd
        lucro_linha = valor_linha - custo_linha
        total_vendido += valor_linha
        total_investido += custo_linha
        lucro_total += lucro_linha
        if v.get("comanda_id"):
            # Valor total de comandas no período — mesma base de "Vendas
            # Totais" (data de consumo, inclusive não recebido ainda), só
            # filtrado pra linhas que vieram de comanda.
            total_vendido_comandas += valor_linha

        status_v = v.get("status") or "quitada"
        if status_v == "quitada":
            fracao = 1.0
        elif status_v == "parcial":
            fracao = fracao_paga_comanda.get(v.get("comanda_id"), 0.0)
        else:  # pendente
            fracao = 0.0

        # Pendente é sempre pelo status atual (não depende de quando o
        # pagamento vai acontecer) — continua calculado direto aqui, só
        # deixou de vir por subtração no final: "recebido"/"realizado" agora
        # tem uma base de data diferente (data do pagamento, não do consumo
        # — ver mais abaixo), então a subtração deixaria de bater.
        total_a_receber += valor_linha * (1 - fracao)
        lucro_pendente += lucro_linha * (1 - fracao)
        investimento_pendente_periodo += custo_linha * (1 - fracao)
        if fracao < 0.999:
            qtd_itens_nao_pagos += 1

        nome = v.get("nome", "").strip() or "Sem nome"
        produto_stats[nome]["qtd"] += qtd
        # Ranking por produto continua no regime de competência (fração pelo
        # status atual) — ratear pagamento por pagamento num ranking de
        # produto teria complexidade desproporcional pro benefício.
        produto_stats[nome]["lucro"] += lucro_linha * fracao

    total_vendido = round(total_vendido, 2)
    total_vendido_comandas = round(total_vendido_comandas, 2)
    total_investido = round(total_investido, 2)
    lucro_total = round(lucro_total, 2)
    total_a_receber = round(total_a_receber, 2)
    lucro_pendente = round(lucro_pendente, 2)
    investimento_pendente_periodo = round(investimento_pendente_periodo, 2)
    margem_media = round((lucro_total / total_vendido * 100), 1) if total_vendido > 0 else 0.0

    # Top produtos por lucro já efetivamente realizado (não conta lucro sobre
    # venda ainda não recebida no ranking).
    top_produtos = sorted(
        [{"nome": k, "quantidade": v["qtd"], "lucro": round(v["lucro"], 2)}
         for k, v in produto_stats.items()],
        key=lambda x: x["lucro"], reverse=True
    )[:5]

    # Valor Recebido / Lucro Realizado — regime de caixa: contam na data em
    # que o dinheiro de fato entrou, não na data em que o item foi consumido.
    # Venda avulsa é sempre quitada na hora (consumo = pagamento), então
    # continua vindo direto de vendas_filtradas. Venda de comanda vem dos
    # pagamentos individuais da própria comanda, cada um na sua data real —
    # aplica a margem média dos itens da comanda em cima do valor de cada
    # pagamento. Aproveita o mesmo laço que já monta "Pagamentos por forma"
    # por data de pagamento (perto do que essa seção já fazia só pra formas
    # de pagamento), evitando passar pelas mesmas comandas duas vezes.
    LABEL_FORMA_PAGAMENTO = {
        "pix": "Pix",
        "dinheiro": "Dinheiro",
        "credito": "Cartão",
        "debito": "Débito",
        "fiado": "Faturado",
    }
    # valor_recebido_vendas/valor_recebido_comandas separados porque são de
    # natureza diferente: venda avulsa é sempre paga na hora (mesma data do
    # consumo), enquanto pagamento de comanda pode acontecer dias/meses
    # depois do consumo — misturar os dois num só número escondia de onde
    # vinha o dinheiro. valor_recebido continua existindo como a soma dos
    # dois, pra não quebrar nada que já lê essa variável.
    # Item de comanda não guarda quem lançou — só o sale_uid da linha de
    # venda correspondente, que tem o usuário. Com filtro de usuário, busca o
    # dono de cada item das comandas com pagamento no período (em blocos, pra
    # não estourar a URL do Supabase); sem sale_uid, cai no criado_por da comanda.
    usuario_por_sale_uid = {}
    if filtro_usuario:
        uids_itens = []
        for c in todas_comandas:
            if not isinstance(c, dict):
                continue
            tem_pag_periodo = any(
                (d := parse_date_input(p.get("data", "")))
                and not (data_de and d < data_de) and not (data_ate and d > data_ate)
                for p in c.get("pagamentos", [])
            )
            if tem_pag_periodo:
                uids_itens += [i["sale_uid"] for i in c.get("itens", []) if i.get("sale_uid")]
        for ini in range(0, len(uids_itens), 150):
            for s in db.get_sales_by_uids(uids_itens[ini:ini + 150]):
                usuario_por_sale_uid[s["uid"]] = s.get("usuario") or ""

    valor_recebido_vendas = 0.0
    valor_recebido_comandas = 0.0
    lucro_realizado = 0.0
    # Restante de comanda assumido pela casa (cliente não pagou): não é
    # dinheiro que entrou — vira prejuízo = custo da parte assumida.
    prejuizo_assumido = 0.0
    pag_totais = defaultdict(float)
    for v in vendas_filtradas:
        if v.get("comanda_id"):
            continue
        qtd = to_int(v.get("quantidade", 0))
        valor_linha = to_float(v.get("valor_venda", 0)) * qtd
        custo_linha = to_float(v.get("valor_investido", 0)) * qtd
        valor_recebido_vendas += valor_linha
        lucro_realizado += valor_linha - custo_linha
        forma = normalizar_pagamento(v.get("forma_pagamento", ""))
        pag_totais[forma] += valor_linha
    for c in todas_comandas:
        if not isinstance(c, dict):
            continue
        itens_c = c.get("itens", [])
        receita_c = sum(to_float(i.get("valor_venda", 0)) * to_int(i.get("quantidade", 0)) for i in itens_c)
        custo_c = sum(to_float(i.get("valor_investido", 0)) * to_int(i.get("quantidade", 0)) for i in itens_c)
        margem_c = (receita_c - custo_c) / receita_c if receita_c > 0 else 0.0
        # Com filtro de usuário, cada pagamento da comanda conta só na
        # proporção dos itens que esse usuário lançou nela.
        fracao_usuario_c = 1.0
        if filtro_usuario:
            receita_usuario_c = sum(
                to_float(i.get("valor_venda", 0)) * to_int(i.get("quantidade", 0))
                for i in itens_c
                if (usuario_por_sale_uid.get(i.get("sale_uid")) or c.get("criado_por") or SEM_USUARIO) == filtro_usuario
            )
            fracao_usuario_c = receita_usuario_c / receita_c if receita_c > 0 else 0.0
            if fracao_usuario_c <= 0:
                continue
        for p in c.get("pagamentos", []):
            d = parse_date_input(p.get("data", ""))
            if not d or (data_de and d < data_de) or (data_ate and d > data_ate):
                continue
            valor_p = to_float(p.get("valor", 0)) * fracao_usuario_c
            if _e_assumido(p):
                prejuizo_assumido += valor_p * (custo_c / receita_c if receita_c > 0 else 1.0)
                continue
            valor_recebido_comandas += valor_p
            lucro_realizado += valor_p * margem_c
            forma = normalizar_pagamento(p.get("forma", ""))
            pag_totais[forma] += valor_p
    valor_recebido = round(valor_recebido_vendas + valor_recebido_comandas, 2)
    valor_recebido_vendas = round(valor_recebido_vendas, 2)
    valor_recebido_comandas = round(valor_recebido_comandas, 2)
    lucro_realizado = round(lucro_realizado, 2)
    prejuizo_assumido = round(prejuizo_assumido, 2)
    pagamentos = []
    for forma, total in sorted(pag_totais.items(), key=lambda x: x[1], reverse=True):
        pct = round(total / valor_recebido * 100) if valor_recebido > 0 else 0
        pagamentos.append({"forma": LABEL_FORMA_PAGAMENTO.get(forma, forma.capitalize()), "total": round(total, 2), "pct": pct})

    # Contagem de comandas por status, no mesmo período (por data de abertura)
    num_comandas_abertas = 0
    num_comandas_parciais = 0
    num_comandas_quitadas = 0
    STATUS_ABERTA = ("aberta", "aguardando_pagamento", "agendado", "atrasada", "em_pagamento")
    for c in todas_comandas:
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

    # Sobras/faltas de caixa: diferença (contado - esperado) dos fechamentos
    # de caixa concluídos dentro do período — soma todas as formas de
    # pagamento pra dar o total que precisa ser contabilizado.
    total_sobra_falta = 0.0
    for c in todas_caixas:
        if c.get("status") != "fechado":
            continue
        d_fech = parse_date_input(c.get("data_fechamento", ""))
        if not d_fech:
            continue
        if data_de and d_fech < data_de:
            continue
        if data_ate and d_fech > data_ate:
            continue
        dif = c.get("diferencas") or {}
        total_sobra_falta += (
            to_float(dif.get("dinheiro", 0)) + to_float(dif.get("pix", 0))
            + to_float(dif.get("credito", 0)) + to_float(dif.get("debito", 0))
        )
    total_sobra_falta = round(total_sobra_falta, 2)

    # ── Despesas administrativas (aba nova, independente da despesa de
    # caixa/turno já existente) e Investimento em Estoque (ledger de lotes
    # já usado na feature de Investidores) — ambos filtrados pelo mesmo
    # período do Financeiro. (pode_ver_despesas, todas_despesas,
    # despesas_categorias e despesas_recorrentes já vieram do lote paralelo
    # buscado no início da função — reaproveitados aqui.)
    despesas_categorias_map = {c["id"]: c["nome"] for c in despesas_categorias}
    filtro_categoria_id = (request.args.get("categoria_id") or "").strip()

    despesas_periodo = []
    total_despesas_periodo = 0.0
    maior_despesa_periodo = 0.0
    gasto_por_categoria = defaultdict(float)
    if pode_ver_despesas:
        for d in todas_despesas:
            dd = parse_date_input(d.get("data", ""))
            if not dd:
                continue
            if data_de and dd < data_de:
                continue
            if data_ate and dd > data_ate:
                continue
            if filtro_categoria_id and d.get("categoria_id") != filtro_categoria_id:
                continue
            despesas_periodo.append(d)
            valor_d = to_float(d.get("valor", 0))
            total_despesas_periodo += valor_d
            maior_despesa_periodo = max(maior_despesa_periodo, valor_d)
            gasto_por_categoria[d.get("categoria_id")] += valor_d
        despesas_periodo.sort(key=lambda x: x.get("data", ""), reverse=True)
    total_despesas_periodo = round(total_despesas_periodo, 2)
    qtd_despesas_periodo = len(despesas_periodo)
    categoria_maior_gasto = None
    if gasto_por_categoria:
        cat_id_top = max(gasto_por_categoria, key=gasto_por_categoria.get)
        categoria_maior_gasto = despesas_categorias_map.get(cat_id_top)

    total_investimento_estoque_periodo = 0.0
    for li in todos_lotes_investimento:
        d_lote = parse_date_input(li.get("data_entrada", ""))
        if not d_lote:
            continue
        if data_de and d_lote < data_de:
            continue
        if data_ate and d_lote > data_ate:
            continue
        total_investimento_estoque_periodo += to_float(li.get("valor_total", 0))
    total_investimento_estoque_periodo = round(total_investimento_estoque_periodo, 2)

    # A lista de lotes do período fica na tela Fornecedores (aba Lotes); aqui
    # só o link "ver lotes →" no card de Investimento em Estoque.
    pode_ver_lotes = session.get("tipo") == "admin" or (
        tem_permissao("ver_lotes_periodo") and tem_permissao("ver_fornecedores"))

    lucro_disponivel = round(lucro_realizado - total_despesas_periodo - prejuizo_assumido, 2)

    return render_template(
        "financeiro.html",
        vendas=vendas_filtradas,
        total_vendido=total_vendido,
        total_vendido_comandas=total_vendido_comandas,
        investimento_pendente_periodo=investimento_pendente_periodo,
        qtd_itens_nao_pagos=qtd_itens_nao_pagos,
        total_investido=total_investido,
        lucro_total=lucro_total,
        lucro_realizado=lucro_realizado,
        lucro_pendente=lucro_pendente,
        margem_media=margem_media,
        num_vendas_geral=num_vendas_geral,
        total_a_receber=total_a_receber,
        num_comandas_abertas=num_comandas_abertas,
        num_comandas_parciais=num_comandas_parciais,
        num_comandas_quitadas=num_comandas_quitadas,
        valor_recebido=valor_recebido,
        valor_recebido_vendas=valor_recebido_vendas,
        valor_recebido_comandas=valor_recebido_comandas,
        total_sobra_falta=total_sobra_falta,
        num_vendas=len(vendas_filtradas),
        data_ini_raw=data_ini_raw,
        data_fim_raw=data_fim_raw,
        data_ini_br=data_ini_br,
        data_fim_br=data_fim_br,
        top_produtos=top_produtos,
        pagamentos=pagamentos,
        nomes_sugestoes=nomes_sugestoes,
        hoje_raw=today_brt().strftime("%Y-%m-%d"),
        despesas_categorias=despesas_categorias,
        despesas_categorias_map=despesas_categorias_map,
        despesas_recorrentes=despesas_recorrentes,
        despesas_periodo=despesas_periodo,
        filtro_categoria_id=filtro_categoria_id,
        total_despesas_periodo=total_despesas_periodo,
        qtd_despesas_periodo=qtd_despesas_periodo,
        maior_despesa_periodo=maior_despesa_periodo,
        categoria_maior_gasto=categoria_maior_gasto,
        total_investimento_estoque_periodo=total_investimento_estoque_periodo,
        lucro_disponivel=lucro_disponivel,
        prejuizo_assumido=prejuizo_assumido,
        pode_ver_por_usuario=pode_ver_por_usuario,
        pode_ver_lotes=pode_ver_lotes,
        vendas_por_usuario=vendas_por_usuario if pode_ver_por_usuario else [],
        usuarios_filtro=usuarios_filtro,
        filtro_usuario=filtro_usuario,
        filtro_usuario_nome=filtro_usuario_nome,
        usuario=session.get("usuario", ""),
        tipo=session.get("tipo"),
        permissoes=session.get("permissoes", []),
    )


# ================== DESPESAS ==================

@app.route("/despesas/categorias/nova", methods=["POST"])
def despesa_categoria_nova():
    if not usuario_logado():
        return jsonify({"erro": "não autorizado"}), 401
    if session.get("tipo") != "admin" and not tem_permissao("ver_despesas"):
        return jsonify({"erro": "não autorizado"}), 403
    nome = (request.form.get("nome") or "").strip()
    if not nome:
        return jsonify({"erro": "Nome obrigatório"}), 400
    existente = next((c for c in db.get_all_despesas_categorias() if c["nome"].strip().lower() == nome.lower()), None)
    if existente:
        return jsonify(existente)
    nova = {"id": uuid.uuid4().hex, "nome": nome}
    try:
        db.insert_despesa_categoria(nova)
    except RuntimeError as e:
        return jsonify({"erro": str(e)}), 500
    return jsonify(nova)


@app.route("/despesas/nova", methods=["POST"])
def despesa_nova():
    if not usuario_logado():
        return jsonify({"erro": "não autorizado"}), 401
    if session.get("tipo") != "admin" and not tem_permissao("ver_despesas"):
        return jsonify({"erro": "não autorizado"}), 403

    data = (request.form.get("data") or "").strip()
    categoria_id = (request.form.get("categoria_id") or "").strip()
    descricao = (request.form.get("descricao") or "").strip()
    valor = to_float(request.form.get("valor"), 0.0)
    if not data or not categoria_id or not descricao or valor <= 0:
        return jsonify({"erro": "Preencha data, categoria, descrição e um valor válido."}), 400

    nova = {
        "id": uuid.uuid4().hex,
        "data": normalizar_data_str(data),
        "categoria_id": categoria_id,
        "descricao": descricao,
        "valor": valor,
        "observacao": (request.form.get("observacao") or "").strip(),
        "usuario": session.get("usuario", ""),
        "criado_em": now_brt().strftime("%Y-%m-%d %H:%M"),
    }
    try:
        db.insert_despesa(nova)
    except RuntimeError as e:
        return jsonify({"erro": str(e)}), 500
    db.registrar_log(
        session.get("usuario", ""), "despesa_criada", f"Lançou despesa: {descricao} (R$ {valor:.2f})",
        now_brt().strftime("%Y-%m-%d"), now_brt().strftime("%H:%M"),
    )
    return jsonify(nova)


@app.route("/despesas/<did>/editar", methods=["POST"])
def despesa_editar(did):
    if not usuario_logado():
        return jsonify({"erro": "não autorizado"}), 401
    if session.get("tipo") != "admin" and not tem_permissao("ver_despesas"):
        return jsonify({"erro": "não autorizado"}), 403
    despesa = db.get_despesa(did)
    if not despesa:
        return jsonify({"erro": "Despesa não encontrada"}), 404

    data = (request.form.get("data") or "").strip()
    categoria_id = (request.form.get("categoria_id") or "").strip()
    descricao = (request.form.get("descricao") or "").strip()
    valor = to_float(request.form.get("valor"), 0.0)
    if not data or not categoria_id or not descricao or valor <= 0:
        return jsonify({"erro": "Preencha data, categoria, descrição e um valor válido."}), 400

    despesa.update({
        "data": normalizar_data_str(data),
        "categoria_id": categoria_id,
        "descricao": descricao,
        "valor": valor,
        "observacao": (request.form.get("observacao") or "").strip(),
    })
    try:
        db.update_despesa(despesa)
    except RuntimeError as e:
        return jsonify({"erro": str(e)}), 500
    db.registrar_log(
        session.get("usuario", ""), "despesa_editada", f"Editou despesa: {descricao} (R$ {valor:.2f})",
        now_brt().strftime("%Y-%m-%d"), now_brt().strftime("%H:%M"),
    )
    return jsonify(despesa)


@app.route("/despesas/<did>/excluir", methods=["POST"])
def despesa_excluir(did):
    if not usuario_logado():
        return jsonify({"erro": "não autorizado"}), 401
    if session.get("tipo") != "admin" and not tem_permissao("ver_despesas"):
        return jsonify({"erro": "não autorizado"}), 403
    despesa = db.get_despesa(did)
    if not despesa:
        return jsonify({"erro": "Despesa não encontrada"}), 404
    try:
        db.delete_despesa(did)
    except RuntimeError as e:
        return jsonify({"erro": str(e)}), 500
    db.registrar_log(
        session.get("usuario", ""), "despesa_excluida", f"Excluiu despesa: {despesa.get('descricao', '')}",
        now_brt().strftime("%Y-%m-%d"), now_brt().strftime("%H:%M"),
    )
    return jsonify({"ok": True})


def _gerar_despesas_recorrentes_pendentes(
    despesas_existentes: list | None = None,
    despesas_recorrentes_existentes: list | None = None,
) -> list:
    """Lança automaticamente, no mês atual, as despesas recorrentes ativas
    cujo dia de vencimento já chegou e que ainda não têm instância gerada
    esse mês. Sem cron: roda de forma leve toda vez que financeiro() carrega.

    despesas_existentes / despesas_recorrentes_existentes: listas já
    buscadas por quem chamou (financeiro() já busca as duas), pra não
    repetir a mesma busca completa de novo aqui. Se não forem passadas
    (outros usos futuros da função), busca por conta própria — mesmo
    comportamento de antes.

    Retorna a lista das despesas efetivamente criadas nesta chamada (pode
    ser vazia), pra quem chamou poder somar na própria lista em memória sem
    precisar buscar despesas de novo no banco só pra enxergar o que acabou
    de ser inserido."""
    hoje = now_brt().date()
    ano, mes = hoje.year, hoje.month
    ultimo_dia_mes = calendar.monthrange(ano, mes)[1]

    todas = despesas_existentes if despesas_existentes is not None else db.get_all_despesas()
    despesas_do_mes = [
        d for d in todas
        if d.get("recorrencia_id") and (d.get("data") or "").startswith(f"{ano:04d}-{mes:02d}")
    ]
    recorrencias_ja_geradas = {d["recorrencia_id"] for d in despesas_do_mes}

    recorrentes = (
        despesas_recorrentes_existentes if despesas_recorrentes_existentes is not None
        else db.get_all_despesas_recorrentes()
    )

    novas_geradas = []
    for r in recorrentes:
        if not r.get("ativo"):
            continue
        if r["id"] in recorrencias_ja_geradas:
            continue
        dia_alvo = min(to_int(r.get("dia_mes"), 1), ultimo_dia_mes)
        if hoje.day < dia_alvo:
            continue
        data_gerada = date(ano, mes, dia_alvo).isoformat()
        nova = {
            "id": uuid.uuid4().hex,
            "data": data_gerada,
            "categoria_id": r.get("categoria"),
            "descricao": r.get("descricao", ""),
            "valor": to_float(r.get("valor"), 0.0),
            "observacao": r.get("observacao") or "",
            "usuario": "Recorrente (automático)",
            "criado_em": now_brt().strftime("%Y-%m-%d %H:%M"),
            "recorrencia_id": r["id"],
        }
        try:
            db.insert_despesa(nova)
            novas_geradas.append(nova)
        except RuntimeError:
            pass
    return novas_geradas


@app.route("/despesas/recorrentes/nova", methods=["POST"])
def despesa_recorrente_nova():
    if not usuario_logado():
        return jsonify({"erro": "não autorizado"}), 401
    if session.get("tipo") != "admin" and not tem_permissao("ver_despesas"):
        return jsonify({"erro": "não autorizado"}), 403

    categoria_id = (request.form.get("categoria_id") or "").strip()
    descricao = (request.form.get("descricao") or "").strip()
    valor = to_float(request.form.get("valor"), 0.0)
    dia_mes = to_int(request.form.get("dia_mes"), 0)
    if not categoria_id or not descricao or valor <= 0 or not (1 <= dia_mes <= 31):
        return jsonify({"erro": "Preencha categoria, descrição, valor e um dia do mês entre 1 e 31."}), 400

    nova = {
        "id": uuid.uuid4().hex,
        "categoria": categoria_id,
        "descricao": descricao,
        "valor": valor,
        "dia_mes": dia_mes,
        "observacao": (request.form.get("observacao") or "").strip(),
        "ativo": True,
        "criado_por": session.get("usuario", ""),
        "criado_em": now_brt().strftime("%Y-%m-%d %H:%M"),
    }
    try:
        db.insert_despesa_recorrente(nova)
    except RuntimeError as e:
        return jsonify({"erro": str(e)}), 500
    db.registrar_log(
        session.get("usuario", ""), "despesa_recorrente_criada", f"Criou despesa recorrente: {descricao} (R$ {valor:.2f}, dia {dia_mes})",
        now_brt().strftime("%Y-%m-%d"), now_brt().strftime("%H:%M"),
    )
    return jsonify(nova)


@app.route("/despesas/recorrentes/<rid>/editar", methods=["POST"])
def despesa_recorrente_editar(rid):
    if not usuario_logado():
        return jsonify({"erro": "não autorizado"}), 401
    if session.get("tipo") != "admin" and not tem_permissao("ver_despesas"):
        return jsonify({"erro": "não autorizado"}), 403
    recorrente = db.get_despesa_recorrente(rid)
    if not recorrente:
        return jsonify({"erro": "Despesa recorrente não encontrada"}), 404

    categoria_id = (request.form.get("categoria_id") or "").strip()
    descricao = (request.form.get("descricao") or "").strip()
    valor = to_float(request.form.get("valor"), 0.0)
    dia_mes = to_int(request.form.get("dia_mes"), 0)
    if not categoria_id or not descricao or valor <= 0 or not (1 <= dia_mes <= 31):
        return jsonify({"erro": "Preencha categoria, descrição, valor e um dia do mês entre 1 e 31."}), 400

    recorrente.update({
        "categoria": categoria_id,
        "descricao": descricao,
        "valor": valor,
        "dia_mes": dia_mes,
        "observacao": (request.form.get("observacao") or "").strip(),
        "ativo": (request.form.get("ativo") or "1") == "1",
    })
    try:
        db.update_despesa_recorrente(recorrente)
    except RuntimeError as e:
        return jsonify({"erro": str(e)}), 500
    db.registrar_log(
        session.get("usuario", ""), "despesa_recorrente_editada", f"Editou despesa recorrente: {descricao}",
        now_brt().strftime("%Y-%m-%d"), now_brt().strftime("%H:%M"),
    )
    return jsonify(recorrente)


@app.route("/despesas/recorrentes/<rid>/excluir", methods=["POST"])
def despesa_recorrente_excluir(rid):
    if not usuario_logado():
        return jsonify({"erro": "não autorizado"}), 401
    if session.get("tipo") != "admin" and not tem_permissao("ver_despesas"):
        return jsonify({"erro": "não autorizado"}), 403
    recorrente = db.get_despesa_recorrente(rid)
    if not recorrente:
        return jsonify({"erro": "Despesa recorrente não encontrada"}), 404
    try:
        db.delete_despesa_recorrente(rid)
    except RuntimeError as e:
        return jsonify({"erro": str(e)}), 500
    db.registrar_log(
        session.get("usuario", ""), "despesa_recorrente_excluida", f"Excluiu despesa recorrente: {recorrente.get('descricao', '')}",
        now_brt().strftime("%Y-%m-%d"), now_brt().strftime("%H:%M"),
    )
    return jsonify({"ok": True})


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


def bar_fifo_consume(product_id: str, qty_needed: float, ignorar_falta: bool = False,
                      produto_prefetched: dict | None = None, local_id: str | None = None,
                      salvar: bool = True) -> tuple[float, list]:
    """Consume qty_needed do estoque principal (products) via FIFO.
    Se o produto não existir no estoque principal, usa bar_stock_batches como fallback.

    Antes só parava de consumir quando os lotes acabavam, sem avisar — dava
    pra vender um pacote com ingrediente zerado sem erro nenhum. Agora, se
    não tiver estoque suficiente, lança ValueError (igual consumir_fifo_local
    já faz pros produtos "normais"), a menos que ignorar_falta=True (usuário
    confirmou "continuar mesmo assim" na tela de venda).

    produto_prefetched: quando o chamador já buscou esse produto antes (ex:
    pra ler conteudo_valor e converter unidade), passa ele aqui pra não
    buscar de novo do banco.

    salvar=False: só altera o produto recebido (produto_prefetched) e NÃO
    grava — usado quando a venda tem vários itens e quem chama grava cada
    produto uma vez no fim (senão a gravação de um item apagava a baixa de
    outro item do mesmo produto no mesmo carrinho).

    local_id: local da venda/comanda. Mesma regra de consumir_fifo_local —
    PEPS só nos lotes com saldo nesse local, baixando a quantidade do lote
    E o saldo daquele local (antes baixava só a quantidade, e o saldo por
    local ficava maior que o real). Fora do local principal não existe
    "continuar mesmo assim"."""
    local_id = local_id or LOCAL_PADRAO
    if local_id != LOCAL_PADRAO:
        ignorar_falta = False
    produto = produto_prefetched if produto_prefetched is not None else db.get_product(product_id)
    if produto:
        lotes = produto.get("lotes", [])
        lotes.sort(key=lambda l: str(l.get("data", "")))
        disponivel = sum(to_float(_dist_lote(l).get(local_id, 0)) for l in lotes)
        if qty_needed > disponivel and not ignorar_falta:
            nome_local = next((l["nome"] for l in db.get_all_locais_estoque() if l["id"] == local_id), local_id)
            raise ValueError(
                f"Estoque insuficiente de \"{produto.get('nome', product_id)}\" no(a) {nome_local}: "
                f"tem {round(disponivel, 3)}, precisa de {round(qty_needed, 3)}."
            )
        remaining = qty_needed
        total_cost = 0.0
        log = []
        i = 0
        while remaining > 0 and i < len(lotes):
            lote = lotes[i]
            dist = _dist_lote(lote)
            avail = to_float(dist.get(local_id, 0))
            if avail <= 0:
                i += 1
                continue
            custo_unit = to_float(lote.get("custo_unit", 0))
            consume = min(avail, remaining)
            total_cost += consume * custo_unit
            log.append({"data_lote": lote.get("data"), "qty": round(consume, 6), "unit_cost": custo_unit,
                        "lote_id": lote.get("id"), "component_id": product_id, "codigo": produto.get("codigo"),
                        "local_id": local_id})
            dist = dict(dist)
            dist[local_id] = round(avail - consume, 6)
            lote["distribuicao"] = dist
            novo_qtd = round(to_float(lote.get("qtd", 0)) - consume, 6)
            lote["qtd"] = novo_qtd
            if novo_qtd < RESTO_LOTE_MINIMO:
                lotes.pop(i)
            else:
                i += 1
            remaining = round(remaining - consume, 6)
        atualizar_campos_derivados_estoque(produto)
        if salvar:
            db.update_product(produto)
        return round(total_cost, 4), log

    # Fallback: bar_stock_batches (ingredientes cadastrados apenas no bar)
    batches = db.bar_get_available_batches(product_id)
    disponivel_batches = sum(float(b["quantity_remaining"]) for b in batches)
    if qty_needed > disponivel_batches and not ignorar_falta:
        raise ValueError(
            f"Estoque insuficiente do ingrediente: tem {disponivel_batches}, precisa de {round(qty_needed, 3)}."
        )
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
        log.append({"batch_id": b["id"], "qty": consume, "unit_cost": unit_cost, "component_id": product_id})
        db.bar_update_batch_remaining(b["id"], round(avail - consume, 6))
        remaining -= consume
    return round(total_cost, 4), log


def bar_calc_drink_cost(product_id: str, quantity: float, componentes_override: list | None = None, ignorar_falta: bool = False,
                        local_id: str | None = None, produtos_cache: dict | None = None,
                        salvar: bool = True) -> tuple[float, list]:
    """Calculate cost and consume stock for a composite or simple product.

    componentes_override: quando informado (lista de {"component_id",
    "quantity", "unidade_uso"}), substitui a receita oficial do pacote só
    pra essa chamada — usado quando o usuário troca um ingrediente/quantidade
    na hora de registrar a venda, sem alterar a receita cadastrada.

    ignorar_falta: repassado pra bar_fifo_consume — quando True, deixa vender
    mesmo sem estoque suficiente de algum ingrediente (usuário confirmou
    "continuar mesmo assim" na tela)."""
    product = db.bar_get_product(product_id)
    if not product:
        return 0.0, []

    if product["type"] == "composite":
        components = componentes_override if componentes_override is not None else db.bar_get_components(product_id)
        total_cost = 0.0
        all_logs = []
        for comp in components:
            comp_pid_inner = (comp.get("component_id") or "").replace("-", "")
            qtd_por_unidade = to_float(comp.get("quantity"), 0)
            if not comp_pid_inner or qtd_por_unidade <= 0:
                continue
            unidade_uso = (comp.get("unidade_uso") or "UN").strip()
            qtd_bruta = qtd_por_unidade * quantity
            # produtos_cache: o MESMO objeto de produto pra todos os itens da
            # venda (ingrediente repetido ou também vendido avulso no carrinho)
            if produtos_cache is not None:
                if comp_pid_inner not in produtos_cache:
                    produtos_cache[comp_pid_inner] = db.get_product(comp_pid_inner)
                produto_comp = produtos_cache[comp_pid_inner]
            else:
                produto_comp = db.get_product(comp_pid_inner)
            cv = float((produto_comp or {}).get("conteudo_valor") or 1)
            needed = qtd_bruta / cv if (cv > 1 and unidade_uso.upper() != "UN") else qtd_bruta
            cost, log = bar_fifo_consume(comp_pid_inner, needed, ignorar_falta=ignorar_falta, produto_prefetched=produto_comp,
                                         local_id=local_id, salvar=salvar)
            if cost == 0 and produto_comp:
                vi = float(produto_comp.get("valor_investido", 0) or 0)
                cost = needed * vi
            total_cost += cost
            all_logs.extend(log)
        return round(total_cost, 4), all_logs
    else:
        return bar_fifo_consume(product_id, quantity, ignorar_falta=ignorar_falta, local_id=local_id)


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

    categorias = _categorias_existentes(todos, campo="category")
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
        precos_local_pacote = ler_precos_local_form(request.form, price)
        if not name:
            erro = "Nome obrigatório."
        elif precos_local_pacote and not db.precos_local_disponivel():
            erro = MSG_SQL_PRECOS_LOCAL.replace("O produto", "O pacote")
        else:
            try:
                novo_pacote = {
                    "name": name, "category": category, "unit": "un",
                    "type": "composite", "sale_price": price
                }
                if precos_local_pacote:
                    novo_pacote["precos_local"] = precos_local_pacote
                pid = db.bar_insert_product(novo_pacote)
                comp_ids   = request.form.getlist("component_id[]")
                comp_qtys  = request.form.getlist("component_qty[]")
                comp_units = request.form.getlist("unidade_uso[]")
                while len(comp_units) < len(comp_ids):
                    comp_units.append("UN")
                for cid, qty_s, unit in zip(comp_ids, comp_qtys, comp_units):
                    qty = to_float(qty_s)
                    if cid and qty > 0:
                        db.bar_insert_component({"product_id": pid, "component_id": _pid_to_uuid(cid), "quantity": qty, "unidade_uso": unit or "UN"})
            except Exception as e:
                erro = f"Não foi possível salvar o pacote: {e}"
            if not erro:
                return redirect(url_for("bar_produtos"))

    insumos = [{"id": p["pid"], "name": p["nome"], "unit": p.get("unit", "UN"),
                "cost": p.get("valor_investido", 0), "stock": p.get("quantidade", 0),
                "conteudo_valor": p.get("conteudo_valor") or 0,
                "conteudo_unidade": p.get("conteudo_unidade") or ""}
               for p in db.get_all_products()]
    categorias = db.get_all_categorias()
    return render_template("bar_produto_form.html", produto=None, components=[], insumos=insumos, erro=erro,
                           locais_estoque=db.get_all_locais_estoque(), local_padrao=LOCAL_PADRAO,
        categorias=categorias,
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
        precos_local_pacote = ler_precos_local_form(request.form, price)
        if not name:
            erro = "Nome obrigatório."
        elif precos_local_pacote and not db.precos_local_disponivel():
            erro = MSG_SQL_PRECOS_LOCAL.replace("O produto", "O pacote")
        else:
            try:
                db.bar_update_product(pid, {"name": name, "sale_price": price, "category": category,
                                            "precos_local": precos_local_pacote})
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
            except Exception as e:
                erro = f"Não foi possível salvar o pacote: {e}"
            if not erro:
                return redirect(url_for("bar_produtos"))

    insumos    = [{"id": p["pid"], "name": p["nome"], "unit": p.get("unit", "UN"),
                   "cost": p.get("valor_investido", 0), "stock": p.get("quantidade", 0),
                   "conteudo_valor": p.get("conteudo_valor") or 0,
                   "conteudo_unidade": p.get("conteudo_unidade") or ""}
                  for p in db.get_all_products()]
    components = db.bar_get_components(pid)
    categorias = db.get_all_categorias()
    return render_template("bar_produto_form.html", produto=produto, components=components, insumos=insumos, erro=erro,
                           locais_estoque=db.get_all_locais_estoque(), local_padrao=LOCAL_PADRAO,
        categorias=categorias,
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
            from urllib.parse import quote
            usuario_alvo = (request.form.get("usuario_alvo") or "").strip()
            nova_senha = request.form.get("nova_senha") or ""
            confirmar = request.form.get("confirmar_senha")
            if usuario_alvo not in usuarios:
                return redirect(url_for("permissoes") + "?erro=" + quote("Usuário não encontrado."))
            volta = url_for("permissoes") + "?usuario=" + quote(usuario_alvo)
            if len(nova_senha) < 4:
                return redirect(volta + "&erro=" + quote("A senha precisa ter pelo menos 4 caracteres. Nada foi alterado."))
            if confirmar is not None and confirmar != nova_senha:
                return redirect(volta + "&erro=" + quote("As duas senhas não são iguais. Nada foi alterado."))
            db.update_user_senha(usuario_alvo, db.hash_senha(nova_senha))
            db.encerrar_sessoes_usuario(usuario_alvo, session.get("usuario", ""), quando=now_brt().strftime("%Y-%m-%d %H:%M:%S"))
            _log("senha_alterada", f"Senha redefinida para: {usuario_alvo}", usuario_alvo)
            return redirect(volta + "&msg=" + quote(
                f"Senha de {usuario_alvo} alterada. Se estava conectado, foi desconectado e entra com a senha nova."))

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

    # Nunca mandar senha/PIN (mesmo criptografados) pro navegador.
    usuarios_tela = {
        nome: {**{k: v for k, v in info.items() if k not in ("senha", "pin_hash")},
               "tem_pin": bool(info.get("pin_hash"))}
        for nome, info in usuarios.items()
    }

    return render_template(
        "permissoes.html",
        usuarios=usuarios_tela,
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


# ================== FORNECEDORES ==================
# Cadastro de fornecedores, lotes de cada um e compras (como/quando pagar).
# Os lotes continuam em lote_investimentos com o fornecedor em texto livre; um
# lote é de um fornecedor quando esse texto bate (via _chave_fornecedor) com o
# nome dele ou com um dos "apelidos" — nomes antigos juntados na tela.

FORMAS_PAGAMENTO_FORNECEDOR = {
    "pix": "Pix", "boleto": "Boleto", "dinheiro": "Dinheiro",
    "cartao": "Cartão", "transferencia": "Transferência", "a_prazo": "A prazo (faturado)",
}
# usado pelo modal de novo fornecedor dos formulários de lote (_select_fornecedor.html)
app.jinja_env.globals["FORMAS_PAGAMENTO_FORNECEDOR"] = FORMAS_PAGAMENTO_FORNECEDOR


def _chave_fornecedor(txt) -> str:
    """Normaliza o nome pra comparar: sem acento, minúsculo, só letras/números."""
    import re
    import unicodedata
    t = unicodedata.normalize("NFKD", str(txt or "")).encode("ascii", "ignore").decode().lower()
    t = re.sub(r"[^a-z0-9]+", " ", t).strip()
    return "" if t in ("none", "null") else t


def _fornecedores_para_sugestao() -> list:
    """Fornecedores cadastrados, pra sugerir nos formulários de lote (vazio se
    a tabela ainda não existir)."""
    try:
        return [f for f in db.get_all_fornecedores() if f.get("ativo")]
    except Exception:
        return []


def _fornecedor_cadastrado(texto, fornecedores: list | None = None) -> tuple[str, str]:
    """Resolve um texto de fornecedor pro cadastro. Devolve (nome_cadastrado, texto_sem_cadastro):
    só um dos dois vem preenchido — usado pra pré-selecionar o fornecedor nos formulários."""
    ch = _chave_fornecedor(texto)
    if not ch:
        return "", ""
    for f in (fornecedores if fornecedores is not None else _fornecedores_para_sugestao()):
        if ch in {_chave_fornecedor(n) for n in [f["nome"]] + f["apelidos"]}:
            return f["nome"], ""
    return "", str(texto).strip()


def _auth_fornecedores(gerenciar: bool = False):
    if not usuario_logado():
        return redirect(url_for("login"))
    if session.get("tipo") == "admin":
        return None
    if not tem_permissao("ver_fornecedores") or (gerenciar and not tem_permissao("gerenciar_fornecedores")):
        return redirect(url_for("vendas"))
    return None


def _pode_gerenciar_fornecedores() -> bool:
    return session.get("tipo") == "admin" or tem_permissao("gerenciar_fornecedores")


def _status_parcela(p: dict, hoje_iso: str) -> str:
    if p.get("pago"):
        return "paga"
    return "vencida" if (p.get("vencimento") or "") < hoje_iso else "aberta"


def _status_compra(compra: dict, hoje_iso: str) -> str:
    parcelas = compra.get("parcelas") or []
    if not parcelas:
        return "pendente"
    status = [_status_parcela(p, hoje_iso) for p in parcelas]
    if all(s == "paga" for s in status):
        return "paga"
    if "vencida" in status:
        return "atrasada"
    if "paga" in status:
        return "parcial"
    return "pendente"


def _dados_fornecedores():
    """Busca tudo que as telas de fornecedor usam e monta os cruzamentos:
    fornecedor de cada lote, compra de cada lote e totais por fornecedor."""
    with ThreadPoolExecutor(max_workers=3) as executor:
        fut_f = executor.submit(db.get_all_fornecedores)
        fut_c = executor.submit(db.get_all_fornecedor_compras)
        fut_l = executor.submit(db.get_all_lote_investimentos)
        fornecedores, compras, lotes = fut_f.result(), fut_c.result(), fut_l.result()

    hoje_iso = today_brt().isoformat()
    por_chave = {}
    for f in fornecedores:
        for nome in [f["nome"]] + f["apelidos"]:
            ch = _chave_fornecedor(nome)
            if ch:
                por_chave.setdefault(ch, f)

    compra_do_lote = {}
    for c in compras:
        c["status"] = _status_compra(c, hoje_iso)
        c["em_aberto"] = round(sum(to_float(p.get("valor")) for p in c["parcelas"] if not p.get("pago")), 2)
        c["pago"] = round(sum(to_float(p.get("valor")) for p in c["parcelas"] if p.get("pago")), 2)
        for p in c["parcelas"]:
            p["status"] = _status_parcela(p, hoje_iso)
        for lid in c["lote_ids"]:
            compra_do_lote[lid] = c

    lotes_por_fornecedor = defaultdict(list)
    fora_do_padrao = 0  # lotes de um fornecedor cadastrado escritos com outra grafia
    for li in lotes:
        li["compra"] = compra_do_lote.get(li["id"])
        li["fornecedor_cad"] = None
        f = por_chave.get(_chave_fornecedor(li.get("fornecedor")))
        if f:
            li["fornecedor_cad"] = {"id": f["id"], "nome": f["nome"]}
            lotes_por_fornecedor[f["id"]].append(li)
            if (li.get("fornecedor") or "").strip() != f["nome"]:
                fora_do_padrao += 1

    compras_por_fornecedor = defaultdict(list)
    for c in compras:
        compras_por_fornecedor[c["fornecedor_id"]].append(c)

    for f in fornecedores:
        lf = lotes_por_fornecedor.get(f["id"], [])
        cf = compras_por_fornecedor.get(f["id"], [])
        abertas = sorted(
            (p["vencimento"] for c in cf for p in c["parcelas"] if not p.get("pago") and p.get("vencimento")))
        f["resumo"] = {
            "qtd_lotes": len(lf),
            "total_comprado": round(sum(to_float(li.get("valor_total")) for li in lf), 2),
            "sem_compra": round(sum(to_float(li.get("valor_total")) for li in lf if not li["compra"]), 2),
            "qtd_sem_compra": sum(1 for li in lf if not li["compra"]),
            "em_aberto": round(sum(c["em_aberto"] for c in cf), 2),
            "pago": round(sum(c["pago"] for c in cf), 2),
            "qtd_compras": len(cf),
            "proximo_vencimento": abertas[0] if abertas else "",
            "tem_vencida": any(c["status"] == "atrasada" for c in cf),
            "ultima_compra": max((li.get("data_entrada") or "" for li in lf), default=""),
        }

    return {
        "fornecedores": fornecedores,
        "fornecedores_map": {f["id"]: f for f in fornecedores},
        "compras": compras,
        "lotes_por_fornecedor": lotes_por_fornecedor,
        "compras_por_fornecedor": compras_por_fornecedor,
        "fora_do_padrao": fora_do_padrao,
        "lotes": lotes,
        "hoje_iso": hoje_iso,
    }


def _nome_ja_usado(nome: str, ignorar_id: str | None = None):
    """Fornecedor que já usa esse nome (no nome ou nos apelidos), se houver."""
    ch = _chave_fornecedor(nome)
    for f in db.get_all_fornecedores():
        if f["id"] == ignorar_id:
            continue
        if ch in {_chave_fornecedor(n) for n in [f["nome"]] + f["apelidos"]}:
            return f
    return None


def _dados_form_fornecedor(form) -> dict:
    forma = (form.get("forma_pagamento_padrao") or "").strip()
    return {
        "nome": (form.get("nome") or "").strip(),
        "cnpj": (form.get("cnpj") or "").strip(),
        "telefone": (form.get("telefone") or "").strip(),
        "contato": (form.get("contato") or "").strip(),
        "email": (form.get("email") or "").strip(),
        "chave_pix": (form.get("chave_pix") or "").strip(),
        "forma_pagamento_padrao": forma if forma in FORMAS_PAGAMENTO_FORNECEDOR else "",
        "prazo_dias_padrao": max(0, to_int(form.get("prazo_dias_padrao"), 0)),
        "observacoes": (form.get("observacoes") or "").strip()[:500],
        "ativo": form.get("ativo", "1") != "0",
    }


def _voltar_fornecedores(padrao: str, erro: str = "", msg: str = ""):
    """Volta pra página de origem (só caminhos internos de /fornecedores)."""
    destino = (request.form.get("next") or "").strip()
    if not destino.startswith("/fornecedores"):
        destino = padrao
    params = {k: v for k, v in (("erro", erro), ("msg", msg)) if v}
    if params:
        from urllib.parse import urlencode
        destino += ("&" if "?" in destino else "?") + urlencode(params)
    return redirect(destino)


def _log_fornecedor(tipo_evento: str, descricao: str):
    db.registrar_log(session.get("usuario", ""), tipo_evento, descricao,
                     now_brt().strftime("%Y-%m-%d"), now_brt().strftime("%H:%M"))


@app.route("/fornecedores")
def fornecedores_home():
    redir = _auth_fornecedores()
    if redir: return redir

    base_ctx = dict(
        pode_gerenciar=_pode_gerenciar_fornecedores(),
        formas_pagamento=FORMAS_PAGAMENTO_FORNECEDOR,
        erro=request.args.get("erro", ""), msg=request.args.get("msg", ""),
        tipo=session.get("tipo"), permissoes=session.get("permissoes", []),
    )
    if not db.fornecedores_disponivel():
        return render_template("fornecedores.html", tabelas_ok=False, **base_ctx)

    dados = _dados_fornecedores()
    hoje = today_brt()
    hoje_iso = dados["hoje_iso"]
    em_7_dias = (hoje + timedelta(days=7)).isoformat()
    inicio_mes = hoje.replace(day=1).isoformat()

    contas = []
    kpis = {"a_pagar": 0.0, "vencido": 0.0, "vence_7d": 0.0, "pago_mes": 0.0,
            "qtd_vencidas": 0, "qtd_7d": 0}
    for c in dados["compras"]:
        f = dados["fornecedores_map"].get(c["fornecedor_id"])
        for p in c["parcelas"]:
            valor = to_float(p.get("valor"))
            if p.get("pago"):
                if (p.get("data_pagamento") or "") >= inicio_mes:
                    kpis["pago_mes"] += valor
                continue
            kpis["a_pagar"] += valor
            venc = p.get("vencimento") or ""
            if venc < hoje_iso:
                kpis["vencido"] += valor
                kpis["qtd_vencidas"] += 1
            elif venc <= em_7_dias:
                kpis["vence_7d"] += valor
                kpis["qtd_7d"] += 1
            dias = (parse_date_input(venc) - hoje).days if parse_date_input(venc) else None
            contas.append({"compra": c, "fornecedor": f, "parcela": p, "dias": dias})
    contas.sort(key=lambda x: x["parcela"].get("vencimento") or "9999")
    kpis = {k: (round(v, 2) if isinstance(v, float) else v) for k, v in kpis.items()}

    fornecedores = sorted(dados["fornecedores"], key=lambda f: (not f["ativo"], f["nome"].lower()))

    # Aba "Lotes": lotes que deram entrada no período (veio do Financeiro).
    # Sem período na URL, mostra o mês atual.
    pode_ver_lotes = session.get("tipo") == "admin" or tem_permissao("ver_lotes_periodo")
    lotes_data_ini = (request.args.get("data_ini") or "").strip() or inicio_mes
    lotes_data_fim = (request.args.get("data_fim") or "").strip() or hoje_iso
    lotes_periodo, resumo_lotes = [], {}
    if pode_ver_lotes:
        usuarios_map = db.get_all_users()
        lotes_periodo = sorted(
            (li for li in dados["lotes"] if lotes_data_ini <= (li.get("data_entrada") or "") <= lotes_data_fim),
            key=lambda li: (li.get("data_entrada") or "", li.get("criado_em") or ""), reverse=True)
        gasto_por_fornecedor = defaultdict(float)
        for li in lotes_periodo:
            criador = li.get("criado_por") or ""
            li["criado_por_nome"] = usuarios_map.get(criador, {}).get("nome") or criador
            nome_f = li["fornecedor_cad"]["nome"] if li["fornecedor_cad"] else (li.get("fornecedor") or "").strip()
            if _chave_fornecedor(nome_f):
                gasto_por_fornecedor[nome_f] += to_float(li.get("valor_total"))
        resumo_lotes = {
            "qtd": len(lotes_periodo),
            "produtos": len({li.get("pid") or li.get("produto_nome") for li in lotes_periodo}),
            "total": round(sum(to_float(li.get("valor_total")) for li in lotes_periodo), 2),
            "maior_fornecedor": max(gasto_por_fornecedor, key=gasto_por_fornecedor.get) if gasto_por_fornecedor else "",
            "maior_fornecedor_valor": round(max(gasto_por_fornecedor.values()), 2) if gasto_por_fornecedor else 0.0,
        }

    return render_template(
        "fornecedores.html", tabelas_ok=True,
        pode_ver_lotes=pode_ver_lotes, lotes_periodo=lotes_periodo, resumo_lotes=resumo_lotes,
        lotes_data_ini=lotes_data_ini, lotes_data_fim=lotes_data_fim,
        ultima_compra_data=max((li.get("data_entrada") or "" for li in dados["lotes"]), default=""),
        fornecedores=fornecedores, contas=contas, kpis=kpis,
        fora_do_padrao=dados["fora_do_padrao"],
        hoje_iso=hoje_iso, **base_ctx,
    )


@app.route("/fornecedores/novo", methods=["POST"])
def fornecedor_novo():
    # Vindo do modal dos formulários de lote/produto (fetch): responde JSON
    # pra página continuar onde está; pela tela Fornecedores, redirect normal.
    via_fetch = request.headers.get("X-Requested-With") == "fetch"
    redir = _auth_fornecedores(gerenciar=True)
    if redir:
        return (jsonify({"erro": "Você não tem permissão para cadastrar fornecedores."}), 403) if via_fetch else redir

    def falha(msg, status=400):
        if via_fetch:
            return jsonify({"erro": msg}), status
        return _voltar_fornecedores(url_for("fornecedores_home"), erro=msg)

    dados = _dados_form_fornecedor(request.form)
    if not dados["nome"]:
        return falha("Informe o nome do fornecedor.")
    existente = _nome_ja_usado(dados["nome"])
    if existente:
        return falha(f"Esse nome já pertence ao fornecedor “{existente['nome']}”.")
    # Criado a partir de um nome antigo dos lotes: guarda a grafia original
    # como apelido se ela for diferente do nome escolhido.
    apelidos = []
    for nome_antigo in request.form.getlist("apelidos[]"):
        nome_antigo = (nome_antigo or "").strip()
        if nome_antigo and _chave_fornecedor(nome_antigo) != _chave_fornecedor(dados["nome"]) and nome_antigo not in apelidos:
            apelidos.append(nome_antigo)
    novo = {
        "id": uuid.uuid4().hex, **dados, "apelidos": apelidos,
        "criado_por": session.get("usuario", ""), "criado_em": now_brt().strftime("%Y-%m-%d %H:%M"),
    }
    try:
        db.insert_fornecedor(novo)
    except RuntimeError as e:
        return falha(str(e), 500)
    _log_fornecedor("fornecedor_criado", f"Cadastrou fornecedor: {novo['nome']}")
    if via_fetch:
        return jsonify({"id": novo["id"], "nome": novo["nome"]})
    return _voltar_fornecedores(url_for("fornecedor_detalhe", fid=novo["id"]), msg="Fornecedor cadastrado.")


@app.route("/fornecedores/<fid>/editar", methods=["POST"])
def fornecedor_editar(fid):
    redir = _auth_fornecedores(gerenciar=True)
    if redir: return redir
    fornecedor = db.get_fornecedor(fid)
    if not fornecedor:
        return redirect(url_for("fornecedores_home"))
    dados = _dados_form_fornecedor(request.form)
    voltar = url_for("fornecedor_detalhe", fid=fid)
    if not dados["nome"]:
        return _voltar_fornecedores(voltar, erro="Informe o nome do fornecedor.")
    existente = _nome_ja_usado(dados["nome"], ignorar_id=fid)
    if existente:
        return _voltar_fornecedores(voltar, erro=f"Esse nome já pertence ao fornecedor “{existente['nome']}”.")
    # Trocou o nome: o nome antigo vira apelido, pra não "soltar" os lotes dele.
    if _chave_fornecedor(fornecedor["nome"]) != _chave_fornecedor(dados["nome"]):
        fornecedor["apelidos"].append(fornecedor["nome"])
    fornecedor.update(dados)
    fornecedor["apelidos"] = [a for a in fornecedor["apelidos"] if _chave_fornecedor(a) != _chave_fornecedor(dados["nome"])]
    try:
        db.update_fornecedor(fornecedor)
    except RuntimeError as e:
        return _voltar_fornecedores(voltar, erro=str(e))
    _log_fornecedor("fornecedor_editado", f"Editou fornecedor: {fornecedor['nome']}")
    return _voltar_fornecedores(voltar, msg="Dados salvos.")


@app.route("/fornecedores/padronizar-nomes", methods=["POST"])
def fornecedores_padronizar_nomes():
    """Reescreve o fornecedor dos lotes (e dos produtos) com o nome do
    cadastro — some com as várias grafias do mesmo fornecedor. Os apelidos
    continuam guardados, então nada se perde se alguém digitar o nome antigo."""
    redir = _auth_fornecedores(gerenciar=True)
    if redir: return redir
    voltar = url_for("fornecedores_home", aba="fornecedores")
    fornecedores = db.get_all_fornecedores()
    por_chave = {}
    for f in fornecedores:
        for nome in [f["nome"]] + f["apelidos"]:
            if _chave_fornecedor(nome):
                por_chave.setdefault(_chave_fornecedor(nome), f["nome"])

    def nome_certo(texto):
        atual = (texto or "").strip()
        certo = por_chave.get(_chave_fornecedor(atual))
        return certo if certo and certo != atual else None

    # 1) ledger de lotes (Financeiro / Investidores / Fornecedores)
    ids_por_nome = defaultdict(list)
    for li in db.get_all_lote_investimentos(incluir_removidos=True):
        certo = nome_certo(li.get("fornecedor"))
        if certo:
            ids_por_nome[certo].append(li["id"])
    # 2) lotes dentro de cada produto + fornecedor do produto
    produtos_alterados = []
    lotes_produto = 0
    for produto in db.get_all_products():
        mudou = False
        for l in produto.get("lotes") or []:
            certo = nome_certo(l.get("fornecedor"))
            if certo:
                l["fornecedor"] = certo
                lotes_produto += 1
                mudou = True
        certo = nome_certo(produto.get("fornecedor"))
        if certo:
            produto["fornecedor"] = certo
            mudou = True
        if mudou:
            produtos_alterados.append(produto)

    try:
        for nome, ids in ids_por_nome.items():
            db.update_fornecedor_lotes_investimento(ids, nome)
        for produto in produtos_alterados:
            db.update_product(produto)
    except Exception as e:
        return _voltar_fornecedores(voltar, erro=f"Parou no meio: {e}. Pode clicar de novo para continuar.")

    qtd_lotes = sum(len(ids) for ids in ids_por_nome.values())
    _log_fornecedor("fornecedor_nomes_padronizados",
                    f"Padronizou o fornecedor de {qtd_lotes} lote(s) e {len(produtos_alterados)} produto(s)")
    return _voltar_fornecedores(
        voltar, msg=f"Pronto: {qtd_lotes} lote(s) e {len(produtos_alterados)} produto(s) agora usam o nome do cadastro.")


@app.route("/fornecedores/<fid>")
def fornecedor_detalhe(fid):
    redir = _auth_fornecedores()
    if redir: return redir
    if not db.fornecedores_disponivel():
        return redirect(url_for("fornecedores_home"))
    dados = _dados_fornecedores()
    fornecedor = dados["fornecedores_map"].get(fid)
    if not fornecedor:
        return redirect(url_for("fornecedores_home"))

    data_ini_raw = (request.args.get("data_ini") or "").strip()
    data_fim_raw = (request.args.get("data_fim") or "").strip()
    lotes = sorted(dados["lotes_por_fornecedor"].get(fid, []),
                   key=lambda li: (li.get("data_entrada") or "", li.get("criado_em") or ""), reverse=True)
    if data_ini_raw:
        lotes = [li for li in lotes if (li.get("data_entrada") or "") >= data_ini_raw]
    if data_fim_raw:
        lotes = [li for li in lotes if (li.get("data_entrada") or "") <= data_fim_raw]

    lotes_map = {li["id"]: li for li in dados["lotes_por_fornecedor"].get(fid, [])}
    compras = dados["compras_por_fornecedor"].get(fid, [])
    for c in compras:
        c["lotes"] = [lotes_map[lid] for lid in c["lote_ids"] if lid in lotes_map]

    return render_template(
        "fornecedor_detalhe.html",
        fornecedor=fornecedor, lotes=lotes, compras=compras,
        data_ini_raw=data_ini_raw, data_fim_raw=data_fim_raw,
        hoje_iso=dados["hoje_iso"],
        pode_gerenciar=_pode_gerenciar_fornecedores(),
        formas_pagamento=FORMAS_PAGAMENTO_FORNECEDOR,
        erro=request.args.get("erro", ""), msg=request.args.get("msg", ""),
        aba=request.args.get("aba", ""),
        tipo=session.get("tipo"), permissoes=session.get("permissoes", []),
    )


@app.route("/fornecedores/<fid>/compras/nova", methods=["POST"])
def fornecedor_compra_nova(fid):
    redir = _auth_fornecedores(gerenciar=True)
    if redir: return redir
    voltar = url_for("fornecedor_detalhe", fid=fid, aba="lotes")
    dados = _dados_fornecedores()
    fornecedor = dados["fornecedores_map"].get(fid)
    if not fornecedor:
        return redirect(url_for("fornecedores_home"))

    lotes_fornecedor = {li["id"]: li for li in dados["lotes_por_fornecedor"].get(fid, [])}
    lote_ids = list(dict.fromkeys(request.form.getlist("lote_ids[]")))
    if not lote_ids:
        return _voltar_fornecedores(voltar, erro="Selecione pelo menos um lote.")
    for lid in lote_ids:
        li = lotes_fornecedor.get(lid)
        if not li:
            return _voltar_fornecedores(voltar, erro="Um dos lotes selecionados não é deste fornecedor.")
        if li["compra"]:
            return _voltar_fornecedores(voltar, erro=f"O lote de {li['produto_nome']} já está em outra compra.")

    valor_lotes = round(sum(to_float(lotes_fornecedor[lid].get("valor_total")) for lid in lote_ids), 2)
    valor_total = round(to_float(request.form.get("valor_total"), valor_lotes), 2)
    forma = (request.form.get("forma_pagamento") or "").strip()
    if forma not in FORMAS_PAGAMENTO_FORNECEDOR:
        return _voltar_fornecedores(voltar, erro="Escolha a forma de pagamento.")
    if valor_total <= 0:
        return _voltar_fornecedores(voltar, erro="Valor da compra inválido.")

    vencimentos = request.form.getlist("parcela_vencimento[]")
    valores = request.form.getlist("parcela_valor[]")
    ja_paga = request.form.get("ja_paga") == "1"
    hoje_iso = today_brt().isoformat()
    parcelas = []
    for i, (venc, val) in enumerate(zip(vencimentos, valores), start=1):
        venc = (venc or "").strip()
        if not parse_date_input(venc):
            return _voltar_fornecedores(voltar, erro=f"Vencimento da parcela {i} inválido.")
        parcelas.append({
            "n": i, "vencimento": venc, "valor": round(to_float(val), 2),
            "pago": ja_paga,
            "data_pagamento": hoje_iso if ja_paga else "",
            "forma_pago": forma if ja_paga else "",
            "pago_por": session.get("usuario", "") if ja_paga else "",
        })
    if not parcelas:
        return _voltar_fornecedores(voltar, erro="Informe ao menos uma parcela.")
    if any(p["valor"] <= 0 for p in parcelas):
        return _voltar_fornecedores(voltar, erro="Toda parcela precisa ter valor maior que zero.")
    soma = round(sum(p["valor"] for p in parcelas), 2)
    if abs(soma - valor_total) > 0.01:
        return _voltar_fornecedores(
            voltar, erro=f"A soma das parcelas (R$ {soma:.2f}) precisa ser igual ao valor da compra (R$ {valor_total:.2f}).")

    compra = {
        "id": uuid.uuid4().hex,
        "fornecedor_id": fid,
        "data_compra": normalizar_data_str(request.form.get("data_compra")),
        "numero_nota": (request.form.get("numero_nota") or "").strip(),
        "lote_ids": lote_ids,
        "valor_lotes": valor_lotes,
        "valor_total": valor_total,
        "forma_pagamento": forma,
        "parcelas": parcelas,
        "observacoes": (request.form.get("observacoes") or "").strip()[:500],
        "criado_por": session.get("usuario", ""),
        "criado_em": now_brt().strftime("%Y-%m-%d %H:%M"),
        "removida": False,
    }
    try:
        db.insert_fornecedor_compra(compra)
    except RuntimeError as e:
        return _voltar_fornecedores(voltar, erro=str(e))
    _log_fornecedor("fornecedor_compra_criada",
                    f"Registrou compra de R$ {valor_total:.2f} ({len(lote_ids)} lote(s)) com {fornecedor['nome']}")
    return redirect(url_for("fornecedor_detalhe", fid=fid, aba="compras", msg="Compra registrada."))


def _compra_e_parcela(cid: str, n: int):
    compra = db.get_fornecedor_compra(cid)
    if not compra or compra["removida"]:
        return None, None
    parcela = next((p for p in compra["parcelas"] if to_int(p.get("n")) == n), None)
    return compra, parcela


@app.route("/fornecedores/compras/<cid>/parcela/<int:n>/pagar", methods=["POST"])
def fornecedor_parcela_pagar(cid, n):
    redir = _auth_fornecedores(gerenciar=True)
    if redir: return redir
    compra, parcela = _compra_e_parcela(cid, n)
    if not parcela:
        return _voltar_fornecedores(url_for("fornecedores_home"), erro="Parcela não encontrada.")
    voltar = url_for("fornecedor_detalhe", fid=compra["fornecedor_id"], aba="compras")
    data_pag = (request.form.get("data_pagamento") or "").strip()
    forma = (request.form.get("forma_pago") or compra["forma_pagamento"] or "").strip()
    parcela.update({
        "pago": True,
        "data_pagamento": data_pag if parse_date_input(data_pag) else today_brt().isoformat(),
        "forma_pago": forma if forma in FORMAS_PAGAMENTO_FORNECEDOR else compra["forma_pagamento"],
        "pago_por": session.get("usuario", ""),
    })
    try:
        db.update_fornecedor_compra(compra)
    except RuntimeError as e:
        return _voltar_fornecedores(voltar, erro=str(e))
    fornecedor = db.get_fornecedor(compra["fornecedor_id"]) or {"nome": "?"}
    _log_fornecedor("fornecedor_parcela_paga",
                    f"Pagou parcela {n} (R$ {to_float(parcela.get('valor')):.2f}) para {fornecedor['nome']}")
    return _voltar_fornecedores(voltar, msg=f"Parcela {n} marcada como paga.")


@app.route("/fornecedores/compras/<cid>/parcela/<int:n>/desfazer", methods=["POST"])
def fornecedor_parcela_desfazer(cid, n):
    redir = _auth_fornecedores(gerenciar=True)
    if redir: return redir
    compra, parcela = _compra_e_parcela(cid, n)
    if not parcela:
        return _voltar_fornecedores(url_for("fornecedores_home"), erro="Parcela não encontrada.")
    voltar = url_for("fornecedor_detalhe", fid=compra["fornecedor_id"], aba="compras")
    parcela.update({"pago": False, "data_pagamento": "", "forma_pago": "", "pago_por": ""})
    try:
        db.update_fornecedor_compra(compra)
    except RuntimeError as e:
        return _voltar_fornecedores(voltar, erro=str(e))
    fornecedor = db.get_fornecedor(compra["fornecedor_id"]) or {"nome": "?"}
    _log_fornecedor("fornecedor_parcela_desfeita", f"Desfez pagamento da parcela {n} de {fornecedor['nome']}")
    return _voltar_fornecedores(voltar, msg=f"Pagamento da parcela {n} desfeito.")


@app.route("/fornecedores/compras/<cid>/remover", methods=["POST"])
def fornecedor_compra_remover(cid):
    redir = _auth_fornecedores(gerenciar=True)
    if redir: return redir
    compra = db.get_fornecedor_compra(cid)
    if not compra:
        return redirect(url_for("fornecedores_home"))
    voltar = url_for("fornecedor_detalhe", fid=compra["fornecedor_id"], aba="compras")
    compra["removida"] = True
    try:
        db.update_fornecedor_compra(compra)
    except RuntimeError as e:
        return _voltar_fornecedores(voltar, erro=str(e))
    fornecedor = db.get_fornecedor(compra["fornecedor_id"]) or {"nome": "?"}
    _log_fornecedor("fornecedor_compra_removida",
                    f"Excluiu compra de R$ {compra['valor_total']:.2f} de {fornecedor['nome']}")
    return _voltar_fornecedores(voltar, msg="Compra excluída. Os lotes dela ficaram livres de novo.")


# ================== RUN ==================
if __name__ == "__main__":
    app.run(debug=True, port=5003)
