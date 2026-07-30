-- ==================== ISA MODAS - ÍNDICES DE PERFORMANCE (ETAPA 1) ====================
-- Execute este arquivo no SQL Editor do Supabase.
--
-- Objetivo: acelerar consultas já existentes no código (db.py) que hoje
-- filtram/ordenam por colunas sem índice, causando varredura de tabela
-- inteira (ou quase) proporcional ao volume de dados.
--
-- Este script é 100% aditivo: apenas cria índices (CREATE INDEX IF NOT
-- EXISTS). Não altera dados, não altera colunas, não altera nenhuma regra
-- de negócio. Pode ser executado quantas vezes for preciso sem efeito
-- colateral (IF NOT EXISTS ignora índices que já existem).
--
-- CONCURRENTLY evita bloquear escritas na tabela enquanto o índice é
-- construído (mais lento para criar, mas não trava o sistema em produção).
-- Se o SQL Editor do Supabase reclamar de "CREATE INDEX CONCURRENTLY cannot
-- run inside a transaction block", rode cada bloco abaixo separadamente
-- (um de cada vez), ou remova a palavra CONCURRENTLY — com os volumes atuais
-- (poucos milhares de linhas) a criação é praticamente instantânea mesmo sem
-- CONCURRENTLY.

-- ---------------------------------------------------------------------
-- sales: filtro por comanda_id (update_sales_status_by_comanda,
-- update_sales_comanda_nome em db.py) — hoje sem índice algum.
-- ---------------------------------------------------------------------
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_sales_comanda_id
    ON sales(comanda_id);

-- ---------------------------------------------------------------------
-- sales: índice composto (excluida, data) — quase toda consulta de
-- período (get_all_sales, get_sales_by_date_range, get_sales_open_by_date)
-- filtra as duas colunas juntas. Hoje só "data" é indexada isoladamente.
-- Mantém o índice simples idx_sales_data (ainda usado por outras consultas
-- que filtram só por data).
-- ---------------------------------------------------------------------
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_sales_excluida_data
    ON sales(excluida, data);

-- ---------------------------------------------------------------------
-- commands: filtro por cliente_id (get_saldo_cliente,
-- tem_comandas_abertas_cliente em db.py) — chamado em loop na listagem
-- de clientes, sem índice hoje.
-- ---------------------------------------------------------------------
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_commands_cliente_id
    ON commands(cliente_id);

-- ---------------------------------------------------------------------
-- caixas: ORDER BY data_abertura (db.py:628, get_all_caixas) sem índice.
-- ---------------------------------------------------------------------
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_caixas_data_abertura
    ON caixas(data_abertura);

-- ---------------------------------------------------------------------
-- despesas: ORDER BY data (db.py:1110, get_all_despesas) sem índice.
-- Tabela pequena hoje, mas cresce todo mês.
-- ---------------------------------------------------------------------
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_despesas_data
    ON despesas(data);

-- ---------------------------------------------------------------------
-- audit_log: filtro gte("data", ...) (db.py:234, tela de /logs) sem índice.
-- ---------------------------------------------------------------------
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_audit_log_data
    ON audit_log(data);

-- ---------------------------------------------------------------------
-- lote_investimentos: filtro por "removido" + ORDER BY data_entrada juntos
-- (db.py:1044-1045, get_all_lote_investimentos) sem índice.
-- ---------------------------------------------------------------------
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_lote_investimentos_removido_data
    ON lote_investimentos(removido, data_entrada);

-- ==================== FIM ====================
-- Nenhuma tabela, coluna ou dado foi alterado. Apenas 7 índices novos,
-- todos IF NOT EXISTS.
