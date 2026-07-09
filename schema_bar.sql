-- ==================== BAR - SCHEMA SUPABASE ====================
-- Execute no SQL Editor do Supabase

-- Produtos do bar (simples = insumo/ingrediente, composite = drink)
CREATE TABLE IF NOT EXISTS bar_products (
    id          TEXT PRIMARY KEY DEFAULT gen_random_uuid()::text,
    name        TEXT NOT NULL,
    category    TEXT NOT NULL DEFAULT 'insumo',   -- 'bebida', 'comida', 'insumo'
    type        TEXT NOT NULL DEFAULT 'simple',   -- 'simple' | 'composite'
    unit        TEXT NOT NULL DEFAULT 'un',       -- 'ml', 'g', 'un'
    sale_price  NUMERIC(10,2) NOT NULL DEFAULT 0,
    active      BOOLEAN NOT NULL DEFAULT true,
    created_at  TIMESTAMPTZ DEFAULT NOW(),
    codigo      TEXT UNIQUE  -- código de rastreio, prefixo "P" (ver tracking_codes em schema.sql)
);
-- Se a tabela já existir, rodar no Supabase SQL Editor:
-- ALTER TABLE bar_products ADD COLUMN IF NOT EXISTS codigo TEXT UNIQUE;

-- Lotes de estoque (controle FIFO por produto simples)
-- quantity_total e quantity_remaining estão SEMPRE na unidade base (ml, g, ou un)
-- unit_value: quantas unidades base tem cada item comprado (ex: 1000 ml por garrafa)
CREATE TABLE IF NOT EXISTS bar_stock_batches (
    id                 TEXT PRIMARY KEY DEFAULT gen_random_uuid()::text,
    product_id         TEXT NOT NULL REFERENCES bar_products(id),
    quantity_total     NUMERIC(14,6) NOT NULL,      -- total em unidade base (quantity * unit_value)
    quantity_remaining NUMERIC(14,6) NOT NULL,      -- restante em unidade base
    unit_value         NUMERIC(10,4) NOT NULL DEFAULT 1, -- unidade base por item comprado
    total_cost         NUMERIC(10,2) NOT NULL DEFAULT 0,
    created_at         TIMESTAMPTZ DEFAULT NOW()
);

-- Se a tabela já existe, adicione unit_value:
-- ALTER TABLE bar_stock_batches ADD COLUMN IF NOT EXISTS unit_value NUMERIC(10,4) NOT NULL DEFAULT 1;

-- Componentes de receita (ingredientes de drinks compostos)
CREATE TABLE IF NOT EXISTS bar_product_components (
    id           TEXT PRIMARY KEY DEFAULT gen_random_uuid()::text,
    product_id   TEXT NOT NULL REFERENCES bar_products(id),   -- drink (composite)
    component_id TEXT NOT NULL REFERENCES bar_products(id),   -- ingrediente (simple)
    quantity     NUMERIC(14,6) NOT NULL,                      -- quantidade na unidade base
    created_at   TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(product_id, component_id)
);

-- Vendas do bar (cabeçalho)
CREATE TABLE IF NOT EXISTS bar_sales (
    id          TEXT PRIMARY KEY DEFAULT gen_random_uuid()::text,
    total_price NUMERIC(10,2) NOT NULL DEFAULT 0,
    total_cost  NUMERIC(10,2) NOT NULL DEFAULT 0,
    profit      NUMERIC(10,2) NOT NULL DEFAULT 0,
    created_at  TIMESTAMPTZ DEFAULT NOW()
);

-- Itens de venda do bar (detalhe)
CREATE TABLE IF NOT EXISTS bar_sale_items (
    id                TEXT PRIMARY KEY DEFAULT gen_random_uuid()::text,
    sale_id           TEXT NOT NULL REFERENCES bar_sales(id),
    product_id        TEXT NOT NULL REFERENCES bar_products(id),
    quantity          NUMERIC(14,6) NOT NULL,
    price_unit        NUMERIC(10,2) NOT NULL DEFAULT 0,
    cost_unit         NUMERIC(10,4) NOT NULL DEFAULT 0,
    profit_unit       NUMERIC(10,4) NOT NULL DEFAULT 0,
    batch_consumption JSONB NOT NULL DEFAULT '[]',
    created_at        TIMESTAMPTZ DEFAULT NOW()
);

-- ==================== MIGRAÇÃO: bar_products ====================
-- Adiciona os novos campos ao produto do bar (execute se a tabela já existir):
ALTER TABLE bar_products ADD COLUMN IF NOT EXISTS composition_type TEXT DEFAULT NULL;
ALTER TABLE bar_products ADD COLUMN IF NOT EXISTS unit_value       NUMERIC(12,6) NOT NULL DEFAULT 1;

-- ==================== MIGRAÇÃO: tabela sales ====================
-- Adiciona produto_tipo para distinguir itens do estoque de drinks do bar
-- Execute apenas se a coluna ainda não existir:
ALTER TABLE sales ADD COLUMN IF NOT EXISTS produto_tipo TEXT NOT NULL DEFAULT 'estoque';

-- Índices
CREATE INDEX IF NOT EXISTS idx_bar_products_active     ON bar_products(active);
CREATE INDEX IF NOT EXISTS idx_bar_products_codigo     ON bar_products(codigo);
CREATE INDEX IF NOT EXISTS idx_bar_batches_product     ON bar_stock_batches(product_id);
CREATE INDEX IF NOT EXISTS idx_bar_batches_remaining   ON bar_stock_batches(quantity_remaining);
CREATE INDEX IF NOT EXISTS idx_bar_components_product  ON bar_product_components(product_id);
CREATE INDEX IF NOT EXISTS idx_bar_sales_created       ON bar_sales(created_at);
CREATE INDEX IF NOT EXISTS idx_bar_sale_items_sale     ON bar_sale_items(sale_id);
