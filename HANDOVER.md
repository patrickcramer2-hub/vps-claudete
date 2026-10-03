# Handover: Claudete / SEFAS Assistencial
**Sessão de origem:** claude.ai/code (nuvem, sem acesso direto ao VPS)  
**Destino:** sessão local com acesso direto ao VPS `root@177.7.41.57`  
**Branch ativo:** `claude/intelligent-curie-ri5741`  
**Data:** 2026-10-03

---

## 1. Contexto do projeto

Patrick é dono da CCN (Centro Clínico Niterói) e parceiro comercial da SEFAS Assistencial.

**Claudete** é o sistema multi-tenant de atendimento por WhatsApp:
- **CCN Inbound**: Claudete recebe mensagens de pacientes, faz triagem, identifica CPF, agenda consultas e faz handoff para humano via Chatwoot
- **SEFAS Outbound**: Claudete dispara mensagens comerciais para leads prospectos, faz abordagem consultiva de venda de planos de assistência familiar, atualiza status no Google Sheets e PostgreSQL, e faz handoff para humano via Chatwoot

**Meta de Patrick:** vender a CCN até junho de 2026. A Claudete é ativo valorizável do negócio.

---

## 2. Infraestrutura VPS

**IP:** `177.7.41.57`  
**SO:** Ubuntu 24.04  
**Todos os serviços rodam em Docker.**

### Containers ativos (verificar com `docker ps`):
| Container | Imagem | Porta externa | Função |
|---|---|---|---|
| `claudete-dashboard` | imagem local | via traefik | Flask app (CCN + SEFAS APIs) |
| `evolution-api` | evoapicloud/evolution-api:v2.3.7 | 8080 | Gateway WhatsApp |
| `n8n-58b6-n8n-1` | docker.n8n.io/n8nio/n8n | 32774 | Orquestrador de workflows |
| `chatwoot-app` | chatwoot | 3000 | CRM / handoff humano |
| `chatwoot-worker` | chatwoot | 3000 | Worker do Chatwoot |
| `chatwoot-redis` | redis | 6379 | Cache Chatwoot |
| `chatwoot-db` | postgres | 5432 | DB Chatwoot |
| `traefik-traefik-1` | traefik | - | Proxy reverso |
| `ccn-site` | imagem local | 8081 | Site institucional CCN |
| `evolution-postgres` | postgres | 5432 | DB principal (evolution_db) |
| `evolution-redis` | redis | 6379 | Cache Evolution |

### Volumes relevantes:
- Flask app: `/docker/claudete-dashboard/` montado em `/app` no container `claudete-dashboard`
- n8n data: dentro do container `n8n-58b6-n8n-1` (SQLite em `/home/node/.n8n/`)

### PostgreSQL principal:
```
Host (interno Docker): evolution-postgres
DB: evolution_db
User: evolution
Password: evolution123
```

---

## 3. O que foi construído/alterado nesta sessão

### 3.1 Flask app (`dashboard/app.py`)
**Status:** alterado, commitado, pushado para branch. **NÃO foi re-deployado ao VPS.**

Mudanças:
- Linha ~3376: comentário atualizado com novo sheet ID SEFAS
- Linhas 3625-3626: `_SEFAS_SHEET_ID` agora aponta para `1gZ8I6RIzYadKodo7qv6Efk2jV2viUXAVfUXXNkdPthM`

**Ação necessária:** fazer deploy do `app.py` atualizado ao VPS.

```bash
cd /tmp
git clone --depth=1 --branch claude/intelligent-curie-ri5741 \
  https://github.com/patrickcramer2-hub/vps-claudete.git vps-deploy
cp vps-deploy/dashboard/app.py /docker/claudete-dashboard/app.py
docker restart claudete-dashboard
docker logs claudete-dashboard --tail=20
rm -rf /tmp/vps-deploy
```

### 3.2 Workflow SEFAS Outbound (`sefas/workflow_sefas_outbound.json`)
**Status:** alterado, commitado, pushado, **importado no n8n com sucesso.**

Mudanças no arquivo:
- 4 referências ao sheet ID antigo (`11jba-gDTFNhDowz4XlDb5rkJEQNy6bJqNKyUMH-iQVw`) substituídas por `1gZ8I6RIzYadKodo7qv6Efk2jV2viUXAVfUXXNkdPthM`
- `flag_teste: 'SIM'` corrigido para `flag_teste: 'NAO'` no nó `Preparar Registro Historico SEFAS`

**Nós desativados intencionalmente (NÃO ativar antes do warm-up):**
- `Intervalo entre envios (Anti-ban)`
- `Enviar Mensagem SEFAS`

**Ação necessária no n8n:**
1. Abrir o workflow "SEFAS - OUTBOUND CAMPANHA"
2. Reconfigurar credencial Google Sheets nos 4 nós com alerta vermelho
3. Verificar URL da Evolution API: `http://177.7.41.57:8080/message/sendText/SEFAS-Assistencial`
4. Verificar API key da Evolution: `C5E05E4209E4-4A8C-9441-672DB9A3AD0A`
5. NÃO ativar ainda

### 3.3 Tabela `leads` no PostgreSQL
**Status:** criada e confirmada com `\d leads`.

```sql
CREATE TABLE leads (
  id            SERIAL PRIMARY KEY,
  client_id     TEXT NOT NULL DEFAULT 'sefas',
  campaign_id   TEXT NOT NULL DEFAULT 'sefas-2026',
  nome          TEXT,
  primeiro_nome TEXT,
  telefone      TEXT NOT NULL,
  remoteJid     TEXT,
  origem        TEXT DEFAULT 'lista-sefas',
  variacao      TEXT DEFAULT 'S1',
  status        TEXT DEFAULT 'pendente',
  plano_indicado TEXT,
  notas         TEXT,
  criado_em     TIMESTAMPTZ DEFAULT NOW(),
  enviado_em    TIMESTAMPTZ,
  respondeu_em  TIMESTAMPTZ,
  handoff_em    TIMESTAMPTZ,
  UNIQUE(telefone, client_id)
);
```

5 índices: pkey, idx_leads_status, idx_leads_tel, idx_leads_criado, idx_leads_campaign

### 3.4 Google Sheets SEFAS
**ID:** `1gZ8I6RIzYadKodo7qv6Efk2jV2viUXAVfUXXNkdPthM`

Apps Script executado com sucesso criou:
- Aba `SEFAS_OUTBOUND`: Status / Ação Envio / Variação / Primeiro Nome / Telefone / Origem / Plano Indicado / Data Envio / remoteJid
- Aba `Histórico Outbound`: Data / Campanha / Lead / Telefone / Variação / Status / Evento
- Aba `Histórico Recepção`: chave_demanda / telefone / paciente_nome / historico_conversa / ultima_mensagem / ultima_interacao_ts / status_lead / campanha

5 leads de controle inseridos (linhas 4-8):
| Nome | Telefone | Variação |
|---|---|---|
| Matheus | 5521998392077 | S1 |
| Bruno | 5521970429512 | S2 |
| Bruno (Navega) | 5521994028023 | S1 |
| Liz | 5521995012280 | S2 |
| Patrick | 5521996614070 | S1 |

### 3.5 Instâncias Evolution API
- `Claudete-recep`: **DESCONECTADA** - erro `device_removed` nos logs. Precisa re-escanear QR.
- `claudete2`: status desconhecido, verificar.
- `SEFAS-Assistencial`: criada mas **QR code nunca foi escaneado**. Precisa escanear para iniciar warm-up de 7 dias.

Manager: `http://177.7.41.57:8080/manager`  
API Key Evolution: `C5E05E4209E4-4A8C-9441-672DB9A3AD0A`

---

## 4. O que está PENDENTE (por ordem de prioridade)

### P1 - Bloqueante para tudo
- [ ] Re-escanear QR da `Claudete-recep` (CCN inbound parado)
- [ ] Escanear QR da `SEFAS-Assistencial` e iniciar contagem de 7 dias de warm-up
- [ ] Deploy do `app.py` atualizado ao VPS (novo sheet ID)

### P2 - Para o SEFAS Inbound funcionar
- [ ] Construir workflow `SEFAS - INBOUND` baseado no CCN inbound (ver seção 5)
- [ ] Criar inbox SEFAS no Chatwoot (Settings > Inboxes > New Inbox > API)
- [ ] Criar team "Equipe SEFAS" no Chatwoot
- [ ] Criar etiquetas no Chatwoot: `sefas-interesse`, `sefas-duvida`, `sefas-handoff`, `sefas-luto`, `sefas-recusa`, `sefas-fechado`
- [ ] Configurar `GOOGLE_SERVICE_ACCOUNT_JSON` no container `claudete-dashboard`

### P3 - Para o SEFAS Outbound disparar (pós warm-up)
- [ ] Configurar credencial Google Sheets no n8n (nós do workflow SEFAS OUTBOUND)
- [ ] Importar leads reais via `POST /api/sefas/leads/import` (CSV: colunas nome, telefone)
- [ ] Sincronizar leads ao Sheets: `POST /api/sefas/sheets/sync`
- [ ] Habilitar nós Anti-ban + Enviar Mensagem no n8n
- [ ] Ativar o workflow SEFAS OUTBOUND

### P4 - Melhorias pendentes
- [ ] Injetar `static/client-switcher.js` nos HTMLs do VPS (dashboard.html, vendas.html, handoffs.html)
- [ ] Construir página `/admin` Visão Global (CCN + SEFAS agregados)
- [ ] Corrigir bug Cardiologia (Dra. Raquel vs Dra. Nayara no dashboard CCN)
- [ ] 63 processos zombie no VPS - investigar origem

---

## 5. Tarefa principal: construir o SEFAS Inbound

### Objetivo
Criar o workflow n8n `SEFAS - INBOUND` baseado no `CCN - CLAUDETE INBOUND`.

### Como proceder
```bash
# 1. Listar workflows com IDs
docker exec n8n-58b6-n8n-1 n8n list:workflow

# 2. Exportar CCN inbound (substituir <ID>)
docker exec n8n-58b6-n8n-1 n8n export:workflow --id=<ID_CCN_INBOUND> \
  --output=/tmp/ccn_inbound.json

# 3. Inspecionar estrutura
cat /tmp/ccn_inbound.json | python3 -m json.tool | head -200
```

### Diferenças esperadas (SEFAS vs CCN)

| Componente | CCN Inbound | SEFAS Inbound |
|---|---|---|
| Instância Evolution | Claudete-recep / claudete2 | SEFAS-Assistencial |
| Inbox Chatwoot | Inbox CCN | Inbox SEFAS (criar) |
| System prompt IA | Claudete CCN (triagem clínica) | Claudete SEFAS (consultora familiar) |
| Lógica pós-resposta | Identifica CPF, agenda | Atualiza status lead, conduz venda |
| Handoff | Team CCN | Team SEFAS |
| Nota privada | Resumo clínico | Template comercial SEFAS (ver abaixo) |

### System prompt SEFAS

```
Você é a Claudete, assistente virtual da Poli Master.

IDENTIDADE
Você representa a Poli Master apresentando a SEFAS Assistencial.
Sempre se identifique como assistente virtual. Nunca se passe por humano.

PRODUTO
A SEFAS Assistencial é um programa de assistência familiar com:
- Telemedicina 24h (até 6 consultas por mês)
- Rede de descontos em saúde, farmácia, academia, óticas e cursos
- Assistência residencial e automotiva (até 2 acionamentos por ano)
- Assistência pet com televeterinária 24h
- Cesta natalidade e cesta por óbito
- Proteção funerária nacional

PLANOS
Marfim - R$ 59/mês: proteção essencial, cobertura funerária + telemedicina para o titular.
Jade - R$ 79/mês: família completa, telemedicina familiar + TotalPass + cremação + cobertura maior.

COMPOSIÇÃO FAMILIAR PADRÃO
Até 6 pessoas: titular (até 65 anos) + cônjuge (até 65 anos) + até 4 filhos/enteados sem limite de idade.
Dependentes extras: sem limite de quantidade, sem exigência de parentesco.
Valores extras por dependente:
- 0 a 65 anos: Marfim +R$7 / Jade +R$9
- 66 a 75 anos: Marfim +R$12 / Jade +R$14
- 76 a 80 anos: Marfim +R$15 / Jade +R$19
- 81 a 90 anos: Marfim +R$48 / Jade +R$55

CARÊNCIAS
Assistência funerária: 180 dias.
Telemedicina, TotalPass e demais assistências: 48 horas úteis após o primeiro pagamento.
Vigência: 48 meses. Taxa de adesão: R$ 50 no boleto (isenta no cartão recorrente).

FLUXO DE CONVERSA (nesta ordem)
1. Apresentar o conceito: assistência familiar com benefícios em vida e proteção para imprevistos
2. Criar valor com benefícios do dia a dia (telemedicina, TotalPass, rede de descontos)
3. Perguntar sobre a família: quantas pessoas, idades aproximadas
4. Recomendar o plano certo (Marfim ou Jade)
5. Apresentar carência com transparência total
6. Direcionar para o canal oficial de contratação

REGRAS ABSOLUTAS
- Nunca chamar de plano funerário na abertura
- Nunca usar medo da morte como argumento
- Nunca pedir CPF, RG, cartão ou senha pelo WhatsApp
- Nunca prometer desconto ou condição especial sem autorização
- Nunca improvisar informação contratual
- Se o cliente mencionar luto recente: encerrar a abordagem comercial com acolhimento
- Se o cliente pedir SAIR: respeitar imediatamente e encerrar

HANDOFF PARA HUMANO
Encaminhar para atendente humano quando:
1. Cliente pedir falar com uma pessoa
2. Dúvida sobre cancelamento, multa ou inadimplência
3. Reclamação ou contestação
4. Pedido de desconto ou condição especial
5. Luto recente mencionado
6. Cliente demonstrar interesse claro em fechar (encaminhar com contexto completo)
7. Dúvida não coberta pela documentação

NOTA PRIVADA AO FAZER HANDOFF
Gere exatamente neste formato (sem markdown, apenas texto):

Atendimento SEFAS recebido da Claudete

Cliente: [nome]
Plano discutido: [Marfim / Jade / ainda não definido]
Família: [composição mencionada ou "não informada"]
Motivo do handoff: [uma frase direta]
O que fazer agora: [instrução específica para o atendente]
Histórico completo: veja as mensagens acima nesta conversa.
```

### Instrução para o atendente por tipo de handoff

| Motivo | Instrução para "O que fazer agora" |
|---|---|
| Quer fechar | Envie o link oficial de contratação e confirme os dados da família. |
| Pediu desconto | Verifique se há condição ativa. Sem condição, mantenha o preço e reforce o valor. |
| Dúvida sobre carência | Esclareça: funeral tem 180 dias, demais benefícios em 48h úteis após primeiro pagamento. |
| Dúvida contratual | Responda com base nas regras oficiais. Não improvise. |
| Luto recente | Não retome a venda. Atenda com acolhimento. |
| Reclamação | Ouça, registre e escale se necessário. Não discuta. |
| Pediu humano | Apresente-se e continue o atendimento a partir do histórico acima. |

---

## 6. Perguntas que a sessão de nuvem não conseguiu confirmar

A nova sessão deve responder cada item abaixo inspecionando o VPS diretamente:

### 6.1 Flask app
```bash
# Qual versão do app.py está rodando no container agora?
docker exec claudete-dashboard grep "_SEFAS_SHEET_ID\|SEFAS_SPREADSHEET_ID" app.py
# Resposta esperada: 1gZ8I6RIzYadKodo7qv6Efk2jV2viUXAVfUXXNkdPthM
# Se mostrar o ID antigo (11jba-...) = precisa fazer deploy

# Qual porta o claudete-dashboard expõe?
docker inspect claudete-dashboard | python3 -c "
import json,sys
d=json.load(sys.stdin)[0]
print(d['HostConfig']['PortBindings'])
print(d['NetworkSettings']['Ports'])
"

# A API SEFAS está respondendo?
PORT=5000  # ou a porta real descoberta acima
curl -s http://localhost:$PORT/sefas | python3 -m json.tool
curl -s http://localhost:$PORT/api/sefas/leads | python3 -m json.tool

# A variável de ambiente GOOGLE_SERVICE_ACCOUNT_JSON existe?
docker exec claudete-dashboard env | grep GOOGLE
# Se vazio: o sync de sheets não vai funcionar ainda
```

### 6.2 PostgreSQL
```bash
# A tabela leads existe e tem a estrutura correta?
docker exec evolution-postgres psql -U evolution -d evolution_db -c "\d leads"

# Os 5 leads de controle foram importados?
docker exec evolution-postgres psql -U evolution -d evolution_db \
  -c "SELECT primeiro_nome, telefone, variacao, status FROM leads WHERE client_id='sefas';"
```

### 6.3 Evolution API
```bash
# Status das 3 instâncias
curl -s http://localhost:8080/instance/fetchInstances \
  -H "apikey: C5E05E4209E4-4A8C-9441-672DB9A3AD0A" | python3 -m json.tool

# Resposta esperada por instância:
# Claudete-recep: state "close" ou "connecting" (estava desconectada)
# claudete2: verificar
# SEFAS-Assistencial: state "close" (QR nunca escaneado)
```

### 6.4 n8n
```bash
# Listar todos os workflows com IDs
docker exec n8n-58b6-n8n-1 n8n list:workflow

# Verificar se o SEFAS OUTBOUND foi importado corretamente
docker exec n8n-58b6-n8n-1 n8n list:workflow | grep SEFAS

# Exportar CCN inbound para usar como base do SEFAS inbound
docker exec n8n-58b6-n8n-1 n8n export:workflow --id=<ID_CCN_INBOUND> \
  --output=/tmp/ccn_inbound.json
cat /tmp/ccn_inbound.json
```

**Dúvidas sobre o n8n:**
- Qual o ID exato do workflow "CCN - CLAUDETE INBOUND"?
- O workflow SEFAS OUTBOUND importado está com os nós de credencial do Google Sheets configurados ou com alerta vermelho?
- Existe credencial Anthropic (Claude API) já configurada no n8n? Se sim, qual o nome/ID?
- Existe credencial Evolution API já configurada?
- A credencial Google Sheets ID `DwGMSdvy5y4ZKSvU` mencionada no workflow SEFAS existe no n8n?

```bash
# Verificar credenciais existentes no n8n via API interna
curl -s http://localhost:32774/api/v1/credentials \
  -H "X-N8N-API-KEY: <API_KEY_N8N>" | python3 -m json.tool
# Obter a API key em n8n > Settings > API > Create API Key
```

### 6.5 Chatwoot
```
Via Chatwoot UI: Settings > Inboxes / Teams / Labels

Perguntas a responder:
- Qual é o domínio/URL do Chatwoot?
- Quais inboxes existem atualmente?
- Existe inbox vinculada à Claudete-recep? E à claudete2?
- Existe team "Equipe CCN"?
- Quais etiquetas existem?
```

### 6.6 client-switcher.js
```bash
# O script está injetado nos HTMLs do dashboard?
grep -l "client-switcher" /docker/claudete-dashboard/*.html
# Se não retornar nada: precisa injetar
# Arquivos alvo: dashboard.html, vendas.html, handoffs.html
# Injeção: adicionar antes de </body>:
# <script src="/static/client-switcher.js"></script>
```

### 6.7 Processos zombie
```bash
# 63 processos zombie foram reportados no último login
ps aux | grep defunct | head -20
# Verificar se são do claudete-dashboard ou n8n
```

---

## 7. Arquitetura de dados (referência)

```
[WhatsApp] <-> [Evolution API] <-> [n8n Webhook]
                                        |
                              [Flask /api/sefas/...]
                                        |
                              [PostgreSQL evolution_db]
                                  tabela: leads
                                        |
                              [Google Sheets sync]
                              (assíncrono, sob demanda)
                              planilha: 1gZ8I...
```

**Sheets é espelho de leitura, não fonte da verdade.**  
**PostgreSQL é a fonte da verdade.**  
**Flask é o cérebro do sistema SEFAS.**

---

## 8. Credenciais e variáveis de ambiente necessárias

### No container `claudete-dashboard` (verificar com `docker exec claudete-dashboard env`):
| Variável | Status | Necessária para |
|---|---|---|
| `ANTHROPIC_API_KEY` | provavelmente configurada | IA responder mensagens |
| `GOOGLE_SERVICE_ACCOUNT_JSON` | provavelmente ausente | sync de sheets |
| `SEFAS_SPREADSHEET_ID` | opcional (tem default no código) | apontar para planilha certa |
| `EVOLUTION_INSTANCES` | deve existir | quais instâncias monitorar |
| `CHATWOOT_BASE` | deve existir | URL do Chatwoot |
| `CHATWOOT_API_TOKEN` | deve existir | autenticação Chatwoot |

```bash
# Inspecionar variáveis de ambiente do container
docker inspect claudete-dashboard | python3 -c "
import json,sys
d=json.load(sys.stdin)[0]
for e in d['Config']['Env']:
    print(e)
"

# Verificar arquivo .env local
cat /docker/claudete-dashboard/.env 2>/dev/null
```

---

## 9. Regras de segurança (não negociáveis)

1. Nunca fazer deploy em produção sem aprovação explícita de Patrick
2. Nunca ativar os nós de disparo do SEFAS OUTBOUND antes do warm-up de 7 dias
3. Nunca commitar `.env` com credenciais reais
4. Nunca fazer push para main
5. Branch de trabalho: `claude/intelligent-curie-ri5741`
6. Sempre apresentar diffs para aprovação antes de aplicar em produção

---

## 10. Resumo executivo para a nova sessão

**Inspecionar primeiro (antes de qualquer ação):**
1. `docker ps` - todos os containers UP?
2. `docker logs claudete-dashboard --tail=30` - Flask saudável?
3. Evolution API: status das 3 instâncias
4. n8n: listar workflows, confirmar SEFAS OUTBOUND importado
5. PostgreSQL: tabela `leads` existe e tem dados?

**Construir depois (com aprovação de Patrick):**
1. Deploy do `app.py` atualizado (novo sheet ID)
2. Workflow SEFAS INBOUND (baseado no CCN inbound exportado)
3. Injeção do `client-switcher.js` nos 3 HTMLs

**NÃO fazer sem aprovação:**
- Ativar qualquer workflow de disparo
- Escanear QR codes (Patrick faz pessoalmente)
- Modificar workflows CCN em produção
