# Handover: Claudete / SEFAS Assistencial
**Sessao de origem:** claude.ai/code (nuvem, sem acesso direto ao VPS)  
**Destino:** sessao local com acesso direto ao VPS `root@177.7.41.57`  
**Branch ativo:** `claude/intelligent-curie-ri5741`  
**Data:** 2026-10-03

---

## 1. Contexto do projeto

Patrick e dono da CCN (Centro Clinico Niteroi) e parceiro comercial da SEFAS Assistencial.

**Claudete** e o sistema multi-tenant de atendimento por WhatsApp:
- **CCN Inbound**: Claudete recebe mensagens de pacientes, faz triagem, identifica CPF, agenda, faz handoff para humano via Chatwoot
- **SEFAS Outbound**: Claudete dispara mensagens comerciais para leads prospectos, faz abordagem consultiva de venda de planos de assistencia familiar, atualiza status no Google Sheets e PostgreSQL, faz handoff para humano via Chatwoot

**Meta de Patrick:** vender a CCN ate junho/2026. A Claudete e ativo valoravel do negocio.

---

## 2. Infraestrutura VPS

**IP:** `177.7.41.57`  
**SO:** Ubuntu 24.04  
**Todos os servicos rodam em Docker.**

### Containers ativos (verificar com `docker ps`):
| Container | Imagem | Porta externa | Funcao |
|---|---|---|---|
| `claudete-dashboard` | imagem local | via traefik | Flask app (CCN + SEFAS APIs) |
| `evolution-api` | evoapicloud/evolution-api:v2.3.7 | 8080 | WhatsApp gateway |
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

## 3. O que foi construido/alterado nesta sessao

### 3.1 Flask app (`dashboard/app.py`)
**Status:** alterado, commitado, pushado para branch. **NAO foi re-deployado ao VPS.**

Mudancas:
- Linha ~3376: comentario atualizado com novo sheet ID SEFAS
- Linhas 3625-3626: `_SEFAS_SHEET_ID` agora aponta para `1gZ8I6RIzYadKodo7qv6Efk2jV2viUXAVfUXXNkdPthM`

**Acao necessaria:** fazer deploy do `app.py` atualizado ao VPS.

Deploy padrao:
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

Mudancas no arquivo:
- 4 referencias ao sheet ID antigo (`11jba-gDTFNhDowz4XlDb5rkJEQNy6bJqNKyUMH-iQVw`) substituidas por `1gZ8I6RIzYadKodo7qv6Efk2jV2viUXAVfUXXNkdPthM`
- `flag_teste: 'SIM'` corrigido para `flag_teste: 'NAO'` no no `Preparar Registro Historico SEFAS`

**Nos desativados intencionalmente (NAO ativar antes do warm-up):**
- `Intervalo entre envios (Anti-ban)`
- `Enviar Mensagem SEFAS`

**Acao necessaria no n8n:**
1. Abrir o workflow "SEFAS - OUTBOUND CAMPANHA"
2. Reconfigurar credencial Google Sheets nos 4 nos que a usam (vai aparecer com alerta vermelho)
3. Verificar se a URL da Evolution API esta correta: `http://177.7.41.57:8080/message/sendText/SEFAS-Assistencial`
4. Verificar API key da Evolution: `C5E05E4209E4-4A8C-9441-672DB9A3AD0A`
5. NAO ativar ainda

### 3.3 Tabela `leads` no PostgreSQL
**Status:** criada e confirmada com `\d leads`.

Estrutura:
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

5 indexes: pkey, idx_leads_status, idx_leads_tel, idx_leads_criado, idx_leads_campaign

### 3.4 Google Sheets SEFAS
**ID:** `1gZ8I6RIzYadKodo7qv6Efk2jV2viUXAVfUXXNkdPthM`

Apps Script rodado com sucesso criou:
- Aba `SEFAS_OUTBOUND`: colunas Status / Acao Envio / Variacao / Primeiro Nome / Telefone / Origem / Plano Indicado / Data Envio / remoteJid
- Aba `Historico Outbound`: Data / Campanha / Lead / Telefone / Variacao / Status / Evento
- Aba `Historico Recepcao`: chave_demanda / telefone / paciente_nome / historico_conversa / ultima_mensagem / ultima_interacao_ts / status_lead / campanha

5 leads de controle inseridos (linhas 4-8):
| Nome | Telefone | Variacao |
|---|---|---|
| Matheus | 5521998392077 | S1 |
| Bruno | 5521970429512 | S2 |
| Bruno (Navega) | 5521994028023 | S1 |
| Liz | 5521995012280 | S2 |
| Patrick | 5521996614070 | S1 |

### 3.5 Instancias Evolution API
- `Claudete-recep`: **DESCONECTADA** - erro `device_removed` nos logs. Precisa re-escanear QR.
- `claudete2`: status desconhecido, verificar
- `SEFAS-Assistencial`: criada mas **QR code nunca foi escaneado**. Precisa escanear para iniciar warm-up de 7 dias.

Manager: `http://177.7.41.57:8080/manager`  
API Key Evolution: `C5E05E4209E4-4A8C-9441-672DB9A3AD0A`

---

## 4. O que esta PENDENTE (por ordem de prioridade)

### P1 - Bloqueante para tudo
- [ ] Re-escanear QR da `Claudete-recep` (CCN inbound parado)
- [ ] Escanear QR da `SEFAS-Assistencial` e iniciar contagem de 7 dias de warm-up
- [ ] Deploy do `app.py` atualizado ao VPS (sheet ID novo)

### P2 - Para o SEFAS Inbound funcionar
- [ ] Construir workflow `SEFAS - INBOUND` baseado no CCN inbound (ver secao 5)
- [ ] Criar inbox SEFAS no Chatwoot (Settings > Inboxes > New Inbox > API)
- [ ] Criar team "Equipe SEFAS" no Chatwoot
- [ ] Criar etiquetas no Chatwoot: `sefas-interesse`, `sefas-duvida`, `sefas-handoff`, `sefas-luto`, `sefas-recusa`, `sefas-fechado`
- [ ] Configurar `GOOGLE_SERVICE_ACCOUNT_JSON` no container `claudete-dashboard` para sync de sheets funcionar

### P3 - Para o SEFAS Outbound disparar (pos warm-up)
- [ ] Configurar credencial Google Sheets no n8n (nos do workflow SEFAS OUTBOUND)
- [ ] Importar leads reais via `POST /api/sefas/leads/import` (CSV com colunas nome, telefone)
- [ ] Sincronizar leads ao Sheets: `POST /api/sefas/sheets/sync`
- [ ] Habilitar nos Anti-ban + Enviar Mensagem no n8n
- [ ] Ativar o workflow SEFAS OUTBOUND

### P4 - Melhorias pendentes
- [ ] Injetar `static/client-switcher.js` nos HTMLs do VPS (dashboard.html, vendas.html, handoffs.html)
- [ ] Construir pagina `/admin` Visao Global (CCN + SEFAS agregados)
- [ ] Fix bug Cardiologia (Dra. Raquel vs Dra. Nayara no dashboard CCN)
- [ ] 63 zombie processes no VPS - investigar origem

---

## 5. Tarefa principal desta sessao: construir o SEFAS Inbound

### Objetivo
Criar o workflow n8n `SEFAS - INBOUND` baseado no `CCN - CLAUDETE INBOUND`.

### Como proceder
```bash
# 1. Listar workflows com IDs
docker exec n8n-58b6-n8n-1 n8n list:workflow

# 2. Exportar CCN inbound (substitua <ID>)
docker exec n8n-58b6-n8n-1 n8n export:workflow --id=<ID_CCN_INBOUND> \
  --output=/tmp/ccn_inbound.json

# 3. Inspecionar estrutura
cat /tmp/ccn_inbound.json | python3 -m json.tool | head -200
```

### Diferencas esperadas (SEFAS vs CCN)

| Componente | CCN Inbound | SEFAS Inbound |
|---|---|---|
| Instancia Evolution | Claudete-recep / claudete2 | SEFAS-Assistencial |
| Chatwoot inbox | Inbox CCN | Inbox SEFAS (criar) |
| System prompt IA | Claudete CCN (triagem clinica) | Claudete SEFAS (consultora familiar) |
| Logica apos resposta | Identifica CPF, agenda | Atualiza status lead, conduz venda |
| Handoff | Team CCN | Team SEFAS |
| Nota privada | Resumo clinico | Template comercial SEFAS (ver abaixo) |

### System prompt SEFAS (base para adaptar)

```
Voce e a Claudete, assistente virtual da Poli Master.

IDENTIDADE
Voce representa a Poli Master apresentando a SEFAS Assistencial.
Sempre se identifique como assistente virtual. Nunca se passe por humano.

PRODUTO
A SEFAS Assistencial e um programa de assistencia familiar com:
- Telemedicina 24h (ate 6 consultas por mes)
- Rede de descontos em saude, farmacia, academia, oticas e cursos
- Assistencia residencial e automotiva (ate 2 acionamentos/ano)
- Assistencia pet com televeterinaria 24h
- Cesta natalidade e cesta por obito
- Protecao funeraria nacional

PLANOS
Marfim - R$ 59/mes: protecao essencial, cobertura funeraria + telemedicina para o titular
Jade - R$ 79/mes: familia completa, telemedicina familiar + TotalPass + cremacao + cobertura maior

COMPOSICAO FAMILIAR PADRAO
Ate 6 pessoas: titular (ate 65 anos) + conjuge (ate 65 anos) + ate 4 filhos/enteados sem limite de idade.
Dependentes extras: sem limite de quantidade, sem exigencia de parentesco.
Valores extras: 0-65 anos Marfim +R$7 / Jade +R$9 | 66-75 Marfim +R$12 / Jade +R$14 | 76-80 Marfim +R$15 / Jade +R$19 | 81-90 Marfim +R$48 / Jade +R$55

CARENCIAS
Assistencia funeraria: 180 dias.
Telemedicina, TotalPass e demais assistencias: 48 horas uteis apos o primeiro pagamento.
Vigencia: 48 meses. Taxa de adesao: R$ 50 no boleto (isenta no cartao recorrente).

FLUXO DE CONVERSA (nesta ordem)
1. Apresentar o conceito: assistencia familiar com beneficios em vida e protecao para imprevistos
2. Criar valor com beneficios do dia a dia (telemedicina, TotalPass, rede de descontos)
3. Perguntar sobre a familia: quantas pessoas, idades aproximadas
4. Recomendar o plano certo (Marfim ou Jade)
5. Apresentar carencia com transparencia total
6. Direcionar para o canal oficial de contratacao

REGRAS ABSOLUTAS
- Nunca chamar de plano funerario na abertura
- Nunca usar medo da morte como argumento
- Nunca pedir CPF, RG, cartao ou senha pelo WhatsApp
- Nunca prometer desconto ou condicao especial sem autorizacao
- Nunca improvisar informacao contratual
- Se cliente mencionar luto recente: encerrar a abordagem comercial com acolhimento
- Se cliente pedir SAIR: respeitar imediatamente e encerrar

HANDOFF PARA HUMANO
Encaminhar para atendente humano quando:
1. Cliente pedir falar com uma pessoa
2. Duvida sobre cancelamento, multa ou inadimplencia
3. Reclamacao ou contestacao
4. Pedido de desconto ou condicao especial
5. Luto recente mencionado
6. Cliente demonstrar interesse claro em fechar (encaminhar com contexto completo)
7. Duvida nao coberta pela documentacao

NOTA PRIVADA AO FAZER HANDOFF
Gere exatamente neste formato (sem travessao, sem markdown, apenas texto):

Atendimento SEFAS recebido da Claudete

Cliente: [nome]
Plano discutido: [Marfim / Jade / ainda nao definido]
Familia: [composicao mencionada ou "nao informada"]
Motivo do handoff: [uma frase direta]
O que fazer agora: [instrucao especifica para o atendente]
Historico completo: veja as mensagens acima nesta conversa.
```

### Instrucao para o atendente por tipo de handoff

| Motivo | Instrucao para "O que fazer agora" |
|---|---|
| Quer fechar | Envie o link oficial de contratacao e confirme os dados da familia. |
| Pediu desconto | Verifique se ha condicao ativa. Sem condicao, mantenha o preco e reforce o valor. |
| Duvida sobre carencia | Esclarea: funeral tem 180 dias, demais beneficios em 48h uteis apos primeiro pagamento. |
| Duvida contratual | Responda com base nas regras oficiais. Nao improvise. |
| Luto recente | Nao retome a venda. Atenda com acolhimento. |
| Reclamacao | Ouca, registre e escale se necessario. Nao discuta. |
| Pediu humano | Apresente-se e continue o atendimento a partir do historico acima. |

---

## 6. Perguntas que a sessao de nuvem nao conseguiu confirmar

A nova sessao deve responder cada item abaixo inspecionando o VPS diretamente:

### 6.1 Flask app
```bash
# Qual versao do app.py esta rodando no container agora?
docker exec claudete-dashboard grep "_SEFAS_SHEET_ID\|SEFAS_SPREADSHEET_ID" app.py

# Resposta esperada: 1gZ8I6RIzYadKodo7qv6Efk2jV2viUXAVfUXXNkdPthM
# Se mostrar o ID antigo (11jba-...) = precisa fazer deploy
```

```bash
# Qual porta o claudete-dashboard expoe?
docker inspect claudete-dashboard | python3 -c "
import json,sys
d=json.load(sys.stdin)[0]
print(d['HostConfig']['PortBindings'])
print(d['NetworkSettings']['Ports'])
"
```

```bash
# A API SEFAS esta respondendo?
PORT=5000  # ou a porta real descoberta acima
curl -s http://localhost:$PORT/sefas | python3 -m json.tool
curl -s http://localhost:$PORT/api/sefas/leads | python3 -m json.tool
```

```bash
# A variavel de ambiente GOOGLE_SERVICE_ACCOUNT_JSON existe?
docker exec claudete-dashboard env | grep GOOGLE
# Se vazio: o sync de sheets nao vai funcionar ainda
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
# Status das 3 instancias
curl -s http://localhost:8080/instance/fetchInstances \
  -H "apikey: C5E05E4209E4-4A8C-9441-672DB9A3AD0A" | python3 -m json.tool

# Resposta esperada por instancia:
# Claudete-recep: state: "close" ou "connecting" (estava desconectada)
# claudete2: verificar
# SEFAS-Assistencial: state: "close" (QR nunca escaneado)
```

### 6.4 n8n
```bash
# Listar todos os workflows com IDs
docker exec n8n-58b6-n8n-1 n8n list:workflow

# Verificar se o SEFAS OUTBOUND foi importado corretamente
docker exec n8n-58b6-n8n-1 n8n list:workflow | grep SEFAS

# Exportar CCN inbound para usar como base
docker exec n8n-58b6-n8n-1 n8n export:workflow --id=<ID_CCN_INBOUND> \
  --output=/tmp/ccn_inbound.json
cat /tmp/ccn_inbound.json
```

**Duvidas sobre o n8n:**
- Qual o ID exato do workflow "CCN - CLAUDETE INBOUND"?
- O workflow SEFAS OUTBOUND importado esta com os nos de credencial do Google Sheets configurados ou com alerta vermelho?
- Existe credencial Anthropic (Claude API) ja configurada no n8n? Se sim, qual o nome/ID?
- Existe credencial Evolution API ja configurada?
- A credencial Google Sheets ID `DwGMSdvy5y4ZKSvU` mencionada no workflow SEFAS existe no n8n?

```bash
# Verificar credenciais existentes no n8n via API interna
curl -s http://localhost:32774/api/v1/credentials \
  -H "X-N8N-API-KEY: <API_KEY_N8N>" | python3 -m json.tool
# Obter a API key em n8n > Settings > API > Create API Key
```

### 6.5 Chatwoot
```bash
# Qual e o dominio/URL do Chatwoot?
# (visivel na aba do browser em: Chatwoot)
# Quais inboxes existem atualmente?
# Existe inbox vinculada a Claudete-recep? E a claudete2?
# Existe team "Equipe CCN"?
# Quais etiquetas existem?
# Via Chatwoot UI: Settings > Inboxes / Teams / Labels
```

### 6.6 client-switcher.js
```bash
# O script esta injetado nos HTMLs do dashboard?
grep -l "client-switcher" /docker/claudete-dashboard/*.html
# Se nao retornar nada: precisa injetar
# Os 3 arquivos alvo: dashboard.html, vendas.html, handoffs.html
# Injecao: adicionar antes de </body>:
# <script src="/static/client-switcher.js"></script>
```

### 6.7 Zombie processes
```bash
# 63 zombie processes foram reportados no ultimo login
# Investigar origem
ps aux | grep defunct | head -20
# Verificar se sao do claudete-dashboard ou n8n
```

---

## 7. Arquitetura de dados (referencia)

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

**Sheets e espelho de leitura, nao fonte da verdade.**  
**PostgreSQL e a fonte da verdade.**  
**Flask e o cerebro do sistema SEFAS.**

---

## 8. Credenciais e variaveis de ambiente necessarias

### No container `claudete-dashboard` (verificar com `docker exec claudete-dashboard env`):
| Variavel | Status | Necessaria para |
|---|---|---|
| `ANTHROPIC_API_KEY` | provavelmente configurada | IA responder mensagens |
| `GOOGLE_SERVICE_ACCOUNT_JSON` | provavelmente ausente | sync de sheets |
| `SEFAS_SPREADSHEET_ID` | opcional (tem default no codigo) | apontar para planilha certa |
| `EVOLUTION_INSTANCES` | deve existir | quais instancias monitorar |
| `CHATWOOT_BASE` | deve existir | URL do Chatwoot |
| `CHATWOOT_API_TOKEN` | deve existir | autenticacao Chatwoot |

Para adicionar `GOOGLE_SERVICE_ACCOUNT_JSON`:
```bash
# Verificar arquivo docker-compose ou .env do claudete-dashboard
cat /docker/claudete-dashboard/.env 2>/dev/null || \
  docker inspect claudete-dashboard | python3 -c "
import json,sys
d=json.load(sys.stdin)[0]
for e in d['Config']['Env']:
    print(e)
"
```

---

## 9. Regras de seguranca (nao negociaveis)

1. Nunca fazer deploy em producao sem aprovacao explicita de Patrick
2. Nunca ativar os nos de disparo do SEFAS OUTBOUND antes do warm-up de 7 dias
3. Nunca commitar `.env` com credenciais reais
4. Nunca fazer push para main
5. Branch de trabalho: `claude/intelligent-curie-ri5741`
6. Sempre apresentar diffs para aprovacao antes de aplicar em producao

---

## 10. Resumo executivo para a nova sessao

**Inspecionar primeiro (antes de qualquer acao):**
1. `docker ps` - todos os containers UP?
2. `docker logs claudete-dashboard --tail=30` - Flask saudavel?
3. Evolution API: status das 3 instancias
4. n8n: listar workflows, confirmar SEFAS OUTBOUND importado
5. PostgreSQL: `leads` table existe e tem dados?

**Construir depois (com aprovacao de Patrick):**
1. Deploy do `app.py` atualizado (novo sheet ID)
2. Workflow SEFAS INBOUND (baseado no CCN inbound exportado)
3. Injecao do `client-switcher.js` nos 3 HTMLs

**NAO fazer sem aprovacao:**
- Ativar qualquer workflow de disparo
- Escanear QR codes (Patrick faz pessoalmente)
- Modificar workflows CCN em producao
