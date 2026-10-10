-- Preço por local (Loja / Escritório) para produtos do estoque e pacotes/combos.
-- Rodar uma vez no SQL Editor do Supabase.
-- precos_local guarda só os locais com preço diferente do padrão, ex.: {"escritorio": 12.0}.
-- Local sem preço próprio usa o preço padrão (valor_venda / sale_price).

ALTER TABLE products     ADD COLUMN IF NOT EXISTS precos_local JSONB NOT NULL DEFAULT '{}';
ALTER TABLE bar_products ADD COLUMN IF NOT EXISTS precos_local JSONB NOT NULL DEFAULT '{}';

-- Faz a API do Supabase enxergar as colunas novas na hora.
NOTIFY pgrst, 'reload schema';
