-- Tela de Fornecedores: cadastro de fornecedores e compras (pagamentos a fornecedor).
-- Rodar uma vez no SQL Editor do Supabase.
--
-- Os lotes continuam em lote_investimentos com o fornecedor em texto. Um lote
-- pertence a um fornecedor quando esse texto bate com o nome dele ou com um dos
-- "apelidos" (nomes antigos juntados na tela) — por isso lote_investimentos não muda.

CREATE TABLE IF NOT EXISTS fornecedores (
    id                      TEXT PRIMARY KEY,
    nome                    TEXT NOT NULL,
    apelidos                JSONB NOT NULL DEFAULT '[]',
    cnpj                    TEXT DEFAULT '',
    telefone                TEXT DEFAULT '',
    contato                 TEXT DEFAULT '',
    email                   TEXT DEFAULT '',
    chave_pix               TEXT DEFAULT '',
    forma_pagamento_padrao  TEXT DEFAULT '',
    prazo_dias_padrao       INTEGER DEFAULT 0,
    observacoes             TEXT DEFAULT '',
    ativo                   BOOLEAN NOT NULL DEFAULT TRUE,
    criado_por              TEXT DEFAULT '',
    criado_em               TEXT DEFAULT ''
);

-- Uma compra agrupa vários lotes de uma mesma entrega/nota e guarda como ela é paga.
-- parcelas: [{n, vencimento, valor, pago, data_pagamento, forma_pago, pago_por}]
CREATE TABLE IF NOT EXISTS fornecedor_compras (
    id               TEXT PRIMARY KEY,
    fornecedor_id    TEXT NOT NULL,
    data_compra      TEXT NOT NULL,
    numero_nota      TEXT DEFAULT '',
    lote_ids         JSONB NOT NULL DEFAULT '[]',
    valor_lotes      NUMERIC(12,2) NOT NULL DEFAULT 0,
    valor_total      NUMERIC(12,2) NOT NULL DEFAULT 0,
    forma_pagamento  TEXT DEFAULT '',
    parcelas         JSONB NOT NULL DEFAULT '[]',
    observacoes      TEXT DEFAULT '',
    criado_por       TEXT DEFAULT '',
    criado_em        TEXT DEFAULT '',
    removida         BOOLEAN NOT NULL DEFAULT FALSE
);

CREATE INDEX IF NOT EXISTS idx_fornecedor_compras_fornecedor ON fornecedor_compras(fornecedor_id);

-- Faz a API do Supabase enxergar as tabelas novas na hora.
NOTIFY pgrst, 'reload schema';
