-- Migration: tabela de leads SEFAS outbound
-- Rodar no banco Postgres do VPS antes de ativar o workflow n8n

CREATE TABLE IF NOT EXISTS sefas_leads (
    id            SERIAL PRIMARY KEY,
    nome          TEXT NOT NULL,
    telefone      VARCHAR(20) NOT NULL,
    origem        TEXT DEFAULT 'lista-sefas',
    status        TEXT NOT NULL DEFAULT 'pendente',
    -- status: pendente | enviado | respondeu | interesse | duvida | recusa | handoff | sem_resposta
    criado_em     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    enviado_em    TIMESTAMPTZ,
    respondeu_em  TIMESTAMPTZ,
    handoff_em    TIMESTAMPTZ,
    plano_indicado TEXT,        -- marfim | jade | a_confirmar
    notas         TEXT,
    UNIQUE(telefone)
);

CREATE INDEX IF NOT EXISTS idx_sefas_status  ON sefas_leads(status);
CREATE INDEX IF NOT EXISTS idx_sefas_tel     ON sefas_leads(telefone);
CREATE INDEX IF NOT EXISTS idx_sefas_criado  ON sefas_leads(criado_em);

COMMENT ON TABLE sefas_leads IS 'Leads da campanha SEFAS Assistencial — outbound WhatsApp via Claudete';
