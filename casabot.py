#!/usr/bin/env python3
"""BuscaPiso Alicante: vigila alquiler de larga temporada en Alicante ciudad
y Santa Pola (zona del polígono dibujado en el enlace de Idealista que dio
el usuario, decodificado localmente ya que Idealista bloquea el acceso
automatizado). Avisa por Telegram de pisos NUEVOS, BAJADAS de precio y
pisos que VUELVEN a estar disponibles tras desaparecer.

Fuentes activas (sin necesitar huella de Chrome ni ninguna técnica de
evasión deliberada):
  - Milanuncios: JSON embebido en la propia página (window.__INITIAL_PROPS__).
    OJO: sí tiene protección (PerimeterX, página "Pardon Our Interruption"),
    solo que no salta con 1-2 peticiones sueltas; saltó en pruebas manuales
    en ráfaga. Un poll real de una vez por hora es mucho más suave, pero no
    hay garantía de que nunca la dispare — por eso el ritmo entre páginas es
    deliberadamente lento y cualquier bloqueo se distingue en el log de un
    cambio real de layout.
  - pisos.com: accesible con Playwright normal. Tiene URLs limpias y
    reutilizables con los filtros de precio y "alquiler residencial de larga
    estancia" ya aplicados (ver config.json).

Fuentes bloqueadas de forma dura e inmediata (DataDome/CloudFront/similar),
pendientes de que el usuario decida cómo abordarlas: Idealista (confirmado
403 incluso con Playwright honesto, sin huella), Habitaclia, Fotocasa,
Yaencontre. Ver PROYECTO.md.
"""
import html
import json
import os
import re
import sys
import time

import requests
from playwright.sync_api import sync_playwright

from notifier import Notifier


def _esc(s):
    """Escapa para Telegram (parse_mode=HTML). Título y municipio vienen de
    anuncios scrapeados (texto que no controlamos); si uno trae un '<' o '&'
    suelto rompe el parseo HTML y el aviso entero no se entrega. Mismo bug ya
    encontrado y arreglado en botviajes/engine.py — aplicarlo aquí también."""
    return html.escape(str(s or ""), quote=False)

RAIZ = os.path.dirname(os.path.abspath(__file__))
HISTORICO = os.path.join(RAIZ, "historico.json")
CONFIG = os.path.join(RAIZ, "config.json")
UMBRAL = 1.0  # € de bajada mínima para que merezca un aviso
MAX_PAGINAS_MILANUNCIOS = 40  # salvaguarda: la provincia entera son ~28 hoy
MAX_PAGINAS_PISOSCOM = 20  # salvaguarda: hoy son ~10 páginas por zona

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36")


def cargar_config():
    with open(CONFIG, encoding="utf-8") as fh:
        return json.load(fh)


# ---- Milanuncios ------------------------------------------------------------

_RE_PROPS = re.compile(r'window\.__INITIAL_PROPS__ = JSON\.parse\("(.*?)"\);</script>', re.DOTALL)


def _milanuncios_pagina(n):
    url = "https://www.milanuncios.com/alquiler-de-pisos-en-alicante/"
    if n > 1:
        url += "?pagina=%d" % n
    r = requests.get(url, headers={"User-Agent": UA}, timeout=25)
    if not r.ok:
        print("  [milanuncios] HTTP %s en página %d" % (r.status_code, n))
        return [], 0
    m = _RE_PROPS.search(r.text)
    if not m:
        if "pardon our interruption" in r.text.lower() or "press & hold" in r.text.lower():
            print("  [milanuncios] bloqueado por protección anti-bot (PerimeterX) en la "
                  "página %d; no es un cambio de layout, se reintenta en el próximo ciclo" % n)
        else:
            print("  [milanuncios] no encontré window.__INITIAL_PROPS__ en la página %d "
                  "(¿cambió el layout?)" % n)
        return [], 0
    try:
        data = json.loads(m.group(1).encode().decode("unicode_escape"))
    except Exception as e:
        print("  [milanuncios] no pude parsear el JSON embebido: %s" % e)
        return [], 0
    pag = data.get("adListPagination") or {}
    ads = ((pag.get("adList") or {}).get("ads")) or []
    total_paginas = ((pag.get("pagination") or {}).get("totalPages")) or 1
    return ads, total_paginas


def buscar_milanuncios(municipios_validos):
    out = []
    ads, total = _milanuncios_pagina(1)
    paginas = min(total, MAX_PAGINAS_MILANUNCIOS)
    todas = list(ads)
    for n in range(2, paginas + 1):
        more, _ = _milanuncios_pagina(n)
        if not more:
            break
        todas.extend(more)
        time.sleep(1.5)
    for a in todas:
        ciudad = ((a.get("location") or {}).get("city") or {}).get("name") or ""
        if not any(muni.lower() in ciudad.lower() or ciudad.lower() in muni.lower()
                  for muni in municipios_validos):
            continue
        precio = ((a.get("price") or {}).get("cashPrice") or {}).get("value")
        if precio is None:
            continue
        tags = {t.get("type"): t.get("text") for t in (a.get("tags") or [])}
        hab = tags.get("dormitorios")
        m2_txt = tags.get("metros cuadrados") or ""
        m2 = re.search(r"\d+", m2_txt)
        out.append({
            "fuente": "milanuncios",
            "id": "milanuncios-%s" % a.get("id"),
            "titulo": a.get("seoTitle") or a.get("title") or ciudad,
            "municipio": ciudad,
            "precio": float(precio),
            "habitaciones": int(hab) if hab and hab.isdigit() else None,
            "m2": int(m2.group()) if m2 else None,
            "url": "https://www.milanuncios.com" + (a.get("url") or ""),
        })
    return out


# ---- pisos.com ---------------------------------------------------------------

_RE_PRECIO = re.compile(r"([\d.,]+)\s*€/mes")
_RE_HAB = re.compile(r"(\d+)\s*habs?\.")
_RE_M2 = re.compile(r"(\d+)\s*m²")
_RE_ZONA = re.compile(r"\(([^()]*)\)")


def _pisos_pagina(pg, url_base, n):
    url = url_base if n == 1 else (url_base.rstrip("/") + "/%d/" % n)
    pg.goto(url, timeout=45000, wait_until="domcontentloaded")
    pg.wait_for_timeout(3000)
    return pg.locator(".ad-preview").all()


def buscar_pisos_com(urls, municipios_validos, precio_max):
    out = []
    with sync_playwright() as p:
        b = p.chromium.launch(headless=True)
        pg = b.new_page(user_agent=UA)
        for url_base in urls:
            n = 1
            while n <= MAX_PAGINAS_PISOSCOM:
                try:
                    tarjetas = _pisos_pagina(pg, url_base, n)
                except Exception as e:
                    print("  [pisos.com] error en %s página %d: %s" % (url_base, n, str(e)[:120]))
                    break
                if not tarjetas:
                    break
                alguno_en_presupuesto = False
                for c in tarjetas:
                    try:
                        t = " ".join(c.inner_text().split())
                    except Exception:
                        continue
                    mp = _RE_PRECIO.search(t)
                    if not mp:
                        continue
                    precio = float(mp.group(1).replace(".", "").replace(",", "."))
                    if precio <= precio_max:
                        alguno_en_presupuesto = True
                    mh = _RE_HAB.search(t)
                    mm = _RE_M2.search(t)
                    # El municipio real va SIEMPRE entre el título y "N hab."
                    # (p.ej. "... Centro-Zona Alta (Alcoi - Alcoy) 1 hab. ...").
                    # Buscar el paréntesis en toda la tarjeta es un error real
                    # ya detectado: la descripción libre de después puede
                    # mencionar la PROVINCIA entre paréntesis ("ibi (alicante)")
                    # y eso colaba un piso de Ibi como si fuera de Alicante
                    # ciudad. Cortar antes de "hab." lo evita.
                    corte = mh.start() if mh else len(t)
                    zonas = _RE_ZONA.findall(t[:corte])
                    municipio = zonas[-1] if zonas else ""
                    if municipios_validos and not any(
                            muni.lower() in municipio.lower() for muni in municipios_validos):
                        continue
                    href = None
                    try:
                        href = c.locator("a").first.get_attribute("href")
                    except Exception:
                        pass
                    if not href:
                        continue
                    titulo = t.split(municipio)[0].strip() if municipio else t[:60]
                    out.append({
                        "fuente": "pisos.com",
                        "id": "pisos.com-%s" % href,
                        "titulo": titulo[:120],
                        "municipio": municipio,
                        "precio": precio,
                        "habitaciones": int(mh.group(1)) if mh else None,
                        "m2": int(mm.group(1)) if mm else None,
                        "url": "https://www.pisos.com" + href,
                    })
                # pisos.com no avisa de "última página" con un código especial:
                # si una página no trae 30 tarjetas, ya no hay más.
                if len(tarjetas) < 30:
                    break
                # Las URLs llevan /asc/ (precio ascendente) precisamente para
                # esto: pisos.com deja de respetar el filtro "hasta-750" a
                # partir de cierta página (se ha visto devolver pisos de
                # hasta 3.000 €/mes ahí), pero ordenado ascendente, si en TODA
                # una página no hay ni un piso dentro de presupuesto, ya no lo
                # va a haber en las siguientes: se para en vez de seguir
                # escaneando páginas irrelevantes hasta el límite de seguridad.
                if not alguno_en_presupuesto:
                    break
                n += 1
                time.sleep(1)
        b.close()
    return out


# ---- filtros y dedupe cruzado -------------------------------------------------

def aplica_filtros(p, cfg):
    if not (0 < p["precio"] <= cfg["precio_max"]):
        return False        # 0 €, negativo o NaN: no es un precio real
    if cfg.get("habitaciones_min") and (p["habitaciones"] or 0) < cfg["habitaciones_min"]:
        return False
    if cfg.get("m2_min") and (p["m2"] or 0) < cfg["m2_min"]:
        return False
    return True


def quitar_duplicados_cruzados(pisos):
    """Mismo precio + mismo municipio + mismas habitaciones + mismos m² en la
    MISMA pasada => casi seguro es el mismo piso publicado en dos portales.
    No es perfecto (no hay forma de comparar direcciones exactas sin pagar un
    geocodificador), pero evita el caso más habitual: la misma inmobiliaria
    publica el mismo piso en Milanuncios y en pisos.com el mismo día."""
    vistos = {}
    out = []
    for p in pisos:
        clave = (round(p["precio"]), p["municipio"].lower().strip(),
                 p["habitaciones"], p["m2"])
        if clave in vistos:
            continue
        vistos[clave] = p["id"]
        out.append(p)
    return out


def main():
    cfg = cargar_config()
    try:
        with open(HISTORICO, encoding="utf-8") as fh:
            historico = json.load(fh)
    except FileNotFoundError:
        historico = {}
    except Exception as e:
        # Si el fichero existe pero está corrupto (truncado a media escritura)
        # y esto fallara en silencio, TODOS los pisos ya conocidos se tratarían
        # como nuevos en este ciclo. No es grave (solo repite avisos), pero
        # debe quedar visible en el log, no silenciado como si no pasara nada.
        print("  [aviso] historico.json no se pudo leer (%s); empiezo de cero" % e)
        historico = {}

    notifier = Notifier()
    destino = os.environ.get("TELEGRAM_CHAT_ID", "")

    print("  buscando en Milanuncios...")
    pisos_milanuncios = buscar_milanuncios(cfg["zonas"]["municipios_validos"])
    print("  [milanuncios] %d anuncios en zona" % len(pisos_milanuncios))

    print("  buscando en pisos.com...")
    pisos_pisoscom = buscar_pisos_com(cfg["pisos_com_urls"], cfg["zonas"]["municipios_validos"],
                                      cfg["precio_max"])
    print("  [pisos.com] %d anuncios en zona" % len(pisos_pisoscom))

    todos = [p for p in (pisos_milanuncios + pisos_pisoscom) if aplica_filtros(p, cfg)]
    todos = quitar_duplicados_cruzados(todos)
    print("  %d anuncios tras filtros y deduplicado cruzado" % len(todos))

    avisos = []
    vistos_ahora = set()
    for p in todos:
        vistos_ahora.add(p["id"])
        anterior = historico.get(p["id"])
        titulo = _esc(p["titulo"])
        pp_hab = ("%d hab." % p["habitaciones"]) if p["habitaciones"] else "hab. sin especificar"
        pp_m2 = ("%d m²" % p["m2"]) if p["m2"] else "m² sin especificar"
        ficha = "%s · %s · %s" % (_esc(p["municipio"]), pp_hab, pp_m2)

        if anterior is None:
            historico[p["id"]] = {
                "precio": p["precio"], "minimo": p["precio"], "maximo": p["precio"],
                "suelo_avisado": p["precio"], "activo": True, "fuente": p["fuente"],
                "titulo": p["titulo"], "url": p["url"], "municipio": p["municipio"],
                "visto": time.strftime("%Y-%m-%d %H:%M"),
            }
            avisos.append("🆕 <b>Piso nuevo</b> (%s)\n<b>%s</b>\n%s\n%.0f €/mes\n👉 <a href=\"%s\">Ver anuncio</a>"
                          % (p["fuente"], titulo, ficha, p["precio"], p["url"]))
            continue

        if not anterior.get("activo", True):
            # Había desaparecido de la búsqueda y ha vuelto a aparecer.
            anterior["activo"] = True
            avisos.append("🔁 <b>Disponible de nuevo</b> (%s)\n<b>%s</b>\n%s\n%.0f €/mes\n👉 <a href=\"%s\">Ver anuncio</a>"
                          % (p["fuente"], titulo, ficha, p["precio"], p["url"]))

        precio_viejo = anterior["precio"]
        anterior["maximo"] = max(anterior.get("maximo", precio_viejo), p["precio"])
        suelo = anterior.get("suelo_avisado", precio_viejo)
        if p["precio"] < suelo - UMBRAL:
            anterior["minimo"] = min(anterior.get("minimo", precio_viejo), p["precio"])
            anterior["suelo_avisado"] = p["precio"]
            avisos.append("📉 <b>Bajada de precio</b> (%s)\n<b>%s</b>\n%s\nAntes %.0f € → ahora <b>%.0f €</b>\n"
                          "Mínimo visto: %.0f €\n👉 <a href=\"%s\">Ver anuncio</a>"
                          % (p["fuente"], titulo, ficha, precio_viejo, p["precio"],
                             anterior["minimo"], p["url"]))
        anterior["precio"] = p["precio"]
        anterior["visto"] = time.strftime("%Y-%m-%d %H:%M")
        anterior["activo"] = True

    # Lo que estaba activo y hoy no ha aparecido: se marca inactivo (no se
    # avisa de la baja, solo se usa para detectar si "vuelve" más adelante).
    for pid, info in historico.items():
        if pid not in vistos_ahora and info.get("activo", True):
            info["activo"] = False
            info["baja_vista"] = time.strftime("%Y-%m-%d %H:%M")

    tmp = HISTORICO + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(historico, fh, ensure_ascii=False, indent=1)
    os.replace(tmp, HISTORICO)

    if not pisos_milanuncios and not pisos_pisoscom:
        print("  CEGUERA: 0 anuncios en ambas fuentes, algo se rompió")
        if destino:
            notifier.telegram(destino, "⚠️ <b>BuscaPiso no ve nada</b>\nMilanuncios y pisos.com han "
                                        "devuelto 0 anuncios a la vez. Puede que hayan cambiado el "
                                        "layout o esté bloqueado.")
        return 1

    if not avisos:
        print("  sin novedades (%d anuncios vigilados)" % len(todos))
    elif not destino:
        print("  %d avisos generados pero TELEGRAM_CHAT_ID no está configurado" % len(avisos))
    else:
        texto = "\n\n".join(avisos[:8])
        if len(avisos) > 8:
            texto += "\n\n... y %d más." % (len(avisos) - 8)
        ok = notifier.telegram(destino, texto)
        print("  %d avisos (%d anuncios vigilados) -> enviado: %s" % (len(avisos), len(todos), ok))
        if not ok:
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
