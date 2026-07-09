-- ==================== ISA MODAS - SUPABASE SCHEMA ====================
-- Execute este arquivo no SQL Editor do Supabase

-- Usuários
CREATE TABLE IF NOT EXISTS users (
    username    TEXT PRIMARY KEY,
    senha       TEXT NOT NULL,
    tipo        TEXT NOT NULL DEFAULT 'funcionario',
    permissoes  TEXT[] NOT NULL DEFAULT '{}',
    ativo       BOOLEAN NOT NULL DEFAULT TRUE
);
-- Se a tabela já existir, rodar no Supabase SQL Editor:
-- ALTER TABLE users ADD COLUMN IF NOT EXISTS ativo BOOLEAN NOT NULL DEFAULT TRUE;

-- Produtos (estoque)
-- lotes é JSONB: [{data, qtd, custo_unit}, ...]
-- Obs: o banco real também tem colunas extras (categoria, tipo_controle,
-- conteudo_valor, conteudo_unidade, unidade_venda, estoque_minimo, validade,
-- fornecedor, tipo_conversao, unidades_por_caixa, observacoes) adicionadas
-- direto no Supabase, ainda não refletidas neste arquivo.
CREATE TABLE IF NOT EXISTS products (
    pid              TEXT PRIMARY KEY,
    nome             TEXT NOT NULL,
    valor_venda      NUMERIC(10,2) NOT NULL DEFAULT 0,
    data             TEXT NOT NULL DEFAULT '',
    quantidade       INTEGER NOT NULL DEFAULT 0,
    valor_investido  NUMERIC(10,6) NOT NULL DEFAULT 0,
    lotes            JSONB NOT NULL DEFAULT '[]',
    codigo           TEXT UNIQUE
);
-- Se a tabela já existir, rodar no Supabase SQL Editor:
-- ALTER TABLE products ADD COLUMN IF NOT EXISTS codigo TEXT UNIQUE;

-- Vendas
-- consumo_lotes é JSONB: [{data_lote, qtd, custo_unit}, ...]
CREATE TABLE IF NOT EXISTS sales (
    uid              TEXT PRIMARY KEY,
    id_venda         TEXT NOT NULL DEFAULT '',
    data             TEXT NOT NULL DEFAULT '',
    produto_pid      TEXT,
    nome             TEXT NOT NULL DEFAULT '',
    valor_venda      NUMERIC(10,2) NOT NULL DEFAULT 0,
    valor_investido  NUMERIC(10,6) NOT NULL DEFAULT 0,
    quantidade       INTEGER NOT NULL DEFAULT 0,
    forma_pagamento  TEXT NOT NULL DEFAULT 'cartao',
    consumo_lotes    JSONB NOT NULL DEFAULT '[]',
    fechado          BOOLEAN NOT NULL DEFAULT FALSE,
    fechado_em       TEXT,
    caixa_dia        TEXT,
    comanda_id       TEXT,
    comanda_nome     TEXT,
    produto_tipo     TEXT DEFAULT 'estoque',
    codigo           TEXT UNIQUE,
    codigo_produto   TEXT,
    hora             TEXT,
    usuario          TEXT,
    excluida         BOOLEAN NOT NULL DEFAULT FALSE,
    excluida_em      TEXT,
    excluida_por     TEXT
);
-- Se a tabela já existir, rodar no Supabase SQL Editor:
-- ALTER TABLE sales ADD COLUMN IF NOT EXISTS codigo TEXT UNIQUE;
-- ALTER TABLE sales ADD COLUMN IF NOT EXISTS codigo_produto TEXT;
-- ALTER TABLE sales ADD COLUMN IF NOT EXISTS hora TEXT;
-- ALTER TABLE sales ADD COLUMN IF NOT EXISTS usuario TEXT;
-- ALTER TABLE sales ADD COLUMN IF NOT EXISTS excluida BOOLEAN NOT NULL DEFAULT FALSE;
-- ALTER TABLE sales ADD COLUMN IF NOT EXISTS excluida_em TEXT;
-- ALTER TABLE sales ADD COLUMN IF NOT EXISTS excluida_por TEXT;

-- Ledger permanente de códigos de rastreio já emitidos (nunca é apagada, nem
-- quando o registro correspondente é cancelado/excluído de vez) — garante que
-- um código (formato "<PREFIXO>-XXXXX": E=estoque, P=pacote do bar, V=venda)
-- nunca seja reutilizado.
CREATE TABLE IF NOT EXISTS tracking_codes (
    codigo     TEXT PRIMARY KEY,
    criado_em  TEXT
);

-- Clientes (fiado)
CREATE TABLE IF NOT EXISTS clientes (
    id                 TEXT PRIMARY KEY,
    nome               TEXT NOT NULL,
    apelido            TEXT DEFAULT '',
    telefone           TEXT NOT NULL DEFAULT '',
    cpf                TEXT DEFAULT '',
    rg                 TEXT DEFAULT '',
    endereco           TEXT DEFAULT '',
    observacoes        TEXT DEFAULT '',
    limite_credito     NUMERIC(10,2) NOT NULL DEFAULT 0,
    dia_vencimento     INTEGER,
    proximo_vencimento TEXT DEFAULT '',
    data_cadastro      TEXT DEFAULT ''
);

-- Comandas (mesas)
-- itens é JSONB: [{item_id, produto_pid, nome, quantidade, valor_venda, valor_investido, consumo_lotes, hora}, ...]
CREATE TABLE IF NOT EXISTS commands (
    cid               TEXT PRIMARY KEY,
    nome              TEXT NOT NULL,
    data_abertura     TEXT,
    hora_abertura     TEXT,
    status            TEXT NOT NULL DEFAULT 'aberta',
    itens             JSONB NOT NULL DEFAULT '[]',
    data_fechamento   TEXT,
    hora_fechamento   TEXT,
    forma_pagamento   TEXT,
    tipo_atendimento  TEXT DEFAULT 'avulso',
    cliente_id        TEXT,
    historico         JSONB NOT NULL DEFAULT '[]'
);

-- Caixa (fechamentos diários)
-- vendas_uids é JSONB: ["uid1", "uid2", ...]
CREATE TABLE IF NOT EXISTS cash_registers (
    id               TEXT PRIMARY KEY,
    dia              TEXT NOT NULL,
    data_fechamento  TEXT,
    pix              NUMERIC(10,2) NOT NULL DEFAULT 0,
    dinheiro         NUMERIC(10,2) NOT NULL DEFAULT 0,
    cartao           NUMERIC(10,2) NOT NULL DEFAULT 0,
    total            NUMERIC(10,2) NOT NULL DEFAULT 0,
    qtd_vendas       INTEGER NOT NULL DEFAULT 0,
    vendas_uids      JSONB NOT NULL DEFAULT '[]'
);

-- Sessões de caixa (PDV completo)
CREATE TABLE IF NOT EXISTS caixas (
    id                  TEXT PRIMARY KEY,
    status              TEXT NOT NULL DEFAULT 'aberto',
    data_abertura       TEXT NOT NULL,
    hora_abertura       TEXT NOT NULL,
    operador_abertura   TEXT NOT NULL,
    valor_abertura      NUMERIC(10,2) NOT NULL DEFAULT 0,
    data_fechamento     TEXT,
    hora_fechamento     TEXT,
    operador_fechamento TEXT,
    total_dinheiro      NUMERIC(10,2) DEFAULT 0,
    total_pix           NUMERIC(10,2) DEFAULT 0,
    total_cartao_cred   NUMERIC(10,2) DEFAULT 0,
    total_cartao_deb    NUMERIC(10,2) DEFAULT 0,
    total_fiado         NUMERIC(10,2) DEFAULT 0,
    total_sangrias      NUMERIC(10,2) DEFAULT 0,
    total_suprimentos   NUMERIC(10,2) DEFAULT 0,
    total_despesas      NUMERIC(10,2) DEFAULT 0,
    total_vendas        NUMERIC(10,2) DEFAULT 0,
    qtd_vendas          INTEGER DEFAULT 0,
    conferencia         JSONB NOT NULL DEFAULT '{}',
    diferencas          JSONB NOT NULL DEFAULT '{}',
    vendas_uids         JSONB NOT NULL DEFAULT '[]'
);

-- Movimentações manuais de caixa (sangria, suprimento, despesa)
CREATE TABLE IF NOT EXISTS caixa_movimentacoes (
    id          TEXT PRIMARY KEY,
    caixa_id    TEXT NOT NULL,
    tipo        TEXT NOT NULL,
    valor       NUMERIC(10,2) NOT NULL,
    descricao   TEXT DEFAULT '',
    data        TEXT NOT NULL,
    hora        TEXT NOT NULL,
    usuario     TEXT NOT NULL
);

-- Índices para buscas frequentes
CREATE INDEX IF NOT EXISTS idx_sales_data ON sales(data);
CREATE INDEX IF NOT EXISTS idx_sales_fechado ON sales(fechado);
CREATE INDEX IF NOT EXISTS idx_sales_caixa_dia ON sales(caixa_dia);
CREATE INDEX IF NOT EXISTS idx_sales_codigo ON sales(codigo);
CREATE INDEX IF NOT EXISTS idx_sales_produto_pid ON sales(produto_pid);
CREATE INDEX IF NOT EXISTS idx_sales_codigo_produto ON sales(codigo_produto);
CREATE INDEX IF NOT EXISTS idx_products_codigo ON products(codigo);
CREATE INDEX IF NOT EXISTS idx_cash_registers_dia ON cash_registers(dia);
CREATE INDEX IF NOT EXISTS idx_commands_status ON commands(status);
CREATE INDEX IF NOT EXISTS idx_caixas_status ON caixas(status);
CREATE INDEX IF NOT EXISTS idx_caixa_mov_caixa_id ON caixa_movimentacoes(caixa_id);

-- Seed: usuários iniciais (não sobrescreve se já existirem)
INSERT INTO users (username, senha, tipo, permissoes)
VALUES
    ('admin', 'kelvin0800', 'admin', '{}'),
    ('isa', 'isa2026', 'funcionario', '{"vendas"}')
ON CONFLICT (username) DO NOTHING;
