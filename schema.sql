-- ==================== ISA MODAS - SUPABASE SCHEMA ====================
-- Execute este arquivo no SQL Editor do Supabase

-- Usuários
CREATE TABLE IF NOT EXISTS users (
    username    TEXT PRIMARY KEY,
    senha       TEXT NOT NULL,
    tipo        TEXT NOT NULL DEFAULT 'funcionario',
    permissoes  TEXT[] NOT NULL DEFAULT '{}'
);

-- Produtos (estoque)
-- lotes é JSONB: [{data, qtd, custo_unit}, ...]
CREATE TABLE IF NOT EXISTS products (
    pid              TEXT PRIMARY KEY,
    nome             TEXT NOT NULL,
    valor_venda      NUMERIC(10,2) NOT NULL DEFAULT 0,
    data             TEXT NOT NULL DEFAULT '',
    quantidade       INTEGER NOT NULL DEFAULT 0,
    valor_investido  NUMERIC(10,6) NOT NULL DEFAULT 0,
    lotes            JSONB NOT NULL DEFAULT '[]'
);

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
    comanda_nome     TEXT
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
    forma_pagamento   TEXT
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

-- Índices para buscas frequentes
CREATE INDEX IF NOT EXISTS idx_sales_data ON sales(data);
CREATE INDEX IF NOT EXISTS idx_sales_fechado ON sales(fechado);
CREATE INDEX IF NOT EXISTS idx_sales_caixa_dia ON sales(caixa_dia);
CREATE INDEX IF NOT EXISTS idx_cash_registers_dia ON cash_registers(dia);
CREATE INDEX IF NOT EXISTS idx_commands_status ON commands(status);

-- Seed: usuários iniciais (não sobrescreve se já existirem)
INSERT INTO users (username, senha, tipo, permissoes)
VALUES
    ('admin', 'kelvin0800', 'admin', '{}'),
    ('isa', 'isa2026', 'funcionario', '{"vendas"}')
ON CONFLICT (username) DO NOTHING;
