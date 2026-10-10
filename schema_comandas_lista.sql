-- Listagem leve de comandas (desempenho).
-- Rodar uma vez no SQL Editor do Supabase.
--
-- As telas que listam TODAS as comandas (Comandas, Caixa, Financeiro, Vendas,
-- Investidores) não usam o detalhe de consumo de estoque de cada item
-- (consumo_lotes / bar_consumo_components), que é ~metade do tamanho da
-- tabela. Esta view devolve as comandas sem esses dois campos. Telas que
-- mexem numa comanda específica continuam lendo a tabela commands completa.

CREATE OR REPLACE VIEW commands_lista AS
SELECT
  c.cid, c.cliente_id, c.codigo, c.criado_por, c.data_abertura, c.data_fechamento,
  c.data_quitada, c.emprestimos, c.forma_pagamento, c.historico, c.hora_abertura,
  c.hora_fechamento,
  COALESCE(
    (SELECT jsonb_agg(i - 'consumo_lotes' - 'bar_consumo_components')
       FROM jsonb_array_elements(COALESCE(c.itens::jsonb, '[]'::jsonb)) AS i),
    '[]'::jsonb
  ) AS itens,
  c.limite, c.local_id, c.margem_customizada, c.mesa, c.nome, c.observacao,
  c.pagamentos, c.status, c.tipo_atendimento, c.vencimento
FROM commands c;

-- Faz a API do Supabase enxergar a view na hora.
NOTIFY pgrst, 'reload schema';
