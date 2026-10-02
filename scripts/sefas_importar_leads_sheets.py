"""
Importa CSV de leads SEFAS para a aba SEFAS_OUTBOUND no Google Sheets.
Higieniza telefones, deduplica e define variação de mensagem (S1/S2 alternado).

Uso:
    python scripts/sefas_importar_leads_sheets.py leads.csv --piloto 150

Variáveis de ambiente necessárias:
    GOOGLE_SERVICE_ACCOUNT_JSON   caminho para o JSON da service account
    SEFAS_SPREADSHEET_ID          ID do Sheets (11jba-gDTFNhDowz4XlDb5rkJEQNy6bJqNKyUMH-iQVw)
"""
import csv, re, sys, os, argparse, json
import gspread
from google.oauth2.service_account import Credentials

SHEET_TAB = "SEFAS_OUTBOUND"
SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

def normalizar_tel(raw: str) -> str | None:
    digits = re.sub(r"\D", "", raw)
    if digits.startswith("55"):
        digits = digits[2:]
    if len(digits) == 11 and digits[2] in "6789":
        return "55" + digits
    if len(digits) == 10:
        return "55" + digits
    return None

def main():
    p = argparse.ArgumentParser()
    p.add_argument("arquivo", help="CSV com colunas: nome, telefone[, origem]")
    p.add_argument("--piloto", type=int, default=150,
                   help="Quantos leads importar (0 = todos). Default: 150")
    p.add_argument("--origem", default="lista-sefas")
    args = p.parse_args()

    sa_path = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")
    sheet_id = os.environ.get("SEFAS_SPREADSHEET_ID",
                               "11jba-gDTFNhDowz4XlDb5rkJEQNy6bJqNKyUMH-iQVw")
    if not sa_path:
        sys.exit("❌  Defina GOOGLE_SERVICE_ACCOUNT_JSON no ambiente.")

    rows, invalidos = [], []
    with open(args.arquivo, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        cols = [c.lower().strip() for c in (reader.fieldnames or [])]
        if "telefone" not in cols or "nome" not in cols:
            sys.exit(f"❌  CSV precisa ter colunas 'nome' e 'telefone'. Encontrado: {cols}")
        for i, row in enumerate(reader, 1):
            nome = row.get("nome", "").strip()
            primeiro_nome = nome.split()[0] if nome else ""
            tel = normalizar_tel(row.get("telefone", ""))
            orig = row.get("origem", args.origem).strip() or args.origem
            if not tel:
                invalidos.append((i, row.get("telefone", "")))
                continue
            rows.append((nome, primeiro_nome, tel, orig))

    # Deduplica no CSV
    seen, rows_dedup = set(), []
    for r in rows:
        if r[2] not in seen:
            seen.add(r[2])
            rows_dedup.append(r)

    lote = rows_dedup[: args.piloto] if args.piloto > 0 else rows_dedup

    print(f"\n📋  Total no CSV:    {len(rows)}")
    print(f"🔄  Deduplicados:   {len(rows_dedup)}")
    print(f"🚫  Inválidos:      {len(invalidos)}")
    print(f"📦  Lote a inserir: {len(lote)}")
    if invalidos:
        print(f"⚠  Inválidos (linhas): {invalidos[:10]}")

    confirm = input(f"\nImportar {len(lote)} leads para Google Sheets? [s/N] ").strip().lower()
    if confirm != "s":
        print("Cancelado.")
        return

    creds = Credentials.from_service_account_file(sa_path, scopes=SCOPES)
    gc = gspread.authorize(creds)
    sh = gc.open_by_key(sheet_id)
    try:
        ws = sh.worksheet(SHEET_TAB)
    except gspread.WorksheetNotFound:
        ws = sh.add_worksheet(SHEET_TAB, rows=5000, cols=10)
        ws.append_row(["Status", "Ação Envio", "Variação", "Primeiro Nome",
                        "Telefone", "Origem", "Plano Indicado", "Data Envio", "remoteJid"])
        print(f"   ℹ  Aba '{SHEET_TAB}' criada com cabeçalho.")

    # Pega telefones existentes para deduplicar contra a planilha
    existing = set()
    all_rows = ws.get_all_records()
    for r in all_rows:
        tel = str(r.get("Telefone", "")).strip()
        if tel:
            existing.add(tel)

    inseridos = 0
    for i, (nome, primeiro_nome, tel, orig) in enumerate(lote):
        if tel in existing:
            continue
        variacao = "S1" if i % 2 == 0 else "S2"
        ws.append_row([
            "pendente", "SIM", variacao, primeiro_nome,
            tel, orig, "a_confirmar", "", ""
        ])
        existing.add(tel)
        inseridos += 1

    print(f"\n✅  Inseridos: {inseridos}")
    print(f"⏭  Ignorados (já existiam): {len(lote) - inseridos}")
    print(f"\nAgora ative o workflow n8n 'SEFAS — OUTBOUND CAMPANHA' para iniciar os envios.")

if __name__ == "__main__":
    main()
