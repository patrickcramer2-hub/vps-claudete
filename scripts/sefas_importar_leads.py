"""
Importa CSV de leads SEFAS para o banco Postgres.
Higieniza telefones, deduplica e gera lote de piloto.

Uso:
    python scripts/sefas_importar_leads.py leads.csv --piloto 150
    python scripts/sefas_importar_leads.py leads.csv --piloto 0   # importa tudo
"""
import csv, re, sys, os, argparse, psycopg2, psycopg2.extras
from datetime import datetime

def normalizar_tel(raw: str) -> str | None:
    digits = re.sub(r'\D', '', raw)
    if digits.startswith('55'):
        digits = digits[2:]
    if len(digits) == 11 and digits[2] in '6789':   # celular com DDD
        return digits
    if len(digits) == 10:                             # fixo — aceitar mas sinalizar
        return digits
    return None

def main():
    p = argparse.ArgumentParser()
    p.add_argument('arquivo', help='CSV com colunas: nome, telefone[, origem]')
    p.add_argument('--piloto', type=int, default=150,
                   help='Quantos leads importar (0 = todos). Default: 150')
    p.add_argument('--origem', default='lista-sefas')
    args = p.parse_args()

    dsn = os.environ.get('DATABASE_URL') or os.environ.get('POSTGRES_DSN')
    if not dsn:
        sys.exit('❌  Defina DATABASE_URL ou POSTGRES_DSN no ambiente.')

    rows, invalidos = [], []
    with open(args.arquivo, newline='', encoding='utf-8-sig') as f:
        reader = csv.DictReader(f)
        cols = [c.lower().strip() for c in (reader.fieldnames or [])]
        if 'telefone' not in cols or 'nome' not in cols:
            sys.exit(f'❌  CSV precisa ter colunas "nome" e "telefone". Encontrado: {cols}')
        for i, row in enumerate(reader, 1):
            nome = row.get('nome','').strip()
            tel  = normalizar_tel(row.get('telefone',''))
            orig = row.get('origem', args.origem).strip() or args.origem
            if not tel:
                invalidos.append((i, row.get('telefone','')))
                continue
            rows.append((nome, tel, orig))

    # deduplicar no CSV
    seen = set()
    rows_dedup = []
    for r in rows:
        if r[1] not in seen:
            seen.add(r[1])
            rows_dedup.append(r)

    lote = rows_dedup[:args.piloto] if args.piloto > 0 else rows_dedup

    print(f'\n📋  Total no CSV:   {len(rows)}')
    print(f'🔄  Deduplicados:  {len(rows_dedup)}')
    print(f'🚫  Inválidos:     {len(invalidos)}')
    print(f'📦  Lote a inserir: {len(lote)}')
    if invalidos:
        print(f'\n⚠  Telefones inválidos (linhas): {invalidos[:10]}{"…" if len(invalidos)>10 else ""}')

    confirm = input(f'\nImportar {len(lote)} leads? [s/N] ').strip().lower()
    if confirm != 's':
        print('Cancelado.')
        return

    conn = psycopg2.connect(dsn)
    cur  = conn.cursor()
    inseridos = ignorados = 0
    for nome, tel, orig in lote:
        cur.execute(
            """INSERT INTO sefas_leads (nome, telefone, origem, status)
               VALUES (%s, %s, %s, 'pendente')
               ON CONFLICT (telefone) DO NOTHING""",
            (nome, tel, orig)
        )
        if cur.rowcount:
            inseridos += 1
        else:
            ignorados += 1

    conn.commit()
    cur.close()
    conn.close()

    print(f'\n✅  Inseridos:  {inseridos}')
    print(f'⏭  Ignorados (já existiam): {ignorados}')
    print(f'\nAgora ative o workflow n8n "SEFAS Outbound Campanha" para iniciar os envios.')

if __name__ == '__main__':
    main()
