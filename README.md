# BuscaPiso Alicante 🏠🔔

Vigila alquiler de larga temporada en Alicante ciudad y Santa Pola (hasta
750 €/mes) en Milanuncios y pisos.com, y avisa por Telegram de pisos nuevos,
bajadas de precio y pisos que vuelven a estar disponibles.

> 📖 Ver [PROYECTO.md](PROYECTO.md) para el estado completo: qué portales
> funcionan, cuáles están bloqueados por protección anti-bot y la decisión
> pendiente sobre Idealista.

## Instalación

```sh
python3 -m venv venv
./venv/bin/pip install -r requirements.txt
./venv/bin/playwright install chromium
cp .env.example .env   # rellena TELEGRAM_BOT_TOKEN y TELEGRAM_CHAT_ID
```

## Uso

```sh
./venv/bin/python casabot.py
```

Los filtros (precio, habitaciones, m², zonas) se editan en `config.json`
sin tocar código.

## Siempre encendido

`.github/workflows/vigilar.yml` lo ejecuta cada hora en GitHub Actions.
Necesita los secretos `TELEGRAM_BOT_TOKEN` y `TELEGRAM_CHAT_ID` en el repo.
