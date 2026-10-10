-- Local de cada cliente (Loja / Escritório).
-- Rodar uma vez no SQL Editor do Supabase.

ALTER TABLE clientes ADD COLUMN IF NOT EXISTS local_id TEXT DEFAULT '';

-- Preenche cada cliente com o local onde ele mais abriu comanda.
-- Quem nunca abriu comanda fica sem local (aparece nos dois até alguém definir).
UPDATE clientes c SET local_id = sub.local_id
FROM (
  SELECT DISTINCT ON (cliente_id) cliente_id, COALESCE(NULLIF(local_id, ''), 'loja') AS local_id
  FROM commands
  WHERE cliente_id IS NOT NULL AND cliente_id <> ''
  GROUP BY cliente_id, COALESCE(NULLIF(local_id, ''), 'loja')
  ORDER BY cliente_id, COUNT(*) DESC
) sub
WHERE c.id = sub.cliente_id AND COALESCE(c.local_id, '') = '';

-- Faz a API do Supabase enxergar a coluna nova na hora.
NOTIFY pgrst, 'reload schema';
