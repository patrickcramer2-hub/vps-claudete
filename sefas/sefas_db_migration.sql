-- Migration: tabela de leads multi-tenant (substitui sefas_leads)
-- Rodar no banco Postgres do VPS (evolution_db via pg_conn) antes de ativar o workflow n8n

CREATE TABLE IF NOT EXISTS leads (
    id             SERIAL PRIMARY KEY,
    nome           TEXT NOT NULL,
    primeiro_nome  TEXT,
    telefone       VARCHAR(20) NOT NULL,
    origem         TEXT DEFAULT 'lista-sefas',
    status         TEXT NOT NULL DEFAULT 'pendente',
    -- status: pendente | enviado | respondeu | interesse | duvida | recusa | handoff | sem_resposta | fechado
    variacao       TEXT,           -- S1 | S2 (A/B test de mensagem)
    client_id      TEXT NOT NULL DEFAULT 'sefas',   -- isolamento multi-tenant
    campaign_id    TEXT DEFAULT 'sefas-2026',
    criado_em      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    enviado_em     TIMESTAMPTZ,
    respondeu_em   TIMESTAMPTZ,
    handoff_em     TIMESTAMPTZ,
    plano_indicado TEXT,           -- marfim | jade | a_confirmar
    notas          TEXT,
    UNIQUE(telefone, client_id)    -- mesmo número pode existir em clientes diferentes
);

CREATE INDEX IF NOT EXISTS idx_leads_status     ON leads(client_id, status);
CREATE INDEX IF NOT EXISTS idx_leads_tel        ON leads(telefone);
CREATE INDEX IF NOT EXISTS idx_leads_criado     ON leads(criado_em);
CREATE INDEX IF NOT EXISTS idx_leads_campaign   ON leads(client_id, campaign_id);

COMMENT ON TABLE leads IS 'Leads outbound WhatsApp — multi-tenant (client_id). Piloto: SEFAS Assistencial.';

-- Migração se a tabela sefas_leads já existir (rodar manualmente se necessário):
-- ALTER TABLE sefas_leads RENAME TO leads;
-- ALTER TABLE leads ADD COLUMN IF NOT EXISTS primeiro_nome TEXT;
-- ALTER TABLE leads ADD COLUMN IF NOT EXISTS variacao TEXT;
-- ALTER TABLE leads ADD COLUMN IF NOT EXISTS client_id TEXT NOT NULL DEFAULT 'sefas';
-- ALTER TABLE leads ADD COLUMN IF NOT EXISTS campaign_id TEXT DEFAULT 'sefas-2026';
-- ALTER TABLE leads DROP CONSTRAINT IF EXISTS sefas_leads_telefone_key;
-- ALTER TABLE leads ADD CONSTRAINT leads_telefone_client_id_key UNIQUE(telefone, client_id);
