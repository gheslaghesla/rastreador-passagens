"""Coleta diária de preços de passagens (ida e volta) GRU/POA -> Texas via Google Flights.

Grava data/AAAA-MM-DD.json com o menor preço por combinação, atualiza data/dias.json
(lista de dias usada pelo dashboard) e envia email se algo ficar abaixo do limite.
Email: variáveis GMAIL_USER, GMAIL_APP_PASSWORD e ALERT_TO (opcional, padrão GMAIL_USER).
"""
import json
import os
import re
import smtplib
import sys
import time
from datetime import datetime
from email.mime.text import MIMEText
from html import escape
from pathlib import Path
from zoneinfo import ZoneInfo

from urllib.parse import urlencode

from fast_flights import FlightData, Passengers
from fast_flights.core import fetch, parse_response
from fast_flights.filter import TFSData
from playwright.sync_api import sync_playwright

ORIGENS = ["GRU", "POA"]
DESTINOS = ["IAH", "HOU", "DFW", "DAL", "AUS", "SAT", "ELP"]
IDAS = ["2027-04-23", "2027-04-24", "2027-04-25"]
VOLTAS = ["2027-05-02", "2027-05-03"]
LIMITE = 4000

DATA_DIR = Path(__file__).parent / "data"


def preco(texto):
    n = re.sub(r"[^\d]", "", texto or "")
    return int(n) if n else None


def _tfs(origem, destino, ida, volta):
    return TFSData.from_interface(
        flight_data=[
            FlightData(date=ida, from_airport=origem, to_airport=destino),
            FlightData(date=volta, from_airport=destino, to_airport=origem),
        ],
        trip="round-trip",
        seat="economy",
        passengers=Passengers(adults=1),
    ).as_b64().decode()


class Navegador:
    """Chromium headless compartilhado, usado quando o HTML simples vem sem resultados."""

    def __init__(self):
        self._pw = None
        self._browser = None

    def html(self, params):
        if not self._browser:
            self._pw = sync_playwright().start()
            self._browser = self._pw.chromium.launch()
        page = self._browser.new_page(locale="en-US")
        try:
            page.goto("https://www.google.com/travel/flights?" + urlencode(params))
            page.locator(".eQ35Ce").wait_for(timeout=90000)
            page.wait_for_timeout(2000)
            return page.evaluate("() => document.querySelector('[role=main]').innerHTML")
        finally:
            page.close()

    def fechar(self):
        if self._browser:
            self._browser.close()
            self._pw.stop()


def buscar(nav, origem, destino, ida, volta):
    params = {"tfs": _tfs(origem, destino, ida, volta), "hl": "en", "tfu": "EgQIABABIgA", "curr": "BRL"}
    try:
        r = parse_response(fetch(params))
    except (AssertionError, RuntimeError):
        # Google às vezes só entrega os voos depois de rodar JavaScript
        class Resp:
            text = nav.html(params)
        r = parse_response(Resp)
    voos = [v for v in r.flights if preco(v.price)]
    if not voos:
        return None
    v = min(voos, key=lambda v: preco(v.price))
    return {
        "preco": preco(v.price),
        "cia": v.name,
        "paradas": v.stops,
        "duracao": v.duration,
        "partida": v.departure,
        "nivel": r.current_price,  # low / typical / high segundo o Google
    }


def brl(n):
    return f"{n:,}".replace(",", ".")


def dm(iso):
    return f"{iso[8:10]}/{iso[5:7]}"


def link(r):
    q = f"Flights from {r['origem']} to {r['destino']} on {r['ida']} through {r['volta']}"
    return "https://www.google.com/travel/flights?" + urlencode({"hl": "pt-BR", "curr": "BRL", "q": q})


def enviar_alerta(abaixo, hoje):
    usuario, senha = os.environ.get("GMAIL_USER"), os.environ.get("GMAIL_APP_PASSWORD")
    if not (usuario and senha):
        print("Email não configurado (GMAIL_USER/GMAIL_APP_PASSWORD); alerta não enviado.", file=sys.stderr)
        return
    dest = os.environ.get("ALERT_TO") or usuario
    painel = os.environ.get("DASHBOARD_URL", "")
    b = abaixo[0]
    linhas = "".join(
        f"<tr><td><b>{r['origem']} → {r['destino']}</b></td><td>{dm(r['ida'])}</td><td>{dm(r['volta'])}</td>"
        f"<td>{escape(r['cia'])}</td><td>{'direto' if r['paradas'] == 0 else r['paradas']}</td>"
        f"<td>{escape(r['duracao'])}</td><td><b>R$ {brl(r['preco'])}</b></td>"
        f"<td><a href=\"{link(r)}\">ver</a></td></tr>"
        for r in abaixo
    )
    html = f"""<p>A coleta de {dm(hoje)} encontrou {len(abaixo)} opção(ões) abaixo de R$ {brl(LIMITE)}:</p>
<table border="1" cellpadding="6" cellspacing="0" style="border-collapse:collapse;font-family:sans-serif;font-size:14px">
<tr><th>Rota</th><th>Ida</th><th>Volta</th><th>Companhia</th><th>Paradas</th><th>Duração</th><th>Preço</th><th></th></tr>
{linhas}</table>
<p>{f'<a href="{painel}">Abrir o dashboard</a>. ' if painel else ''}Preços mudam rápido: confirme no site antes de comprar.</p>"""
    msg = MIMEText(html, "html", "utf-8")
    msg["Subject"] = f"✈️ Passagem para o Texas abaixo de R$ {brl(LIMITE)}: R$ {brl(b['preco'])} ({b['origem']}→{b['destino']})"
    msg["From"], msg["To"] = usuario, dest
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as smtp:
        smtp.login(usuario, senha)
        smtp.send_message(msg)
    print(f"Alerta enviado para {dest}.", file=sys.stderr)


def main():
    hoje = datetime.now(ZoneInfo("America/Sao_Paulo")).date().isoformat()
    DATA_DIR.mkdir(exist_ok=True)
    resultados, erros = [], []
    nav = Navegador()
    for o in ORIGENS:
        for d in DESTINOS:
            for ida in IDAS:
                for volta in VOLTAS:
                    chave = f"{o}-{d} {ida}/{volta}"
                    try:
                        res = buscar(nav, o, d, ida, volta)
                    except Exception as e:  # noqa: BLE001
                        erros.append(f"{chave}: {str(e)[:200]}")
                        res = None
                    if res:
                        resultados.append({"origem": o, "destino": d, "ida": ida, "volta": volta, **res})
                    print(chave, res["preco"] if res else "-", file=sys.stderr)
                    time.sleep(1.5)
    nav.fechar()
    for e in erros:
        print("ERRO", e, file=sys.stderr)

    if not resultados:
        sys.exit("Nenhum preço coletado hoje; nada foi gravado.")

    resultados.sort(key=lambda r: r["preco"])
    dia = {"data": hoje, "resultados": resultados, "erros": len(erros)}
    (DATA_DIR / f"{hoje}.json").write_text(json.dumps(dia, ensure_ascii=False, indent=1))
    dias = sorted(p.stem for p in DATA_DIR.glob("20*.json"))
    (DATA_DIR / "dias.json").write_text(json.dumps(dias))

    b = resultados[0]
    print(f"{len(resultados)} preços, {len(erros)} falhas. Melhor: R$ {b['preco']} {b['origem']}-{b['destino']} "
          f"{b['ida']}/{b['volta']} {b['cia']}", file=sys.stderr)
    abaixo = [r for r in resultados if r["preco"] < LIMITE]
    if abaixo:
        enviar_alerta(abaixo, hoje)


if __name__ == "__main__":
    main()
