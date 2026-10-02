from flask import Flask, jsonify, request as flask_request, send_from_directory
from collections import Counter, defaultdict
import calendar, csv, datetime, io, os, time, zoneinfo, threading, urllib.request, urllib.parse, urllib.error, json, re, sqlite3
import psycopg2, psycopg2.extras

# gspread é opcional — usado só pelo endpoint de sync Sheets.
# Não derruba o app se não estiver instalado.
try:
    import gspread as _gspread
    from google.oauth2.service_account import Credentials as _GCreds
    _GSPREAD_OK = True
except ImportError:
    _GSPREAD_OK = False

app = Flask(__name__)

BRT = zoneinfo.ZoneInfo("America/Sao_Paulo")

QUARK_V1 = "https://api.quark.tec.br/clinic/ext/v1"
QUARK_V2 = "https://api.quark.tec.br/clinic/ext/v2"

for _env in ("QUARK_AUTH_TOKEN", "QUARK_CHAVE_KEY", "QUARK_SECRET_KEY"):
    if not os.environ.get(_env):
        raise RuntimeError(f"variavel de ambiente {_env} nao definida (ver .env / docker-compose.yml)")

QUARK_HEADERS = {
    "Auth-token": os.environ["QUARK_AUTH_TOKEN"],
    "X-Chave-Key": os.environ["QUARK_CHAVE_KEY"],
    "X-Secret-Key": os.environ["QUARK_SECRET_KEY"],
    "Accept": "application/json",
}
QUARK_SLEEP = 2.3  # regra da API: 2-2.5s entre chamadas
QUARK_MAX_PAGES = 50  # trava de seguranca contra loop infinito

# ── Chatwoot ──
# Token ausente ou API fora do ar nunca derruba o resto do painel.
CHATWOOT_BASE = os.environ.get("CHATWOOT_BASE", "")
CHATWOOT_API_TOKEN = os.environ.get("CHATWOOT_API_TOKEN", "")
CHATWOOT_ACCOUNT_ID = int(os.environ.get("CHATWOOT_ACCOUNT_ID", "1"))
_raw_agent_names = os.environ.get("CHATWOOT_AGENT_NAMES_JSON", "")
CHATWOOT_AGENT_NAMES = {int(k): v for k, v in json.loads(_raw_agent_names).items()} if _raw_agent_names else {}
_equipe_cache: dict = {}
_equipe_cache_lock = threading.Lock()
_EQUIPE_CACHE_TTL = 300  # 5 min
TICKET_MEDIO_FALLBACK = int(os.environ.get("TICKET_MEDIO_FALLBACK", "322"))

# ── Evolution (instâncias WhatsApp) ──
# Separadas por vírgula: EVOLUTION_INSTANCES=Inst1,Inst2
EVOLUTION_INSTANCES = [n.strip() for n in os.environ.get("EVOLUTION_INSTANCES", "Claudete-recep,claudete2,SEFAS-Assistencial").split(",")]
_EVO_IN_SQL = "('" + "','".join(EVOLUTION_INSTANCES) + "')"

# ── Time / usuários Quark ──
# Formato JSON: {"419311307":"Mariana","43802210":"Patrick",...}
_raw_usuario_map = os.environ.get("USUARIO_MAP_JSON", "")
USUARIO_MAP = {int(k): v for k, v in json.loads(_raw_usuario_map).items()} if _raw_usuario_map else {}
# Formato JSON: [{"uid":419311307,"nome":"Mariana","wa_hashtag":"mariana"},...]
_raw_team = os.environ.get("TEAM_MEMBERS_JSON", "")
TEAM_MEMBERS = json.loads(_raw_team) if _raw_team else []
USUARIO_API_ID = int(os.environ.get("USUARIO_API_ID", "0"))

MESES_PT = {
    1: "Janeiro", 2: "Fevereiro", 3: "Março", 4: "Abril", 5: "Maio", 6: "Junho",
    7: "Julho", 8: "Agosto", 9: "Setembro", 10: "Outubro", 11: "Novembro", 12: "Dezembro",
}

# FIX: apenas ATENDIMENTO_COMPLETO conta como realizado
STATUS_CONSULTA_REALIZADA = ("ATENDIMENTO_COMPLETO",)

# IDs de cadastros institucionais (transferências internas, não pacientes)
# Formato JSON: [229046903]
_raw_pess = os.environ.get("PESSOA_ID_INSTITUCIONAL_JSON", "")
PESSOA_ID_INSTITUCIONAL = set(json.loads(_raw_pess)) if _raw_pess else set()


def _handoff_conds_sql():
    """Monta condição SQL de handoff a partir de TEAM_MEMBERS (wa_hashtag)."""
    tags = [m["wa_hashtag"] for m in TEAM_MEMBERS if m.get("wa_hashtag")]
    parts = [f"m.message->>'conversation' ILIKE '%%#{t}%%'" for t in tags]
    parts.append("m.message->>'conversation' ILIKE '%%Já passei seu atendimento%%'")
    return "(" + "\n             OR ".join(parts) + ")"


def _pessoa_institucional(pessoa):
    """True se o registro de venda/conta e transferencia interna (CENTRO CLINICO
    NITERoI), nao receita de paciente. Confirmado por reconciliacao exata com o
    relatorio Extrato de Atendimentos em 11/09/2026."""
    if not pessoa:
        return False
    if pessoa.get("id") in PESSOA_ID_INSTITUCIONAL:
        return True
    nome = (pessoa.get("nome") or pessoa.get("pessoaNome") or "").strip().upper()
    return nome.startswith("CENTRO CL")

ESPECIALIDADES_KEYWORDS = {
    "GINECOLOGIA":          ["ginecolog", "gineco"],
    "ENDOCRINOLOGIA":       ["endocrinolog", "endócrino", "endocrino"],
    "NEUROLOGIA":           ["neurolog", "neuro"],
    "FISIOTERAPIA":         ["fisioterapia", "fisioterapeuta", "fisio"],
    "GERIATRIA":            ["geriatria", "geriatra"],
    "CLÍNICO GERAL":        ["clínico geral", "clinico geral"],
    "UROLOGIA":             ["urolog"],
    "DERMATOLOGIA":         ["dermatolog", "dermato"],
    "ORTOPEDIA":            ["ortoped"],
    "ANGIOLOGIA":           ["angiolog", "varizes", "vascular"],
    "PSIQUIATRIA":          ["psiquiatri"],
    "PSICOLOGIA":           ["psicolog"],
    "CARDIOLOGIA":          ["cardiolog"],
    "OFTALMOLOGIA":         ["oftalmolog", "oftalmol", "optometri"],
    "OTORRINOLARINGOLOGIA": ["otorrino", "audiometria"],
    "PNEUMOLOGIA":          ["pneumolog"],
    "LABORATÓRIO":          ["laboratorio", "laboratório", "hemograma", "exame de sangue",
                             "analise clinica", "analises clinicas", "exame laboratorial"],
}

ESPECIALIDADE_STATUS = {
    "GINECOLOGIA": "com_oferta", "ENDOCRINOLOGIA": "com_oferta",
    "NEUROLOGIA": "com_oferta", "FISIOTERAPIA": "com_oferta",
    "GERIATRIA": "com_oferta", "CLÍNICO GERAL": "com_oferta",
    "UROLOGIA": "com_oferta", "DERMATOLOGIA": "com_oferta",
    "CARDIOLOGIA": "com_oferta",  # agenda ativa confirmada via GET /v1/agendas
    "PSIQUIATRIA": "com_oferta",  # agenda ativa confirmada via GET /v1/agendas
    "PSICOLOGIA":  "com_oferta",
    "LABORATÓRIO": "com_oferta",
    "ORTOPEDIA": "sem_oferta", "ANGIOLOGIA": "sem_oferta",
    "PNEUMOLOGIA": "sem_oferta",
    "OFTALMOLOGIA": "parceiro", "OTORRINOLARINGOLOGIA": "parceiro",
}

# IDs obtidos via GET /v1/agendas (23/08/2026) — apenas agendas ativas
ESPECIALIDADE_AGENDA_IDS = {
    "GINECOLOGIA":           [461452883, 350537922],
    "ENDOCRINOLOGIA":        [330491168],   # nome real: "ENDOCRINOLOGIA | CLÍNICO GERAL"
    "NEUROLOGIA":            [336851397, 462415705],
    "FISIOTERAPIA":          [308394598],
    "GERIATRIA":             [463870495, 356083441],  # 356083441 = "CLÍNICO GERAL / GERIATRIA"
    "CLÍNICO GERAL":         [356083441],
    "UROLOGIA":              [103368125],
    "DERMATOLOGIA":          [434734599],
    "CARDIOLOGIA":           [271927862, 461430604],  # IDs confirmados via /v1/agendas
    "PSIQUIATRIA":           [369207861, 414774210],  # IDs confirmados via /v1/agendas
    "OFTALMOLOGIA":          [109880398],             # AVALIAÇÃO OFTALMOLÓGICA (parceiro)
    "OTORRINOLARINGOLOGIA":  [44252142],              # OTORRINOLARINGOLOGIA (parceiro)
}

# Normaliza especialidade.nome do Quark → chave de ESPECIALIDADES_KEYWORDS
QUARK_ESP_NORMALIZER = {
    "GINECOLOGIA E OBSTETRÍCIA":      "GINECOLOGIA",
    "CLINICO GERAL":                  "CLÍNICO GERAL",
    "ORTOPEDIA GERAL":                "ORTOPEDIA",
    "OPTOMETRIA":                     "OFTALMOLOGIA",
    "PNEUMOLOGIA 1":                  "PNEUMOLOGIA",
    "LABORATÓRIO DE ANÁLISES CLÍNICAS": "LABORATÓRIO",
}


def _esp_nome(agendamento):
    nome = (agendamento.get("especialidade") or {}).get("nome") or ""
    return QUARK_ESP_NORMALIZER.get(nome, nome)


CPF_RE = re.compile(r'\d{3}[\.\s]?\d{3}[\.\s]?\d{3}[-\.\s]?\d{2}')

_cache = {"raw": {}, "meses_ordem": [], "ts": 0, "erros": [], "atualizando": False}
_cache_lock = threading.Lock()

# Cache em memoria para endpoints lentos (PostgreSQL/WA).
# Chave: string "endpoint_dias", valor: {"data": dict, "ts": float}.
# TTL padrao: 1 hora. Invalidacao: docker restart ou /api/cache/invalidar.
_api_cache: dict = {}
_api_cache_lock = threading.Lock()
_API_CACHE_TTL = 3600  # segundos

def _api_cached(key: str, ttl: int = _API_CACHE_TTL):
    """Retorna dados do cache se frescos, senao None."""
    with _api_cache_lock:
        entry = _api_cache.get(key)
    if entry and (time.time() - entry['ts']) < ttl:
        return entry['data']
    return None

def _api_cache_set(key: str, data: dict):
    # Não cacheia se o Quark ainda está carregando — evita cachear dados incompletos.
    with _cache_lock:
        quark_pronto = not _cache.get("atualizando", True) and bool(_cache.get("paciente_cpf_map"))
    if not quark_pronto:
        return
    with _api_cache_lock:
        _api_cache[key] = {'data': data, 'ts': time.time()}

def _api_cache_keys():
    with _api_cache_lock:
        return {k: v['ts'] for k, v in _api_cache.items()}


def today_brt():
    return datetime.datetime.now(BRT).date()


def agendador_de(usuario_cadastro_id):
    if usuario_cadastro_id is None:
        return "Portal Online"
    return USUARIO_MAP.get(usuario_cadastro_id, f"Outro ({usuario_cadastro_id})")


def quark_call(base, path, params):
    url = f"{base}/{path}?" + urllib.parse.urlencode(params, doseq=True)
    req = urllib.request.Request(url, headers=QUARK_HEADERS)
    for attempt in range(2):
        try:
            with urllib.request.urlopen(req, timeout=45) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except Exception as e:
            if attempt == 0:
                print(f"[quark_call] timeout em {path}, retry em 5s...", flush=True)
                time.sleep(5)
            else:
                raise


def quark_paginated(base, path, params):
    """Pagina ate a resposta vir vazia. NAO parar so por vir < 100 registros:
    a API do Quark pode retornar paginas com contagem irregular mesmo havendo
    mais paginas depois (bug confirmado na investigacao de divergencia de julho/2026)."""
    all_rows = {}
    page = 1
    while page <= QUARK_MAX_PAGES:
        p = dict(params)
        p["page"] = page
        d = quark_call(base, path, p)
        rows = d.get("response", [])
        if not rows:
            break
        for r in rows:
            all_rows[r["id"]] = r
        page += 1
        time.sleep(QUARK_SLEEP)
    return list(all_rows.values())


def month_bounds_str(year, month):
    last_day = calendar.monthrange(year, month)[1]
    inicio = datetime.date(year, month, 1).strftime("%d-%m-%Y")
    fim = datetime.date(year, month, last_day).strftime("%d-%m-%Y")
    return inicio, fim


def last_n_months_plus_current(n=3):
    """Retorna [(ano,mes), ...] do mais antigo pro mais recente: n meses
    completos anteriores + o mes atual (parcial)."""
    today = today_brt()
    months = [(today.year, today.month)]
    y, m = today.year, today.month
    for _ in range(n):
        m -= 1
        if m == 0:
            m, y = 12, y - 1
        months.append((y, m))
    months.reverse()
    return months


def fetch_month_raw(year, month):
    inicio, fim = month_bounds_str(year, month)
    agend = quark_paginated(QUARK_V1, "agendamentos", {
        "data_agendamento_inicio": inicio, "data_agendamento_fim": fim,
    })
    orc = quark_paginated(QUARK_V1, "orcamentos", {
        "data_inicio": inicio, "data_fim": fim,
    })
    contas = quark_paginated(QUARK_V2, "contas/receber", {
        "dataInicio": inicio, "dataFim": fim,
    })
    # /v1/vendas.dataConta = fonte de receita real, reconciliada exatamente com o
    # relatorio Extrato de Atendimentos do Quark (11/09/2026). contas/receber via
    # valorRecebido/dataBaixa SUBESTIMA fortemente pois cartao quase nunca recebe
    # baixa (~2-3% dos casos) - ver reference_quark_contas_receber.
    vendas = quark_paginated(QUARK_V1, "vendas", {
        "dataInicio": inicio, "dataFim": fim,
    })
    return {"agendamentos": agend, "orcamentos": orc, "contas": contas, "vendas": vendas}


def fetch_pacientes_mes(year, month):
    """Busca pacientes criados/atualizados no mês para mapa CPF→pacienteId."""
    inicio, fim = month_bounds_str(year, month)
    return quark_paginated(QUARK_V2, "pacientes", {
        "data-inicio": inicio, "data-fim": fim,
    })


def build_paciente_cpf_map(months):
    """Retorna {cpf_11_digitos: pacienteId} varrendo os meses disponíveis."""
    cpf_map = {}
    for (y, m) in months:
        try:
            pacientes = fetch_pacientes_mes(y, m)
            for p in pacientes:
                cpf_norm = re.sub(r"[^\d]", "", p.get("cpf") or "")
                if len(cpf_norm) == 11 and p.get("id"):
                    cpf_map[cpf_norm] = p["id"]
            print(f"[build_paciente_cpf_map] {y}-{m:02d}: {len(pacientes)} pacientes", flush=True)
        except Exception as e:
            print(f"[build_paciente_cpf_map] ERRO em {y}-{m:02d}: {e}", flush=True)
        time.sleep(QUARK_SLEEP)
    return cpf_map


def refresh_cache():
    with _cache_lock:
        _cache["atualizando"] = True
    months = last_n_months_plus_current(23)  # oldest→newest
    meses_ordem = [f"{y}-{m:02d}" for (y, m) in months]
    erros = []
    # Load newest months first so the app serves recent data immediately,
    # then backfill historical months incrementally.
    for (y, m) in reversed(months):
        key = f"{y}-{m:02d}"
        try:
            data = fetch_month_raw(y, m)
            print(f"[refresh_cache] {key} OK: "
                  f"{len(data['agendamentos'])} agendamentos, "
                  f"{len(data['orcamentos'])} orcamentos, "
                  f"{len(data['contas'])} contas, "
                  f"{len(data['vendas'])} vendas", flush=True)
            with _cache_lock:
                _cache.setdefault("raw", {})[key] = data
                _cache["meses_ordem"] = [k for k in meses_ordem if k in _cache["raw"]]
                _cache["ts"] = time.time()
        except Exception as e:
            with _cache_lock:
                tem_dados_anteriores = key in _cache.get("raw", {})
            if tem_dados_anteriores:
                print(f"[refresh_cache] AVISO {key}: {e} (dados anteriores mantidos)", flush=True)
            else:
                erros.append(f"{key}: {e}")
                print(f"[refresh_cache] ERRO {key}: sem dados — {e}", flush=True)
    # Mapa CPF → pacienteId (para cruzamento WA × Quark)
    paciente_cpf_map = build_paciente_cpf_map(months)
    with _cache_lock:
        _cache["erros"] = erros
        _cache["paciente_cpf_map"] = paciente_cpf_map
        _cache["atualizando"] = False
    print(f"[refresh_cache] mapa CPF→pacienteId: {len(paciente_cpf_map)} pacientes", flush=True)
    # Invalida o api_cache para que o warming reconstrua com dados Quark frescos.
    with _api_cache_lock:
        _api_cache.clear()
    print("[refresh_cache] api_cache limpo — warming vai reconstruir", flush=True)


_WARM_ENDPOINTS = [
    # Endpoints com período variável — todos os 4 cortes
    '/api/funil?dias=15',                '/api/funil?dias=30',                '/api/funil?dias=60',                '/api/funil?dias=90',
    '/api/especialidades?dias=15',       '/api/especialidades?dias=30',       '/api/especialidades?dias=60',       '/api/especialidades?dias=90',
    '/api/comparativo?dias=15',          '/api/comparativo?dias=30',          '/api/comparativo?dias=60',          '/api/comparativo?dias=90',
    '/api/performance?dias=15',          '/api/performance?dias=30',          '/api/performance?dias=60',          '/api/performance?dias=90',
    '/api/auditoria_financeira?dias=15', '/api/auditoria_financeira?dias=30', '/api/auditoria_financeira?dias=60', '/api/auditoria_financeira?dias=90',
    '/api/tendencias_avancadas?dias=15', '/api/tendencias_avancadas?dias=30', '/api/tendencias_avancadas?dias=60', '/api/tendencias_avancadas?dias=90',
    # Endpoints sem período — cacheados uma vez por refresh
    '/api/historico_mensal',
    '/api/heatmap_wa',
    '/api/projecao_mensal',
    '/api/projection_accuracy',
]

def _warm_api_cache(delay: int = 0):
    """Pre-aquece endpoints lentos após refresh do Quark."""
    if delay:
        time.sleep(delay)
    print(f"[warm] iniciando aquecimento de {len(_WARM_ENDPOINTS)} endpoints...", flush=True)
    for ep in _WARM_ENDPOINTS:
        try:
            urllib.request.urlopen(f'http://localhost:5000{ep}', timeout=120)
            print(f"[warm] {ep} OK", flush=True)
        except Exception as e:
            print(f"[warm] {ep} ERRO: {e}", flush=True)
    print("[warm] aquecimento concluido", flush=True)


def _bg_refresh():
    """Roda refresh na inicialização e depois às 07:00 e 19:00 BRT todos os dias."""
    HORARIOS_BRT = [7, 19]  # horas do dia em que o refresh ocorre
    # Primeira carga imediata
    try:
        refresh_cache()
        threading.Thread(target=_warm_api_cache, daemon=True).start()
    except Exception as e:
        print(f"[_bg_refresh] falha na carga inicial: {e}", flush=True)
    while True:
        agora = datetime.datetime.now(BRT)
        # Calcular próximo horário de refresh
        proximos = []
        for h in HORARIOS_BRT:
            candidato = agora.replace(hour=h, minute=0, second=0, microsecond=0)
            if candidato <= agora:
                candidato += datetime.timedelta(days=1)
            proximos.append(candidato)
        proximo = min(proximos)
        espera = (proximo - agora).total_seconds()
        print(f"[_bg_refresh] próximo refresh: {proximo.strftime('%d/%m %H:%M')} BRT ({int(espera/3600)}h {int((espera%3600)/60)}m)", flush=True)
        time.sleep(espera)
        try:
            refresh_cache()
            threading.Thread(target=_warm_api_cache, daemon=True).start()
        except Exception as e:
            print(f"[_bg_refresh] falha inesperada: {e}", flush=True)


threading.Thread(target=_bg_refresh, daemon=True).start()


# ── Rastreamento de precisão das projeções ──────────────────────────────────

PROJ_DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "projections.db")


def _proj_db_conn():
    conn = sqlite3.connect(PROJ_DB)
    conn.row_factory = sqlite3.Row
    return conn


def _init_projections_db():
    conn = _proj_db_conn()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS handoff_states (
            jid         TEXT    NOT NULL,
            handoff_ts  INTEGER NOT NULL,
            estado      TEXT    NOT NULL DEFAULT 'aguardando',
            agent       TEXT,
            assumed_at  INTEGER,
            desfecho    TEXT,
            desfecho_at INTEGER,
            notas       TEXT,
            PRIMARY KEY (jid, handoff_ts)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS chatwoot_map (
            jid             TEXT    PRIMARY KEY,
            conversation_id INTEGER,
            updated_at      INTEGER
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS insights_conversa (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            jid             TEXT    NOT NULL,
            conversation_id INTEGER,
            tipo            TEXT    NOT NULL,
            categoria       TEXT,
            resumo          TEXT,
            sugestao        TEXT,
            criado_em       INTEGER NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS projection_snapshots (
            id                    INTEGER PRIMARY KEY AUTOINCREMENT,
            snapshot_date         TEXT NOT NULL,
            mes_alvo              TEXT NOT NULL,
            dias_uteis_passados   INTEGER,
            dias_uteis_mes        INTEGER,
            proj_realizados       REAL,
            proj_agendamentos     REAL,
            proj_receita          REAL,
            ritmo_realizados      REAL,
            media_hist_realizados REAL,
            actual_realizados     REAL,
            actual_agendamentos   REAL,
            actual_receita        REAL,
            evaluated_at          TEXT,
            erro_realizados_pct   REAL,
            erro_receita_pct      REAL,
            UNIQUE(snapshot_date, mes_alvo)
        )
    """)
    conn.commit()
    conn.close()
    print("[projections_db] inicializado", flush=True)


_init_projections_db()


def _maybe_snapshot_projection(mes_alvo, dias_uteis_passados, dias_uteis_mes,
                                proj_realizados, proj_agendamentos, proj_receita,
                                ritmo_realizados, media_hist_realizados):
    """Salva snapshot da projeção de hoje para o mês alvo, se ainda não existe."""
    hoje_str = today_brt().isoformat()
    try:
        conn = _proj_db_conn()
        conn.execute("""
            INSERT OR IGNORE INTO projection_snapshots
            (snapshot_date, mes_alvo, dias_uteis_passados, dias_uteis_mes,
             proj_realizados, proj_agendamentos, proj_receita,
             ritmo_realizados, media_hist_realizados)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (hoje_str, mes_alvo, dias_uteis_passados, dias_uteis_mes,
              proj_realizados, proj_agendamentos, proj_receita,
              ritmo_realizados, media_hist_realizados))
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"[snapshot_projection] erro: {e}", flush=True)


def _maybe_evaluate_past_projections(meses_completos_data):
    """
    Preenche os valores reais (actuals) de projeções cujo mês já fechou.
    meses_completos_data: dict {mes_alvo (YYYY-MM): {realizados, agendamentos, receita}}
    """
    hoje_str = today_brt().isoformat()
    mes_atual_str = today_brt().strftime("%Y-%m")
    try:
        conn = _proj_db_conn()
        pendentes = conn.execute("""
            SELECT id, mes_alvo, proj_realizados, proj_receita
            FROM projection_snapshots
            WHERE evaluated_at IS NULL AND mes_alvo < ?
        """, (mes_atual_str,)).fetchall()

        for row in pendentes:
            mes_alvo = row["mes_alvo"]
            if mes_alvo not in meses_completos_data:
                continue
            act = meses_completos_data[mes_alvo]
            ar, aa, arev = act["realizados"], act["agendamentos"], act["receita"]
            pr, prev = row["proj_realizados"], row["proj_receita"]
            err_r   = round((ar - pr) / pr * 100, 1) if pr else None
            err_rev = round((arev - prev) / prev * 100, 1) if prev else None
            conn.execute("""
                UPDATE projection_snapshots
                SET actual_realizados = ?, actual_agendamentos = ?, actual_receita = ?,
                    evaluated_at = ?, erro_realizados_pct = ?, erro_receita_pct = ?
                WHERE id = ?
            """, (ar, aa, arev, hoje_str, err_r, err_rev, row["id"]))

        conn.commit()
        conn.close()
    except Exception as e:
        print(f"[evaluate_projections] erro: {e}", flush=True)


def compute_indicators(agendamentos, orcamentos, contas, vendas, hoje=None):
    if hoje is None:
        hoje = today_brt()

    # pacienteIds com conta recebida no período — proxy de atendimento realizado
    paciente_ids_com_conta = {c.get("pacienteId") for c in contas if c.get("pacienteId")}

    status_count = Counter(r.get("statusMarcacao") for r in agendamentos)
    total_agendamentos = len(agendamentos)

    consultas_rows = [r for r in agendamentos if r.get("statusMarcacao") in STATUS_CONSULTA_REALIZADA]
    consultas_realizadas = len(consultas_rows)
    consultas_por_agendador = Counter(agendador_de(r.get("usuarioCadastroId")) for r in consultas_rows)
    agendamentos_por_agendador = Counter(agendador_de(r.get("usuarioCadastroId")) for r in agendamentos)
    # usuarioCadastroId=API é o unico marcador confiavel de agendamento via bot -
    # meioAgendamento e selecionavel manualmente pela recepcao (corrigido 11/09/2026)
    agend_via_bot = sum(1 for r in agendamentos if r.get("usuarioCadastroId") == USUARIO_API_ID)

    cancelados = status_count.get("CANCELADO", 0) + status_count.get("CANCELADO_VIA_SMS", 0)
    faltou = status_count.get("FALTOU", 0) + status_count.get("AUSENTE_POS_CONSULTA", 0)
    excluidos = status_count.get("EXCLUIDO", 0)

    # Classificação refinada dos "sem desfecho":
    # — aguardando_hoje: está na clínica agora ou chegando hoje (normal)
    # — futuros: agendados para data futura (normal)
    # — realizados_via_conta: passou a data, sem status, mas tem conta → foi atendido
    # — sem_desfecho_real: passou a data, sem status, sem conta → problema real
    STATUS_AGUARDANDO = {"AGUARDANDO_ATENDIMENTO", "EM_ATENDIMENTO", "AGUARDANDO_PRE_CONSULTA"}
    STATUS_AGENDADOS   = {"AGENDADO", "CONFIRMADO"}
    futuros = aguardando_hoje_n = realizados_via_conta = sem_desfecho_real = 0
    for r in agendamentos:
        st = r.get("statusMarcacao")
        if st in STATUS_AGUARDANDO:
            aguardando_hoje_n += 1
        elif st in STATUS_AGENDADOS:
            data_ag = parse_quark_date(r.get("dataAgendamento", ""))
            if data_ag and data_ag > hoje:
                futuros += 1
            else:
                pac_id = r.get("pacienteId")
                if pac_id and pac_id in paciente_ids_com_conta:
                    realizados_via_conta += 1
                else:
                    sem_desfecho_real += 1

    sem_desfecho_total = futuros + aguardando_hoje_n + realizados_via_conta + sem_desfecho_real
    denom_conv = total_agendamentos - excluidos
    conversao_pct = round(consultas_realizadas / denom_conv * 100, 1) if denom_conv else None
    cancelamento_taxa_pct = round(cancelados / total_agendamentos * 100, 1) if total_agendamentos else None

    # Pipeline de orçamentos (propostas enviadas/aceitas) - nao usar pra receita
    # realizada, pois data_inicio/data_fim filtra por data de CRIACAO do orcamento,
    # nao de execucao/pagamento (confirmado 11/09/2026)
    orc_executada = [o for o in orcamentos if o.get("statusOrcamento") == "EXECUTADA"]
    orc_enviado = [o for o in orcamentos if o.get("statusOrcamento") == "ENVIADO_PARA_CLIENTE"]
    orc_executada_valor = round(sum(o.get("valorTotal") or 0 for o in orc_executada), 2)
    orc_enviado_valor = round(sum(o.get("valorTotal") or 0 for o in orc_enviado), 2)
    pacientes_com_orcamento_vendido = len(set(o.get("pacienteId") for o in orc_executada if o.get("pacienteId")))

    # Receita real: /v1/vendas.valorTotal, excluindo estornadas e pessoa institucional.
    # Reconciliado EXATO com o relatorio Extrato de Atendimentos do Quark em 11/09/2026
    # (365 registros / R$52.663,68 na janela de teste) - ver reference_quark_vendas.
    vendas_validas = [v for v in vendas if v.get("ativo") and not _pessoa_institucional(v.get("pessoa"))]

    gasto_por_pessoa = defaultdict(float)
    nome_por_pessoa = {}
    for v in vendas_validas:
        pessoa = v.get("pessoa") or {}
        pid = pessoa.get("id")
        if pid is None:
            continue
        gasto_por_pessoa[pid] += (v.get("valorTotal") or 0)
        if pid not in nome_por_pessoa and pessoa.get("nome"):
            nome_por_pessoa[pid] = pessoa.get("nome")
    receita_total = round(sum(gasto_por_pessoa.values()), 2)
    pacientes_pagantes = len(gasto_por_pessoa)
    gasto_medio = round(receita_total / pacientes_pagantes, 2) if pacientes_pagantes else None
    # ticket médio = receita / nº de vendas (transações), não por paciente único
    ticket_medio = round(receita_total / len(vendas_validas), 2) if vendas_validas else None
    top10 = sorted(gasto_por_pessoa.items(), key=lambda x: -x[1])[:10]
    top10_named = [
        {"pessoaId": pid, "nome": nome_por_pessoa.get(pid, "?"), "valor": round(v, 2)}
        for pid, v in top10
    ]

    return {
        "total_agendamentos": total_agendamentos,
        "status_count": dict(status_count),
        "consultas_realizadas": {
            "total": consultas_realizadas,
            "por_agendador": dict(consultas_por_agendador),
        },
        "agendamentos_por_agendador": dict(agendamentos_por_agendador),
        "agendamentos_via_bot": agend_via_bot,
        "conversao_pct": conversao_pct,
        "cancelamentos": {"total": cancelados, "taxa_pct": cancelamento_taxa_pct},
        "faltou": {"total": faltou},
        "excluidos": {"total": excluidos},
        "sem_desfecho": {
            "total": sem_desfecho_total,
            "futuros": futuros,
            "aguardando_hoje": aguardando_hoje_n,
            "realizados_via_conta": realizados_via_conta,
            "sem_desfecho_real": sem_desfecho_real,
        },
        "orcamentos_vendidos": {"count": len(orc_executada), "valor_total": orc_executada_valor},
        "orcamentos_nao_vendidos": {"count": len(orc_enviado), "valor_total": orc_enviado_valor},
        "financeiro": {
            "receita_total": receita_total,
            "pacientes_pagantes": pacientes_pagantes,
            "gasto_medio_periodo": gasto_medio,
            "ticket_medio": ticket_medio,
            "pacientes_com_orc_vendido": pacientes_com_orcamento_vendido,
            "top10_pacientes": top10_named,
        },
    }


def month_label(year, month, parcial):
    nome = f"{MESES_PT[month]}/{year}"
    return f"{nome} (parcial)" if parcial else nome


def dias_uteis_no_mes(year, month):
    last_day = calendar.monthrange(year, month)[1]
    return sum(1 for d in range(1, last_day + 1) if datetime.date(year, month, d).weekday() < 5)


def parse_quark_date(s):
    """Quark retorna datas como 'dd-mm-yyyy' (com hifens)."""
    if not s:
        return None
    s = str(s)[:10]
    for fmt in ("%d-%m-%Y", "%d/%m/%Y"):
        try:
            import datetime as _dt
            return _dt.datetime.strptime(s, fmt).date()
        except Exception:
            pass
    return None


def filter_by_days(rows, date_field, dias):
    cutoff = today_brt() - datetime.timedelta(days=dias)
    result = []
    for r in rows:
        d = parse_quark_date(r.get(date_field, ""))
        if d and d >= cutoff:
            result.append(r)
    return result


def _all_raw_rows():
    with _cache_lock:
        raw = dict(_cache["raw"])
        meses_ordem = list(_cache["meses_ordem"])
    agend_all, orc_all, contas_all, vendas_all = [], [], [], []
    for key in meses_ordem:
        d = raw.get(key, {})
        agend_all.extend(d.get("agendamentos", []))
        orc_all.extend(d.get("orcamentos", []))
        contas_all.extend(d.get("contas", []))
        vendas_all.extend(d.get("vendas", []))
    return agend_all, orc_all, contas_all, vendas_all


def filter_by_period(rows, date_field, start, end):
    result = []
    for r in rows:
        d = parse_quark_date(r.get(date_field, ""))
        if d and start <= d <= end:
            result.append(r)
    return result


def get_all_quark_rows(dias):
    agend_all, orc_all, contas_all, vendas_all = _all_raw_rows()
    return (
        filter_by_days(agend_all, "dataAgendamento", dias),
        filter_by_days(orc_all, "dataOrcamento", dias),
        filter_by_days(contas_all, "dataBaixa", dias),
        filter_by_days(vendas_all, "dataConta", dias),
    )


def compute_tendencias(dias):
    """Retorna dict com delta % para indicadores-chave vs. período anterior."""
    agend_all, orc_all, contas_all, vendas_all = _all_raw_rows()
    hoje = today_brt()
    end_cur = hoje
    start_cur = hoje - datetime.timedelta(days=dias)
    start_pre = start_cur - datetime.timedelta(days=dias)
    end_pre = start_cur - datetime.timedelta(days=1)

    def period(start, end):
        return (
            filter_by_period(agend_all, "dataAgendamento", start, end),
            filter_by_period(orc_all, "dataOrcamento", start, end),
            filter_by_period(contas_all, "dataBaixa", start, end),
            filter_by_period(vendas_all, "dataConta", start, end),
        )

    cur = compute_indicators(*period(start_cur, end_cur))
    pre = compute_indicators(*period(start_pre, end_pre))

    def delta(cur_v, pre_v, invert=False):
        """Retorna {'valor_atual': x, 'valor_anterior': y, 'delta_pct': z, 'direcao': 'up'|'down'|'estavel', 'positivo': bool}"""
        if cur_v is None and pre_v is None:
            return None
        cv = cur_v or 0
        pv = pre_v or 0
        if pv == 0:
            pct = None
        else:
            pct = round((cv - pv) / pv * 100, 1)
        if pct is None or abs(pct) < 1:
            dir_ = "estavel"
        elif cv > pv:
            dir_ = "up"
        else:
            dir_ = "down"
        # positivo: melhorando ou piorando?
        if dir_ == "estavel":
            positivo = True
        elif invert:
            positivo = (dir_ == "down")
        else:
            positivo = (dir_ == "up")
        return {"valor_atual": cv, "valor_anterior": pv, "delta_pct": pct, "direcao": dir_, "positivo": positivo}

    return {
        "agendamentos": delta(cur["total_agendamentos"], pre["total_agendamentos"]),
        "consultas": delta(cur["consultas_realizadas"]["total"], pre["consultas_realizadas"]["total"]),
        "conversao_pct": delta(cur["conversao_pct"], pre["conversao_pct"]),
        "cancelamentos": delta(cur["cancelamentos"]["total"], pre["cancelamentos"]["total"], invert=True),
        "receita_total": delta(cur["financeiro"]["receita_total"], pre["financeiro"]["receita_total"]),
        "gasto_medio": delta(cur["financeiro"]["gasto_medio_periodo"], pre["financeiro"]["gasto_medio_periodo"]),
        "orcamentos_vendidos": delta(cur["orcamentos_vendidos"]["valor_total"], pre["orcamentos_vendidos"]["valor_total"]),
        "orcamentos_nao_vendidos": delta(cur["orcamentos_nao_vendidos"]["valor_total"], pre["orcamentos_nao_vendidos"]["valor_total"], invert=True),
    }


def pg_conn():
    return psycopg2.connect(
        host="evolution-postgres", database="evolution_db",
        user="evolution", password="evolution123", connect_timeout=10,
    )


def _handoff_ts_atual(jid):
    """Mesmo critério de /api/handoffs (última msg #Mariana/#Maristela/#Tatiara/
    "Já passei..." do bot) para achar a linha vigente de handoff_states de um jid."""
    pg = pg_conn()
    cur = pg.cursor()
    cur.execute(f"""
        SELECT m."messageTimestamp"
        FROM "Message" m
        JOIN "Instance" i ON m."instanceId" = i.id
        WHERE i.name IN {_EVO_IN_SQL}
          AND m.key->>'remoteJid' = %s
          AND m.key->>'fromMe' = 'true'
          AND {_handoff_conds_sql()}
        ORDER BY m."messageTimestamp" DESC
        LIMIT 1
    """, (jid,))
    row = cur.fetchone()
    pg.close()
    return row[0] if row else None


@app.route("/api/performance")
def api_performance():
    dias = min(max(int(flask_request.args.get("dias", 30)), 1), 730)
    with _cache_lock:
        ts = _cache["ts"]
        erros = list(_cache["erros"])
        atualizando = _cache["atualizando"]
        meses_ordem = list(_cache["meses_ordem"])
    if not meses_ordem:
        return jsonify({"status": "carregando", "mensagem": "Primeira carga ainda em andamento."})
    agend, orc, contas, vendas = get_all_quark_rows(dias)
    ind = compute_indicators(agend, orc, contas, vendas)
    tendencias = compute_tendencias(dias)
    cutoff = today_brt() - datetime.timedelta(days=dias)
    return jsonify({
        "status": "ok", "dias": dias,
        "periodo_label": f"Ultimos {dias} dias (desde {cutoff.strftime('%d/%m/%Y')})",
        "atualizado_em": datetime.datetime.fromtimestamp(ts, BRT).strftime("%d/%m %H:%M") if ts else None,
        "atualizando": atualizando, "erros": erros,
        "tendencias": tendencias, **ind,
    })


def chatwoot_agent_summary(dias):
    """Le o relatorio nativo do Chatwoot por agente (v2, summary_reports/agent).
    Retorna None se o token nao estiver configurado (nunca levanta por isso)."""
    if not CHATWOOT_API_TOKEN:
        return None
    until = int(time.time())
    since = until - dias * 86400
    url = (f"{CHATWOOT_BASE}/api/v2/accounts/{CHATWOOT_ACCOUNT_ID}/summary_reports/agent"
           f"?since={since}&until={until}")
    req = urllib.request.Request(url, headers={"api_access_token": CHATWOOT_API_TOKEN})
    with urllib.request.urlopen(req, timeout=8) as resp:
        return json.loads(resp.read().decode("utf-8"))


@app.route("/api/equipe")
def api_equipe():
    """Scout tecnico: desempenho por agente humano, direto do Chatwoot.
    Bloco isolado de proposito (cache/estado proprios) — uma falha aqui
    nunca deve afetar /api/performance nem qualquer outro endpoint existente."""
    dias = min(max(int(flask_request.args.get("dias", 7)), 1), 90)
    _ck = f"equipe_{dias}"
    with _equipe_cache_lock:
        _hit = _equipe_cache.get(_ck)
    if _hit and (time.time() - _hit["ts"]) < _EQUIPE_CACHE_TTL:
        return jsonify(_hit["data"])

    try:
        raw = chatwoot_agent_summary(dias)
    except Exception as e:
        return jsonify({"status": "erro", "mensagem": f"Chatwoot indisponivel: {e}"})

    if raw is None:
        return jsonify({"status": "erro", "mensagem": "CHATWOOT_API_TOKEN nao configurado no .env"})

    agentes = []
    for row in raw:
        aid = row.get("id")
        atendidas = row.get("conversations_count") or 0
        resolvidas = row.get("resolved_conversations_count") or 0
        resp_s = row.get("avg_first_response_time")
        res_s = row.get("avg_resolution_time")
        agentes.append({
            "id": aid,
            "nome": CHATWOOT_AGENT_NAMES.get(aid, f"Agente {aid}"),
            "atendidas": atendidas,
            "resolvidas": resolvidas,
            "primeira_resposta_min": round(resp_s / 60, 1) if resp_s else None,
            "resolucao_h": round(res_s / 3600, 1) if res_s else None,
            "sem_atividade": atendidas == 0 and resolvidas == 0,
        })
    agentes.sort(key=lambda a: -a["atendidas"])

    com_resposta = [a for a in agentes if a["primeira_resposta_min"] is not None]
    com_resolucao = [a for a in agentes if a["resolucao_h"] is not None]
    media_resposta = round(sum(a["primeira_resposta_min"] for a in com_resposta) / len(com_resposta), 1) if com_resposta else None
    media_resolucao = round(sum(a["resolucao_h"] for a in com_resolucao) / len(com_resolucao), 1) if com_resolucao else None

    for a in agentes:
        a["desvio_resposta_min"] = (round(a["primeira_resposta_min"] - media_resposta, 1)
                                     if a["primeira_resposta_min"] is not None and media_resposta is not None else None)
        a["desvio_resolucao_h"] = (round(a["resolucao_h"] - media_resolucao, 1)
                                    if a["resolucao_h"] is not None and media_resolucao is not None else None)

    resultado = {
        "status": "ok",
        "dias": dias,
        "atualizado_em": datetime.datetime.fromtimestamp(int(time.time()), BRT).strftime("%d/%m %H:%M"),
        "agentes": agentes,
        "total_atendidas": sum(a["atendidas"] for a in agentes),
        "total_resolvidas": sum(a["resolvidas"] for a in agentes),
        "media_resposta_min": media_resposta,
        "media_resolucao_h": media_resolucao,
    }
    with _equipe_cache_lock:
        _equipe_cache[_ck] = {"data": resultado, "ts": time.time()}
    return jsonify(resultado)


CHATWOOT_LABEL_ESFRIOU = "esfriou"
CHATWOOT_LABEL_HANDOFF = "handoff"
ESFRIOU_MIN_DIAS = 1   # nao mexe em conversa com menos de 1 dia de silencio
ESFRIOU_MAX_DIAS = 30  # nao vale a pena resgatar lead mais velho que isso

# Agent Bot dedicado da Claudete no Chatwoot (criado 28/09/2026) — usado so pra
# postar a nota privada, pra ela aparecer atribuida a Claudete e nao ao Patrick.
# So autentica como bot (pode criar mensagem e aplicar etiqueta), nao pode
# apagar mensagem nem esta associado a nenhuma inbox de automacao.
CHATWOOT_BOT_TOKEN = os.environ.get("CHATWOOT_BOT_TOKEN", "")

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
ANTHROPIC_MODEL = "claude-haiku-4-5-20251001"
CATEGORIAS_INSIGHT = ["preco", "falta_horario", "duvida_nao_respondida", "pedido_fora_escopo",
                      "cancelamento", "urgencia", "documentacao", "outro"]


def _chatwoot_api(method, path, body=None, token=None):
    url = f"{CHATWOOT_BASE}{path}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                  headers={"api_access_token": token or CHATWOOT_API_TOKEN,
                                           "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read().decode())


def _postar_nota_privada(conversation_id, texto):
    """Posta mensagem interna (so agentes veem, paciente nunca ve), atribuida
    ao Agent Bot Claudete quando configurado (senao cai pro token do agente,
    pra nunca deixar de postar so por falta do bot)."""
    return _chatwoot_api("POST", f"/api/v1/accounts/{CHATWOOT_ACCOUNT_ID}/conversations/{conversation_id}/messages",
                          {"content": texto, "message_type": "outgoing", "private": True},
                          token=CHATWOOT_BOT_TOKEN or None)


def _aplicar_label_se_ausente(conversation_id, label, labels_atuais):
    """So aplica se ainda nao estiver la — nunca remove label existente."""
    if label in labels_atuais:
        return False
    _chatwoot_api("POST", f"/api/v1/accounts/{CHATWOOT_ACCOUNT_ID}/conversations/{conversation_id}/labels",
                  {"labels": labels_atuais + [label]})
    return True


def _buscar_mensagens_chatwoot(conversation_id, limite=30):
    """Busca as mensagens reais da conversa (paciente + atendimento), em ordem
    cronologica. Exclui notas privadas e eventos de sistema (etiqueta
    adicionada, conversa resolvida etc)."""
    data = _chatwoot_api("GET", f"/api/v1/accounts/{CHATWOOT_ACCOUNT_ID}/conversations/{conversation_id}/messages")
    itens = data.get("payload", data)
    mensagens = []
    for m in itens:
        if m.get("private"):
            continue
        if m.get("message_type") not in (0, 1):
            continue
        texto = (m.get("content") or "").strip()
        if not texto:
            continue
        quem = "Paciente" if m.get("message_type") == 0 else "Atendimento"
        mensagens.append({"quem": quem, "texto": texto})
    return mensagens[-limite:]


def _resumir_conversa_ia(mensagens, fato_conhecido=""):
    """Le a conversa real e devolve um resumo confiavel e uma sugestao de
    proxima acao justificada pelo que foi conversado, em portugues natural,
    sem travessao e sem jargao tecnico. Em qualquer falha (rede, chave
    ausente, resposta mal formada) devolve None, nunca lanca excecao — quem
    chamar decide o texto de reserva."""
    if not ANTHROPIC_API_KEY or not mensagens:
        return None

    transcricao = "\n".join(f"{m['quem']}: {m['texto']}" for m in mensagens)
    fato_txt = f"Fato ja confirmado: {fato_conhecido}\n\n" if fato_conhecido else ""
    prompt = (
        "Voce ajuda uma equipe de atendimento de clinica medica a entender rapido uma "
        "conversa de WhatsApp sem precisar reler tudo.\n\n"
        f"{fato_txt}"
        "Conversa, da mensagem mais antiga para a mais recente:\n"
        f"{transcricao}\n\n"
        "Responda em JSON puro, sem nenhum texto fora do JSON, com exatamente estas chaves:\n"
        "{\n"
        '  "resumo": "1 a 2 frases dizendo o que o paciente queria e onde a conversa parou. '
        "Portugues natural e direto, sem jargao tecnico, sem usar o caractere travessao.\",\n"
        '  "sugestao": "1 frase dizendo o que a equipe deveria fazer agora, justificada pelo '
        "que aconteceu na conversa. Sem travessao.\",\n"
        f'  "categoria": "uma destas palavras, exatamente: {", ".join(CATEGORIAS_INSIGHT)}"\n'
        "}"
    )
    body = {
        "model": ANTHROPIC_MODEL,
        "max_tokens": 300,
        "messages": [{"role": "user", "content": prompt}],
    }
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=json.dumps(body).encode(), method="POST",
        headers={"x-api-key": ANTHROPIC_API_KEY, "anthropic-version": "2023-06-01",
                 "content-type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.loads(resp.read().decode())
        texto_resp = data["content"][0]["text"].strip()
        texto_resp = re.sub(r"^```(?:json)?|```$", "", texto_resp, flags=re.MULTILINE).strip()
        parsed = json.loads(texto_resp)
        resumo = (parsed.get("resumo") or "").replace("—", ",").strip()
        sugestao = (parsed.get("sugestao") or "").replace("—", ",").strip()
        categoria = parsed.get("categoria") or "outro"
        if categoria not in CATEGORIAS_INSIGHT:
            categoria = "outro"
        if not resumo or not sugestao:
            return None
        return {"resumo": resumo, "sugestao": sugestao, "categoria": categoria}
    except Exception:
        return None


def _salvar_insight(jid, conversation_id, tipo, categoria, resumo, sugestao):
    with _proj_db_conn() as conn:
        conn.execute(
            "INSERT INTO insights_conversa (jid, conversation_id, tipo, categoria, resumo, sugestao, criado_em) "
            "VALUES (?,?,?,?,?,?,?)",
            (jid, conversation_id, tipo, categoria, resumo, sugestao, int(time.time())),
        )
        conn.commit()


def identificar_conversas_esfriadas():
    """Conversas com CPF coletado, ainda 'ATIVO' (sem agendamento, sem handoff,
    sem encerramento) e sem nenhuma atividade real (bot ou paciente) ha
    ESFRIOU_MIN_DIAS-ESFRIOU_MAX_DIAS dias. Usa claudete_historico (gravado
    pelo n8n com o telefone ja resolvido) como fonte de 'ultima atividade',
    imune ao bug de LID do Postgres da Evolution (ver fix de 27/09/2026)."""
    ts_min = int(time.time()) - ESFRIOU_MAX_DIAS * 86400
    ts_corte = int(time.time()) - ESFRIOU_MIN_DIAS * 86400
    conn = pg_conn()
    cur = conn.cursor()
    cur.execute(f"""
        WITH last_bot AS (
            SELECT DISTINCT ON (m.key->>'remoteJid')
                m.key->>'remoteJid' AS jid,
                m."messageTimestamp" AS bot_ts
            FROM "Message" m
            JOIN "Instance" i ON m."instanceId" = i.id
            WHERE i."name" IN {_EVO_IN_SQL}
              AND (m.key->>'fromMe')::boolean = true
            ORDER BY m.key->>'remoteJid', m."messageTimestamp" DESC
        ),
        historico_ativo AS (
            SELECT telefone,
                   data->>'cpf' AS cpf,
                   extract(epoch FROM updated_at)::bigint AS hist_ts
            FROM claudete_historico
            WHERE COALESCE(data->>'estado_conversa', '') = 'ATIVO'
              AND COALESCE(data->>'cpf', '') != ''
        )
        SELECT lb.jid, ha.cpf, GREATEST(lb.bot_ts, ha.hist_ts) AS ultima_atividade
        FROM last_bot lb
        JOIN historico_ativo ha ON ha.telefone = split_part(lb.jid, '@', 1)
        WHERE GREATEST(lb.bot_ts, ha.hist_ts) BETWEEN %s AND %s
    """, (ts_min, ts_corte))
    candidatos = [{"jid": r[0], "cpf": r[1], "ultima_atividade": r[2]} for r in cur.fetchall()]
    conn.close()

    with _proj_db_conn() as _cw_conn:
        cw_rows = _cw_conn.execute("SELECT jid, conversation_id FROM chatwoot_map").fetchall()
        chatwoot_map = {r["jid"]: r["conversation_id"] for r in cw_rows}

    for c in candidatos:
        c["chatwoot_conversation_id"] = chatwoot_map.get(c["jid"])
    return [c for c in candidatos if c["chatwoot_conversation_id"]]


@app.route("/api/manutencao/marcar_esfriadas", methods=["POST"])
def api_marcar_esfriadas():
    """Job periódico (chamado pelo n8n): aplica a label 'esfriou' nas conversas
    do Chatwoot identificadas como esquecidas — CPF dado, sem agendamento, sem
    handoff, sem atividade ha dias. So COMPLEMENTA labels existentes (nunca
    remove) e nunca mexe em conversa ja resolvida ou ja etiquetada."""
    if not CHATWOOT_API_TOKEN:
        return jsonify({"status": "erro", "mensagem": "CHATWOOT_API_TOKEN nao configurado"})

    try:
        candidatos = identificar_conversas_esfriadas()
    except Exception as e:
        return jsonify({"status": "erro", "mensagem": f"falha ao identificar candidatos: {e}"})

    marcadas, ja_marcadas, resolvidas, erros, notas_erro = 0, 0, 0, 0, 0
    for c in candidatos:
        cid = c["chatwoot_conversation_id"]
        try:
            conv = _chatwoot_api("GET", f"/api/v1/accounts/{CHATWOOT_ACCOUNT_ID}/conversations/{cid}")
        except Exception:
            erros += 1
            continue
        payload = conv.get("payload", conv)
        if payload.get("status") == "resolved":
            resolvidas += 1
            continue
        labels_atuais = payload.get("labels") or []
        if CHATWOOT_LABEL_ESFRIOU in labels_atuais:
            ja_marcadas += 1
            continue
        try:
            _aplicar_label_se_ausente(cid, CHATWOOT_LABEL_ESFRIOU, labels_atuais)
            marcadas += 1
        except Exception:
            erros += 1
            continue
        dias = int((time.time() - c["ultima_atividade"]) / 86400)
        data_txt = datetime.datetime.fromtimestamp(c["ultima_atividade"], BRT).strftime("%d/%m %H:%M")
        fato = f"a conversa esta sem nenhuma atividade ha {dias} dia(s), desde {data_txt}"

        resumo_ia = None
        try:
            mensagens = _buscar_mensagens_chatwoot(cid)
            resumo_ia = _resumir_conversa_ia(mensagens, fato_conhecido=fato)
        except Exception:
            resumo_ia = None

        if resumo_ia:
            texto = (f"🔵 Conversa esfriando, sem atividade ha {dias} dia(s).\n\n"
                      f"{resumo_ia['resumo']}\n\n"
                      f"Sugestao: {resumo_ia['sugestao']}")
            categoria = resumo_ia["categoria"]
        else:
            cpf_txt = f" O paciente informou CPF {c['cpf']}." if c.get("cpf") else ""
            texto = (f"🔵 Conversa esfriando, sem atividade ha {dias} dia(s).\n\n"
                      f"Sem resposta desde {data_txt}.{cpf_txt}")
            categoria = "outro"

        try:
            _postar_nota_privada(cid, texto)
            _salvar_insight(c["jid"], cid, "esfriou", categoria,
                            resumo_ia["resumo"] if resumo_ia else texto, resumo_ia["sugestao"] if resumo_ia else "")
        except Exception:
            notas_erro += 1

    return jsonify({
        "status": "ok",
        "candidatos_encontrados": len(candidatos),
        "marcadas_agora": marcadas,
        "ja_estavam_marcadas": ja_marcadas,
        "puladas_resolvidas": resolvidas,
        "erros": erros,
        "notas_privadas_falharam": notas_erro,
        "executado_em": datetime.datetime.fromtimestamp(int(time.time()), BRT).strftime("%d/%m %H:%M"),
    })


def _buscar_conversation_id_por_telefone(jid):
    """Fallback pra quando chatwoot_map ainda nao tem esse jid — acontece
    sempre no primeiro handoff de um contato novo, porque o mapa so e
    preenchido quando uma conversa e resolvida ou reatribuida (webhook
    'CCN — Chatwoot Handoff Signal'), nunca na criacao da conversa. Busca
    direto na API do Chatwoot pelo telefone e usa a conversa mais recente."""
    telefone = jid.split('@')[0]
    try:
        contatos = _chatwoot_api("GET", f"/api/v1/accounts/{CHATWOOT_ACCOUNT_ID}/contacts/search?q=%2B{telefone}")
        candidatos = contatos.get("payload", [])
        if not candidatos:
            return None
        contact_id = candidatos[0]["id"]
        convs = _chatwoot_api("GET", f"/api/v1/accounts/{CHATWOOT_ACCOUNT_ID}/contacts/{contact_id}/conversations")
        conv_payload = convs.get("payload", [])
        if not conv_payload:
            return None
        conv_payload.sort(key=lambda c: c.get("created_at", 0), reverse=True)
        return conv_payload[0]["id"]
    except Exception:
        return None


@app.route("/api/chatwoot/handoff_note", methods=["POST"])
def api_chatwoot_handoff_note():
    """Chamado pelo n8n no momento do handoff (nos 'Notificar Handoff — Nota
    Privada v' e 'Notificar Handoff — Nota Privada Recepção'). Aplica a
    etiqueta 'handoff' e posta nota privada com o contexto — so dentro do
    Chatwoot, paciente nunca ve."""
    if not CHATWOOT_API_TOKEN:
        return jsonify({"status": "erro", "mensagem": "CHATWOOT_API_TOKEN nao configurado"})

    body = flask_request.get_json(silent=True) or {}
    jid = (body.get("jid") or "").strip()
    if not jid:
        return jsonify({"status": "erro", "mensagem": "jid obrigatorio"}), 400

    with _proj_db_conn() as _cw_conn:
        row = _cw_conn.execute("SELECT conversation_id FROM chatwoot_map WHERE jid = ?", (jid,)).fetchone()
    cid = row["conversation_id"] if row else None

    if not cid:
        cid = _buscar_conversation_id_por_telefone(jid)
        if cid:
            with _proj_db_conn() as _cw_conn:
                _cw_conn.execute("""
                    INSERT INTO chatwoot_map (jid, conversation_id, updated_at)
                    VALUES (?, ?, ?)
                    ON CONFLICT(jid) DO UPDATE SET
                        conversation_id=excluded.conversation_id, updated_at=excluded.updated_at
                """, (jid, cid, int(time.time())))
                _cw_conn.commit()

    if not cid:
        return jsonify({"status": "erro", "mensagem": "conversation_id nao encontrado pra esse jid (nem no mapa nem via busca por telefone no Chatwoot)"})

    motivo = (body.get("motivo") or "").strip() or "(sem contexto identificado)"
    nome = (body.get("nome") or "").strip()
    telefone = (body.get("telefone") or "").strip()
    especialidade = (body.get("especialidade") or "").strip()
    cpf = (body.get("cpf") or "").strip()

    try:
        conv = _chatwoot_api("GET", f"/api/v1/accounts/{CHATWOOT_ACCOUNT_ID}/conversations/{cid}")
    except Exception as e:
        return jsonify({"status": "erro", "mensagem": f"falha ao ler conversa: {e}"})
    payload = conv.get("payload", conv)
    labels_atuais = payload.get("labels") or []

    try:
        etiqueta_aplicada = _aplicar_label_se_ausente(cid, CHATWOOT_LABEL_HANDOFF, labels_atuais)
    except Exception as e:
        return jsonify({"status": "erro", "mensagem": f"falha ao aplicar etiqueta: {e}"})

    fato = f"a Claudete classificou o motivo internamente como {motivo}"
    if especialidade:
        fato += f", especialidade {especialidade}"

    resumo_ia = None
    try:
        mensagens = _buscar_mensagens_chatwoot(cid)
        resumo_ia = _resumir_conversa_ia(mensagens, fato_conhecido=fato)
    except Exception:
        resumo_ia = None

    if resumo_ia:
        texto = (f"🔴 A Claudete pediu ajuda humana nessa conversa.\n\n"
                  f"{resumo_ia['resumo']}\n\n"
                  f"Sugestao: {resumo_ia['sugestao']}")
        categoria = resumo_ia["categoria"]
    else:
        linha_paciente = " e ".join(x for x in [nome, telefone] if x)
        cpf_txt = f" CPF {cpf}." if cpf else ""
        texto = (f"🔴 A Claudete pediu ajuda humana nessa conversa.\n\n"
                  f"Paciente {linha_paciente}.{cpf_txt} O motivo indicado foi {motivo}.")
        categoria = "outro"

    try:
        _postar_nota_privada(cid, texto)
        _salvar_insight(jid, cid, "handoff", categoria,
                        resumo_ia["resumo"] if resumo_ia else texto, resumo_ia["sugestao"] if resumo_ia else "")
    except Exception as e:
        return jsonify({"status": "erro", "mensagem": f"etiqueta aplicada, mas nota falhou: {e}"})

    return jsonify({"status": "ok", "conversation_id": cid,
                     "etiqueta_aplicada_agora": etiqueta_aplicada, "categoria": categoria})


@app.route("/api/funil")
def api_funil():
    dias = min(max(int(flask_request.args.get("dias", 30)), 1), 730)
    _ck = f"funil_{dias}"
    _hit = _api_cached(_ck)
    if _hit:
        return jsonify(_hit)
    ts_inicio = int(time.time()) - dias * 86400
    try:
        conn = pg_conn()
        cur = conn.cursor()
        EXCL = '%%@g.us'
        HMAR = '%%#mariana%%'
        CPF_PAT = r'\d{3}[\. ]?\d{3}[\. ]?\d{3}[-\. ]?\d{2}'

        cur.execute(
            "SELECT COUNT(DISTINCT m.key->>'remoteJid') FROM \"Message\" m "
            "JOIN \"Instance\" i ON m.\"instanceId\" = i.id "
            f"WHERE i.\"name\" IN {_EVO_IN_SQL} "
            "AND m.key->>'remoteJid' NOT LIKE %s "
            "AND m.\"messageTimestamp\" >= %s AND m.key->>'fromMe' = 'false'",
            (EXCL, ts_inicio))
        total_conversas = cur.fetchone()[0] or 0

        # Conversas por numero de origem (3449=Claudete-recep legado, 2042=claudete2
        # novo/site) — acompanhamento da migracao, nao soma exato com total_conversas
        # pois quem escreveu pros dois numeros no periodo conta nos dois lados
        cur.execute(
            "SELECT i.\"name\", COUNT(DISTINCT m.key->>'remoteJid') FROM \"Message\" m "
            "JOIN \"Instance\" i ON m.\"instanceId\" = i.id "
            f"WHERE i.\"name\" IN {_EVO_IN_SQL} "
            "AND m.key->>'remoteJid' NOT LIKE %s "
            "AND m.\"messageTimestamp\" >= %s AND m.key->>'fromMe' = 'false' "
            "GROUP BY i.\"name\"",
            (EXCL, ts_inicio))
        conversas_por_instancia = dict(cur.fetchall())
        _inst0 = EVOLUTION_INSTANCES[0] if EVOLUTION_INSTANCES else 'Claudete-recep'
        _inst1 = EVOLUTION_INSTANCES[1] if len(EVOLUTION_INSTANCES) > 1 else 'claudete2'
        conversas_3449 = conversas_por_instancia.get(_inst0, 0)
        conversas_2042 = conversas_por_instancia.get(_inst1, 0)

        cur.execute(
            "SELECT COUNT(DISTINCT m.key->>'remoteJid') FROM \"Message\" m "
            "JOIN \"Instance\" i ON m.\"instanceId\" = i.id "
            f"WHERE i.\"name\" IN {_EVO_IN_SQL} "
            "AND m.key->>'remoteJid' NOT LIKE %s "
            "AND m.\"messageTimestamp\" >= %s AND m.key->>'fromMe' = 'false' "
            "AND COALESCE(m.message->>'conversation', "
            "m.message->'extendedTextMessage'->>'text', '') ~ %s",
            (EXCL, ts_inicio, CPF_PAT))
        cpfs = cur.fetchone()[0] or 0

        cur.execute(
            "SELECT COUNT(DISTINCT m.key->>'remoteJid') FROM \"Message\" m "
            "JOIN \"Instance\" i ON m.\"instanceId\" = i.id "
            f"WHERE i.\"name\" IN {_EVO_IN_SQL} "
            "AND m.key->>'remoteJid' NOT LIKE %s "
            "AND m.\"messageTimestamp\" >= %s AND m.key->>'fromMe' = 'true' "
            "AND LOWER(COALESCE(m.message->>'conversation', '')) LIKE %s",
            (EXCL, ts_inicio, HMAR))
        handoffs = cur.fetchone()[0] or 0

        semanas = []
        hoje = today_brt()
        n_sem = max(dias // 7, 1)
        for w in range(n_sem - 1, -1, -1):
            fim_s = hoje - datetime.timedelta(weeks=w)
            ini_s = fim_s - datetime.timedelta(days=6)
            ts_i = int(datetime.datetime(ini_s.year, ini_s.month, ini_s.day, tzinfo=BRT).timestamp())
            ts_f = int(datetime.datetime(fim_s.year, fim_s.month, fim_s.day, 23, 59, 59, tzinfo=BRT).timestamp())
            cur.execute(
                "SELECT COUNT(DISTINCT m.key->>'remoteJid') "
                'FROM "Message" m JOIN "Instance" i ON m."instanceId" = i.id '
                f"WHERE i.\"name\" IN {_EVO_IN_SQL} "
                "AND m.key->>'remoteJid' NOT LIKE %s "
                "AND m.\"messageTimestamp\" BETWEEN %s AND %s "
                "AND m.key->>'fromMe' = 'false'",
                ('%@g.us', ts_i, ts_f))
            semanas.append({"label": ini_s.strftime("%d/%m"), "valor": cur.fetchone()[0] or 0})

        # Período anterior para tendência
        ts_prev_fim = ts_inicio - 1
        ts_prev_ini = ts_inicio - dias * 86400

        def pg_count(sql, params):
            cur.execute(sql, params)
            return cur.fetchone()[0] or 0

        BASE_Q = (
            "SELECT COUNT(DISTINCT m.key->>'remoteJid') FROM \"Message\" m "
            "JOIN \"Instance\" i ON m.\"instanceId\" = i.id "
            f"WHERE i.\"name\" IN {_EVO_IN_SQL} "
            "AND m.key->>'remoteJid' NOT LIKE %s "
        )
        conv_pre = pg_count(BASE_Q + "AND m.\"messageTimestamp\" BETWEEN %s AND %s AND m.key->>'fromMe' = 'false'",
                            (EXCL, ts_prev_ini, ts_prev_fim))
        cpfs_pre = pg_count(BASE_Q + "AND m.\"messageTimestamp\" BETWEEN %s AND %s AND m.key->>'fromMe' = 'false' "
                            "AND COALESCE(m.message->>'conversation', m.message->'extendedTextMessage'->>'text', '') ~ %s",
                            (EXCL, ts_prev_ini, ts_prev_fim, CPF_PAT))
        hand_pre = pg_count(BASE_Q + "AND m.\"messageTimestamp\" BETWEEN %s AND %s AND m.key->>'fromMe' = 'true' "
                            "AND LOWER(COALESCE(m.message->>'conversation', '')) LIKE %s",
                            (EXCL, ts_prev_ini, ts_prev_fim, HMAR))

        def wa_delta(cur_v, pre_v, invert=False):
            if pre_v == 0:
                return {"valor_atual": cur_v, "valor_anterior": pre_v, "delta_pct": None, "direcao": "estavel", "positivo": True}
            pct = round((cur_v - pre_v) / pre_v * 100, 1)
            if abs(pct) < 1:
                dir_ = "estavel"
            elif cur_v > pre_v:
                dir_ = "up"
            else:
                dir_ = "down"
            positivo = True if dir_ == "estavel" else ((dir_ == "down") if invert else (dir_ == "up"))
            return {"valor_atual": cur_v, "valor_anterior": pre_v, "delta_pct": pct, "direcao": dir_, "positivo": positivo}

        # CPFs reais das conversas WA (para cruzamento com Quark)
        cur.execute(
            "SELECT DISTINCT (regexp_matches("
            "COALESCE(m.message->>'conversation', m.message->'extendedTextMessage'->>'text',''),"
            "%s, 'g'))[1] "
            "FROM \"Message\" m JOIN \"Instance\" i ON m.\"instanceId\" = i.id "
            f"WHERE i.\"name\" IN {_EVO_IN_SQL} "
            "AND m.key->>'remoteJid' NOT LIKE %s "
            "AND m.\"messageTimestamp\" >= %s AND m.key->>'fromMe' = 'false'",
            (CPF_PAT, EXCL, ts_inicio))
        wa_cpfs_norm = {re.sub(r"[^\d]", "", row[0]) for row in cur.fetchall()
                        if row[0] and len(re.sub(r"[^\d]", "", row[0])) == 11}

        # --- PROTOTIPO: recorte "handoff pos-intencao" ---
        # Mede a conversao da equipe humana sobre conversas em que a intencao de
        # compra ja estava evidente (CPF ja coletado) ANTES do #mariana - nao
        # mistura com handoffs de conversas ainda frias (duvida clinica, plano
        # nao identificado etc), que nao tem "venda" pra fechar.
        cur.execute(
            "SELECT jid, MIN(ts) AS first_cpf_ts, (array_agg(cpf ORDER BY ts))[1] AS cpf FROM ("
            "  SELECT m.key->>'remoteJid' AS jid, m.\"messageTimestamp\" AS ts,"
            "         (regexp_matches(COALESCE(m.message->>'conversation', m.message->'extendedTextMessage'->>'text',''), %s, 'g'))[1] AS cpf"
            "  FROM \"Message\" m JOIN \"Instance\" i ON m.\"instanceId\" = i.id"
            f"  WHERE i.\"name\" IN {_EVO_IN_SQL} AND m.key->>'remoteJid' NOT LIKE %s"
            "    AND m.\"messageTimestamp\" >= %s AND m.key->>'fromMe' = 'false'"
            "    AND COALESCE(m.message->>'conversation', m.message->'extendedTextMessage'->>'text','') ~ %s"
            ") sub GROUP BY jid",
            (CPF_PAT, EXCL, ts_inicio, CPF_PAT))
        cpf_ts_por_jid = {row[0]: {"ts": row[1], "cpf": row[2]} for row in cur.fetchall()}

        cur.execute(
            "SELECT m.key->>'remoteJid' AS jid, m.\"messageTimestamp\" AS ts"
            " FROM \"Message\" m JOIN \"Instance\" i ON m.\"instanceId\" = i.id"
            f" WHERE i.\"name\" IN {_EVO_IN_SQL} AND m.key->>'remoteJid' NOT LIKE %s"
            "   AND m.\"messageTimestamp\" >= %s AND m.key->>'fromMe' = 'true'"
            "   AND LOWER(COALESCE(m.message->>'conversation','')) LIKE %s",
            (EXCL, ts_inicio, HMAR))
        handoff_ts_por_jid = defaultdict(list)
        for jid, ts in cur.fetchall():
            handoff_ts_por_jid[jid].append(ts)

        # Saude Claudete: tempo mediano de resposta (seg)
        try:
            cur.execute(f"""
                WITH incoming AS (
                    SELECT m.key->>'remoteJid' AS jid, m."messageTimestamp" AS ts
                    FROM "Message" m JOIN "Instance" i ON m."instanceId" = i.id
                    WHERE i.name IN {_EVO_IN_SQL}
                      AND m.key->>'remoteJid' NOT LIKE %s
                      AND (m.key->>'fromMe')::boolean = false
                      AND m."messageTimestamp" >= %s
                ),
                responses AS (
                    SELECT m.key->>'remoteJid' AS jid, m."messageTimestamp" AS ts
                    FROM "Message" m JOIN "Instance" i ON m."instanceId" = i.id
                    WHERE i.name IN {_EVO_IN_SQL}
                      AND m.key->>'remoteJid' NOT LIKE %s
                      AND (m.key->>'fromMe')::boolean = true
                      AND m."messageTimestamp" >= %s
                ),
                diffs AS (
                    SELECT r.ts - i.ts AS diff
                    FROM incoming i JOIN responses r ON r.jid = i.jid AND r.ts > i.ts AND r.ts <= i.ts + 300
                )
                SELECT percentile_cont(0.5) WITHIN GROUP (ORDER BY diff) FROM diffs
            """, (EXCL, ts_inicio, EXCL, ts_inicio))
            tempo_resp = cur.fetchone()[0]
            tempo_resp_mediano = round(float(tempo_resp), 1) if tempo_resp is not None else None
        except Exception:
            tempo_resp_mediano = None

        # Taxa de retorno: usuarios que conversaram no periodo anterior e voltaram neste
        try:
            cur.execute(
                "SELECT DISTINCT m.key->>'remoteJid' FROM \"Message\" m "
                "JOIN \"Instance\" i ON m.\"instanceId\" = i.id "
                f"WHERE i.name IN {_EVO_IN_SQL} "
                "AND m.key->>'remoteJid' NOT LIKE %s "
                "AND m.\"messageTimestamp\" BETWEEN %s AND %s AND m.key->>'fromMe' = 'false'",
                (EXCL, ts_prev_ini, ts_prev_fim))
            jids_prev = {r[0] for r in cur.fetchall()}
            cur.execute(
                "SELECT DISTINCT m.key->>'remoteJid' FROM \"Message\" m "
                "JOIN \"Instance\" i ON m.\"instanceId\" = i.id "
                f"WHERE i.name IN {_EVO_IN_SQL} "
                "AND m.key->>'remoteJid' NOT LIKE %s "
                "AND m.\"messageTimestamp\" >= %s AND m.key->>'fromMe' = 'false'",
                (EXCL, ts_inicio))
            jids_cur = {r[0] for r in cur.fetchall()}
            retornaram = len(jids_prev & jids_cur)
            taxa_retorno = round(retornaram / len(jids_prev) * 100, 1) if jids_prev else None
        except Exception:
            taxa_retorno = None

        # Breakdown de conversas sem CPF — para barra de atrito detalhada
        ts_48h_ago = int(time.time()) - 48 * 3600
        try:
            cur.execute(f"""
                WITH no_cpf AS (
                    SELECT DISTINCT m.key->>'remoteJid' AS jid
                    FROM "Message" m
                    JOIN "Instance" i ON m."instanceId" = i.id
                    WHERE i."name" IN {_EVO_IN_SQL}
                      AND m.key->>'remoteJid' NOT LIKE %s
                      AND m."messageTimestamp" >= %s
                      AND (m.key->>'fromMe')::boolean = false
                    EXCEPT
                    SELECT DISTINCT m2.key->>'remoteJid'
                    FROM "Message" m2
                    JOIN "Instance" i2 ON m2."instanceId" = i2.id
                    WHERE i2."name" IN {_EVO_IN_SQL}
                      AND m2.key->>'remoteJid' NOT LIKE %s
                      AND m2."messageTimestamp" >= %s
                      AND (m2.key->>'fromMe')::boolean = false
                      AND COALESCE(m2.message->>'conversation',
                                   m2.message->'extendedTextMessage'->>'text', '') ~ %s
                ),
                stats AS (
                    SELECT
                        m.key->>'remoteJid' AS jid,
                        COUNT(*) FILTER (WHERE (m.key->>'fromMe')::boolean = false) AS n_in,
                        MAX(m."messageTimestamp") AS last_ts
                    FROM "Message" m
                    JOIN "Instance" i ON m."instanceId" = i.id
                    WHERE i."name" IN {_EVO_IN_SQL}
                      AND m.key->>'remoteJid' NOT LIKE %s
                      AND m."messageTimestamp" >= %s
                    GROUP BY m.key->>'remoteJid'
                )
                SELECT
                    COUNT(*) FILTER (WHERE s.n_in = 1),
                    COUNT(*) FILTER (WHERE s.n_in BETWEEN 2 AND 5),
                    COUNT(*) FILTER (WHERE s.n_in >= 6),
                    COUNT(*) FILTER (WHERE s.last_ts >= %s)
                FROM no_cpf nc
                JOIN stats s ON nc.jid = s.jid
            """, (EXCL, ts_inicio, EXCL, ts_inicio, CPF_PAT, EXCL, ts_inicio, ts_48h_ago))
            _r = cur.fetchone()
            atrito_cpf = {
                "total_sem_cpf":    total_conversas - cpfs,
                "apenas_1_msg":     _r[0] or 0,
                "msgs_2_5":         _r[1] or 0,
                "msgs_6_mais":      _r[2] or 0,
                "ainda_ativos_48h": _r[3] or 0,
            }
        except Exception as e:
            print(f"[api_funil] erro no atrito_cpf: {e}", flush=True)
            atrito_cpf = {"total_sem_cpf": total_conversas - cpfs}

        # Dados brutos para análise de abandono pós-CPF — classificação feita após conn.close()
        raw_pos_cpf = {}
        if cpf_ts_por_jid:
            jids_com_cpf = list(cpf_ts_por_jid.keys())
            try:
                cur.execute(f"""
                    WITH last_bot AS (
                        SELECT DISTINCT ON (m.key->>'remoteJid')
                            m.key->>'remoteJid' AS jid,
                            m."messageTimestamp"  AS bot_ts,
                            COALESCE(m.message->>'conversation',
                                     m.message->'extendedTextMessage'->>'text', '') AS bot_text
                        FROM "Message" m
                        JOIN "Instance" i ON m."instanceId" = i.id
                        WHERE i."name" IN {_EVO_IN_SQL}
                          AND m.key->>'remoteJid' = ANY(%s)
                          AND m."messageTimestamp" >= %s
                          AND (m.key->>'fromMe')::boolean = true
                        ORDER BY m.key->>'remoteJid', m."messageTimestamp" DESC
                    ),
                    last_user AS (
                        SELECT DISTINCT ON (m.key->>'remoteJid')
                            m.key->>'remoteJid' AS jid,
                            m."messageTimestamp"  AS user_ts
                        FROM "Message" m
                        JOIN "Instance" i ON m."instanceId" = i.id
                        WHERE i."name" IN {_EVO_IN_SQL}
                          AND m.key->>'remoteJid' = ANY(%s)
                          AND m."messageTimestamp" >= %s
                          AND (m.key->>'fromMe')::boolean = false
                        ORDER BY m.key->>'remoteJid', m."messageTimestamp" DESC
                    ),
                    confirmados AS (
                        SELECT DISTINCT m.key->>'remoteJid' AS jid
                        FROM "Message" m
                        JOIN "Instance" i ON m."instanceId" = i.id
                        WHERE i."name" IN {_EVO_IN_SQL}
                          AND m.key->>'remoteJid' = ANY(%s)
                          AND m."messageTimestamp" >= %s
                          AND (m.key->>'fromMe')::boolean = true
                          AND LOWER(COALESCE(m.message->>'conversation',
                                             m.message->'extendedTextMessage'->>'text', ''))
                              LIKE '%%agendamento confirmado%%'
                    ),
                    -- Casamento por identidade correta (27/09/2026): o Postgres da Evolution
                    -- grava mensagens de contatos com "LID" do WhatsApp sob um id opaco
                    -- (ex: 273988949393585@lid) desconectado do JID por telefone que o bot usa
                    -- pra responder — a conversa fica "partida" em dois registros e a resposta
                    -- real do paciente fica invisivel pro last_user acima. O n8n nunca ve esse
                    -- problema (recebe o JID por telefone ja resolvido no webhook), entao a
                    -- claudete_historico (gravada pelo proprio n8n, chave = telefone) e imune a
                    -- esse bug. Usamos o updated_at dela como reforco, nunca como substituicao.
                    historico_check AS (
                        SELECT telefone, extract(epoch FROM updated_at)::bigint AS hist_ts
                        FROM claudete_historico
                    )
                    SELECT lb.jid, lb.bot_ts, lb.bot_text,
                           GREATEST(COALESCE(lu.user_ts, 0), COALESCE(hc.hist_ts, 0)) AS user_ts_raw,
                           (c.jid IS NOT NULL) AS booking_confirmed
                    FROM last_bot lb
                    LEFT JOIN last_user lu ON lb.jid = lu.jid
                    LEFT JOIN historico_check hc ON hc.telefone = split_part(lb.jid, '@', 1)
                    LEFT JOIN confirmados c ON lb.jid = c.jid
                """, (jids_com_cpf, ts_inicio, jids_com_cpf, ts_inicio, jids_com_cpf, ts_inicio))
                for row in cur.fetchall():
                    user_ts_corrigido = row[3] if row[3] and row[3] > 0 else None
                    raw_pos_cpf[row[0]] = {"bot_ts": row[1], "bot_text": row[2] or "", "user_ts": user_ts_corrigido, "booking_confirmed": row[4]}
            except Exception as e:
                print(f"[api_funil] erro no raw_pos_cpf: {e}", flush=True)

        conn.close()
        agend, _, _, vendas_periodo = get_all_quark_rows(dias)
        # agendamentos via Claudete: usuarioCadastroId=API (unico marcador confiavel,
        # meioAgendamento e selecao manual - corrigido 11/09/2026)
        agend_cl = sum(1 for a in agend if a.get("usuarioCadastroId") == USUARIO_API_ID)
        conv_pct = round(agend_cl / cpfs * 100, 1) if cpfs else None

        # Cruzamento WA CPF × Quark pacienteId → realizados influenciados pela Claudete
        with _cache_lock:
            cpf_map = dict(_cache.get("paciente_cpf_map", {}))
        wa_paciente_ids = {cpf_map[c] for c in wa_cpfs_norm if c in cpf_map}
        influenciados = [
            a for a in agend
            if a.get("pacienteId") in wa_paciente_ids
            and a.get("statusMarcacao") in STATUS_CONSULTA_REALIZADA
        ]
        autonomos_realizados = [
            a for a in influenciados
            if a.get("usuarioCadastroId") == USUARIO_API_ID
        ]

        # Análise de abandono pós-CPF — Universo B (com CPF, sem agendamento)
        _PAT_LEMBRETE = re.compile(
            r'caro\(a\)\s+paciente.*você\s+possui\s+um\s+agendamento'
            r'|passando\s+para\s+confirmar\s+seu\s+atendimento.*agendado\s+para',
            re.I | re.DOTALL
        )
        _STAGE_PATS = [
            ("handoff",          re.compile(r'#mariana|nossa\s*equipe|acionar\s+a\s+equipe|passei\s+seu\s+atendimento|algu[eé]m\s+te\s+respond|atendente|ser[aá]\s*atendid|equipe\s*vai|chamar\s*nossa|atendimento\s+humano', re.I)),
            ("apos_preco",       re.compile(r'r\$\s*\d|valor\s+d[ae]\s+consulta|consulta\s+custa|investimento|desconto|clube\s+ccn|check.?up', re.I)),
            ("sem_agenda",       re.compile(r'sem\s*(vaga|hor[aá]rios?)|hor[aá]rios?\s*indispon|n[aã]o\s*t[ei]m(?:os)?\s*hor[aá]rios?|nenhum\s*hor[aá]rio|sem\s*disponib|n[aã]o\s+encontrei\s+agenda|rede\s+parceira', re.I)),
            ("sem_plano",        re.compile(r'exclusivamente\s+particular|n[aã]o\s+trabalhamos\s+com\s+nenhum\s+plano|n[aã]o\s+(aceit|oper)\w*\s+plano|sem\s+plano\s+de\s+sa[uú]de', re.I)),
            ("confirmacao",      re.compile(r'confirmar?\s+(seu|o)\s+agend|posso\s+confirmar|gostaria\s+de\s+confirmar|confirme|agendamento\s+est[aá]\s+registrado|seu\s+agendamento\s+est[aá]', re.I)),
            ("oferta_horario",   re.compile(r'para\s+quando\s+quer|quer\s+agendar\s+para\s+quando|qual\s+(a|o)\s+melhor\s+(dia|data|hor[aá]rio)|podemos\s+agendar\s+para|tenho\s+\w+\s+dia\s+\d|posso\s+verificar\s+os\s+hor[aá]rios', re.I)),
            ("falha_agendamento",re.compile(r'falha\s+no\s+agendamento|erro\s+quark|⚠', re.I)),
            ("arquivo_audio",    re.compile(r'n[aã]o\s+consigo\s+abrir\s+arquivos|n[aã]o\s+consigo\s+ouvir|recebendo\s+seus\s+[aá]udios|listar\s+(os\s+)?exames\s+por\s+texto|listar\s+o\s+que\s+precisa\s+por\s+texto', re.I)),
            ("encerramento",     re.compile(r'de\s+nada.{0,30}(qualquer\s+coisa|estou\s+por\s+aqui|[eé]\s+s[oó]\s+chamar|pode\s+perguntar)|sem\s+problema.{0,30}(qualquer|quando\s+precisar|[eé]\s+s[oó])', re.I)),
        ]
        def _classify_stage(text):
            if not text:
                return "midia_sem_texto"
            for name, pat in _STAGE_PATS:
                if pat.search(text):
                    return name
            return "outros"

        try:
            # Quark cross-ref: quando cpf_map disponível, subtrai quem agendou via Claudete
            jids_agendados_quark = set()
            if cpf_map and agend:
                agend_pids_cl = {a.get("pacienteId") for a in agend if a.get("usuarioCadastroId") == USUARIO_API_ID}
                cpfs_agendados = {cpf for cpf, pid in cpf_map.items() if pid in agend_pids_cl}
                jids_agendados_quark = {
                    jid for jid, info in cpf_ts_por_jid.items()
                    if re.sub(r"[^\d]", "", info.get("cpf") or "") in cpfs_agendados
                }

            # Carregar mapa Chatwoot para links diretos nas conversas
            try:
                with _proj_db_conn() as _cw_conn:
                    _cw_rows = _cw_conn.execute("SELECT jid, conversation_id FROM chatwoot_map").fetchall()
                    _chatwoot_map = {r["jid"]: r["conversation_id"] for r in _cw_rows}
            except Exception:
                _chatwoot_map = {}

            ts_48h_ago_b = int(time.time()) - 48 * 3600
            _ALL_STAGES = ["handoff", "apos_preco", "sem_agenda", "sem_plano", "confirmacao", "oferta_horario", "falha_agendamento", "arquivo_audio", "encerramento", "midia_sem_texto", "outros"]
            contagens_b = {s: 0 for s in _ALL_STAGES}
            stage_tempos_b = {s: [] for s in _ALL_STAGES}
            contatos_b = {s: [] for s in _ALL_STAGES}
            pipeline_quente_b = 0
            agendados_detectados = 0
            # Quem falou por último: separa abandono real (bot falou por último)
            # de fila humana parada (handoff já disparado antes da última msg do bot)
            # e bot travado sem handoff (ninguém respondeu, nem bot nem humano)
            quem_falou_ultimo = {"abandono_real": 0, "fila_humana_parada": 0, "bot_travado_sem_handoff": 0}

            for jid, row in raw_pos_cpf.items():
                if row.get("booking_confirmed") or jid in jids_agendados_quark:
                    agendados_detectados += 1
                    continue
                # Lembrete de agendamento existente = paciente já tem consulta marcada
                if _PAT_LEMBRETE.search(row["bot_text"]):
                    agendados_detectados += 1
                    continue
                stage = _classify_stage(row["bot_text"])
                contagens_b[stage] += 1
                # delta só quando bot falou por último (padrão de abandono)
                if row["bot_ts"] and row["user_ts"] and row["bot_ts"] > row["user_ts"]:
                    delta_min = (row["bot_ts"] - row["user_ts"]) / 60
                    stage_tempos_b[stage].append(delta_min)
                quente = bool(row.get("user_ts") and row["user_ts"] >= ts_48h_ago_b)
                if quente:
                    pipeline_quente_b += 1

                if row.get("user_ts") and row.get("bot_ts") and row["user_ts"] > row["bot_ts"]:
                    handoffs_jid = handoff_ts_por_jid.get(jid, [])
                    primeiro_handoff = min(handoffs_jid) if handoffs_jid else None
                    if primeiro_handoff and primeiro_handoff <= row["bot_ts"]:
                        quem_falou_ultimo["fila_humana_parada"] += 1
                    else:
                        quem_falou_ultimo["bot_travado_sem_handoff"] += 1
                else:
                    quem_falou_ultimo["abandono_real"] += 1

                fone = jid.split("@")[0]
                cpf_info = cpf_ts_por_jid.get(jid, {})
                contatos_b[stage].append({
                    "fone": fone,
                    "cpf": re.sub(r"[^\d]", "", cpf_info.get("cpf") or ""),
                    "demanda": (row["bot_text"] or "")[:100],
                    "bot_ts": row.get("bot_ts"),
                    "user_ts": row.get("user_ts"),
                    "quente": quente,
                    "chatwoot_id": _chatwoot_map.get(jid),
                })

            # ordena por mais recente primeiro, limita a 100 por stage
            for s in _ALL_STAGES:
                contatos_b[s].sort(key=lambda x: x.get("user_ts") or x.get("bot_ts") or 0, reverse=True)
                contatos_b[s] = contatos_b[s][:100]

            atrito_pos_cpf = {
                "total": sum(contagens_b.values()),
                **contagens_b,
                "agendados_detectados": agendados_detectados,
                "tempo_medio_min": {
                    s: round(sum(v) / len(v), 1)
                    for s, v in stage_tempos_b.items() if v
                },
                "pipeline_quente_48h": pipeline_quente_b,
                "quem_falou_por_ultimo": quem_falou_ultimo,
                "lista_contatos": contatos_b,
            }
        except Exception as e:
            print(f"[api_funil] erro no atrito_pos_cpf: {e}", flush=True)
            atrito_pos_cpf = {"total": 0}

        # --- PROTOTIPO: recorte "handoff pos-intencao" (continuacao) ---
        try:
            handoffs_pos_intencao = {
                jid: {"cpf": re.sub(r"[^\d]", "", info["cpf"] or ""),
                      "handoff_ts": min(t for t in handoff_ts_por_jid.get(jid, []) if t >= info["ts"])}
                for jid, info in cpf_ts_por_jid.items()
                if any(t >= info["ts"] for t in handoff_ts_por_jid.get(jid, []))
            }
            n_pos_intencao = len(handoffs_pos_intencao)
            pacientes_pos_intencao_ids = {
                cpf_map[info["cpf"]] for info in handoffs_pos_intencao.values()
                if info["cpf"] in cpf_map
            }
            agend_pos_intencao = [a for a in agend if a.get("pacienteId") in pacientes_pos_intencao_ids]
            pacientes_com_agendamento = {a.get("pacienteId") for a in agend_pos_intencao}
            realizados_pos_intencao = [
                a for a in agend_pos_intencao if a.get("statusMarcacao") in STATUS_CONSULTA_REALIZADA
            ]
            pacientes_com_atendimento = {a.get("pacienteId") for a in realizados_pos_intencao}
            vendas_validas_periodo = [
                v for v in vendas_periodo if v.get("ativo") and not _pessoa_institucional(v.get("pessoa"))
            ]
            receita_pos_intencao = round(sum(
                v.get("valorTotal") or 0 for v in vendas_validas_periodo
                if ((v.get("pessoa") or {}).get("paciente") or {}).get("id") in pacientes_pos_intencao_ids
            ), 2)
            conversao_pos_intencao = {
                "descricao": "Conversas com CPF ja coletado (intencao de compra evidente) ANTES do "
                              "#mariana - mede a conversao da intervencao humana sobre leads ja "
                              "qualificados pela Claudete, nao a captacao do lead em si.",
                "handoffs_pos_intencao": n_pos_intencao,
                "pacientes_identificados_no_quark": len(pacientes_pos_intencao_ids),
                "converteram_agendamento": len(pacientes_com_agendamento),
                "converteram_atendimento_realizado": len(pacientes_com_atendimento),
                "conversao_agendamento_pct": round(len(pacientes_com_agendamento) / n_pos_intencao * 100, 1)
                    if n_pos_intencao else None,
                "conversao_atendimento_pct": round(len(pacientes_com_atendimento) / len(pacientes_com_agendamento) * 100, 1)
                    if pacientes_com_agendamento else None,
                "receita_capturada": receita_pos_intencao,
            }
        except Exception as e:
            print(f"[api_funil] erro no recorte pos_intencao: {e}", flush=True)
            conversao_pos_intencao = {"erro": str(e)}

        _result = {
            "status": "ok", "dias": dias, "total_conversas": total_conversas,
            "conversas_3449": conversas_3449, "conversas_2042": conversas_2042,
            "cpfs_coletados": cpfs, "agendamentos_claudete": agend_cl,
            "handoffs": handoffs, "conversao_cpf_agend_pct": conv_pct,
            "conversas_por_semana": semanas,
            "atrito_cpf": atrito_cpf,
            "atrito_pos_cpf": atrito_pos_cpf,
            "conversao_pos_intencao": conversao_pos_intencao,
            "realizados_claudete": {
                "influenciados": len(influenciados),
                "autonomos": len(autonomos_realizados),
                "cpf_map_size": len(cpf_map),
                "wa_cpfs_encontrados": len(wa_cpfs_norm),
                "wa_paciente_ids": len(wa_paciente_ids),
            },
            "tendencias": {
                "conversas": wa_delta(total_conversas, conv_pre),
                "cpfs": wa_delta(cpfs, cpfs_pre),
                "handoffs": wa_delta(handoffs, hand_pre, invert=True),
            },
            "saude_claudete": {
                "tempo_resposta_mediano_seg": tempo_resp_mediano,
                "taxa_identificacao_cpf_pct": round(cpfs / total_conversas * 100, 1) if total_conversas else None,
                "taxa_retorno_pct": taxa_retorno,
            },
        }
        _api_cache_set(_ck, _result)
        return jsonify(_result)
    except Exception as e:
        print(f"[api_funil] erro: {e}", flush=True)
        return jsonify({"status": "erro", "mensagem": str(e)}), 500


@app.route("/api/especialidades")
def api_especialidades():
    dias = min(max(int(flask_request.args.get("dias", 30)), 1), 730)
    _ck = f"especialidades_{dias}"
    _hit = _api_cached(_ck)
    if _hit:
        return jsonify(_hit)
    ts_inicio = int(time.time()) - dias * 86400
    try:
        conn = pg_conn()
        cur = conn.cursor()
        EXCL2 = '%@g.us'
        demanda_wa = {}
        for esp, keywords in ESPECIALIDADES_KEYWORDS.items():
            kw_params = [f"%{kw}%" for kw in keywords]
            conds = " OR ".join([
                "LOWER(COALESCE(m.message->>'conversation',"
                "m.message->'extendedTextMessage'->>'text','')) LIKE %s"
                for _ in keywords
            ])
            cur.execute(
                'SELECT COUNT(DISTINCT m.key->>\'remoteJid\') FROM "Message" m '
                'JOIN "Instance" i ON m."instanceId" = i.id '
                f"WHERE i.\"name\" IN {_EVO_IN_SQL} "
                "AND m.key->>'remoteJid' NOT LIKE %s "
                "AND m.\"messageTimestamp\" >= %s "
                "AND (m.key->>'fromMe')::boolean = false "
                "AND (" + conds + ")",
                (EXCL2, ts_inicio) + tuple(kw_params)
            )
            demanda_wa[esp] = cur.fetchone()[0] or 0
        conn.close()
        agend, orc, _, vendas = get_all_quark_rows(dias)
        agendado  = {}
        realizado = {}
        pac_por_esp    = {}  # esp -> set de pacienteId realizados
        marcacao_por_esp = {}  # esp -> set de agendamento.id realizados
        for a in agend:
            esp = _esp_nome(a)
            if esp not in ESPECIALIDADES_KEYWORDS:
                continue
            pac = a.get("pacienteId")
            mid = a.get("id")  # id do agendamento = marcacaoId nas vendas
            is_realiz = a.get("statusMarcacao") in STATUS_CONSULTA_REALIZADA
            agendado[esp] = agendado.get(esp, 0) + 1
            if is_realiz:
                realizado[esp] = realizado.get(esp, 0) + 1
                if pac:
                    pac_por_esp.setdefault(esp, set()).add(pac)
                if mid:
                    marcacao_por_esp.setdefault(esp, set()).add(mid)

        # Receita por especialidade:
        # - consultas: vendas com marcacaoId ligado ao agendamento realizado (link direto)
        # - exames: orçamentos EXECUTADA do mesmo paciente no período (venda.pessoa.paciente.id = pacienteId)
        vendas_validas = [v for v in vendas if v.get("ativo") and not _pessoa_institucional(v.get("pessoa"))]
        orc_exec = [o for o in orc if o.get("statusOrcamento") == "EXECUTADA"]

        receita_consultas = {}
        receita_exames    = {}
        for esp in set(pac_por_esp) | set(marcacao_por_esp):
            marcacoes = marcacao_por_esp.get(esp, set())
            pacs      = pac_por_esp.get(esp, set())
            receita_consultas[esp] = round(
                sum((v.get("valorTotal") or 0) for v in vendas_validas
                    if v.get("marcacaoId") in marcacoes), 2)
            receita_exames[esp] = round(
                sum((o.get("valorTotal") or 0) for o in orc_exec
                    if o.get("pacienteId") in pacs), 2)

        # Ticket medio global (mesma janela) - fallback pra especialidades sem
        # realizado suficiente pra ter ticket proprio (mesma logica do frontend).
        ticket_medio_global = (
            round(sum((v.get("valorTotal") or 0) for v in vendas_validas) / len(vendas_validas), 2)
            if vendas_validas else None
        )

        def _receita_perdida(gap, receita_esp, realizado_esp):
            if gap <= 0:
                return 0
            ticket_esp = (receita_esp / realizado_esp) if realizado_esp > 0 else None
            ticket = ticket_esp if ticket_esp is not None else ticket_medio_global
            return round(gap * ticket, 2) if ticket is not None else 0

        resultado = sorted([
            {
                "especialidade": esp,
                "demanda_wa": demanda_wa.get(esp, 0),
                "agendado_quark": agendado.get(esp, 0),
                "realizado_quark": realizado.get(esp, 0),
                "receita_consultas": receita_consultas.get(esp, 0),
                "receita_exames": receita_exames.get(esp, 0),
                "gap": max(demanda_wa.get(esp, 0) - agendado.get(esp, 0), 0),
                "receita_perdida": _receita_perdida(
                    max(demanda_wa.get(esp, 0) - agendado.get(esp, 0), 0),
                    receita_consultas.get(esp, 0) + receita_exames.get(esp, 0),
                    realizado.get(esp, 0),
                ),
                "status": ESPECIALIDADE_STATUS.get(esp, "sem_oferta"),
            }
            for esp in ESPECIALIDADES_KEYWORDS
        # Prioriza por $$ em risco, nao por volume de conversas (pedido do Patrick,
        # 18/09/2026): uma especialidade com gap pequeno mas ticket alto deve
        # aparecer antes de outra com gap grande mas ticket baixo.
        ], key=lambda x: -x["receita_perdida"])
        _result = {"status": "ok", "dias": dias, "especialidades": resultado}
        _api_cache_set(_ck, _result)
        return jsonify(_result)
    except Exception as e:
        print(f"[api_especialidades] erro: {e}", flush=True)
        return jsonify({"status": "erro", "mensagem": str(e)}), 500


@app.route("/api/comparativo")
def api_comparativo():
    """Retorna série semanal de 3 indicadores para atual e período anterior — para gráficos."""
    dias = min(max(int(flask_request.args.get("dias", 30)), 1), 730)
    _ck = f"comparativo_{dias}"
    _hit = _api_cached(_ck)
    if _hit:
        return jsonify(_hit)
    with _cache_lock:
        meses_ordem = list(_cache["meses_ordem"])
    if not meses_ordem:
        return jsonify({"status": "carregando"})
    agend_all, orc_all, contas_all, vendas_all = _all_raw_rows()
    hoje = today_brt()
    n_sem = max(dias // 7, 1)

    def semanas_range(offset_dias):
        """Gera n_sem semanas terminando em hoje - offset_dias dias."""
        fim_base = hoje - datetime.timedelta(days=offset_dias)
        result = []
        for w in range(n_sem - 1, -1, -1):
            fim_s = fim_base - datetime.timedelta(weeks=w)
            ini_s = fim_s - datetime.timedelta(days=6)
            result.append((ini_s, fim_s))
        return result

    def agg_semana(ini_s, fim_s, rows_agend, rows_vendas):
        ag = filter_by_period(rows_agend, "dataAgendamento", ini_s, fim_s)
        vd = filter_by_period(rows_vendas, "dataConta", ini_s, fim_s)
        return {
            "agendamentos": len(ag),
            "realizados": sum(1 for r in ag if r.get("statusMarcacao") in STATUS_CONSULTA_REALIZADA),
            "via_bot": sum(1 for r in ag if r.get("usuarioCadastroId") == USUARIO_API_ID),
            "receita": round(sum((v.get("valorTotal") or 0) for v in vd
                                 if v.get("ativo") and not _pessoa_institucional(v.get("pessoa"))), 2),
        }

    semanas_cur = semanas_range(0)
    semanas_pre = semanas_range(dias)
    series_cur, series_pre = [], []
    for ini_s, fim_s in semanas_cur:
        d = agg_semana(ini_s, fim_s, agend_all, vendas_all)
        d["label"] = ini_s.strftime("%d/%m")
        series_cur.append(d)
    for ini_s, fim_s in semanas_pre:
        d = agg_semana(ini_s, fim_s, agend_all, vendas_all)
        d["label"] = ini_s.strftime("%d/%m")
        series_pre.append(d)

    _result = {
        "status": "ok", "dias": dias,
        "atual": series_cur,
        "anterior": series_pre,
    }
    _api_cache_set(_ck, _result)
    return jsonify(_result)


@app.route("/api/historico_mensal")
def api_historico_mensal():
    """Retorna indicadores agregados mês a mês usando o cache existente."""
    with _cache_lock:
        raw = dict(_cache["raw"])
        meses_ordem = list(_cache["meses_ordem"])
    if not meses_ordem:
        return jsonify({"status": "carregando"})
    hoje = today_brt()

    # Contatos WA por mes, separados por numero (3449=Claudete-recep, legado /
    # 2042=claudete2, novo canal do site) — acompanhamento da migracao de numero
    wa_por_canal = {}
    try:
        conn = pg_conn()
        cur = conn.cursor()
        cur.execute(
            "SELECT"
            "  EXTRACT(YEAR  FROM to_timestamp(msg.\"messageTimestamp\") AT TIME ZONE 'America/Sao_Paulo')::int AS ano,"
            "  EXTRACT(MONTH FROM to_timestamp(msg.\"messageTimestamp\") AT TIME ZONE 'America/Sao_Paulo')::int AS mes_n,"
            "  i.name AS instancia,"
            "  COUNT(DISTINCT msg.key->>'remoteJid') AS contatos"
            " FROM \"Message\" msg"
            " JOIN \"Instance\" i ON msg.\"instanceId\" = i.id"
            f" WHERE i.name IN {_EVO_IN_SQL}"
            "   AND (msg.key->>'fromMe')::boolean = false"
            "   AND msg.key->>'remoteJid' NOT LIKE '%%@g.us'"
            " GROUP BY ano, mes_n, i.name"
        )
        for row in cur.fetchall():
            key = f"{int(row[0])}-{int(row[1]):02d}"
            wa_por_canal.setdefault(key, {})[row[2]] = row[3]
        conn.close()
    except Exception:
        pass

    result = []
    meses_completos_data = {}
    for key in meses_ordem:
        y, m = int(key.split("-")[0]), int(key.split("-")[1])
        is_parcial = (y == hoje.year and m == hoje.month)
        d = raw.get(key, {})
        agend = d.get("agendamentos", [])
        orc = d.get("orcamentos", [])
        contas = d.get("contas", [])
        vendas = d.get("vendas", [])
        ind = compute_indicators(agend, orc, contas, vendas)
        if not is_parcial:
            meses_completos_data[key] = {
                "realizados":   ind["consultas_realizadas"]["total"],
                "agendamentos": ind["total_agendamentos"],
                "receita":      ind["financeiro"]["receita_total"],
            }
        result.append({
            "mes": MESES_PT[m][:3],
            "mes_ano": f"{MESES_PT[m][:3]}/{str(y)[2:]}",
            "agendamentos": ind["total_agendamentos"],
            "realizados": ind["consultas_realizadas"]["total"],
            "conversao_pct": ind["conversao_pct"],
            "cancelamentos": ind["cancelamentos"]["total"],
            "faltou": ind["faltou"]["total"],
            "receita": ind["financeiro"]["receita_total"],
            "sem_desfecho_real": ind["sem_desfecho"]["sem_desfecho_real"],
            "wa_3449": wa_por_canal.get(key, {}).get(EVOLUTION_INSTANCES[0] if EVOLUTION_INSTANCES else "Claudete-recep", 0),
            "wa_2042": wa_por_canal.get(key, {}).get(EVOLUTION_INSTANCES[1] if len(EVOLUTION_INSTANCES) > 1 else "claudete2", 0),
            "parcial": is_parcial,
        })
    _maybe_evaluate_past_projections(meses_completos_data)
    return jsonify({"status": "ok", "meses": result})


@app.route("/api/heatmap_wa")
def api_heatmap_wa():
    """Retorna conversas únicas por dia-da-semana × hora (BRT), últimos 30 dias."""
    try:
        conn = pg_conn()
        cur = conn.cursor()
        cur.execute(f"""
            SELECT
                EXTRACT(DOW FROM to_timestamp(m."messageTimestamp" - 3*3600))::int AS dow,
                EXTRACT(HOUR FROM to_timestamp(m."messageTimestamp" - 3*3600))::int AS hora,
                COUNT(DISTINCT m.key->>'remoteJid') AS conversas
            FROM "Message" m JOIN "Instance" i ON m."instanceId" = i.id
            WHERE i.name IN {_EVO_IN_SQL}
                AND m.key->>'remoteJid' NOT LIKE '%@g.us'
                AND (m.key->>'fromMe')::boolean = false
                AND m."messageTimestamp" >= extract(epoch from now() - interval '30 days')
            GROUP BY 1, 2
            ORDER BY 1, 2
        """)
        cells = [{"dow": r[0], "hora": r[1], "conversas": r[2]} for r in cur.fetchall()]
        conn.close()
        return jsonify({"status": "ok", "cells": cells})
    except Exception as e:
        print(f"[api_heatmap_wa] erro: {e}", flush=True)
        return jsonify({"status": "erro", "mensagem": str(e)}), 500


@app.route("/api/projecao_mensal")
def api_projecao_mensal():
    """Projeta o mês atual com base no ritmo dos dias úteis já decorridos."""
    with _cache_lock:
        raw = dict(_cache["raw"])
        meses_ordem = list(_cache["meses_ordem"])
    if not meses_ordem:
        return jsonify({"status": "carregando"})
    hoje = today_brt()
    # Meses completos para média histórica (excluir mês atual)
    meses_completos = [k for k in meses_ordem
                       if not (int(k.split("-")[0]) == hoje.year and int(k.split("-")[1]) == hoje.month)]
    # Mês atual
    mes_key = f"{hoje.year}-{hoje.month:02d}"
    if mes_key not in raw:
        return jsonify({"status": "erro", "mensagem": "mês atual não no cache"})
    d_atual = raw[mes_key]
    ind_atual = compute_indicators(d_atual.get("agendamentos", []),
                                   d_atual.get("orcamentos", []),
                                   d_atual.get("contas", []),
                                   d_atual.get("vendas", []))
    # Dias úteis
    dias_passados = hoje.day
    dias_uteis_passados = sum(1 for dd in range(1, hoje.day + 1)
                              if datetime.date(hoje.year, hoje.month, dd).weekday() < 5)
    dias_uteis_mes = dias_uteis_no_mes(hoje.year, hoje.month)
    total_dias_mes = calendar.monthrange(hoje.year, hoje.month)[1]
    # Médias históricas (últimos 3 meses completos)
    def media_h(getter):
        vals = [getter(compute_indicators(raw[k].get("agendamentos", []),
                                          raw[k].get("orcamentos", []),
                                          raw[k].get("contas", []),
                                          raw[k].get("vendas", [])))
                for k in meses_completos[-3:] if k in raw]
        vals = [v for v in vals if v is not None]
        return round(sum(vals) / len(vals), 1) if vals else None
    media_agend   = media_h(lambda i: i["total_agendamentos"])
    media_realiz  = media_h(lambda i: i["consultas_realizadas"]["total"])
    media_receita = media_h(lambda i: i["financeiro"]["receita_total"])
    media_conv    = media_h(lambda i: i["conversao_pct"])
    # Melhor mês histórico
    melhor_agend_n = max((len(raw[k].get("agendamentos", [])) for k in meses_completos if k in raw), default=0)
    # Projeção por ritmo diário útil
    def proj(v):
        if v is None or dias_uteis_passados == 0:
            return None
        return round(v / dias_uteis_passados * dias_uteis_mes, 2)

    proj_realiz = proj(ind_atual["consultas_realizadas"]["total"])
    proj_agend  = proj(ind_atual["total_agendamentos"])
    proj_rev    = proj(ind_atual["financeiro"]["receita_total"])
    ritmo_r     = round(ind_atual["consultas_realizadas"]["total"] / dias_uteis_passados, 2) if dias_uteis_passados else None

    _maybe_snapshot_projection(
        mes_alvo=mes_key,
        dias_uteis_passados=dias_uteis_passados,
        dias_uteis_mes=dias_uteis_mes,
        proj_realizados=proj_realiz,
        proj_agendamentos=proj_agend,
        proj_receita=proj_rev,
        ritmo_realizados=ritmo_r,
        media_hist_realizados=media_realiz,
    )

    return jsonify({
        "status": "ok",
        "mes_atual": f"{MESES_PT[hoje.month]}/{hoje.year}",
        "dias_passados": dias_passados,
        "dias_restantes": total_dias_mes - dias_passados,
        "dias_uteis_passados": dias_uteis_passados,
        "dias_uteis_mes": dias_uteis_mes,
        "atual": {
            "agendamentos": ind_atual["total_agendamentos"],
            "realizados": ind_atual["consultas_realizadas"]["total"],
            "receita": ind_atual["financeiro"]["receita_total"],
            "conversao_pct": ind_atual["conversao_pct"],
            "agendamentos_por_agendador": dict(ind_atual["agendamentos_por_agendador"]),
        },
        "projecao": {
            "agendamentos": proj_agend,
            "realizados": proj_realiz,
            "receita": proj_rev,
        },
        "media_historica": {
            "agendamentos": media_agend,
            "realizados": media_realiz,
            "receita": media_receita,
            "conversao_pct": media_conv,
        },
        "melhor_mes_agendamentos": melhor_agend_n,
    })


@app.route("/api/semana_detalhe")
def api_semana_detalhe():
    """
    Composição detalhada de uma semana ISO.
    Parâmetro: semana=YYYY-WW  (ex: 2026-W37)
    Retorna funil, especialidades, distribuição por dia e médias das demais semanas.
    """
    semana_str = flask_request.args.get("semana", "")
    try:
        # Parsear YYYY-WW -> segunda e domingo da semana
        year_s, week_s = semana_str.split("-W")
        year_n, week_n = int(year_s), int(week_s)
        seg = datetime.date.fromisocalendar(year_n, week_n, 1)   # segunda
        dom = datetime.date.fromisocalendar(year_n, week_n, 7)   # domingo
    except Exception:
        return jsonify({"status": "erro", "mensagem": "semana inválida, use YYYY-WW"}), 400

    DIAS_PT = ["Seg", "Ter", "Qua", "Qui", "Sex", "Sab", "Dom"]
    EXCL = '%@g.us'
    CPF_PAT = r'\d{3}[\. ]?\d{3}[\. ]?\d{3}[-\. ]?\d{2}'
    ts_ini = int(datetime.datetime(seg.year, seg.month, seg.day, tzinfo=BRT).timestamp())
    ts_fim = int(datetime.datetime(dom.year, dom.month, dom.day, 23, 59, 59, tzinfo=BRT).timestamp())

    # ── Dados Quark (do cache) ──
    agend_all, orc_all, contas_all, vendas_all = _all_raw_rows()
    agend_sem  = filter_by_period(agend_all,  "dataAgendamento", seg, dom)
    orc_sem    = filter_by_period(orc_all,    "dataOrcamento",   seg, dom)
    contas_sem = filter_by_period(contas_all, "dataBaixa",       seg, dom)
    vendas_sem = filter_by_period(vendas_all, "dataConta",       seg, dom)
    ind = compute_indicators(agend_sem, orc_sem, contas_sem, vendas_sem)

    # Agendamentos por dia da semana (0=seg … 6=dom no isoweekday-1)
    por_dia_agend = [0] * 7
    por_dia_realiz = [0] * 7
    for a in agend_sem:
        d = parse_quark_date(a.get("dataAgendamento", ""))
        if d:
            idx = d.isoweekday() - 1   # 0=seg, 6=dom
            por_dia_agend[idx] += 1
            if a.get("statusMarcacao") in STATUS_CONSULTA_REALIZADA:
                por_dia_realiz[idx] += 1

    # Agendamentos por agendador
    agend_por_ag = Counter(agendador_de(a.get("usuarioCadastroId")) for a in agend_sem)

    # Especialidades Quark nessa semana
    esp_agendado = {}
    for a in agend_sem:
        esp = _esp_nome(a)
        if esp in ESPECIALIDADES_KEYWORDS:
            esp_agendado[esp] = esp_agendado.get(esp, 0) + 1

    # ── Dados WA (Evolution DB) ──
    try:
        conn = pg_conn()
        cur = conn.cursor()

        # Total conversas na semana
        cur.execute(
            "SELECT COUNT(DISTINCT m.key->>'remoteJid') FROM \"Message\" m "
            "JOIN \"Instance\" i ON m.\"instanceId\" = i.id "
            f"WHERE i.name IN {_EVO_IN_SQL} "
            "AND m.key->>'remoteJid' NOT LIKE %s "
            "AND m.\"messageTimestamp\" BETWEEN %s AND %s "
            "AND (m.key->>'fromMe')::boolean = false",
            (EXCL, ts_ini, ts_fim))
        conversas_sem = cur.fetchone()[0] or 0

        # CPFs identificados
        cur.execute(
            "SELECT COUNT(DISTINCT m.key->>'remoteJid') FROM \"Message\" m "
            "JOIN \"Instance\" i ON m.\"instanceId\" = i.id "
            f"WHERE i.name IN {_EVO_IN_SQL} "
            "AND m.key->>'remoteJid' NOT LIKE %s "
            "AND m.\"messageTimestamp\" BETWEEN %s AND %s "
            "AND (m.key->>'fromMe')::boolean = false "
            "AND COALESCE(m.message->>'conversation',"
            "m.message->'extendedTextMessage'->>'text','') ~ %s",
            (EXCL, ts_ini, ts_fim, CPF_PAT))
        cpfs_sem = cur.fetchone()[0] or 0

        # Conversas por dia da semana (para WA)
        cur.execute(
            "SELECT EXTRACT(DOW FROM to_timestamp(m.\"messageTimestamp\" - 3*3600))::int AS dow,"
            " COUNT(DISTINCT m.key->>'remoteJid') "
            "FROM \"Message\" m JOIN \"Instance\" i ON m.\"instanceId\" = i.id "
            f"WHERE i.name IN {_EVO_IN_SQL} "
            "AND m.key->>'remoteJid' NOT LIKE %s "
            "AND m.\"messageTimestamp\" BETWEEN %s AND %s "
            "AND (m.key->>'fromMe')::boolean = false "
            "GROUP BY 1",
            (EXCL, ts_ini, ts_fim))
        wa_por_dow_raw = {r[0]: r[1] for r in cur.fetchall()}
        # DOW 0=Dom, 1=Seg … 6=Sab → converter para 0=Seg … 6=Dom
        wa_por_dia = [wa_por_dow_raw.get((i + 1) % 7, 0) for i in range(7)]

        # Especialidades WA nessa semana
        esp_demanda_wa = {}
        for esp, keywords in ESPECIALIDADES_KEYWORDS.items():
            kw_params = [f"%{kw}%" for kw in keywords]
            conds = " OR ".join(
                "LOWER(COALESCE(m.message->>'conversation',"
                "m.message->'extendedTextMessage'->>'text','')) LIKE %s"
                for _ in keywords)
            cur.execute(
                "SELECT COUNT(DISTINCT m.key->>'remoteJid') FROM \"Message\" m "
                "JOIN \"Instance\" i ON m.\"instanceId\" = i.id "
                f"WHERE i.name IN {_EVO_IN_SQL} "
                "AND m.key->>'remoteJid' NOT LIKE %s "
                "AND m.\"messageTimestamp\" BETWEEN %s AND %s "
                "AND (m.key->>'fromMe')::boolean = false "
                "AND (" + conds + ")",
                (EXCL, ts_ini, ts_fim) + tuple(kw_params))
            v = cur.fetchone()[0] or 0
            if v > 0:
                esp_demanda_wa[esp] = v

        conn.close()
    except Exception as e:
        print(f"[api_semana_detalhe] WA erro: {e}", flush=True)
        conversas_sem = cpfs_sem = 0
        wa_por_dia = [0] * 7
        esp_demanda_wa = {}

    # ── Médias das demais semanas (últimas 12 semanas excluindo a atual) ──
    hoje = today_brt()
    medias = {"conversas": None, "agendamentos": None, "realizados": None, "conversao_pct": None}
    try:
        outras_agend = []
        outras_real = []
        for w_off in range(1, 13):
            ref = hoje - datetime.timedelta(weeks=w_off)
            iso = ref.isocalendar()
            if iso[0] == year_n and iso[1] == week_n:
                continue
            s_ini = datetime.date.fromisocalendar(iso[0], iso[1], 1)
            s_fim = datetime.date.fromisocalendar(iso[0], iso[1], 7)
            ags = filter_by_period(agend_all, "dataAgendamento", s_ini, s_fim)
            outras_agend.append(len(ags))
            outras_real.append(sum(1 for a in ags if a.get("statusMarcacao") in STATUS_CONSULTA_REALIZADA))
        if outras_agend:
            avg_ag = round(sum(outras_agend) / len(outras_agend), 1)
            avg_re = round(sum(outras_real) / len(outras_real), 1)
            medias["agendamentos"] = avg_ag
            medias["realizados"] = avg_re
            medias["conversao_pct"] = round(avg_re / avg_ag * 100, 1) if avg_ag else None
    except Exception as e:
        print(f"[api_semana_detalhe] médias erro: {e}", flush=True)

    # ── Montar especialidades unificadas ──
    todos_esp = sorted(
        set(esp_demanda_wa) | set(esp_agendado),
        key=lambda e: -(esp_demanda_wa.get(e, 0) + esp_agendado.get(e, 0))
    )
    especialidades = [
        {
            "especialidade": esp,
            "demanda_wa": esp_demanda_wa.get(esp, 0),
            "agendado_quark": esp_agendado.get(esp, 0),
            "gap": max(esp_demanda_wa.get(esp, 0) - esp_agendado.get(esp, 0), 0),
            "status": ESPECIALIDADE_STATUS.get(esp, "sem_oferta"),
        }
        for esp in todos_esp
    ]

    por_dia = [
        {
            "dia": DIAS_PT[i],
            "agendamentos": por_dia_agend[i],
            "realizados": por_dia_realiz[i],
            "conversas_wa": wa_por_dia[i],
        }
        for i in range(7)
    ]

    return jsonify({
        "status": "ok",
        "semana": semana_str,
        "periodo": {"inicio": seg.strftime("%d/%m"), "fim": dom.strftime("%d/%m")},
        "funil": {
            "conversas": conversas_sem,
            "cpfs_identificados": cpfs_sem,
            "agendamentos": ind["total_agendamentos"],
            "realizados": ind["consultas_realizadas"]["total"],
            "cancelamentos": ind["cancelamentos"]["total"],
            "faltou": ind["faltou"]["total"],
            "conversao_pct": ind["conversao_pct"],
            "sem_desfecho": ind["sem_desfecho"],
            "agendamentos_bot": sum(1 for a in agend_sem if a.get("meioAgendamento") == "ON_LINE"),
        },
        "especialidades": especialidades,
        "por_dia": por_dia,
        "agendador": dict(agend_por_ag),
        "medias_semanas_anteriores": medias,
    })



@app.route("/api/auditoria_financeira")
def api_auditoria_financeira():
    dias = int(flask_request.args.get("dias", 30))
    try:
        _, _, contas, vendas = get_all_quark_rows(dias)
        if not vendas:
            return jsonify({"status": "carregando"})

        venda_usuario = {v["id"]: v.get("usuarioCadastro") for v in vendas if v.get("ativo")}

        def _forma(descricao):
            d = (descricao or "").strip()
            dl = d.lower()
            if "fechamento do caixa" in dl or "sangria do caixa" in dl or "suprimento do caixa" in dl:
                return "Fechamento de Caixa", True
            partes = d.split(" - ")
            tipo = partes[0].strip()
            if tipo == "Transferência Bancária":
                if len(partes) > 1 and "PIX" in partes[1].upper():
                    return "PIX", False
                return "Transferência Bancária", False
            return tipo or "Outros", False

        from collections import defaultdict
        user_data = defaultdict(lambda: {
            "vids": set(),
            "formas": defaultdict(float),
            "interno": defaultdict(float),
        })

        for c in contas:
            if not c.get("ativo"):
                continue
            vid = c.get("vendaId")
            uid = venda_usuario.get(vid)
            if uid is None:
                continue
            forma, is_interno = _forma(c.get("descricao"))
            valor = float(c.get("valorRecebido") or 0)
            ud = user_data[uid]
            ud["vids"].add(vid)
            if is_interno:
                ud["interno"][forma] += valor
            else:
                ud["formas"][forma] += valor

        resultado = []
        for uid, ud in user_data.items():
            total_liq = sum(ud["formas"].values())
            total_int = sum(ud["interno"].values())
            formas = []
            for forma, valor in sorted(ud["formas"].items(), key=lambda x: -x[1]):
                pct = round(valor / total_liq * 100, 1) if total_liq > 0 else 0
                formas.append({"forma": forma, "valor": round(valor, 2), "pct": pct, "interno": False})
            for forma, valor in sorted(ud["interno"].items(), key=lambda x: -x[1]):
                formas.append({"forma": forma, "valor": round(valor, 2), "pct": None, "interno": True})
            resultado.append({
                "uid": uid,
                "nome": USUARIO_MAP.get(uid, f"Usuário {uid}"),
                "n_vendas": len(ud["vids"]),
                "total_liquido": round(total_liq, 2),
                "total_interno": round(total_int, 2),
                "formas": formas,
            })

        resultado.sort(key=lambda x: -x["total_liquido"])
        return jsonify({"status": "ok", "dias": dias, "usuarios": resultado})
    except Exception as e:
        return jsonify({"status": "erro", "mensagem": str(e)}), 500


@app.route("/api/vendas_team")
def api_vendas_team():
    dias = int(flask_request.args.get("dias", 30))
    try:
        agend, _, _, vendas = get_all_quark_rows(dias)
        if not vendas and not agend:
            return jsonify({"status": "carregando"})

        team_uids = {m["uid"] for m in TEAM_MEMBERS}

        # Agendamentos por uid
        agend_por = defaultdict(list)
        for a in agend:
            uid = a.get("usuarioCadastroId")
            if uid in team_uids:
                agend_por[uid].append(a)

        # Vendas por uid (soma valorTotal, excluindo inativos e institucionais)
        vendas_por = defaultdict(float)
        for v in vendas:
            if v.get("ativo") and not _pessoa_institucional(v.get("pessoa")):
                uid = v.get("usuarioCadastro")
                if uid in team_uids:
                    vendas_por[uid] += float(v.get("valorTotal") or 0)

        # WA contatos únicos atendidos por hashtag
        wa_por = {}
        try:
            now_brt = datetime.datetime.now(BRT)
            ts_fim = int(now_brt.timestamp())
            ts_ini = int((now_brt - datetime.timedelta(days=dias)).timestamp())
            conn = pg_conn()
            cur = conn.cursor()
            for m in TEAM_MEMBERS:
                if not m["wa_hashtag"]:
                    continue
                tag = m["wa_hashtag"].lower()
                cur.execute(
                    "SELECT COUNT(DISTINCT msg.key->>'remoteJid') FROM \"Message\" msg "
                    "JOIN \"Instance\" i ON msg.\"instanceId\" = i.id "
                    f"WHERE i.name IN {_EVO_IN_SQL} "
                    "AND (msg.key->>'fromMe')::boolean = true "
                    "AND msg.\"messageTimestamp\" BETWEEN %s AND %s "
                    "AND LOWER(TRIM(COALESCE(msg.message->>'conversation',"
                    "msg.message->'extendedTextMessage'->>'text',''))) ~ %s",
                    (ts_ini, ts_fim, f"^#{tag}\\]?$")
                )
                wa_por[m["uid"]] = cur.fetchone()[0] or 0
            conn.close()
        except Exception:
            pass

        resultado = []
        for m in TEAM_MEMBERS:
            uid = m["uid"]
            agends   = agend_por.get(uid, [])
            tot_ag   = len(agends)
            realizados = sum(1 for a in agends if a.get("statusMarcacao") in STATUS_CONSULTA_REALIZADA)
            tot_vend = round(vendas_por.get(uid, 0), 2)
            wa       = wa_por.get(uid)  # None → hashtag não configurado

            resultado.append({
                "uid":               uid,
                "nome":              m["nome"],
                "wa_configurado":    m["wa_hashtag"] is not None,
                "wa_contatos":       wa,
                "agendamentos":      tot_ag,
                "realizados":        realizados,
                "total_vendido":     tot_vend,
                "taxa_agend_realiz": round(realizados / tot_ag * 100, 1) if tot_ag else None,
                "taxa_wa_agend":     round(tot_ag / wa * 100, 1) if wa else None,
                "ticket_realizado":  round(tot_vend / realizados, 2) if realizados else None,
            })

        return jsonify({"status": "ok", "dias": dias, "pessoas": resultado})
    except Exception as e:
        return jsonify({"status": "erro", "mensagem": str(e)}), 500


@app.route("/api/tendencias_avancadas")
def api_tendencias_avancadas():
    try:
        dias = min(max(int(flask_request.args.get("dias", 30)), 7), 730)
        hoje = today_brt()
        cutoff = hoje - datetime.timedelta(days=dias)

        agend_all, orc_all, _contas, vendas_all = _all_raw_rows()

        date_range = [hoje - datetime.timedelta(days=i) for i in range(dias - 1, -1, -1)]
        date_strs = [d.isoformat() for d in date_range]

        agend_by_day = Counter()
        realiz_by_day = Counter()
        for a in agend_all:
            d = parse_quark_date(a.get("dataAgendamento", ""))
            if d and cutoff <= d <= hoje:
                agend_by_day[d.isoformat()] += 1
                if a.get("statusMarcacao") in STATUS_CONSULTA_REALIZADA:
                    realiz_by_day[d.isoformat()] += 1

        receita_by_day = {}
        for v in vendas_all:
            if not v.get("ativo") or _pessoa_institucional(v.get("pessoa")):
                continue
            d = parse_quark_date(v.get("dataConta", ""))
            if d and cutoff <= d <= hoje:
                key = d.isoformat()
                receita_by_day[key] = receita_by_day.get(key, 0) + (v.get("valorTotal") or 0)

        wa_by_day = {}
        ts_cutoff = int(datetime.datetime(cutoff.year, cutoff.month, cutoff.day, 0, 0, 0, tzinfo=BRT).timestamp())
        ts_hoje_fim = int(datetime.datetime(hoje.year, hoje.month, hoje.day, 23, 59, 59, tzinfo=BRT).timestamp())
        try:
            conn = pg_conn()
            cur = conn.cursor()
            cur.execute(
                "SELECT DATE(to_timestamp(msg.\"messageTimestamp\") AT TIME ZONE 'America/Sao_Paulo') AS dia,"
                " COUNT(DISTINCT msg.key->>'remoteJid') AS contatos"
                " FROM \"Message\" msg"
                " JOIN \"Instance\" i ON msg.\"instanceId\" = i.id"
                f" WHERE i.name IN {_EVO_IN_SQL}"
                " AND (msg.key->>'fromMe')::boolean = false"
                " AND msg.\"messageTimestamp\" BETWEEN %s AND %s"
                " AND msg.key->>'remoteJid' NOT LIKE '%%@g.us'"
                " GROUP BY dia ORDER BY dia",
                (ts_cutoff, ts_hoje_fim)
            )
            for row in cur.fetchall():
                wa_by_day[row[0].isoformat()] = row[1]
            conn.close()
        except Exception:
            pass

        agend_series   = [agend_by_day.get(d, 0) for d in date_strs]
        realiz_series  = [realiz_by_day.get(d, 0) for d in date_strs]
        receita_series = [round(receita_by_day.get(d, 0), 2) for d in date_strs]
        wa_series      = [wa_by_day.get(d, 0) for d in date_strs]

        def rolling_avg(series, window=7):
            result = []
            for i in range(len(series)):
                if i < window - 1:
                    result.append(None)
                else:
                    chunk = series[i - window + 1 : i + 1]
                    result.append(round(sum(chunk) / window, 1))
            return result

        def rolling_conv(num_series, den_series, window=7):
            result = []
            for i in range(len(num_series)):
                if i < window - 1:
                    result.append(None)
                else:
                    n = sum(num_series[i - window + 1:i + 1])
                    d = sum(den_series[i - window + 1:i + 1])
                    result.append(round(n / d * 100, 1) if d else None)
            return result

        wa_conv_series   = rolling_conv(agend_series, wa_series)
        agend_conv_series = rolling_conv(realiz_series, agend_series)

        # Momentum = avg of last 7 complete days (excludes today, which may be partial)
        w = min(7, len(agend_series) - 1)
        def _avg(s): return round(sum(s) / len(s), 1) if s else 0
        mom_agend   = _avg(agend_series[-(w + 1):-1])
        mom_realiz  = _avg(realiz_series[-(w + 1):-1])
        mom_wa      = _avg(wa_series[-(w + 1):-1])
        mom_receita = round(sum(receita_series[-(w + 1):-1]) / w, 2) if w else 0

        total_agend  = sum(agend_series)
        total_realiz = sum(realiz_series)
        total_wa     = sum(wa_series)
        taxa_wa_agend_rec  = round(mom_agend / mom_wa * 100, 1) if mom_wa else None
        taxa_agend_realiz_rec = round(mom_realiz / mom_agend * 100, 1) if mom_agend else None
        taxa_wa_agend_hist    = round(total_agend / total_wa * 100, 1) if total_wa else None
        taxa_agend_realiz_hist = round(total_realiz / total_agend * 100, 1) if total_agend else None

        # Projection: agendamentos already in calendar for next 30d + momentum estimate
        cutoff_fut = hoje + datetime.timedelta(days=30)
        agend_futuros_30 = sum(
            1 for a in agend_all
            if (parse_quark_date(a.get("dataAgendamento", "")) or datetime.date.min) > hoje
            and (parse_quark_date(a.get("dataAgendamento", "")) or datetime.date.min) <= cutoff_fut
        )
        agend_estimados  = round(mom_agend * 30)
        conv_r           = (taxa_agend_realiz_rec or taxa_agend_realiz_hist or 80) / 100
        realizados_proj  = round(agend_estimados * conv_r)
        receita_proj     = round(realizados_proj * (mom_receita / mom_realiz) if mom_realiz else realizados_proj * TICKET_MEDIO_FALLBACK)

        # ── Auto-insights ────────────────────────────────────────────────────
        insights = []

        if len(agend_series) >= 14:
            hist_agend_avg = _avg(agend_series[:-7])
            hist_wa_avg    = _avg(wa_series[:-7])

            # WA→agend conversion drop
            if taxa_wa_agend_hist and taxa_wa_agend_rec:
                delta = taxa_wa_agend_rec - taxa_wa_agend_hist
                if delta < -10:
                    insights.append({
                        "tipo": "queda_conversao_wa",
                        "titulo": "Queda na conversão WA para agendamento",
                        "descricao": f"Últimos 7 dias: {taxa_wa_agend_rec}% | Histórico {dias}d: {taxa_wa_agend_hist}% (queda de {abs(round(delta, 1))} p.p.)",
                        "acao": "Revisar script pós-handover. O bot está gerando leads — o gargalo pode estar na abordagem humana. Analisar transcrições de conversas que não geraram agendamento.",
                        "impacto": "alto" if abs(delta) > 20 else "médio",
                        "score": abs(delta)
                    })

            # agend→realizado drop (no-show/cancel)
            if taxa_agend_realiz_hist and taxa_agend_realiz_rec:
                delta_nr = taxa_agend_realiz_hist - taxa_agend_realiz_rec
                if delta_nr > 10:
                    insights.append({
                        "tipo": "aumento_noshow",
                        "titulo": "Aumento de faltas e cancelamentos",
                        "descricao": f"Últimos 7 dias: {taxa_agend_realiz_rec}% realizados | Histórico: {taxa_agend_realiz_hist}% (queda de {round(delta_nr, 1)} p.p.)",
                        "acao": "Intensificar confirmação 24h antes. Verificar se o lembrete automático está ativo no bot para todas as especialidades.",
                        "impacto": "alto" if delta_nr > 20 else "médio",
                        "score": delta_nr
                    })

            # WA demand trend
            if hist_wa_avg > 0 and mom_wa > 0:
                pct_wa = (mom_wa - hist_wa_avg) / hist_wa_avg * 100
                if pct_wa > 20:
                    insights.append({
                        "tipo": "demanda_acelerada",
                        "titulo": "Demanda WA acelerando",
                        "descricao": f"Últimos 7 dias: {mom_wa:.1f} contatos/dia | Período anterior: {hist_wa_avg:.1f}/dia (+{round(pct_wa)}%)",
                        "acao": "Alta demanda entrante — garantir que handover para equipe comercial é feito em menos de 2h para maximizar conversão.",
                        "impacto": "alto",
                        "score": pct_wa
                    })
                elif pct_wa < -20:
                    insights.append({
                        "tipo": "demanda_desacelerada",
                        "titulo": "Queda na demanda WA",
                        "descricao": f"Últimos 7 dias: {mom_wa:.1f} contatos/dia | Período anterior: {hist_wa_avg:.1f}/dia ({round(pct_wa)}%)",
                        "acao": "Considerar ação de reativação: pacientes com histórico de agendamento há mais de 90 dias sem retorno.",
                        "impacto": "médio",
                        "score": abs(pct_wa)
                    })

        # Day-of-week conversion pattern
        dow_agend  = Counter()
        dow_realiz = Counter()
        for i, ds in enumerate(date_strs):
            dobj = datetime.date.fromisoformat(ds)
            dow = dobj.weekday()
            dow_agend[dow]  += agend_series[i]
            dow_realiz[dow] += realiz_series[i]
        dow_conv = {dow: round(dow_realiz[dow] / dow_agend[dow] * 100, 1) for dow in dow_agend if dow_agend[dow] >= 5}
        DOW_NAMES = ["Segunda", "Terça", "Quarta", "Quinta", "Sexta", "Sábado", "Domingo"]
        if len(dow_conv) >= 3:
            best  = max(dow_conv, key=dow_conv.get)
            worst = min(dow_conv, key=dow_conv.get)
            gap   = dow_conv[best] - dow_conv[worst]
            if gap > 15:
                insights.append({
                    "tipo": "padrao_dia_semana",
                    "titulo": f"Grande diferença de conversão por dia da semana",
                    "descricao": f"Maior: {DOW_NAMES[best]} ({dow_conv[best]}%) | Menor: {DOW_NAMES[worst]} ({dow_conv[worst]}%) — diferença de {round(gap, 1)} p.p.",
                    "acao": f"Priorizar confirmações e follow-ups nas vésperas de {DOW_NAMES[worst]}. Concentrar agendamentos nos dias de maior conversão.",
                    "impacto": "médio",
                    "score": gap
                })

        # Open orcamentos (revenue opportunity)
        orc_abertos = [
            o for o in orc_all
            if o.get("statusOrcamento") not in ("EXECUTADA", "CANCELADO")
            and (parse_quark_date(o.get("dataOrcamento", "")) or datetime.date.min) >= cutoff
        ]
        if orc_abertos:
            valor_ab = sum(o.get("valorTotal") or 0 for o in orc_abertos)
            insights.append({
                "tipo": "orcamentos_abertos",
                "titulo": f"{len(orc_abertos)} orçamento(s) aberto(s) sem fechamento",
                "descricao": f"Valor potencial: R${valor_ab:,.2f} no período de {dias} dias",
                "acao": "Follow-up ativo via WA com pacientes que receberam orçamento mas não confirmaram. Oportunidade de receita imediata.",
                "impacto": "alto" if valor_ab > 5000 else "médio",
                "score": min(valor_ab / 100, 100)
            })

        # Specialty with biggest recent drop
        esp_recent = Counter()
        esp_hist   = Counter()
        hist_weeks = max((dias - 7) / 7, 1)
        for a in agend_all:
            d = parse_quark_date(a.get("dataAgendamento", ""))
            if not d:
                continue
            esp = _esp_nome(a)
            delta_d = (hoje - d).days
            if delta_d <= 7:
                esp_recent[esp] += 1
            elif cutoff <= d <= hoje - datetime.timedelta(days=7):
                esp_hist[esp] += 1
        for esp in esp_hist:
            hist_wk = esp_hist[esp] / hist_weeks
            if hist_wk >= 3 and esp_recent[esp] < hist_wk * 0.7:
                drop_pct = round((esp_recent[esp] - hist_wk) / hist_wk * 100, 1)
                insights.append({
                    "tipo": "especialidade_em_queda",
                    "titulo": f"{esp}: queda de {abs(drop_pct)}% vs. média",
                    "descricao": f"Esta semana: {esp_recent[esp]} agendamentos | Média histórica semanal: {hist_wk:.1f}",
                    "acao": f"Verificar disponibilidade de agenda em {esp} e comunicar recepção para priorizar agendamentos.",
                    "impacto": "médio",
                    "score": abs(drop_pct)
                })

        # Contato ativo — sempre relevante (resgate de faltantes + confirmação + fila de espera)
        faltantes_periodo = sum(
            1 for a in agend_all
            if (parse_quark_date(a.get("dataAgendamento", "")) or datetime.date.min) >= cutoff
            and a.get("statusMarcacao") in ("FALTOU", "AUSENTE_POS_CONSULTA")
        )
        top_esp_nomes = [esp for esp, _ in esp_hist.most_common(2)]
        esp_str = " e ".join(top_esp_nomes) if top_esp_nomes else "especialidades de maior demanda"
        faltantes_desc = (
            f"{faltantes_periodo} paciente(s) não compareceram nos últimos {dias} dias — "
            "contato via WhatsApp nas 48h seguintes recupera 20–30% da receita perdida."
        ) if faltantes_periodo > 0 else (
            "Nenhuma falta registrada no período — bom indicador de confirmação."
        )
        insights.append({
            "tipo": "contato_ativo",
            "titulo": "Pacientes para contato ativo",
            "descricao": (
                f"<strong>Resgate de faltantes:</strong> {faltantes_desc} "
                f"<strong>Confirmação preventiva:</strong> contato ativo 24h antes reduz no-show em ~30%; "
                "recomendado para todos os agendamentos da semana seguinte. "
                f"<strong>Fila de espera:</strong> {esp_str} concentram a maior demanda "
                "— lista de espera ativa captura pacientes prontos para agendar."
            ),
            "acao": "Ativar fluxos automáticos da Claudete para resgate de faltantes, confirmação antecipada e gestão de fila de espera nas especialidades críticas.",
            "impacto": "alto" if faltantes_periodo > 10 else ("médio" if faltantes_periodo > 0 else "baixo"),
            "score": faltantes_periodo,
        })

        insights.sort(key=lambda x: x.pop("score", 0), reverse=True)
        top_insights = insights[:3]

        return jsonify({
            "status": "ok",
            "dias": dias,
            "periodo_label": f"Últimos {dias} dias (desde {cutoff.strftime('%d/%m/%Y')})",
            "serie": {
                "datas": date_strs,
                "agendamentos": agend_series,
                "realizados": realiz_series,
                "receita": receita_series,
                "wa_contatos": wa_series,
                "wa_conv":    wa_conv_series,
                "agend_conv": agend_conv_series,
                "agendamentos_7d": rolling_avg(agend_series),
                "realizados_7d":   rolling_avg(realiz_series),
                "wa_7d":           rolling_avg(wa_series),
            },
            "momentum": {
                "agendamentos_dia": mom_agend,
                "realizados_dia":   mom_realiz,
                "wa_contatos_dia":  mom_wa,
                "receita_dia":      mom_receita,
                "taxa_wa_agend_recente":   taxa_wa_agend_rec,
                "taxa_agend_realiz_recente": taxa_agend_realiz_rec,
                "taxa_wa_agend_historico":  taxa_wa_agend_hist,
                "taxa_agend_realiz_historico": taxa_agend_realiz_hist,
            },
            "projecao_30d": {
                "agendamentos_em_agenda": agend_futuros_30,
                "agendamentos_estimados": agend_estimados,
                "realizados_projetados":  realizados_proj,
                "receita_projetada":      receita_proj,
            },
            "insights": top_insights,
        })
    except Exception as e:
        return jsonify({"status": "erro", "mensagem": str(e)}), 500


@app.route("/api/projection_accuracy")
def api_projection_accuracy():
    """Histórico de precisão das projeções mensais vs. valores reais."""
    try:
        conn = _proj_db_conn()
        rows = conn.execute("""
            SELECT snapshot_date, mes_alvo, dias_uteis_passados, dias_uteis_mes,
                   proj_realizados, proj_agendamentos, proj_receita,
                   ritmo_realizados, media_hist_realizados,
                   actual_realizados, actual_agendamentos, actual_receita,
                   evaluated_at, erro_realizados_pct, erro_receita_pct
            FROM projection_snapshots
            ORDER BY mes_alvo, snapshot_date
        """).fetchall()
        conn.close()

        snapshots = [dict(r) for r in rows]

        # Para o MAPE: usar o snapshot com mais dias úteis passados de cada mês (projeção mais tardia)
        representativos = {}
        for s in snapshots:
            if s["evaluated_at"] is None:
                continue
            m = s["mes_alvo"]
            if m not in representativos or (s["dias_uteis_passados"] or 0) > (representativos[m]["dias_uteis_passados"] or 0):
                representativos[m] = s

        rep_list  = list(representativos.values())
        erros_r   = [abs(s["erro_realizados_pct"]) for s in rep_list if s["erro_realizados_pct"] is not None]
        erros_rev = [abs(s["erro_receita_pct"])    for s in rep_list if s["erro_receita_pct"]    is not None]
        vies_r    = [s["erro_realizados_pct"] for s in rep_list if s["erro_realizados_pct"] is not None]
        vies_rev  = [s["erro_receita_pct"]    for s in rep_list if s["erro_receita_pct"]    is not None]

        mape_realizados = round(sum(erros_r)   / len(erros_r),   1) if erros_r   else None
        mape_receita    = round(sum(erros_rev) / len(erros_rev), 1) if erros_rev else None
        vies_realizados = round(sum(vies_r)    / len(vies_r),    1) if vies_r    else None
        vies_receita    = round(sum(vies_rev)  / len(vies_rev),  1) if vies_rev  else None

        return jsonify({
            "status": "ok",
            "snapshots": snapshots,
            "resumo": {
                "n_meses_avaliados": len(rep_list),
                "n_meses_pendentes": len(set(s["mes_alvo"] for s in snapshots if s["evaluated_at"] is None)),
                "mape_realizados": mape_realizados,
                "mape_receita":    mape_receita,
                "vies_realizados": vies_realizados,
                "vies_receita":    vies_receita,
            }
        })
    except Exception as e:
        return jsonify({"status": "erro", "mensagem": str(e)}), 500


_ESPECIALIDADES_KW = {
    "Cirurgia":       ["cirurgi", "operação", "operar", "encaminhamento cirúrgic", "cirurgião"],
    "Cardiologia":    ["cardiolog", "cardíac", "coração", "pressão alta", "hipertensão", "arritmia"],
    "Ortopedia":      ["ortopedi", "joelho", "coluna", "quadril", "tornozelo", "ombro", "fratura", "osso", "fisio"],
    "Dermatologia":   ["dermatolog", "pele", "mancha", "acne", "dermatite"],
    "Oftalmologia":   ["oftalmolog", "olhos", "visão", "óculos", "catarata", "glaucoma"],
    "Neurologia":     ["neurolog", "enxaqueca", "epilepsia", "convulsão"],
    "Ginecologia":    ["ginecolog", "pré-natal", "prenatal", "útero", "menstrua", "obstetri"],
    "Pediatria":      ["pediatr", "criança", "bebê", "infantil"],
    "Psiquiatria":    ["psiquiatr", "ansiedade", "depressão", "pânico"],
    "Urologia":       ["urolog", "próstata", "rim", "bexiga"],
    "Endocrinologia": ["endocrinolog", "diabetes", "tireoide", "hormônio", "glicemia"],
    "Clínica Geral":  ["clínico geral", "check-up", "checkup", "consulta geral"],
}

def _detectar_especialidade(textos):
    texto = " ".join(t.lower() for t in textos if t)
    for esp, kws in _ESPECIALIDADES_KW.items():
        if any(kw in texto for kw in kws):
            return esp
    return None


@app.route("/api/handoffs")
def api_handoffs():
    import time
    dias = int(flask_request.args.get("dias", 7))
    try:
        pg = pg_conn()
        cur = pg.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(f"""
            SELECT DISTINCT ON (m.key->>'remoteJid')
                m.key->>'remoteJid'          AS jid,
                m."messageTimestamp"          AS handoff_ts,
                m.message->>'conversation'    AS handoff_msg
            FROM "Message" m
            JOIN "Instance" i ON m."instanceId" = i.id
            WHERE i.name IN {_EVO_IN_SQL}
              AND m."messageTimestamp" >= EXTRACT(EPOCH FROM (NOW() - INTERVAL '%s days'))
              AND m.key->>'remoteJid' NOT LIKE '%%@g.us'
              AND m.key->>'fromMe' = 'true'
              AND {_handoff_conds_sql()}
            ORDER BY m.key->>'remoteJid', m."messageTimestamp" DESC
        """, (dias,))
        handoffs_raw = cur.fetchall()

        jids = [r["jid"] for r in handoffs_raw]
        stats = {}
        msgs_completas = {}
        recorrente_map = {}
        push_name_map = {}

        if jids:
            fmt = ",".join(["%s"] * len(jids))

            # Estatísticas e CPF por JID
            cur.execute(f"""
                SELECT
                    m.key->>'remoteJid'                        AS jid,
                    COUNT(*)                                    AS total_msgs,
                    MAX(m."messageTimestamp")                   AS ultima_ts,
                    MAX(CASE WHEN m.key->>'fromMe'='false'
                             THEN m."messageTimestamp" END)    AS ultima_ts_paciente,
                    (regexp_matches(
                        string_agg(COALESCE(m.message->>'conversation',''), ' '),
                        '[0-9]{{3}}[. ]?[0-9]{{3}}[. ]?[0-9]{{3}}[-. ]?[0-9]{{2}}'
                    ))[1]                                      AS cpf
                FROM "Message" m
                JOIN "Instance" i ON m."instanceId" = i.id
                WHERE i.name IN {_EVO_IN_SQL}
                  AND m.key->>'remoteJid' = ANY(ARRAY[{fmt}])
                GROUP BY m.key->>'remoteJid'
            """, jids)
            for row in cur.fetchall():
                stats[row["jid"]] = dict(row)

            # Últimas 8 mensagens com remetente para o painel de conversa
            cur.execute(f"""
                SELECT jid, sender, texto, ts FROM (
                    SELECT
                        m.key->>'remoteJid'  AS jid,
                        CASE WHEN m.key->>'fromMe'='true' THEN 'bot' ELSE 'paciente' END AS sender,
                        COALESCE(
                            m.message->>'conversation',
                            m.message->'extendedTextMessage'->>'text',
                            ''
                        )                    AS texto,
                        m."messageTimestamp" AS ts,
                        ROW_NUMBER() OVER (
                            PARTITION BY m.key->>'remoteJid'
                            ORDER BY m."messageTimestamp" DESC
                        ) AS rn
                    FROM "Message" m
                    JOIN "Instance" i ON m."instanceId" = i.id
                    WHERE i.name IN {_EVO_IN_SQL}
                      AND m.key->>'remoteJid' = ANY(ARRAY[{fmt}])
                      AND COALESCE(
                            m.message->>'conversation',
                            m.message->'extendedTextMessage'->>'text',
                            ''
                          ) != ''
                ) sub
                WHERE rn <= 8
                ORDER BY jid, ts ASC
            """, jids)
            for row in cur.fetchall():
                msgs_completas.setdefault(row["jid"], []).append({
                    "sender": row["sender"],
                    "texto":  row["texto"],
                    "ts":     row["ts"],
                })

            # Detecta paciente recorrente: mensagens fora da janela atual (>dias)
            cur.execute(f"""
                SELECT
                    m.key->>'remoteJid'  AS jid,
                    COUNT(*)             AS msgs_anteriores
                FROM "Message" m
                JOIN "Instance" i ON m."instanceId" = i.id
                WHERE i.name IN {_EVO_IN_SQL}
                  AND m.key->>'remoteJid' = ANY(ARRAY[{fmt}])
                  AND m."messageTimestamp" < EXTRACT(EPOCH FROM (NOW() - INTERVAL '%s days'))
                  AND m."messageTimestamp" >= EXTRACT(EPOCH FROM (NOW() - INTERVAL '90 days'))
                GROUP BY m.key->>'remoteJid'
            """, jids + [dias])
            for row in cur.fetchall():
                recorrente_map[row["jid"]] = int(row["msgs_anteriores"])

            # Busca pushName na tabela Contact para todos os JIDs
            cur.execute(f"""
                SELECT c."remoteJid" AS jid, c."pushName" AS push_name
                FROM "Contact" c
                JOIN "Instance" i ON c."instanceId" = i.id
                WHERE i.name IN {_EVO_IN_SQL}
                  AND c."remoteJid" = ANY(ARRAY[{fmt}])
            """, jids)
            push_name_map = {}
            for row in cur.fetchall():
                if row["push_name"]:
                    push_name_map[row["jid"]] = row["push_name"]

        pg.close()

        sqlite = _proj_db_conn()
        states = {}
        cmap = {}
        if jids:
            fmt_q = ",".join(["?"] * len(jids))
            for row in sqlite.execute(
                f"SELECT * FROM handoff_states WHERE jid IN ({fmt_q})", jids
            ).fetchall():
                states[row["jid"]] = dict(row)
            for row in sqlite.execute(
                f"SELECT jid, conversation_id FROM chatwoot_map WHERE jid IN ({fmt_q})", jids
            ).fetchall():
                cmap[row["jid"]] = row["conversation_id"]
        sqlite.close()

        now_ts = int(time.time())
        result = []
        for r in handoffs_raw:
            jid = r["jid"]
            s = stats.get(jid, {})
            st = states.get(jid, {})
            msgs = msgs_completas.get(jid, [])
            msgs_ant = recorrente_map.get(jid, 0)
            especialidade = _detectar_especialidade(
                [m["texto"] for m in msgs] + [(r["handoff_msg"] or "")]
            )
            # Extrair telefone: direto do JID para @s.whatsapp.net; nulo para @lid
            if jid.endswith("@s.whatsapp.net"):
                telefone = jid.replace("@s.whatsapp.net", "")
            else:
                telefone = None

            result.append({
                "jid":              jid,
                "telefone":         telefone,
                "nome_contato":     push_name_map.get(jid),
                "handoff_ts":       r["handoff_ts"],
                "handoff_msg":      (r["handoff_msg"] or "")[:120],
                "total_msgs":       s.get("total_msgs", 0),
                "ultima_ts":        s.get("ultima_ts"),
                "ultima_ts_paciente": s.get("ultima_ts_paciente"),
                "mins_aguardando":  round((now_ts - r["handoff_ts"]) / 60) if r["handoff_ts"] else None,
                "cpf":              s.get("cpf"),
                "msgs_completas":   msgs,
                "especialidade":    especialidade,
                "recorrente":       msgs_ant > 0,
                "qtd_contatos_ant": msgs_ant,
                "estado":           st.get("estado", "aguardando"),
                "agent":            st.get("agent"),
                "assumed_at":       st.get("assumed_at"),
                "desfecho":         st.get("desfecho"),
                "desfecho_at":      st.get("desfecho_at"),
                "notas":            st.get("notas"),
                "chatwoot_conversation_id": cmap.get(jid),
            })

        return jsonify({"status": "ok", "handoffs": result})
    except Exception as e:
        return jsonify({"status": "erro", "mensagem": str(e)}), 500


@app.route("/api/handoffs/assumir", methods=["POST"])
def api_handoffs_assumir():
    import time
    data = flask_request.get_json()
    jid = data.get("jid")
    handoff_ts = data.get("handoff_ts")
    agent = data.get("agent")
    if not (jid and handoff_ts and agent):
        return jsonify({"status": "erro", "mensagem": "jid, handoff_ts e agent são obrigatórios"}), 400
    try:
        conn = _proj_db_conn()
        conn.execute("""
            INSERT INTO handoff_states (jid, handoff_ts, estado, agent, assumed_at)
            VALUES (?, ?, 'assumido', ?, ?)
            ON CONFLICT(jid, handoff_ts) DO UPDATE SET
                estado=excluded.estado, agent=excluded.agent, assumed_at=excluded.assumed_at
        """, (jid, handoff_ts, agent, int(time.time())))
        conn.commit()
        conn.close()
        return jsonify({"status": "ok"})
    except Exception as e:
        return jsonify({"status": "erro", "mensagem": str(e)}), 500


@app.route("/api/handoffs/desfecho", methods=["POST"])
def api_handoffs_desfecho():
    import time
    data = flask_request.get_json()
    jid = data.get("jid")
    handoff_ts = data.get("handoff_ts")
    desfecho = data.get("desfecho")
    notas = data.get("notas", "")
    if not (jid and handoff_ts and desfecho):
        return jsonify({"status": "erro", "mensagem": "jid, handoff_ts e desfecho são obrigatórios"}), 400
    try:
        conn = _proj_db_conn()
        conn.execute("""
            INSERT INTO handoff_states (jid, handoff_ts, estado, desfecho, desfecho_at, notas)
            VALUES (?, ?, 'fechado', ?, ?, ?)
            ON CONFLICT(jid, handoff_ts) DO UPDATE SET
                estado='fechado', desfecho=excluded.desfecho,
                desfecho_at=excluded.desfecho_at, notas=excluded.notas
        """, (jid, handoff_ts, desfecho, int(time.time()), notas))
        conn.commit()
        conn.close()
        return jsonify({"status": "ok"})
    except Exception as e:
        return jsonify({"status": "erro", "mensagem": str(e)}), 500


@app.route("/api/handoffs/chatwoot-webhook", methods=["POST"])
def api_handoffs_chatwoot_webhook():
    """Ponte Chatwoot -> board: fecha/assume o card local quando a conversa
    é resolvida ou reatribuída no Chatwoot. Mesma extração de evento usada
    no workflow n8n 'CCN — Chatwoot Handoff Signal' (Processar Evento)."""
    b = flask_request.get_json(silent=True) or {}
    event = b.get("event")
    meta = b.get("meta") or {}
    sender = meta.get("sender") or {}
    assignee = meta.get("assignee") or {}
    phone = re.sub(r"[^0-9]", "", sender.get("phone_number") or "")
    if not phone:
        return jsonify({"status": "ok", "skipped": "no phone"})

    is_resolved = event == "conversation_status_changed" and b.get("status") == "resolved"
    changed = b.get("changed_attributes") or []
    assignee_changed = event == "conversation_updated" and any(
        "assignee_id" in c for c in changed if isinstance(c, dict)
    )
    if not (is_resolved or assignee_changed):
        return jsonify({"status": "ok", "skipped": f"irrelevant {event}"})

    jid = f"{phone}@s.whatsapp.net"
    conversation_id = b.get("id")

    try:
        conn = _proj_db_conn()
        if conversation_id:
            conn.execute("""
                INSERT INTO chatwoot_map (jid, conversation_id, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(jid) DO UPDATE SET
                    conversation_id=excluded.conversation_id, updated_at=excluded.updated_at
            """, (jid, conversation_id, int(time.time())))
            conn.commit()
        conn.close()

        handoff_ts = _handoff_ts_atual(jid)
        if not handoff_ts:
            return jsonify({"status": "ok", "skipped": "sem handoff correspondente no board"})

        conn = _proj_db_conn()
        if is_resolved:
            conn.execute("""
                INSERT INTO handoff_states (jid, handoff_ts, estado, desfecho, desfecho_at)
                VALUES (?, ?, 'fechado', 'resolvido_chatwoot', ?)
                ON CONFLICT(jid, handoff_ts) DO UPDATE SET
                    estado='fechado', desfecho='resolvido_chatwoot', desfecho_at=excluded.desfecho_at
            """, (jid, handoff_ts, int(time.time())))
        else:
            nome_assignee = assignee.get("name") or "equipe"
            agent = next(
                (m["nome"] for m in TEAM_MEMBERS if m["nome"].lower() in nome_assignee.lower()),
                nome_assignee,
            )
            conn.execute("""
                INSERT INTO handoff_states (jid, handoff_ts, estado, agent, assumed_at)
                VALUES (?, ?, 'assumido', ?, ?)
                ON CONFLICT(jid, handoff_ts) DO UPDATE SET
                    estado='assumido', agent=excluded.agent, assumed_at=excluded.assumed_at
            """, (jid, handoff_ts, agent, int(time.time())))
        conn.commit()
        conn.close()
        return jsonify({"status": "ok"})
    except Exception as e:
        return jsonify({"status": "erro", "mensagem": str(e)}), 500


@app.route("/api/handoffs/gestao")
def api_handoffs_gestao():
    """Métricas de gestão: por agente, por especialidade, tempo até assumir."""
    import time
    dias = int(flask_request.args.get("dias", 30))
    try:
        conn = _proj_db_conn()
        cutoff = int(time.time()) - dias * 86400

        rows = conn.execute("""
            SELECT jid, handoff_ts, estado, agent, assumed_at,
                   desfecho, desfecho_at, notas
            FROM handoff_states
            WHERE handoff_ts >= ?
            ORDER BY handoff_ts DESC
        """, (cutoff,)).fetchall()
        conn.close()

        agentes = {m["nome"]: {
            "assumidos": 0, "fechados": 0,
            "agendou": 0, "perdido": 0, "outros_desfecho": 0,
            "tempo_assumir_secs": [],
        } for m in TEAM_MEMBERS}
        agentes["(sem agente)"] = {"assumidos": 0, "fechados": 0,
            "agendou": 0, "perdido": 0, "outros_desfecho": 0,
            "tempo_assumir_secs": []}

        por_especialidade = {}
        historico = []

        for r in rows:
            agent_key = r["agent"] if r["agent"] in agentes else "(sem agente)"
            a = agentes[agent_key]

            if r["estado"] in ("assumido", "fechado"):
                a["assumidos"] += 1
                if r["assumed_at"] and r["handoff_ts"]:
                    delta = r["assumed_at"] - r["handoff_ts"]
                    if 0 < delta < 86400:
                        a["tempo_assumir_secs"].append(delta)

            if r["estado"] == "fechado":
                a["fechados"] += 1
                if r["desfecho"] in ("agendou", "reagendou"):
                    a["agendou"] += 1
                elif r["desfecho"] == "perdido":
                    a["perdido"] += 1
                else:
                    a["outros_desfecho"] += 1

            historico.append({
                "jid":         r["jid"][-6:],
                "handoff_ts":  r["handoff_ts"],
                "estado":      r["estado"],
                "agent":       r["agent"],
                "assumed_at":  r["assumed_at"],
                "desfecho":    r["desfecho"],
                "desfecho_at": r["desfecho_at"],
                "notas":       r["notas"],
                "tempo_assumir_min": round((r["assumed_at"] - r["handoff_ts"]) / 60)
                    if (r["assumed_at"] and r["handoff_ts"] and r["assumed_at"] > r["handoff_ts"])
                    else None,
            })

        resultado_agentes = []
        for nome, d in agentes.items():
            if nome == "(sem agente)" and d["assumidos"] == 0:
                continue
            tempos = d["tempo_assumir_secs"]
            resultado_agentes.append({
                "nome":           nome,
                "assumidos":      d["assumidos"],
                "fechados":       d["fechados"],
                "agendou":        d["agendou"],
                "perdido":        d["perdido"],
                "outros":         d["outros_desfecho"],
                "conversao_pct":  round(d["agendou"] / d["fechados"] * 100) if d["fechados"] else None,
                "tempo_medio_assumir_min": round(sum(tempos) / len(tempos) / 60) if tempos else None,
            })

        total = len(rows)
        fechados = [r for r in rows if r["estado"] == "fechado"]
        agendaram = sum(1 for r in fechados if r["desfecho"] in ("agendou", "reagendou"))
        aguardando = sum(1 for r in rows if r["estado"] == "aguardando")
        assumidos = sum(1 for r in rows if r["estado"] == "assumido")
        todos_tempos = [
            (r["assumed_at"] - r["handoff_ts"]) / 60
            for r in rows
            if r["assumed_at"] and r["handoff_ts"] and 0 < (r["assumed_at"] - r["handoff_ts"]) < 86400
        ]

        return jsonify({
            "status": "ok",
            "dias": dias,
            "resumo": {
                "total":           total,
                "aguardando":      aguardando,
                "assumidos":       assumidos,
                "fechados":        len(fechados),
                "agendaram":       agendaram,
                "conversao_pct":   round(agendaram / len(fechados) * 100) if fechados else None,
                "tempo_medio_assumir_min": round(sum(todos_tempos) / len(todos_tempos)) if todos_tempos else None,
            },
            "por_agente":    resultado_agentes,
            "historico":     historico[:200],
        })
    except Exception as e:
        return jsonify({"status": "erro", "mensagem": str(e)}), 500


@app.route("/api/cache/status")
def api_cache_status():
    agora = time.time()
    keys = _api_cache_keys()
    with _cache_lock:
        quark_atualizando = _cache.get("atualizando", False)
        quark_meses = len(_cache.get("raw", {}))
        quark_cpf_map = len(_cache.get("paciente_cpf_map", {}))
        quark_ts = _cache.get("ts", 0)
    return jsonify({
        "total_keys": len(keys),
        "quark_cache": {
            "atualizando": quark_atualizando,
            "meses_carregados": quark_meses,
            "cpf_map_size": quark_cpf_map,
            "ultima_atualizacao_seg": round(agora - quark_ts) if quark_ts else None,
        },
        "entries": {
            k: {
                "idade_seg": round(agora - ts),
                "expira_em_seg": max(0, round(_API_CACHE_TTL - (agora - ts))),
                "fresco": (agora - ts) < _API_CACHE_TTL,
            }
            for k, ts in sorted(keys.items())
        }
    })


@app.route("/api/cache/invalidar", methods=["POST"])
def api_cache_invalidar():
    """Limpa todo o _api_cache. Proximo acesso recalcula do zero."""
    with _api_cache_lock:
        n = len(_api_cache)
        _api_cache.clear()
    print(f"[cache] invalidado manualmente ({n} chaves removidas)", flush=True)
    return jsonify({"ok": True, "chaves_removidas": n})


# ═══════════════════════════════════════════════════════════════════════════
# MÓDULO SEFAS ASSISTENCIAL — outbound WhatsApp
#
# Fontes de dados disponíveis para o dashboard (mapeadas em out/2026):
#
# 1. PostgreSQL leads (esta app, via pg_conn()):
#    - tabela: leads WHERE client_id='sefas'
#    - campos: status, variacao, enviado_em, respondeu_em, handoff_em,
#              plano_indicado, origem, campaign_id
#    - status: pendente|enviado|respondeu|interesse|duvida|recusa|handoff|sem_resposta|fechado
#
# 2. Google Sheets (tab SEFAS_OUTBOUND, planilha 11jba-gDTFNhDowz4XlDb5rkJEQNy6bJqNKyUMH-iQVw):
#    - espelho operacional lido pelo n8n para controle de envios
#    - colunas: Status, Ação Envio, Variação, Primeiro Nome, Telefone,
#               Origem, Plano Indicado, Data Envio, remoteJid
#
# 3. Evolution API / evolution_db (via pg_conn()):
#    - tabela Message JOIN Instance WHERE name='SEFAS-Assistencial'
#    - disponível: messageTimestamp, fromMe, remoteJid
#    - ATENÇÃO: instância ainda não criada — aguarda warm-up de 7 dias
#
# 4. Chatwoot (via CHATWOOT_BASE + CHATWOOT_API_TOKEN):
#    - handoffs equipe SEFAS — reutiliza chatwoot_agent_summary()
#    - métricas: conversations_count, resolved, avg_first_response_time
#
# 5. n8n workflow 'SEFAS — OUTBOUND CAMPANHA':
#    - teto 50/dia, janela horária, anti-ban 30min
#    - log em 'Historico Outbound' (Sheets) e 'Histórico Recepção'
#    - A/B test via coluna Variação (S1/S2)
# ═══════════════════════════════════════════════════════════════════════════

_SEFAS_CLIENT_ID = "sefas"


def _sefas_normalizar_tel(raw: str):
    digits = re.sub(r"\D", "", raw)
    if digits.startswith("55"):
        digits = digits[2:]
    if len(digits) == 11 and digits[2] in "6789":
        return "55" + digits
    if len(digits) == 10:
        return "55" + digits
    return None


@app.route("/sefas")
def sefas_index():
    return jsonify({
        "modulo": "SEFAS Assistencial",
        "status": "API ativa — dashboard em construção",
        "endpoints": [
            "POST /api/sefas/leads/import",
            "GET  /api/sefas/metrics",
            "GET  /api/sefas/leads",
            "PUT  /api/sefas/leads/<id>",
        ],
    })


@app.route("/api/sefas/leads/import", methods=["POST"])
def api_sefas_leads_import():
    """Importa CSV (nome, telefone[, origem]) para tabela leads.
    Deduplica por (telefone, client_id). Alterna variação S1/S2."""
    f = flask_request.files.get("file")
    if not f:
        return jsonify({"status": "erro", "mensagem": "campo 'file' obrigatório"}), 400
    origem_padrao = flask_request.form.get("origem", "lista-sefas")
    campaign_id   = flask_request.form.get("campaign_id", "sefas-2026")

    content = f.read().decode("utf-8-sig", errors="replace")
    reader  = csv.DictReader(io.StringIO(content))
    cols    = [c.lower().strip() for c in (reader.fieldnames or [])]
    if "telefone" not in cols or "nome" not in cols:
        return jsonify({"status": "erro",
                        "mensagem": f"CSV precisa de 'nome' e 'telefone'. Encontrado: {cols}"}), 400

    inseridos = invalidos = duplicados_csv = duplicados_db = 0
    seen_csv, rows_ok = set(), []
    for row in reader:
        nome = row.get("nome", "").strip()
        tel  = _sefas_normalizar_tel(row.get("telefone", ""))
        orig = row.get("origem", origem_padrao).strip() or origem_padrao
        if not tel:
            invalidos += 1
            continue
        if tel in seen_csv:
            duplicados_csv += 1
            continue
        seen_csv.add(tel)
        rows_ok.append((nome, nome.split()[0] if nome else "", tel, orig))

    if not rows_ok:
        return jsonify({"status": "ok", "inseridos": 0, "invalidos": invalidos,
                        "duplicados_csv": duplicados_csv, "duplicados_db": 0})

    try:
        conn = pg_conn()
        cur  = conn.cursor()
        cur.execute("SELECT telefone FROM leads WHERE client_id = %s", (_SEFAS_CLIENT_ID,))
        existing = {r[0] for r in cur.fetchall()}

        for i, (nome, primeiro_nome, tel, orig) in enumerate(rows_ok):
            if tel in existing:
                duplicados_db += 1
                continue
            variacao = "S1" if i % 2 == 0 else "S2"
            cur.execute("""
                INSERT INTO leads
                    (nome, primeiro_nome, telefone, origem, status, variacao,
                     client_id, campaign_id)
                VALUES (%s, %s, %s, %s, 'pendente', %s, %s, %s)
                ON CONFLICT (telefone, client_id) DO NOTHING
            """, (nome, primeiro_nome, tel, orig, variacao, _SEFAS_CLIENT_ID, campaign_id))
            if cur.rowcount:
                existing.add(tel)
                inseridos += 1

        conn.commit()
        conn.close()
    except Exception as e:
        return jsonify({"status": "erro", "mensagem": str(e)}), 500

    return jsonify({
        "status": "ok",
        "inseridos": inseridos,
        "invalidos": invalidos,
        "duplicados_csv": duplicados_csv,
        "duplicados_db": duplicados_db,
    })


@app.route("/api/sefas/metrics")
def api_sefas_metrics():
    """Métricas operacionais da campanha SEFAS — funil + A/B + planos."""
    try:
        conn = pg_conn()
        cur  = conn.cursor()

        cur.execute("SELECT status, COUNT(*) FROM leads WHERE client_id = %s GROUP BY status",
                    (_SEFAS_CLIENT_ID,))
        por_status = dict(cur.fetchall())

        cur.execute("SELECT variacao, COUNT(*) FROM leads WHERE client_id = %s GROUP BY variacao",
                    (_SEFAS_CLIENT_ID,))
        por_variacao = dict(cur.fetchall())

        cur.execute("""
            SELECT plano_indicado, COUNT(*) FROM leads
            WHERE client_id = %s AND plano_indicado IS NOT NULL GROUP BY plano_indicado
        """, (_SEFAS_CLIENT_ID,))
        por_plano = dict(cur.fetchall())

        cur.execute("""
            SELECT DATE(enviado_em AT TIME ZONE 'America/Sao_Paulo'), COUNT(*)
            FROM leads WHERE client_id = %s AND enviado_em >= NOW() - INTERVAL '30 days'
            GROUP BY 1 ORDER BY 1
        """, (_SEFAS_CLIENT_ID,))
        envios_por_dia = [{"data": str(r[0]), "count": r[1]} for r in cur.fetchall()]

        conn.close()
    except Exception as e:
        return jsonify({"status": "erro", "mensagem": str(e)}), 500

    total      = sum(por_status.values())
    enviados   = sum(por_status.get(s, 0) for s in
                     ("enviado", "respondeu", "interesse", "handoff", "fechado", "recusa", "sem_resposta"))
    responderam = sum(por_status.get(s, 0) for s in ("respondeu", "interesse", "handoff", "fechado"))
    interesse   = sum(por_status.get(s, 0) for s in ("interesse", "handoff", "fechado"))
    handoffs    = por_status.get("handoff", 0)
    fechados    = por_status.get("fechado", 0)

    return jsonify({
        "status": "ok",
        "total_leads": total,
        "por_status": por_status,
        "funil": {
            "total":      total,
            "enviados":   enviados,
            "responderam": responderam,
            "interesse":  interesse,
            "handoffs":   handoffs,
            "fechados":   fechados,
            "taxa_resposta_pct":   round(responderam / enviados * 100, 1) if enviados else None,
            "taxa_interesse_pct":  round(interesse / enviados * 100, 1)   if enviados else None,
            "taxa_handoff_pct":    round(handoffs / enviados * 100, 1)    if enviados else None,
            "taxa_fechamento_pct": round(fechados / handoffs * 100, 1)    if handoffs else None,
        },
        "variacao_ab": por_variacao,
        "por_plano": por_plano,
        "envios_por_dia": envios_por_dia,
        "atualizado_em": datetime.datetime.now(BRT).strftime("%d/%m %H:%M"),
    })


@app.route("/api/sefas/leads")
def api_sefas_leads():
    """Lista leads SEFAS com paginação e filtro por status."""
    status_f = flask_request.args.get("status")
    page     = max(int(flask_request.args.get("page", 1)), 1)
    per_page = min(int(flask_request.args.get("per_page", 50)), 200)
    offset   = (page - 1) * per_page

    try:
        conn   = pg_conn()
        cur    = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        conds  = ["client_id = %s"]
        params = [_SEFAS_CLIENT_ID]
        if status_f:
            conds.append("status = %s")
            params.append(status_f)
        where = " AND ".join(conds)
        cur.execute(f"SELECT COUNT(*) FROM leads WHERE {where}", params)
        total = cur.fetchone()["count"]
        cur.execute(
            f"SELECT id, nome, primeiro_nome, telefone, origem, status, variacao, "
            f"plano_indicado, campaign_id, criado_em, enviado_em, respondeu_em, handoff_em, notas "
            f"FROM leads WHERE {where} ORDER BY criado_em DESC LIMIT %s OFFSET %s",
            params + [per_page, offset],
        )
        leads = [dict(r) for r in cur.fetchall()]
        conn.close()
    except Exception as e:
        return jsonify({"status": "erro", "mensagem": str(e)}), 500

    return jsonify({"status": "ok", "total": total, "page": page,
                    "per_page": per_page, "leads": leads})


@app.route("/api/sefas/leads/<int:lead_id>", methods=["PUT"])
def api_sefas_lead_update(lead_id):
    """Atualiza status, plano_indicado ou notas de um lead."""
    body    = flask_request.get_json(silent=True) or {}
    allowed = {"status", "plano_indicado", "notas"}
    updates = {k: v for k, v in body.items() if k in allowed}
    if not updates:
        return jsonify({"status": "erro", "mensagem": "nada para atualizar"}), 400

    ts_auto = {"enviado": "enviado_em = NOW()", "respondeu": "respondeu_em = NOW()",
               "handoff": "handoff_em = NOW()"}
    set_parts = [f"{k} = %s" for k in updates]
    if "status" in updates and updates["status"] in ts_auto:
        set_parts.append(ts_auto[updates["status"]])
    vals = list(updates.values()) + [_SEFAS_CLIENT_ID, lead_id]

    try:
        conn = pg_conn()
        cur  = conn.cursor()
        cur.execute(f"UPDATE leads SET {', '.join(set_parts)} "
                    "WHERE client_id = %s AND id = %s", vals)
        conn.commit()
        conn.close()
        if cur.rowcount == 0:
            return jsonify({"status": "erro", "mensagem": "lead não encontrado"}), 404
    except Exception as e:
        return jsonify({"status": "erro", "mensagem": str(e)}), 500

    return jsonify({"status": "ok", "atualizado": lead_id})


# ── Google Sheets sync ────────────────────────────────────────────────────
_SEFAS_SHEET_ID  = os.environ.get("SEFAS_SPREADSHEET_ID",
                                   "11jba-gDTFNhDowz4XlDb5rkJEQNy6bJqNKyUMH-iQVw")
_SEFAS_SHEET_TAB = "SEFAS_OUTBOUND"
_SHEETS_SCOPES   = ["https://www.googleapis.com/auth/spreadsheets"]
_SHEET_HEADER    = ["Status", "Ação Envio", "Variação", "Primeiro Nome",
                    "Telefone", "Origem", "Plano Indicado", "Data Envio", "remoteJid"]


def _sheets_client():
    if not _GSPREAD_OK:
        raise RuntimeError("gspread não instalado — pip install gspread google-auth")
    sa_env = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "")
    if not sa_env:
        raise RuntimeError("GOOGLE_SERVICE_ACCOUNT_JSON não definido no ambiente")
    if sa_env.strip().startswith("{"):
        creds = _GCreds.from_service_account_info(json.loads(sa_env), scopes=_SHEETS_SCOPES)
    else:
        creds = _GCreds.from_service_account_file(sa_env, scopes=_SHEETS_SCOPES)
    return _gspread.authorize(creds)


@app.route("/api/sefas/sheets/sync", methods=["POST"])
def api_sefas_sheets_sync():
    """Sincroniza leads pendentes do PostgreSQL → Google Sheets (SEFAS_OUTBOUND).
    Apenas insere novos; nunca sobrescreve linhas existentes."""
    try:
        gc = _sheets_client()
    except RuntimeError as e:
        return jsonify({"status": "erro", "mensagem": str(e)}), 503

    try:
        conn = pg_conn()
        cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("""
            SELECT primeiro_nome, telefone, origem, variacao,
                   COALESCE(plano_indicado, 'a_confirmar') AS plano_indicado
            FROM leads WHERE client_id = %s AND status = 'pendente'
            ORDER BY criado_em
        """, (_SEFAS_CLIENT_ID,))
        pg_leads = cur.fetchall()
        conn.close()
    except Exception as e:
        return jsonify({"status": "erro", "mensagem": f"PostgreSQL: {e}"}), 500

    try:
        sh = gc.open_by_key(_SEFAS_SHEET_ID)
        try:
            ws = sh.worksheet(_SEFAS_SHEET_TAB)
        except _gspread.WorksheetNotFound:
            ws = sh.add_worksheet(_SEFAS_SHEET_TAB, rows=5000, cols=len(_SHEET_HEADER))
            ws.append_row(_SHEET_HEADER)
        existing = {str(r.get("Telefone", "")).strip()
                    for r in ws.get_all_records()}
    except Exception as e:
        return jsonify({"status": "erro", "mensagem": f"Google Sheets: {e}"}), 500

    rows_novos, ignorados = [], 0
    for lead in pg_leads:
        tel = str(lead["telefone"]).strip()
        if tel in existing:
            ignorados += 1
            continue
        rows_novos.append([
            "pendente", "SIM",
            lead["variacao"] or "S1",
            lead["primeiro_nome"] or "",
            tel,
            lead["origem"] or "lista-sefas",
            lead["plano_indicado"] or "a_confirmar",
            "", "",
        ])
        existing.add(tel)

    if rows_novos:
        try:
            ws.append_rows(rows_novos, value_input_option="USER_ENTERED")
        except Exception as e:
            return jsonify({"status": "erro",
                            "mensagem": f"append_rows falhou: {e}",
                            "inseridos_antes_do_erro": len(rows_novos)}), 500

    return jsonify({
        "status": "ok",
        "inseridos": len(rows_novos),
        "ignorados_ja_existiam": ignorados,
        "total_pg": len(pg_leads),
        "atualizado_em": datetime.datetime.now(BRT).strftime("%d/%m %H:%M"),
    })


@app.route("/static/<path:filename>")
def static_files(filename):
    return send_from_directory("static", filename)


@app.route("/")
def index():
    return send_from_directory(".", "dashboard.html")

@app.route("/vendas")
def vendas():
    return send_from_directory(".", "vendas.html")

@app.route("/handoffs")
def handoffs():
    return send_from_directory(".", "handoffs.html")


# ── Aliases /ccn (para URL painel.centrocliniconiteroi.com.br/ccn/...) ──
@app.route("/ccn")
def ccn_index():
    return send_from_directory(".", "dashboard.html")

@app.route("/ccn/vendas")
def ccn_vendas():
    return send_from_directory(".", "vendas.html")

@app.route("/ccn/handoffs")
def ccn_handoffs():
    return send_from_directory(".", "handoffs.html")


@app.route("/admin")
def admin():
    return jsonify({
        "modulo": "Admin",
        "status": "stub — painel em construção",
        "endpoints_disponiveis": [
            "GET  /admin",
            "GET  /api/sefas/metrics",
            "GET  /api/sefas/leads",
            "POST /api/sefas/leads/import",
            "POST /api/sefas/sheets/sync",
        ],
    })


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)
