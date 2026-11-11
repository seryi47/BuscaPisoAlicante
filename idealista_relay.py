#!/usr/bin/env python3
"""Relé de alertas de Idealista: Idealista -> email -> Telegram.

Idealista bloquea de verdad el acceso automatizado a su web (confirmado con
un navegador honesto, sin ningún camuflaje: 403). Su API oficial es solo
para socios con aprobación, no hay autoservicio para un particular. La única
vía legítima que queda es la que Idealista mismo ofrece: guardar una
búsqueda en tu cuenta y activar sus alertas por email. Esto NO es scraping
ni evasión de ningún tipo: es la propia Idealista avisando a un usuario
registrado, tal y como está pensado.

Este script no toca idealista.com para nada. Entra por IMAP a TU Gmail
(con una contraseña de aplicación, no tu contraseña normal), busca los
correos que Idealista ya te mandó, saca de ahí el piso/precio/enlace, y
manda el mismo tipo de aviso que el resto del bot por Telegram. Lo nuevo
se marca como leído para no repetirlo.

Puesta en marcha (una sola vez, la hace el usuario):
  1. Entra en idealista.com con tu cuenta, haz la búsqueda con el filtro de
     zona + precio que ya usamos, y guárdala activando alertas por email.
  2. Activa la verificación en 2 pasos en tu cuenta de Google si no la
     tienes, y genera una "contraseña de aplicación" en
     myaccount.google.com/apppasswords (elige "Correo" como app).
  3. Variables de entorno: GMAIL_ADDRESS (tu Gmail) y GMAIL_APP_PASSWORD
     (la contraseña de 16 caracteres, SIN espacios).

El formato exacto del correo de Idealista no se ha visto todavía (primera
versión, a falta de un correo real de muestra): el parser busca enlaces a
"idealista.com/inmueble/" y el precio en € más cercano en el texto. Si el
formato real difiere, hay que afinar _parsear_html() con un correo real.
"""
import email
import imaplib
import json
import os
import re
import sys
import time

from bs4 import BeautifulSoup

from notifier import Notifier

RAIZ = os.path.dirname(os.path.abspath(__file__))
HISTORICO = os.path.join(RAIZ, "idealista_historico.json")
IMAP_HOST = "imap.gmail.com"
# Remitentes reales de Idealista (a confirmar/ampliar con un correo real).
REMITENTES = ("@idealista.com", "@mail.idealista.com", "@idealista.es")

_RE_PRECIO = re.compile(r"([\d.,]+)\s*€")
_RE_LINK = re.compile(r"https?://(?:www\.)?idealista\.com/inmueble/\d+[^\s\"'<>]*")


def _conectar():
    direccion = os.environ.get("GMAIL_ADDRESS", "").strip()
    clave = os.environ.get("GMAIL_APP_PASSWORD", "").strip()
    if not direccion or not clave:
        print("  [idealista_relay] faltan GMAIL_ADDRESS/GMAIL_APP_PASSWORD; se omite")
        return None
    m = imaplib.IMAP4_SSL(IMAP_HOST)
    m.login(direccion, clave)
    return m


def _buscar_no_leidos(m):
    m.select("INBOX")
    criterios = " ".join('FROM "%s"' % r for r in REMITENTES)
    # IMAP SEARCH no tiene "OR" encadenado simple en todos los servidores;
    # se busca por cada remitente por separado y se juntan los ids.
    ids = set()
    for remitente in REMITENTES:
        ok, datos = m.search(None, "UNSEEN", "FROM", '"%s"' % remitente)
        if ok == "OK" and datos and datos[0]:
            ids.update(datos[0].split())
    return sorted(ids, key=lambda x: int(x))


def _texto_plano(msg):
    """Prefiere el texto plano; si solo hay HTML, lo convierte a texto."""
    for parte in msg.walk():
        if parte.get_content_type() == "text/plain":
            try:
                return parte.get_payload(decode=True).decode(
                    parte.get_content_charset() or "utf-8", errors="replace")
            except Exception:
                continue
    for parte in msg.walk():
        if parte.get_content_type() == "text/html":
            try:
                html_ = parte.get_payload(decode=True).decode(
                    parte.get_content_charset() or "utf-8", errors="replace")
                return BeautifulSoup(html_, "html.parser").get_text(" ", strip=True)
            except Exception:
                continue
    return ""


def _parsear_correo(msg_id, msg):
    """Saca de un correo de Idealista los pisos que anuncia.

    Formato REAL todavía no visto: se asume que cada piso trae un enlace
    "idealista.com/inmueble/<id>" cerca de un precio en €. Si Idealista
    manda varios pisos en un mismo correo (resumen diario), se intenta
    separarlos por línea; si no se puede, se devuelve como un solo bloque
    con el primer precio y el primer enlace (mejor que nada, pero avisa
    por log para que se afine en cuanto llegue un correo real).
    """
    texto = _texto_plano(msg)
    enlaces = _RE_LINK.findall(texto)
    if not enlaces:
        print("  [idealista_relay] correo %s sin enlaces a inmuebles reconocibles; "
              "revisar formato real" % msg_id.decode())
        return []
    out = []
    lineas = texto.splitlines()
    for enlace in enlaces:
        # Busca el precio en la misma línea que el enlace, o en las 2 de alrededor.
        precio = None
        for i, linea in enumerate(lineas):
            if enlace in linea or enlace[-12:] in linea:
                ventana = " ".join(lineas[max(0, i - 2):i + 3])
                mp = _RE_PRECIO.search(ventana)
                if mp:
                    precio = mp.group(1).replace(".", "").replace(",", ".")
                break
        out.append({
            "id": enlace.split("/inmueble/")[-1].split("/")[0].split("-")[0],
            "url": enlace,
            "precio": float(precio) if precio else None,
        })
    return out


def main():
    try:
        with open(HISTORICO, encoding="utf-8") as fh:
            historico = json.load(fh)
    except FileNotFoundError:
        historico = {}
    except Exception as e:
        print("  [aviso] idealista_historico.json no se pudo leer (%s); empiezo de cero" % e)
        historico = {}

    notifier = Notifier()
    destino = os.environ.get("TELEGRAM_CHAT_ID", "")

    m = _conectar()
    if m is None:
        return 0  # no configurado todavía: no es un fallo, solo "aún no"

    avisos = []
    try:
        ids = _buscar_no_leidos(m)
        print("  [idealista_relay] %d correo(s) nuevo(s) de Idealista" % len(ids))
        for msg_id in ids:
            ok, datos = m.fetch(msg_id, "(RFC822)")
            if ok != "OK":
                continue
            msg = email.message_from_bytes(datos[0][1])
            for piso in _parsear_correo(msg_id, msg):
                if piso["id"] in historico:
                    continue
                historico[piso["id"]] = {
                    "url": piso["url"], "precio": piso["precio"],
                    "visto": time.strftime("%Y-%m-%d %H:%M"),
                }
                precio_txt = ("%.0f €/mes" % piso["precio"]) if piso["precio"] else "precio sin detectar"
                avisos.append("🆕 <b>Piso nuevo en Idealista</b>\n%s\n👉 <a href=\"%s\">Ver anuncio</a>"
                              % (precio_txt, piso["url"]))
            # Se marca como leído solo si se pudo procesar sin excepción,
            # para no perder un correo si algo falla a mitad.
            m.store(msg_id, "+FLAGS", "\\Seen")
    finally:
        try:
            m.logout()
        except Exception:
            pass

    tmp = HISTORICO + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(historico, fh, ensure_ascii=False, indent=1)
    os.replace(tmp, HISTORICO)

    if not avisos:
        print("  [idealista_relay] sin novedades")
    elif not destino:
        print("  [idealista_relay] %d avisos generados pero TELEGRAM_CHAT_ID no está configurado"
              % len(avisos))
    else:
        texto = "\n\n".join(avisos[:8])
        if len(avisos) > 8:
            texto += "\n\n... y %d más." % (len(avisos) - 8)
        ok = notifier.telegram(destino, texto)
        print("  [idealista_relay] %d avisos -> enviado: %s" % (len(avisos), ok))
        if not ok:
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
