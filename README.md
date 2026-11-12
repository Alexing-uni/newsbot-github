# newsbot

Bot de inversión guiado por noticias y datos de mercado, construido como **proyecto educativo**: aprender
cómo funciona un sistema de trading de principio a fin (datos, señales, riesgo, ejecución, medición) y
**comprobar con datos si leer las noticias da alguna ventaja**.

> **Aviso.** Esto no es asesoramiento financiero ni fiscal y no promete rentabilidad. Se publica con fines
> educativos. Si lo usas con dinero real, es bajo tu responsabilidad y solo con dinero que puedas perder.

*English summary: an educational, news-driven trading bot (crypto on Bitvavo and EU/US stocks on Trading 212)
with LLM news scoring, technical signals, risk limits, broker-side stop-losses and human confirmation of
every order through Telegram. It ran with real (very small) money for ten days; the honest result is in
"Resultados reales" below: no measurable edge after costs.*

## Qué hace

Cada pocos minutos, en horario de mercado:

1. **Lee noticias** (RSS y Google News) y las puntúa por valor con un modelo de lenguaje (Claude Haiku, con
   tope diario de llamadas), FinBERT en local o un léxico. Pesa cada titular por fuente y antigüedad.
2. **Calcula señales técnicas**: tendencia (medias móviles), impulso intradía, bandas de Bollinger, RSI,
   caídas fuertes, eventos con fecha (resultados, lanzamientos) y un modelo de velas preentrenado (Kronos).
3. **Funde todo en una señal** de −1 a +1 con pesos configurables y decide comprar, mantener o vender.
4. **Dimensiona la posición** por riesgo: stop a 2,5 × ATR, pérdida máxima por operación, límites por
   valor, sector y efectivo mínimo, y niveles según el capital.
5. **Propone la orden por Telegram** con su razonamiento y botones ✅/❌. Solo se ejecuta si la aceptas
   (las condiciones de la API de Trading 212 no permiten operar sin intervención humana).
6. Tras comprar, **deja el stop-loss en el propio broker**, que funciona aunque el ordenador se apague.
7. **Lo mide todo**: guarda cada señal y luego comprueba qué componentes aciertan de verdad (correlación
   de rangos con intervalos de confianza) antes de dejar que el bot les dé más peso.

## Arquitectura

```
noticias (RSS) ─► news/ingest ─► sentiment/  (Claude → FinBERT → léxico, con caché y tope de gasto)
precios (Yahoo, ccxt) ─► features/  (medias, ATR, intradía, sesiones de mercado)
                              │
               stocks/engine ─┤  señal por valor (pesos en config_*.toml)
                              │  + eventos con fecha, Kronos, rebote tras caída
                              ▼
               stocks/sizing + risk/  (tamaño por riesgo, sectores, kill-switch por drawdown)
                              ▼
               reporting/telegram  (propuesta ✅/❌, /estado, /pensamiento, avisos)
                              ▼
               execution/  (PaperBroker | Trading 212 | Bitvavo)  + stop en el broker
                              ▼
               storage/ SQLite  ─►  research/learn  (qué componente predice y cuánto)
```

- `newsbot/bot.py`: punto de entrada y bucle continuo (`--loop`), con un hilo por cartera.
- `newsbot/stocks/`: motor de bolsa multiposición, autopsia de pérdidas, eventos y explicaciones.
- `newsbot/research/`: backtest, walk-forward, Monte Carlo y aprendizaje de pesos.
- `newsbot/watchdog.py`: vigilante que reinicia el bucle si se cuelga.
- `config_usa.toml`, `config_bolsa.toml`: una cartera por fichero (EE. UU. y Europa); `config.toml`: cripto.
- `docs/CRIPTO.md`: documentación de la primera fase (cripto, una operación al día).

## Medidas de seguridad

- **Papel por defecto.** El modo real exige `CONFIRM_LIVE=yes` y las claves en `.env`.
- **Cada orden necesita tu ✅ en Telegram.** Las propuestas caducan a los pocos minutos y no se ejecutan si el
  precio se ha movido demasiado.
- Stop-loss en el broker, **kill-switch** si la cartera cae un X % desde su máximo, máximo de operaciones al día
  y espera antes de volver a comprar lo que acaba de vender.
- Detecta **precios congelados** de la fuente de datos y no compra mientras dure.
- Avisa de los **días de pánico**, pero nunca compra por ellos: solo los guarda para medir qué pasó después.
- Nunca guarda ni registra claves; las claves de API deben tener permiso de operar, **nunca de retirar**.

## Resultados reales (lo más importante)

Funcionó con dinero real del 3 al 12 de noviembre de 2026, con unos 50 € en Trading 212:

| Operación | Resultado |
|---|---|
| WMT (Xetra), stop del broker por un hueco de liquidez | −0,54 € |
| WMT (Xetra), venta al cerrar la cartera europea | −0,25 € |
| PLTR (EE. UU.), objetivo de beneficio | +0,06 € |
| XOM (EE. UU.), stop-loss | ≈ −0,48 € |
| **Total** | **≈ −1,2 €** (1 de 4 operaciones ganadoras) |

Diez días son muy poca muestra. Pero las mediciones de 180 días de histórico dicen lo mismo:

- **Ningún componente de la señal gana con claridad a los costes.** Lo único con poder predictivo
  significativo fue Bollinger (volver a la media), y en su mejor 10 % daba +0,3 % a 24 h: lo mismo que cuesta
  operar (~0,4 % ida y vuelta en EE. UU. con cuenta en euros).
- **Las noticias no demostraron ventaja.** La parte con más peso (35 %) no tenía pruebas a favor.
- **El impulso ("lo que sube sigue subiendo") iba al revés** a 4 horas vista.
- **Comprar en los días de pánico** solo dio señales prometedoras en 5 casos, demasiado pocos para fiarse.

## Lecciones aprendidas

1. **Los costes deciden.** Un 0,15 % de cambio de divisa en cada compra y venta se come casi cualquier
   ventaja a corto plazo.
2. **La liquidez importa.** La línea en euros de una acción de EE. UU. en Xetra casi no tiene compradores
   antes de que abra Nueva York: un stop saltó a un precio al que el mercado nunca llegó a cotizar.
3. **Medir antes de creer.** El bot guarda cada señal y comprueba después si acertó; eso evita cambiar las
   reglas por un día bueno o malo.
4. **Un titular no es una ventaja.** Lo que sale en las noticias ya lo sabe todo el mercado y ya está en el precio.
5. **Con poco capital, lo que más hace crecer la cuenta es lo que ahorras, no lo que rinde.**

## Uso (modo papel)

```bash
python -m venv .venv && . .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -e ".[dev]"                              # FinBERT opcional: pip install -e ".[finbert]"
cp .env.example .env                                 # todo es opcional en papel
python -m newsbot.bot --config config_usa_paper.toml --dry-run   # qué haría ahora, sin guardar nada
python -m newsbot.bot --loop                         # bucle continuo con las carteras de config.toml
pytest -q && ruff check .
```

El componente Kronos es opcional (peso 0 por defecto). Para usarlo, clona su código en `vendor/Kronos`:

```bash
git clone https://github.com/shiyu-coder/Kronos vendor/Kronos
```

## Licencia

MIT (ver `LICENSE`). Kronos tiene su propia licencia MIT y no se incluye en este repositorio.
