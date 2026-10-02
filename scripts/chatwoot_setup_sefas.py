"""
Cria inbox "SEFAS Assistencial" + team "Master Atendimento" no Chatwoot.
Rodar UMA vez, quando os e-mails dos 5 agentes estiverem disponíveis.

Uso:
    python scripts/chatwoot_setup_sefas.py

Variáveis de ambiente necessárias (copiar do .env do VPS):
    CHATWOOT_BASE          ex: https://app.chatwoot.com
    CHATWOOT_API_TOKEN     token de superadmin
    CHATWOOT_ACCOUNT_ID    número da conta (normalmente 1)
"""
import os, sys, json, urllib.request, urllib.error

BASE  = os.environ.get('CHATWOOT_BASE', '').rstrip('/')
TOKEN = os.environ.get('CHATWOOT_API_TOKEN', '')
ACCT  = os.environ.get('CHATWOOT_ACCOUNT_ID', '1')

if not BASE or not TOKEN:
    sys.exit('❌  Defina CHATWOOT_BASE e CHATWOOT_API_TOKEN no ambiente.')

HEADERS = {
    'Content-Type': 'application/json',
    'api_access_token': TOKEN,
}

AGENTES = [
    # Preencher com os e-mails reais antes de rodar
    {'nome': 'Príncipe',      'email': 'PREENCHER@email.com'},
    {'nome': 'Bruno',         'email': 'PREENCHER@email.com'},
    {'nome': 'Bruno Navega',  'email': 'PREENCHER@email.com'},
    {'nome': 'Liz',           'email': 'PREENCHER@email.com'},
    {'nome': 'Letícia',       'email': 'PREENCHER@email.com'},
]

def api(method, path, data=None):
    url = f'{BASE}/api/v1/accounts/{ACCT}/{path}'
    body = json.dumps(data).encode() if data else None
    req  = urllib.request.Request(url, data=body, headers=HEADERS, method=method)
    try:
        with urllib.request.urlopen(req) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        print(f'  ⚠  HTTP {e.code}: {e.read().decode()}')
        return None

def main():
    emails_faltando = [a for a in AGENTES if 'PREENCHER' in a['email']]
    if emails_faltando:
        print('❌  Preencha os e-mails dos agentes antes de rodar:')
        for a in emails_faltando:
            print(f'   - {a["nome"]}')
        sys.exit(1)

    # 1. Criar inbox
    print('\n1️⃣  Criando inbox "SEFAS Assistencial"...')
    inbox = api('POST', 'inboxes', {
        'name': 'SEFAS Assistencial',
        'channel': {'type': 'api', 'webhook_url': ''},
    })
    if not inbox:
        sys.exit('❌  Falhou ao criar inbox.')
    inbox_id = inbox['id']
    print(f'   ✅  Inbox criada — ID: {inbox_id}')

    # 2. Criar team
    print('\n2️⃣  Criando team "Master Atendimento"...')
    team = api('POST', 'teams', {'name': 'Master Atendimento'})
    if not team:
        sys.exit('❌  Falhou ao criar team.')
    team_id = team['id']
    print(f'   ✅  Team criado — ID: {team_id}')

    # 3. Buscar IDs dos agentes pelo e-mail
    print('\n3️⃣  Buscando agentes...')
    todos = api('GET', 'agents') or []
    mapa_email = {a['email']: a['id'] for a in todos}
    ids_agentes = []
    for ag in AGENTES:
        aid = mapa_email.get(ag['email'])
        if aid:
            ids_agentes.append(aid)
            print(f'   ✅  {ag["nome"]} — ID {aid}')
        else:
            print(f'   ⚠  {ag["nome"]} ({ag["email"]}) não encontrado — convide-o primeiro no Chatwoot.')

    # 4. Adicionar agentes ao team
    if ids_agentes:
        print(f'\n4️⃣  Adicionando {len(ids_agentes)} agentes ao team...')
        r = api('POST', f'teams/{team_id}/team_members', {'user_ids': ids_agentes})
        if r is not None:
            print(f'   ✅  Agentes adicionados.')

    # 5. Associar team à inbox (via inbox members)
    print(f'\n5️⃣  Associando inbox ao team...')
    api('POST', f'inbox_members', {'inbox_id': inbox_id, 'user_ids': ids_agentes})
    print('   ✅  Inbox associada.')

    print(f'\n🎉  Setup concluído!')
    print(f'   Inbox ID : {inbox_id}  ← adicionar ao .env como CHATWOOT_SEFAS_INBOX_ID')
    print(f'   Team ID  : {team_id}')
    print(f'\n⚠  Adicione CHATWOOT_SEFAS_INBOX_ID={inbox_id} no .env do VPS antes de ativar o n8n.')

if __name__ == '__main__':
    main()
