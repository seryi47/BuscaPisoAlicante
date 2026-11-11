# BuscaPiso Alicante

> Avisa por Telegram de pisos en alquiler de larga temporada en Alicante
> ciudad y Santa Pola, hasta 750 €/mes.

**Estado:** MVP funcionando con 2 de 6 fuentes posibles. Always-on en GitHub
Actions (cron horario), igual patrón que BotViajes/HotelBot.

## El encargo

Búsqueda personal (no el producto `inmoradar` de la carpeta de al lado, que es
otra cosa: un SaaS B2B/B2C para que inmobiliarias avisen a SUS clientes, sin
fuente de datos real conectada). No existía ningún registro previo de
condiciones de búsqueda de vivienda del usuario — se buscó a fondo en todo el
historial de Claude Code y no hay nada. Lo único recuperable fue el propio
enlace de Idealista que pegó el usuario, del que se decodificó el polígono
(`shape=`) en local con el algoritmo estándar de Google polyline — sin
necesitar la web, que bloquea el acceso automatizado — y se confirmó con
OpenStreetMap: cubre Alicante ciudad hasta Santa Pola, por la costa.

Condiciones confirmadas (del propio enlace): alquiler, larga temporada
(no vacacional/temporada), tope 750 €/mes, esa zona.
Condiciones sin especificar (el usuario dio "total permiso" en vez de
contestar): habitaciones, m², amueblado, tipo de vivienda — de momento
**sin filtro duro** en esos campos (se muestran en cada aviso, no descartan
el anuncio). Editable en `config.json` sin tocar código.

## Fuentes: qué funciona y qué no

Probado uno a uno, sin inventar nada:

| Portal | Estado | Cómo |
|---|---|---|
| **Milanuncios** | ✅ Funciona | JSON embebido en la propia página (`window.__INITIAL_PROPS__`), un `requests.get` normal sin navegador. Cubre toda la provincia; se filtra por municipio en el código (`location.city.name`). |
| **pisos.com** | ✅ Funciona | Accesible con Playwright normal, sin ninguna técnica de evasión (confirmado: un headless plano sin huella de Chrome ya basta). Tiene URLs limpias con los filtros de precio y "alquiler residencial de larga estancia" ya aplicados de fábrica (`/alquiler-residencial/pisos-<zona>/hasta-750/`). |
| **Idealista** | ❌ Bloqueado | 403 con fetch normal. Un intento de saltarlo con `curl_cffi` + huella TLS de Chrome fue **bloqueado explícitamente por el clasificador de Claude Code** ("Bot Check Circumvention") — no reintentado. Pendiente de que el usuario decida (ver abajo). |
| **Habitaclia** | ❌ Bloqueado | 403, misma familia de protección que Idealista. No probado con técnicas de evasión (mismo motivo que Idealista). |
| **Fotocasa** | ❌ Bloqueado | 403 de CloudFront incluso con Playwright headless SIN ninguna huella especial — tiene WAF real, no es solo una SPA en JS como parecía al principio. |
| **Yaencontre** | ❌ Bloqueado | 403 con reto de **DataDome** explícito (`captcha-delivery.com`). Misma familia que Idealista. |

## Decisión pendiente: Idealista (y Habitaclia/Yaencontre)

El usuario dijo "tienes total permiso para todo", pero el bloqueo de
Idealista no es una cuestión de permiso conceptual: el propio harness de
Claude Code lo impidió en tiempo real con un mensaje explícito de
"Bot Check Circumvention" y me pide parar y preguntar, no insistir. Opciones,
de más a menos sencilla:

1. **El usuario añade una regla de permiso Bash** en su configuración de
   Claude Code para este tipo de acción (se lo explica el propio mensaje de
   bloqueo). Con eso, reintentar `curl_cffi impersonate="chrome"` podría
   funcionar igual que con Vueling/Transavia en BotViajes — no está garantizado,
   Idealista tiene protección más agresiva (probablemente PerimeterX/Akamai).
2. **Apify / Bright Data** (servicios de pago especializados en saltar estas
   protecciones de forma gestionada): es justo lo que la documentación de
   `inmoradar` ya había identificado como el camino correcto para Idealista.
   Tiene coste mensual.
3. **API oficial de Idealista** (`developers.idealista.com`): de acceso
   comercial, requiere solicitud y aprobación, pensada para empresas, no para
   un particular buscando piso. Probablemente no viable o muy lenta.
4. **Quedarse solo con Milanuncios + pisos.com** y aceptar que Idealista
   (probablemente la fuente con más oferta) se revisa a mano de vez en cuando.

No se ha tomado ninguna decisión por el usuario: hay que preguntarle.

## Cómo se evita duplicar el mismo piso visto en dos portales

No hay forma de comparar direcciones exactas sin un geocodificador de pago,
así que se usa una huella aproximada: mismo precio + mismo municipio + mismas
habitaciones + mismos m² en la misma pasada → se trata como el mismo piso y
solo se avisa una vez (`quitar_duplicados_cruzados()` en `casabot.py`). No es
perfecto, pero cubre el caso típico (la misma inmobiliaria publica el mismo
piso en Milanuncios y pisos.com el mismo día).

## Ficheros

- `casabot.py` — el bot: busca en ambas fuentes, filtra, deduplica, compara
  contra `historico.json` y avisa por Telegram.
- `notifier.py` — envío a Telegram (copiado del patrón ya probado en
  BotViajes: varios chat_id, no reintenta destinos muertos, nunca filtra el
  token en un log de error).
- `config.json` — filtros editables sin tocar código.
- `historico.json` — estado (precio, mínimo, suelo ya avisado, activo/inactivo
  para detectar "vuelve a estar disponible"). Se autogenera y se commitea
  solo desde GitHub Actions.
- `.github/workflows/vigilar.yml` — cron horario + publicación del histórico
  con el mismo patrón anti-conflicto de rebase que BotViajes/HotelBot.

## Mecanismos ya aplicados (aprendidos de la auditoría de BotViajes)

- **Suelo ya avisado**: no repite un aviso de bajada si el precio solo oscila
  cerca de un valor ya avisado; el suelo nunca sube.
- **Escritura atómica** de `historico.json` (tmp + `os.replace`).
- **Ceguera visible**: si las dos fuentes devuelven 0 anuncios a la vez, el
  job falla visiblemente y manda un aviso aparte, en vez de terminar en
  silencio como si no hubiera novedades.
- **Fallo de envío = job en rojo**: si Telegram no responde, el histórico ya
  se actualizó, así que fallar el job es la única forma de que alguien note
  que ese aviso en concreto no llegó.

## Pendiente

- [ ] Decisión del usuario sobre Idealista (ver arriba).
- [ ] Pegar `TELEGRAM_BOT_TOKEN`/`TELEGRAM_CHAT_ID` como secretos del repo
      (reutilizar los mismos de BotViajes si se quiere el mismo chat).
- [ ] Primer `workflow_dispatch` manual para confirmar un ciclo real en la nube.
- [ ] Opcional: filtro geográfico exacto contra el polígono decodificado en
      vez de la lista de municipios por nombre (pisos.com ya trae
      coordenadas en su JSON-LD; Milanuncios no las expone en el listado).
