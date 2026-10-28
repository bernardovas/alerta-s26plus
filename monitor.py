#!/usr/bin/env python3
"""
Alerta de preço do Samsung Galaxy S26+ com aviso no Telegram.

Roda a cada hora pelo Agendador de Tarefas do Windows. Para cada loja configurada em config.json:
  1. encontra os anúncios do S26+ (páginas conhecidas + busca na loja);
  2. aplica os filtros (modelo, armazenamento, cor, aparelho novo, vendedor oficial);
  3. avisa no Telegram quando algum anúncio válido fica abaixo do preço máximo.

O estado (o que já foi avisado e a saúde de cada loja) fica em state.json,
para não repetir o mesmo alerta toda hora.

Os segredos (token e chat do Telegram) só são lidos de variáveis de ambiente
(ou do arquivo local segredos.env) e nunca são impressos nos logs.
"""

from __future__ import annotations

import json
import os
import random
import re
import time
import unicodedata
from datetime import datetime, timedelta, timezone
from html import escape
from pathlib import Path
from urllib.parse import quote, urljoin

from bs4 import BeautifulSoup
from curl_cffi import requests as creq

ROOT = Path(__file__).resolve().parent
CONFIG = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
STATE_PATH = ROOT / "state.json"
DEBUG_DIR = ROOT / "debug"

DEBUG_HTML = os.getenv("DEBUG_HTML") == "1"
TEST_MODE = os.getenv("TEST_MODE") == "1"

BRT = timezone(timedelta(hours=-3))
LIMITE = float(CONFIG["preco_maximo"])
ARMAZ_OK = {a.upper() for a in CONFIG["armazenamentos_aceitos"]}

# --------------------------------------------------------------------------- #
# Utilidades de texto e preço
# --------------------------------------------------------------------------- #


def norm(s: str | None) -> str:
    """Minúsculas e sem acentos, para comparar textos."""
    s = unicodedata.normalize("NFKD", s or "")
    return "".join(c for c in s if not unicodedata.combining(c)).lower()


def brl(valor) -> float | None:
    """Converte '5.012,20', 'R$ 4.999', 4999 ou '4999.00' em float."""
    if valor is None:
        return None
    if isinstance(valor, (int, float)):
        return float(valor)
    s = str(valor).replace("\xa0", " ").strip()
    if re.fullmatch(r"\d+(\.\d+)?", s):
        return float(s)
    m = re.search(r"(\d{1,3}(?:\.\d{3})+|\d+)(?:,(\d{1,2}))?", s)
    if not m:
        return None
    inteiro = m.group(1).replace(".", "")
    cent = (m.group(2) or "0").ljust(2, "0")
    return float(f"{inteiro}.{cent}")


def fmt_brl(v: float) -> str:
    return "R$ " + f"{v:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


RX_COR_EXCLUIDA = re.compile(
    r"\b(" + "|".join(re.escape(norm(c)) for c in CONFIG["cores_excluidas"]) + r")\b"
)

# Termos que indicam aparelho não novo. Valem para título, condição e cor.
RX_NAO_NOVO_TITULO = [
    r"\busad[oa]s?\b", r"semi-?nov[oa]", r"recondicionad", r"renovad[oa]", r"\brefurbish",
    r"vitrine", r"mostruario", r"open ?box", r"caixa aberta", r"embalagem aberta",
    r"reembalad", r"de exposicao", r"\bavariad", r"\boutlet\b", r"\bgrade [abc]\b",
    r"saude da bateria", r"bateria (com|em|a) \d{2}\s?%", r"\d{2}\s?% de saude",
    r"\bexcelente estado\b", r"\bseminovo\b", r"\bpouco uso\b",
]
# Para descrições longas usamos só os termos fortes: o texto de marketing
# fala em "% de bateria em 30 minutos", o que daria falso positivo.
RX_NAO_NOVO_DESC = [
    r"\bproduto usado\b", r"\baparelho usado\b", r"semi-?novo", r"recondicionad",
    r"vitrine", r"mostruario", r"open ?box", r"caixa aberta", r"embalagem aberta",
    r"reembalad", r"saude da bateria", r"\bpouco uso\b",
]


def eh_s26_plus(titulo: str, url: str = "") -> bool:
    t = norm(titulo)
    u = norm(url)
    alvo = f"{t} {u}"
    if re.search(r"\bultra\b|\bedge\b|\bfe\b|capa|capinha|pelicula|case\b|carregador", t):
        return False
    if re.search(r"s26\s?(\+|plus)", t) or "s26-plus" in u or "s26plus" in alvo:
        return True
    # Alguns anúncios escrevem só "Galaxy S26 ... Tela 6.7": a tela de 6,7" é a do Plus.
    if re.search(r"galaxy\s?s26\b", t) and re.search(r"6[.,]7\b|6[.,]7\"|tela de 67\b", t):
        return True
    if "galaxy-s26" in u and re.search(r"tela-(de-)?6-?7\b|tela-de-67", u):
        return True
    return False


def armazenamento_de(texto: str) -> str | None:
    m = re.search(r"\b(128|256|512)\s?gb\b|\b1\s?tb\b", norm(texto))
    if not m:
        return None
    return (m.group(1) + "GB") if m.group(1) else "1TB"


def cor_no_titulo(titulo: str) -> str | None:
    cores = ["preto", "preta", "azul", "violeta", "dourado", "prata", "cinza", "verde",
             "branco", "branca", "rosa", "navy", "black", "silver", "blue", "violet",
             "gold", "white", "pink"]
    t = norm(titulo)
    for c in cores:
        if re.search(rf"\b{c}\b", t):
            return c.capitalize()
    return None


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #


class Bloqueado(Exception):
    """A loja respondeu com captcha, verificação ou bloqueio."""


# As lojas bloqueiam clientes HTTP simples; por isso as páginas são abertas
# num navegador de verdade (o Microsoft Edge do Windows) controlado pelo Playwright.
_PW = None
_CTX = None


def _contexto():
    global _PW, _CTX
    if _CTX is not None:
        return _CTX
    from playwright.sync_api import sync_playwright

    _PW = sync_playwright().start()
    oculto = os.getenv("NAVEGADOR_HEADLESS") == "1" or CONFIG.get("navegador_modo") == "headless"
    args = ["--disable-blink-features=AutomationControlled", "--window-size=1366,900"]
    if not oculto:
        # Janela real, mas posicionada fora da tela para não atrapalhar.
        args += ["--window-position=-32000,-32000"]
    base = dict(user_data_dir=str(ROOT / "perfil-navegador"), headless=oculto, locale="pt-BR",
                timezone_id="America/Sao_Paulo", viewport={"width": 1366, "height": 900},
                ignore_default_args=["--enable-automation"], args=args)
    ultimo_erro = None
    for canal in [CONFIG.get("navegador", "msedge"), "chrome", None]:
        try:
            kw = dict(base)
            if canal:
                kw["channel"] = canal
            _CTX = _PW.chromium.launch_persistent_context(**kw)
            print(f"Navegador: {canal or 'chromium'}")
            return _CTX
        except Exception as e:
            ultimo_erro = e
    raise RuntimeError(f"não consegui abrir o navegador: {ultimo_erro}")


def fechar_navegador() -> None:
    global _PW, _CTX
    try:
        if _CTX is not None:
            _CTX.close()
        if _PW is not None:
            _PW.stop()
    except Exception:
        pass
    _PW = _CTX = None


SINAIS_BLOQUEIO = [
    "/errors/validatecaptcha", "digite os caracteres que voce ve", "api-services-support@amazon",
    "account-verification", "gz/account-verification",
    "ops! algo deu errado", "access denied", "request unsuccessful", "px-captcha",
    "are you a robot", "voce e um robo",
]


def parece_bloqueio(html: str, url_final: str) -> bool:
    if "account-verification" in url_final or "/captcha" in url_final:
        return True
    cabeca = norm(html[:20000])
    if len(html) < 60000:
        return any(s in cabeca for s in SINAIS_BLOQUEIO)
    # Páginas grandes podem citar "captcha" em scripts; olhamos só sinais fortes.
    return "/errors/validatecaptcha" in cabeca or "account-verification" in cabeca


_contador_debug = 0
DIAGNOSTICO: list[dict] = []


def baixar(url: str, loja: str) -> tuple[int, str, str]:
    """Abre a página no navegador. Retorna (status, html, url_final). Lança Bloqueado se bloqueada."""
    global _contador_debug
    ctx = _contexto()
    pagina = ctx.pages[0] if ctx.pages else ctx.new_page()
    time.sleep(random.uniform(2.0, 4.5))
    try:
        resp = pagina.goto(url, wait_until="domcontentloaded", timeout=45000)
    except Exception as e:
        DIAGNOSTICO.append({"loja": loja, "url": url[:150], "erro": f"{type(e).__name__}: {e}"[:300]})
        raise Bloqueado(f"página não carregou ({type(e).__name__})") from None
    status = resp.status if resp else 0
    try:
        pagina.wait_for_load_state("networkidle", timeout=10000)
    except Exception:
        pass
    html = pagina.content()
    # Algumas lojas mostram uma verificação rápida e depois recarregam sozinhas.
    if (status in (403, 429, 503) or parece_bloqueio(html, pagina.url)) and "__NEXT_DATA__" not in html:
        pagina.wait_for_timeout(8000)
        html = pagina.content()
    final = pagina.url
    t = re.search(r"<title[^>]*>(.*?)</title>", html, re.S | re.I)
    pronto = "__NEXT_DATA__" in html or "productTitle" in html or len(html) > 150000
    bloqueado = parece_bloqueio(html, final) or (status in (403, 429, 503) and not pronto)
    DIAGNOSTICO.append({"loja": loja, "url": url[:150], "final": final[:150], "status": status,
                        "tamanho": len(html), "titulo": (t.group(1).strip()[:120] if t else ""),
                        "bloqueio": bloqueado})
    if DEBUG_HTML:
        DEBUG_DIR.mkdir(exist_ok=True)
        _contador_debug += 1
        (DEBUG_DIR / f"{loja}_{_contador_debug:02d}_{status}.html").write_text(
            f"<!-- {url} -> {final} -->\n" + html, encoding="utf-8")
    if bloqueado:
        raise Bloqueado(f"bloqueio (HTTP {status}) em {final[:120]}")
    return (200 if pronto and status >= 400 else status), html, final


def next_data(html: str) -> dict | None:
    m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.S)
    if not m:
        return None
    try:
        return json.loads(m.group(1))
    except json.JSONDecodeError:
        return None


def jsonld_produtos(soup: BeautifulSoup) -> list[dict]:
    produtos = []
    for s in soup.select('script[type="application/ld+json"]'):
        try:
            data = json.loads(s.string or s.get_text() or "null")
        except (json.JSONDecodeError, TypeError):
            continue
        pilha = [data]
        while pilha:
            x = pilha.pop()
            if isinstance(x, list):
                pilha.extend(x)
            elif isinstance(x, dict):
                tipo = x.get("@type")
                if tipo == "Product" or (isinstance(tipo, list) and "Product" in tipo):
                    produtos.append(x)
                pilha.extend(v for v in x.values() if isinstance(v, (list, dict)))
    return produtos


def ofertas_jsonld(prod: dict) -> list[dict]:
    o = prod.get("offers")
    if isinstance(o, dict):
        if o.get("@type") == "AggregateOffer" and isinstance(o.get("offers"), list):
            return o["offers"]
        return [o]
    return o if isinstance(o, list) else []


# --------------------------------------------------------------------------- #
# Modelo de anúncio
# --------------------------------------------------------------------------- #


def anuncio(loja, chave, titulo, url, preco=None, preco_desc="", vendedor="", cor=None,
            armazenamento=None, descricao="", condicao="", indisponivel=False):
    return {
        "loja": loja, "chave": f"{loja}:{chave}", "titulo": (titulo or "").strip(),
        "url": url, "preco": preco, "preco_desc": preco_desc, "vendedor": (vendedor or "").strip(),
        "cor": cor, "armazenamento": armazenamento or armazenamento_de(titulo or ""),
        "descricao": descricao or "", "condicao": condicao or "", "indisponivel": indisponivel,
    }


def avaliar(a: dict, vendedores_ok: list[str]) -> str | None:
    """Retorna o motivo da rejeição, ou None se o anúncio atende a todos os requisitos."""
    if not eh_s26_plus(a["titulo"], a["url"]):
        return "não é o S26+"
    if a["indisponivel"]:
        return "sem estoque"
    if a["armazenamento"] and a["armazenamento"].upper() not in ARMAZ_OK:
        return f"armazenamento {a['armazenamento']}"
    titulo_cel = a["titulo"].split(" + ")[0]  # em kits, ignora a cor do acessório
    texto = norm(" ".join([titulo_cel, a["cor"] or "", a["condicao"]]))
    if RX_COR_EXCLUIDA.search(texto):
        return "cor excluída"
    for rx in RX_NAO_NOVO_TITULO:
        if re.search(rx, texto):
            return "não é novo/lacrado"
    desc = norm(a["descricao"])
    for rx in RX_NAO_NOVO_DESC:
        if re.search(rx, desc):
            return "descrição indica que não é novo"
    vend = norm(a["vendedor"])
    if not vend:
        return "vendedor não identificado"
    if not any(norm(v) in vend for v in vendedores_ok):
        return f"vendedor não aceito ({a['vendedor'][:40]})"
    if a["preco"] is None:
        return "sem preço"
    return None


# --------------------------------------------------------------------------- #
# Lojas
# --------------------------------------------------------------------------- #


def loja_amazon(cfg: dict) -> tuple[list[dict], list[str]]:
    anuncios, erros = [], []
    sementes = list(dict.fromkeys(cfg["asins_semente"]))
    variantes: dict[str, tuple[str | None, str | None]] = {a: (None, None) for a in sementes}
    paginas: dict[str, str] = {}

    # 1) Descobre as variações (cor x armazenamento) a partir da primeira página que abrir.
    for asin in sementes:
        try:
            _, html, _ = baixar(f"https://www.amazon.com.br/dp/{asin}?th=1&psc=1", "amazon")
        except Bloqueado:
            raise  # se a primeira página já foi bloqueada, as outras também serão
        paginas[asin] = html
        m = re.search(r'"dimensionValuesDisplayData"\s*:\s*(\{[^{}]*\})', html)
        if m:
            try:
                for a, vals in json.loads(m.group(1)).items():
                    cor = vals[0] if len(vals) > 0 else None
                    arm = vals[1] if len(vals) > 1 else None
                    variantes[a] = (cor, arm)
            except (json.JSONDecodeError, IndexError):
                pass
            break

    # 2) Abre cada variação que não seja de cor excluída.
    for asin, (cor, arm) in variantes.items():
        if cor and RX_COR_EXCLUIDA.search(norm(cor)):
            continue
        if arm and arm.upper() not in ARMAZ_OK:
            continue
        html = paginas.get(asin)
        if html is None:
            try:
                _, html, _ = baixar(f"https://www.amazon.com.br/dp/{asin}?th=1&psc=1", "amazon")
            except Bloqueado as e:
                erros.append(str(e))
                break
        anuncios.append(_amazon_pagina(html, asin, cor, arm))
    return anuncios, erros


def _amazon_pagina(html: str, asin: str, cor_hint, arm_hint) -> dict:
    soup = BeautifulSoup(html, "lxml")
    titulo = (soup.select_one("#productTitle") or soup.select_one("title"))
    titulo = titulo.get_text(" ", strip=True) if titulo else ""

    preco = None
    for sel in [
        "#corePrice_feature_div .priceToPay",
        "#corePriceDisplay_desktop_feature_div .priceToPay",
        "#corePriceDisplay_desktop_feature_div .apexPriceToPay",
        ".apexPriceToPay",
        "#corePrice_feature_div .a-price:not(.a-text-price)",
        "#corePriceDisplay_desktop_feature_div .a-price:not(.a-text-price)",
    ]:
        el = soup.select_one(sel)
        if not el:
            continue
        off = el.select_one(".a-offscreen")
        txt = off.get_text(strip=True) if off else ""
        if not txt:
            inteiro = el.select_one(".a-price-whole")
            frac = el.select_one(".a-price-fraction")
            if inteiro:
                txt = inteiro.get_text(strip=True).rstrip(",") + "," + (frac.get_text(strip=True) if frac else "00")
        preco = brl(txt)
        if preco:
            break

    vendedor = ""
    blocos = []
    for sel in ["#merchantInfoFeature_feature_div", "#tabular-buybox", "#offerDisplayFeatures_desktop",
                '[offer-display-feature-name="desktop-merchant-info"]', "#merchant-info"]:
        el = soup.select_one(sel)
        if el:
            blocos.append(el.get_text("\n", strip=True))
    texto_vend = "\n".join(blocos)
    # Ex.: "Enviado / Vendido\nSamsung Loja Oficial" ou "Enviado por\nAmazon\nVendido por\nAmazon.com.br"
    for m in re.finditer(r"Vendido(?:\s+por)?\s*:?\s*([^\n]*)(?:\n([^\n]+))?", texto_vend, re.I):
        valor = (m.group(1) or "").strip(" /:")
        if not valor or norm(valor) in ("por", "/"):
            valor = (m.group(2) or "").strip(" /:")
        if valor and norm(valor) not in ("por", "enviado", "enviado por"):
            vendedor = valor
            break
    if not vendedor:
        el = soup.select_one("#sellerProfileTriggerId")
        if el:
            vendedor = el.get_text(" ", strip=True)

    cor = cor_hint
    el = soup.select_one("#variation_color_name .selection, #inline-twister-expanded-dimension-text-color_name")
    if el and el.get_text(strip=True):
        cor = el.get_text(strip=True)

    indisp = bool(soup.select_one("#outOfStock")) or (preco is None and "indispon" in norm(
        (soup.select_one("#availability") or soup).get_text(" ", strip=True)[:3000]))

    return anuncio("amazon", asin, titulo, f"https://www.amazon.com.br/dp/{asin}", preco=preco,
                   preco_desc="à vista", vendedor=vendedor, cor=cor or cor_no_titulo(titulo),
                   armazenamento=arm_hint or armazenamento_de(titulo), indisponivel=indisp)


MAGALU = "https://www.magazineluiza.com.br"


def loja_magalu(cfg: dict) -> tuple[list[dict], list[str]]:
    anuncios, erros = [], []
    aceitos = {norm(v) for v in cfg["vendedores_aceitos"]}
    caminhos = list(cfg.get("paths_semente", []))

    for termo in cfg.get("buscas", []):
        url = f"{MAGALU}/busca/{quote(termo, safe='').replace('%20', '+')}/"
        try:
            _, html, _ = baixar(url, "magalu")
        except Bloqueado as e:
            erros.append(str(e))
            continue
        nd = next_data(html) or {}
        itens = (((nd.get("props") or {}).get("pageProps") or {}).get("data") or {}).get("search") or {}
        for it in itens.get("items") or []:
            titulo, path = it.get("title") or "", it.get("path") or ""
            if not path or not eh_s26_plus(titulo, path):
                continue
            caminhos.append(path)

    vistos = set()
    for path in caminhos:
        base = path.split("?")[0]
        if base in vistos:
            continue
        vistos.add(base)
        try:
            a = _magalu_produto(base, aceitos)
            if a and a["vendedor"] and norm(a["vendedor"]) not in aceitos and "samsung" in aceitos:
                # A oferta principal é de outro vendedor: tenta a oferta da própria Samsung.
                b = _magalu_produto(base + "?seller_id=samsung", aceitos)
                if b and norm(b["vendedor"]) == "samsung":
                    a = b
            if a:
                anuncios.append(a)
        except Bloqueado as e:
            erros.append(str(e))
    return anuncios, erros


def _magalu_produto(path: str, aceitos: set[str]) -> dict | None:
    status, html, final = baixar(urljoin(MAGALU, path), "magalu")
    if status == 404:
        return None
    nd = next_data(html) or {}
    item = ((((nd.get("props") or {}).get("pageProps") or {}).get("data") or {}).get("item")) or {}
    if not item:
        return None
    titulo = item.get("title") or ""
    ofertas = item.get("offers") or []
    # Prefere uma oferta de vendedor aceito; senão, fica com a primeira (será rejeitada).
    oferta = next((o for o in ofertas if norm(((o.get("seller") or {}).get("id")) or "") in aceitos),
                  ofertas[0] if ofertas else {})
    seller = oferta.get("seller") or {}
    vendedor_id = seller.get("id") or seller.get("name") or ""

    candidatos = []
    if oferta.get("price"):
        candidatos.append((float(oferta["price"]), "no cartão"))
    bp = oferta.get("bestPrice") or {}
    if bp.get("totalAmount"):
        candidatos.append((float(bp["totalAmount"]), bp.get("paymentMethodDescription") or "melhor preço"))
    preco, desc = min(candidatos) if candidatos else (None, "")

    cor, arm = None, None
    for at in item.get("attributes") or []:
        rotulo = norm(f"{at.get('type', '')} {at.get('label', '')}")
        if "cor" in rotulo.split() or rotulo.startswith("cor"):
            cor = at.get("current")
        if "armazenamento" in rotulo:
            arm = at.get("current")
    cor = cor or cor_no_titulo(titulo)

    url_limpa = urljoin(MAGALU, path.split("?")[0])
    if norm(vendedor_id) == "samsung":
        url_limpa += "?seller_id=samsung"
    var = item.get("variationId") or item.get("id") or path
    return anuncio("magalu", f"{var}:{norm(vendedor_id)}", titulo, url_limpa, preco=preco,
                   preco_desc=desc, vendedor=vendedor_id, cor=cor, armazenamento=arm,
                   descricao=re.sub(r"<[^>]+>", " ", item.get("description") or ""),
                   indisponivel=item.get("available") is False)


def loja_mercadolivre(cfg: dict) -> tuple[list[dict], list[str]]:
    anuncios, erros = [], []
    for url in cfg.get("buscas", []):
        try:
            _, html, _ = baixar(url, "mercadolivre")
        except Bloqueado as e:
            erros.append(str(e))
            continue
        soup = BeautifulSoup(html, "lxml")
        for card in soup.select("li.ui-search-layout__item, div.poly-card"):
            t = card.select_one(".poly-component__title, .ui-search-item__title, h2, h3")
            link = card.select_one("a.poly-component__title, a.ui-search-link, a[href]")
            if not t or not link:
                continue
            titulo = t.get_text(" ", strip=True)
            href = link.get("href", "").split("#")[0]
            preco_el = (card.select_one(".poly-price__current")
                        or card.select_one(".ui-search-price__second-line")
                        or card.select_one(".andes-money-amount:not(.andes-money-amount--previous)"))
            preco = None
            if preco_el:
                frac = preco_el.select_one(".andes-money-amount__fraction")
                cents = preco_el.select_one(".andes-money-amount__cents")
                if frac is None and "andes-money-amount__fraction" in (preco_el.get("class") or []):
                    frac = preco_el
                if frac:
                    preco = brl(frac.get_text(strip=True) + "," + (cents.get_text(strip=True) if cents else "00"))
            vend_el = card.select_one(".poly-component__seller, .ui-search-official-store-label")
            vendedor = vend_el.get_text(" ", strip=True) if vend_el else ""
            if not vendedor and "_Loja_samsung" in url:
                vendedor = "Samsung (filtro de loja oficial)"
            cond_el = card.select_one(".poly-component__item-condition, .ui-search-item__group__element--condition")
            m = re.search(r"MLB-?\d+", href)
            anuncios.append(anuncio("mercadolivre", m.group(0).replace("-", "") if m else href[:80], titulo,
                                    href, preco=preco, preco_desc="à vista", vendedor=vendedor,
                                    cor=cor_no_titulo(titulo),
                                    condicao=cond_el.get_text(" ", strip=True) if cond_el else ""))

    for url in cfg.get("urls_semente", []):
        try:
            _, html, final = baixar(url, "mercadolivre")
        except Bloqueado as e:
            erros.append(str(e))
            continue
        a = _pagina_generica("mercadolivre", url, html)
        if a:
            anuncios.append(a)
    return anuncios, erros


def _pagina_generica(loja: str, url: str, html: str) -> dict | None:
    """Lê uma página de produto pelos dados estruturados (JSON-LD) e pelo texto visível."""
    soup = BeautifulSoup(html, "lxml")
    texto = soup.get_text("\n", strip=True)
    produtos = jsonld_produtos(soup)
    titulo, preco, vendedor, indisp, cor = "", None, "", False, None
    if produtos:
        p = produtos[0]
        titulo = p.get("name") or ""
        cor = p.get("color")
        precos = []
        for o in ofertas_jsonld(p):
            v = brl(o.get("price") or o.get("lowPrice"))
            if v:
                precos.append(v)
            av = str(o.get("availability") or "")
            if "OutOfStock" in av:
                indisp = True
            s = o.get("seller") or {}
            if isinstance(s, dict) and s.get("name"):
                vendedor = s["name"]
        preco = min(precos) if precos else None
    if not titulo:
        h1 = soup.select_one("h1")
        titulo = h1.get_text(" ", strip=True) if h1 else ""
    if preco is None:
        m = re.search(r"R\$\s?([\d.]+,\d{2})\s*(?:no pix|à vista|a vista)", texto, re.I)
        preco = brl(m.group(1)) if m else None
    if not vendedor:
        m = re.search(r"(?:Vendido e entregue por|Vendido por|Loja oficial)\s*:?\s*\n?\s*([^\n]{2,40})", texto, re.I)
        if m:
            vendedor = m.group(1)
            if "loja oficial" in m.group(0).lower():
                vendedor = "Loja oficial " + vendedor
    if re.search(r"não temos estoque|produto indisponível|esgotado", texto, re.I):
        indisp = True
    cond = ""
    m = re.search(r"\b(Novo|Usado|Recondicionado)\s*\|", texto)
    if m:
        cond = m.group(1)
    if not titulo:
        return None
    chave = re.search(r"/p/([A-Z]*\d+)", url)
    return anuncio(loja, chave.group(1) if chave else url[-60:], titulo, url.split("?")[0], preco=preco,
                   preco_desc="à vista", vendedor=vendedor, cor=cor or cor_no_titulo(titulo),
                   condicao=cond, indisponivel=indisp)


def loja_casasbahia(cfg: dict) -> tuple[list[dict], list[str]]:
    anuncios, erros = [], []
    urls = list(cfg.get("urls_semente", []))
    for busca in cfg.get("buscas", []):
        try:
            status, html, _ = baixar(busca, "casasbahia")
        except Bloqueado as e:
            erros.append(str(e))
            continue
        if status >= 400:
            continue
        achados = set(re.findall(r'href="(/[^"#?]*?/p/\d+)', html))
        achados |= {u.replace("https://www.casasbahia.com.br", "")
                    for u in re.findall(r'"(https://www\.casasbahia\.com\.br/[^"#?]+/p/\d+)', html)}
        for path in achados:
            if "galaxy-s26" in path and re.search(r"6-7", path) and not RX_COR_EXCLUIDA.search(path.replace("-", " ")):
                urls.append("https://www.casasbahia.com.br" + path)
    for url in dict.fromkeys(urls):
        try:
            status, html, _ = baixar(url, "casasbahia")
        except Bloqueado as e:
            erros.append(str(e))
            continue
        if status >= 400:
            continue
        a = _pagina_generica("casasbahia", url, html)
        if a:
            anuncios.append(a)
    return anuncios, erros


LOJAS = {
    "amazon": loja_amazon,
    "magalu": loja_magalu,
    "mercadolivre": loja_mercadolivre,
    "casasbahia": loja_casasbahia,
}

# --------------------------------------------------------------------------- #
# Telegram
# --------------------------------------------------------------------------- #


def segredos() -> tuple[str, str]:
    """Lê o token e o chat do Telegram das variáveis de ambiente ou do arquivo segredos.env."""
    valores = {"TELEGRAM_BOT_TOKEN": os.getenv("TELEGRAM_BOT_TOKEN", ""),
               "TELEGRAM_CHAT_ID": os.getenv("TELEGRAM_CHAT_ID", "")}
    arq = ROOT / "segredos.env"
    if arq.exists():
        for linha in arq.read_text(encoding="utf-8-sig").splitlines():
            if "=" in linha and not linha.lstrip().startswith("#"):
                k, v = linha.split("=", 1)
                if k.strip() in valores and not valores[k.strip()]:
                    valores[k.strip()] = v.strip().strip('"')
    return valores["TELEGRAM_BOT_TOKEN"].strip(), valores["TELEGRAM_CHAT_ID"].strip()


def telegram(texto: str) -> bool:
    token, chat = segredos()
    if not token or not chat:
        print("AVISO: token ou chat do Telegram não configurados (rode o instalar.bat); mensagem não enviada.")
        return False
    ok = True
    partes, atual = [], ""
    for linha in texto.split("\n"):
        if len(atual) + len(linha) + 1 > 3800:
            partes.append(atual)
            atual = ""
        atual += linha + "\n"
    partes.append(atual)
    for parte in partes:
        try:
            r = creq.post(f"https://api.telegram.org/bot{token}/sendMessage", timeout=30, json={
                "chat_id": chat, "text": parte, "parse_mode": "HTML", "disable_web_page_preview": True,
            })
            if r.status_code != 200:
                # O corpo da resposta do Telegram não contém o token.
                print(f"ERRO: Telegram respondeu {r.status_code}: {r.text[:300]}")
                ok = False
        except Exception as e:  # nunca imprime str(e): pode conter a URL com o token
            print(f"ERRO: falha ao falar com o Telegram ({type(e).__name__}).")
            ok = False
    return ok


def msg_resumo(agora: datetime, resumo: list, ofertas: list) -> str:
    """Mensagem enviada a cada execução: ofertas abaixo do limite em destaque + menor preço de cada loja."""
    titulo = "🧪 <b>Teste do alerta" if TEST_MODE else "📊 <b>Preços do"
    linhas = [f"{titulo} {escape(CONFIG['produto'])}</b>",
              f"Limite: {fmt_brl(LIMITE)} · {agora:%d/%m %H:%M}", ""]

    if ofertas:
        linhas += [f"🔥🔥🔥 <b>OFERTA ABAIXO DE {fmt_brl(LIMITE)}!</b> 🔥🔥🔥", ""]
        for a, nome in ofertas:
            linhas.append(f"🏬 <b>{escape(nome)}</b>: <b><u>{fmt_brl(a['preco'])}</u></b> {escape(a['preco_desc'])} · "
                          f"{escape(a['cor'] or 'cor ?')} · {escape(a['armazenamento'] or '?')}")
            if not a["cor"]:
                linhas.append("    ⚠️ <i>A loja não informa a cor. Confira se não é branco ou rosa.</i>")
            linhas.append(f"    👉 {escape(a['url'])}")
        linhas += ["", "━━━━━━━━━━━━━━━", ""]

    for nome, n_s26, n_ok, menor, erros in resumo:
        if n_s26:
            linhas.append(f"✅ <b>{escape(nome)}</b>: {n_s26} anúncio(s) do S26+ lidos, {n_ok} dentro das regras")
            if menor:
                fogo = " 🔥" if menor["preco"] < LIMITE else ""
                linhas.append(f"    ↳ menor: <b>{fmt_brl(menor['preco'])}</b> {escape(menor['preco_desc'])} · "
                              f"{escape(menor['cor'] or 'cor ?')} · {escape(menor['armazenamento'] or '?')}{fogo}\n"
                              f"    {escape(menor['url'])}")
        else:
            linhas.append(f"❌ <b>{escape(nome)}</b>: não consegui ler"
                          + (f" ({escape(erros[0][:80])})" if erros else ""))

    linhas += ["", f"🔥 Ofertas abaixo do limite: {len(ofertas)}" if ofertas
               else f"Nenhuma oferta abaixo de {fmt_brl(LIMITE)} por enquanto."]
    return "\n".join(linhas)


def msg_alerta(a: dict, nome_loja: str) -> str:
    linhas = [
        f"🔔 <b>{escape(CONFIG['produto'])} abaixo de {fmt_brl(LIMITE)}!</b>",
        "",
        f"🏬 <b>{escape(nome_loja)}</b>",
        f"💰 <b>{fmt_brl(a['preco'])}</b> {escape(a['preco_desc'])}",
        f"🎨 Cor: {escape(a['cor'] or 'não informada')}   💾 {escape(a['armazenamento'] or '?')}",
        f"🧾 Vendedor: {escape(a['vendedor'][:60])}",
        f"📦 {escape(a['titulo'][:150])}",
        "",
        f"🔗 {escape(a['url'])}",
    ]
    if not a["cor"]:
        linhas.insert(5, "⚠️ <i>A loja não informa a cor neste anúncio. Confira se não é branco ou rosa.</i>")
    return "\n".join(linhas)


# --------------------------------------------------------------------------- #
# Execução
# --------------------------------------------------------------------------- #


def carregar_estado() -> dict:
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _hora_de_enviar(agora: datetime) -> bool:
    """A tarefa do Windows dispara a cada 30 min (e ao voltar da suspensão ou ao entrar no Windows).
    O resumo só sai se já passou o intervalo configurado desde o último envio."""
    ultimo = carregar_estado().get("ultimo_resumo")
    if not ultimo:
        return True
    try:
        decorrido = agora - datetime.fromisoformat(ultimo)
    except ValueError:
        return True
    intervalo = timedelta(hours=float(CONFIG.get("intervalo_horas", 2))) - timedelta(minutes=15)
    if decorrido < intervalo:
        print(f"Último resumo há {int(decorrido.total_seconds() // 60)} min; próximo após "
              f"{int(intervalo.total_seconds() // 60)} min. Nada a fazer agora.")
        return False
    return True


def main() -> None:
    agora = datetime.now(BRT)
    if not TEST_MODE and not _hora_de_enviar(agora):
        return
    try:
        _executar(agora)
    finally:
        fechar_navegador()


def _executar(agora: datetime) -> None:
    estado = carregar_estado()
    estado.setdefault("avisados", {})
    estado.setdefault("lojas", {})
    limite_falhas = int(CONFIG.get("avisar_falha_apos_execucoes", 6))

    resumo = []
    ofertas = []

    for loja, fn in LOJAS.items():
        cfg = CONFIG["lojas"].get(loja) or {}
        if not cfg.get("ativo"):
            continue
        nome = cfg.get("nome", loja)
        print(f"\n=== {nome} ===")
        try:
            anuncios, erros = fn(cfg)
        except Bloqueado as e:
            anuncios, erros = [], [str(e)]
        except Exception as e:
            anuncios, erros = [], [f"erro inesperado: {type(e).__name__}: {e}"[:200]]

        # Remove repetidos (o mesmo anúncio pode vir da página e da busca).
        unicos: dict[str, dict] = {}
        for a in anuncios:
            atual = unicos.get(a["chave"])
            if atual is None or (a["preco"] or 1e12) < (atual["preco"] or 1e12):
                unicos[a["chave"]] = a
        anuncios = list(unicos.values())

        s26 = [a for a in anuncios if eh_s26_plus(a["titulo"], a["url"])]
        validos = []
        for a in anuncios:
            motivo = avaliar(a, cfg.get("vendedores_aceitos", []))
            preco = fmt_brl(a["preco"]) if a["preco"] else "sem preço"
            print(f"  {'OK ' if not motivo else '-- '} {preco:>14} | {a['cor'] or '?':<10} | "
                  f"{a['armazenamento'] or '?':<6} | {a['vendedor'][:28]:<28} | {motivo or 'válido'} | {a['titulo'][:70]}")
            if motivo is None:
                validos.append(a)
            elif a["chave"] in estado["avisados"]:
                del estado["avisados"][a["chave"]]  # deixou de valer: pode avisar de novo no futuro
        for e in erros:
            print(f"  !! {e}")
        DIAGNOSTICO.append({"loja": loja, "resumo": {"anuncios": len(anuncios), "erros": erros[:5],
                            "itens": [{"titulo": a["titulo"][:90], "preco": a["preco"], "vendedor": a["vendedor"][:40],
                                       "cor": a["cor"], "motivo": avaliar(a, cfg.get("vendedores_aceitos", [])) or "válido"}
                                      for a in anuncios[:25]]}})

        # Saúde da loja: sem nenhum anúncio do S26+ lido = falha (bloqueio ou página mudou).
        saude = estado["lojas"].setdefault(loja, {"falhas_seguidas": 0, "avisado": False})
        if s26:
            if saude.get("avisado"):
                telegram(f"✅ Voltei a conseguir ler a <b>{escape(nome)}</b>. O monitoramento dela está normal de novo.")
            saude.update(falhas_seguidas=0, avisado=False, ultimo_ok=agora.isoformat(timespec="minutes"))
        else:
            saude["falhas_seguidas"] = saude.get("falhas_seguidas", 0) + 1
            if saude["falhas_seguidas"] >= limite_falhas and not saude.get("avisado"):
                motivo = erros[0] if erros else "nenhum anúncio do S26+ encontrado (a página pode ter mudado)"
                telegram(
                    f"⚠️ Não consigo ler a <b>{escape(nome)}</b> há {saude['falhas_seguidas']} verificações seguidas.\n"
                    f"Motivo: {escape(motivo[:200])}\n"
                    "Sigo tentando e aviso quando voltar. As outras lojas continuam monitoradas."
                )
                saude["avisado"] = True

        menor = min(validos, key=lambda a: a["preco"], default=None)
        resumo.append((nome, len(s26), len(validos), menor, erros))

        for a in validos:
            if a["preco"] < LIMITE:
                ofertas.append((a, nome))

    ofertas.sort(key=lambda x: x[0]["preco"])
    nenhuma_lida = all(n_s26 == 0 for _, n_s26, _, _, _ in resumo)
    if nenhuma_lida and not TEST_MODE:
        # Provavelmente a internet ainda não voltou (ex.: logo após sair da suspensão).
        # Não envia e não marca o horário: tenta de novo na próxima checagem (30 min).
        print("Nenhuma loja respondeu; tento de novo na próxima checagem.")
        enviado = False
    else:
        enviado = telegram(msg_resumo(agora, resumo, ofertas))
    if enviado:
        estado["ultimo_resumo"] = agora.isoformat(timespec="minutes")

    if TEST_MODE:
        (ROOT / "diagnostico.json").write_text(json.dumps(DIAGNOSTICO, ensure_ascii=False, indent=1) + "\n",
                                              encoding="utf-8")

    estado["ultima_execucao"] = agora.isoformat(timespec="minutes")
    STATE_PATH.write_text(json.dumps(estado, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                          encoding="utf-8")
    print(f"\nOfertas abaixo do limite: {len(ofertas)} · resumo enviado: {enviado}")


if __name__ == "__main__":
    main()
