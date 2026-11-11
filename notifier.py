"""Aviso por Telegram. Mismo patrón que botviajes/notifier.py (BotViajes):
admite varios chat_id separados por coma, no reintenta destinos que ya no
existen (grupo borrado / bot expulsado), y nunca deja que un error de red
revele el token en el log.
"""
import os
import re

import requests


def chat_ids(value):
    if value is None:
        return []
    items = value if isinstance(value, (list, tuple)) else re.split(r"[,\s]+", str(value))
    return [str(i).strip() for i in items if str(i).strip()]


class Notifier:
    def __init__(self, tg_token=None):
        self.tg_token = tg_token or os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
        self._muertos = set()

    def telegram(self, chat_id, text):
        ids = chat_ids(chat_id)
        if not self.tg_token or not ids:
            return False
        vivos = [c for c in ids if c not in self._muertos]
        if not vivos:
            return False
        llego = False
        for cid in vivos:
            if self._send_one(cid, text):
                llego = True
        return llego

    def _send_one(self, chat_id, text):
        try:
            r = requests.post(
                "https://api.telegram.org/bot%s/sendMessage" % self.tg_token,
                data={"chat_id": str(chat_id), "text": text, "parse_mode": "HTML",
                      "disable_web_page_preview": "false"},
                timeout=20,
            )
            if not r.ok:
                permanente = r.status_code in (400, 403) and any(
                    m in r.text for m in ("chat was deleted", "bot was kicked",
                                          "bot was blocked", "chat not found",
                                          "user is deactivated"))
                if permanente and str(chat_id) not in self._muertos:
                    self._muertos.add(str(chat_id))
                    print("  [telegram] el destino %s ya no existe; dejo de escribirle" % chat_id)
                elif not permanente:
                    print("  [telegram] %s: %s" % (r.status_code, r.text[:200]))
            return r.ok
        except Exception as e:
            msg = re.sub(re.escape(self.tg_token), "***", str(e)) if self.tg_token else str(e)
            print("  [telegram] error:", msg)
            return False
