# newsbot

Bot de inversión **diaria** (máx. 1 operación/día) en cripto spot, solo largo, que analiza varias
monedas (por defecto BTC, ETH, SOL, XRP y ADA contra EUR) combinando el sentimiento de las noticias
de cada una con tendencia por medias móviles y el Fear & Greed Index. Proyecto de
aprendizaje y portfolio: el objetivo es que sea correcto, reproducible, auditable y prudente, y
**medir con datos si las noticias aportan algo**. No promete rentabilidad.

> **Esto no es asesoramiento financiero ni fiscal. Usa solo dinero que puedas perder.**

## Uso rápido

```bash
python -m venv .venv && . .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -e ".[dev]"                             # FinBERT opcional: pip install -e ".[finbert]"
cp .env.example .env                                # rellena lo que uses (todo es opcional en paper)
python -m newsbot.bot --dry-run                     # decisión de hoy sin guardar nada
python -m newsbot.bot                               # ejecución real (paper por defecto), 1 vez/día
pytest -q && ruff check .
```

| Comando | Qué hace |
|---|---|
| `python -m newsbot.bot` | Decisión del día (concilia antes órdenes pendientes) |
| `--dry-run` / `--force` | Sin guardar nada / ignorar el guard diario (solo depurar) |
| `--compare-sentiment` | Claude vs FinBERT vs léxico sobre los mismos titulares + correlación histórica |
| `--report` | Informe semanal (y a Telegram) |
| `--healthcheck` | Aviso por Telegram si no hay decisión en 26 h (exit 1) |
| `--telegram-poll` | Procesa una vez los comandos pendientes (sin `--loop`; el bucle ya los atiende) |
| `--loop` | **Bucle continuo** (modo recomendado): cada 5 min lee noticias, foros y precios y decide, con máx. 3 operaciones/día y 2 h de enfriamiento |
| `--watch` | Vigilante (cada 15 min): guarda noticias y foros; ante una noticia fuerte adelanta la decisión |
| `--reconcile` / `--resume` | Concilia órdenes live pendientes / rearma kill-switch y quita la pausa |
| `python -m newsbot.research.report --days 1095 [--symbol ETH/EUR]` | Informe `reports/backtest_<MONEDA>.md` |
| `python -m newsbot.research.reoptimize` | Reoptimización manual (el bot la lanza sola cada 30 días) |
| `python -m newsbot.reporting.tax --year 2026` | CSV fiscal FIFO (`--mode paper` para el de prueba) |

## Arquitectura

```
RSS + CryptoPanic ─► news/ingest (ventana 24 h, dedupe por similitud, peso fuente × recencia)
                         │
                   sentiment/ cascada: Claude (JSON validado, caché) → FinBERT → léxico
Fear & Greed ──────────┐ │
ccxt OHLCV ─► features/ (SMA10/30, SMA200 → régimen, ATR14, volatilidad 30 d)
                        ▼
              strategy/fusion.decide()  ── función pura, la misma en el bot y en el backtest
                        ▼
              risk/sizing ─► execution/ (PaperBroker | LiveBroker ccxt)
                        ▼
              storage/ SQLite (headlines, sentiment_runs, decisions, orders, equity_daily,
                               params_history, kv) + journal.csv
                        ▼
              reporting/ (Telegram, informe semanal, CSV fiscal FIFO)
              research/  (backtest, métricas, walk-forward, Monte Carlo, reoptimización)
```

## Cómo decide

Cada día a las 09:05 calcula una señal para **cada moneda** de `general.symbols`, con sus propias
velas y solo los titulares que la mencionan (`[news.assets]`; las noticias generales y
regulatorias cuentan para BTC). Mantiene **como mucho una posición**:

- **Sin posición**: compra la moneda con mejor señal que pase todos los filtros. Si ninguna pasa, no hace nada.
- **Con posición**: solo gestiona esa moneda (vender o mantener) hasta salir; no rota a otra el
  mismo día, porque cada cambio paga dos comisiones.

El mensaje diario muestra la clasificación de todas las monedas. Para añadir otra, agrégala a
`general.symbols` con sus palabras clave en `[news.assets]` (sin memecoins).

**Vigilante de noticias** (`--watch`, cada 15 min): lee las fuentes (incluido el RSS de
r/CryptoCurrency y r/Bitcoin, con poco peso) y guarda los titulares. Si en las últimas 3 h al
menos 3 fuentes distintas hablan de una moneda con sentimiento muy fuerte (|score × confianza| ≥
0,5), adelanta la decisión del día: positiva si no hay posición, negativa sobre la moneda que se
tiene. Una decisión adelantada como mucho al día, nunca más de una operación al día, y los mismos
filtros. Si ya había una decisión HOLD ese día, se guarda en `decisions_archive` antes de
sustituirla. Un candado (`data/.lock`) evita que el vigilante y la ejecución de las 09:05 se pisen.

**Bucle continuo** (`--loop`, el modo del `docker-compose.yml`): cada `tick_minutes` (5) hace
todo lo anterior y además calcula un **impulso intradía** (EMA de 2 h frente a 8 h en velas de
15 min). Puede operar varias veces al día, pero con frenos contra las comisiones:
`max_trades_per_day` (3) y `cooldown_minutes` (120) tras cada operación. Los stops actúan
siempre, también durante el enfriamiento. Los ticks sin operación solo actualizan el capital y
escriben una línea en el log; los que operan se registran completos (la decisión anterior del día
pasa a `decisions_archive`). El impulso describe el movimiento reciente: **no predice el precio**.

`señal = 0,40·noticias + 0,30·tendencia + 0,15·intradía + 0,10·F&G + 0,05·votos`

- **Noticias**: `score × confidence` del sentimiento, suavizado con una EMA de 3 días para que un
  titular suelto no cambie la decisión. El LLM **solo puntúa**: nunca decide comprar o vender.
- **Tendencia**: SMA10 frente a SMA30 sobre **velas cerradas** (la del día se descarta: sin look-ahead).
- **F&G** ([alternative.me](https://alternative.me/crypto/fear-and-greed-index/)): contrario solo
  en los extremos (≤ 25 suma, ≥ 75 resta, en medio 0).
- **Votos**: CryptoPanic, `(pos − neg) / (pos + neg + 10)`. CryptoPanic **ya no tiene API
  gratuita** (plan `growth` o superior); sin `CRYPTOPANIC_TOKEN` vale 0.

Prioridades: stop-loss (−8 % sobre el precio medio) > trailing stop (opcional) > señal. Con señal
≥ umbral (0,25; **0,40 si el precio está bajo la SMA200**) la exposición objetivo va del 50 % al
100 %; con señal ≤ −0,15, a 0 %. Para **abrir o ampliar** además debe cumplirse:

- **Filtro de ventaja**: movimiento esperado = ATR diario × |señal| ≥ 2 × coste de ida y vuelta
  (2 × (0,25 % + 0,1 %) = 0,7 %), es decir ≥ 1,4 %. Con un ATR de BTC del 2,5 % hace falta una
  señal de ~0,56: **la tendencia sola (máx. 0,35) casi nunca basta; hacen falta noticias a favor.**
- **Kill-switch**: si el capital cae un 25 % desde su máximo, solo vende o mantiene hasta que lo
  reactives (`/resume CONFIRMAR` o `--resume`).
- Volatility targeting opcional (`vol_target`), mínimos del exchange (`limits.cost.min`,
  `limits.amount.min`, precisión) y `min_rebalance_pct`.

El tamaño se calcula sobre el **capital total actual** (efectivo + posición): los beneficios se
reinvierten solos. **Con 10 € y un mínimo de 5 € por orden, casi todas las operaciones serán del
50 % o del 100 % del capital**, y muchas señales acabarán en HOLD.

## Resultados del backtest (solo precio, sin noticias)

`reports/backtest.md`, BTC/EUR en Bitvavo, 834 días útiles (2024-2026), comisiones y slippage
incluidos. Es por moneda y sin noticias: no simula la rotación entre monedas.

| | Capital final (de 10 €) | CAGR | Máx. drawdown | Operaciones |
|---|---|---|---|---|
| Estrategia (sin noticias) | 10,29 € | 1,3 % | 11 % | 6 |
| Buy & hold | 11,35 € | 5,7 % | 52 % | 1 |
| DCA semanal | 9,62 € | −1,7 % | 38 % | 120 |

Lectura honesta: la parte técnica sola **rinde menos que comprar y mantener**, aunque con mucho
menos drawdown, porque casi siempre está fuera del mercado. En walk-forward (10 ventanas de 60
días) los umbrales reoptimizados perdieron menos fuera de muestra que los actuales (×0,97 frente a
×0,89), así que la primera reoptimización probablemente propondrá cambiarlos; el Monte Carlo no
marca sobreajuste (percentil 41). Si las noticias mejoran esto solo se
sabrá con el histórico propio que el bot guarda desde el primer día. **Rentabilidad pasada no
garantiza la futura.**

## Pasar a dinero real (Bitvavo)

1. Cuenta verificada (KYC), 2FA activado y los 10 € depositados por SEPA.
2. En Bitvavo → Ajustes → API: crea una key con permisos **"View" (ver) y "Trade" (operar)**,
   nunca "Withdraw", confírmala desde el email que te envía Bitvavo y, si puedes, restríngela a la
   IP de tu servidor. Sin "View" falla con el error 311; sin confirmar, con el 306. El bot no puede
   verificar el permiso de retirada por API (ni ccxt ni Bitvavo lo exponen): es responsabilidad tuya.
3. En `.env`: `BITVAVO_API_KEY`, `BITVAVO_API_SECRET` y `CONFIRM_LIVE=yes`.
4. En `config.toml`: `mode = "live"`. Bitvavo exige un `operatorId` entero en cada orden
   (`execution.operator_id`, por defecto 1001; identifica a este bot en tu cuenta).
5. Prueba antes `python -m newsbot.bot --dry-run` con todo configurado.

Si falta cualquiera de las tres llaves (mode, CONFIRM_LIVE, claves), el bot aborta. En live:

- Lee tus comisiones reales (`fetch_trading_fees`). Si maker < taker, usa una orden límite
  post-only al mejor precio que cancela a los 15 min y completa a mercado; si no, mercado directo.
  Avisa si tu comisión real supera la de `costs.fee_pct`.
- Las compras a mercado van por importe en EUR (`create_market_order_with_cost`).
- `clientOrderId` = UUID derivado de la fecha. La decisión y una orden `pending` se guardan
  **antes** de enviar nada; si el proceso muere, `--reconcile` la busca en el exchange. Nunca se
  reintenta crear una orden.
- Tras cada orden se leen la orden y el balance reales: nunca se supone que se llenó.

## Reoptimización de umbrales (cada 14 días)

Walk-forward (180 días de entrenamiento, 60 de validación) sobre los umbrales de compra y venta
(rejilla de 3 × 3, ±0,05 por paso). Solo cambia algo si mejora fuera de muestra, pasa el Monte Carlo
(no está en el 5 % más afortunado) y queda dentro de límites duros (compra entre 0,15 y 0,60, venta
entre −0,40 y −0,05). Con `adapt.auto_apply_live = true` **se aplica sola también en live** y avisa
por Telegram con `/deshacer <id>`; con `false`, espera tu `/aprobar <id>`.

## Telegram

Crea un bot con @BotFather y pon `TELEGRAM_BOT_TOKEN` y `TELEGRAM_CHAT_ID` en `.env`. Recibirás el
resumen diario (decisión, señal desglosada, capital, P&L, 3 titulares con más peso) y alertas de
stop, kill-switch, órdenes sin llenar, errores y healthcheck. Solo obedece comandos de tu chat.

Con `--loop`, un hilo aparte escucha Telegram con long polling: los comandos se contestan al
momento (también en mitad de un tick) y aparecen en el menú del chat. Alias en inglés entre paréntesis.

| Comando | Qué muestra |
|---|---|
| `/estado` (`/status`) | Último ciclo, capital, posición, ranking de monedas y **últimas noticias** |
| `/noticias [MONEDA] [N]` | Titulares de 24 h con su sentimiento (léxico; `—` si no tiene palabras clave) |
| `/senales`, `/moneda BTC` | Señal de cada moneda y sus componentes, con peso (0 = sombra) |
| `/aprendizaje [1h\|4h\|24h]` | Qué componentes predicen de verdad el precio (IC y acierto, con intervalos) |
| `/operaciones`, `/rendimiento`, `/informe` | Órdenes, rentabilidad, drawdown, comisiones |
| `/propuestas`, `/aprobar <id>`, `/rechazar <id>` | Cambios de parámetros pendientes y en vigor |
| `/deshacer <id>` (`/undo`) | Revierte un cambio ya aplicado, también los automáticos |
| `/pausa`, `/reanudar CONFIRMAR`, `/ayuda` | Control (los que cambian el estado caducan a los 5 min) |

## Aprendizaje (retroalimentación cada 5 minutos)

Cada tick guarda en `ticks` la señal de cada moneda con todos sus componentes sin ponderar,
incluidos cuatro **componentes sombra** tomados de freqtrade y de la literatura: RSI y Bollinger
(contrarios), momentum de 7 días y volumen. Los sombra se miden pero **no operan** mientras su peso
sea 0. `research/learn.py` compara después cada componente con lo que hizo el precio a 1 h, 4 h y
24 h (muestras no solapadas, IC de Spearman con intervalo de Fisher, acierto con intervalo de
Wilson, 5 monedas correlacionadas ≈ 2 apuestas independientes).

Con al menos 400 muestras efectivas (≈ 5 semanas a 4 h), como mucho una vez por semana propone
mover pesos: ±0,05 como máximo, encogido hacia los actuales y siempre sumando 1, y solo los
componentes cuyo efecto es significativo. Con `learn.auto_apply_live = true` **se aplica solo en
live** y avisa con `/deshacer <id>`; con `false`, espera a `/aprobar`. Los cambios aplicados se
acumulan (umbrales de la reoptimización + pesos del aprendizaje); lo que se aplicó solo en papel no
cuenta en live. `python -m newsbot.research.backfill` rellena 60 días de componentes de precio y
F&G (las noticias no: no hay histórico sin look-ahead) para no empezar de cero. A 5 minutos no hay nada
predecible tras comisiones (≈ 0,6 % ida y vuelta): por eso se mide cada 5 min pero se decide despacio.

## Despliegue (portátil o home lab, siempre encendido)

**Con Docker** (Linux, o Windows con Docker Desktop): copia la carpeta `newsbot` (con `.env` y
`data/`, sin `.venv`) y:

```bash
docker compose up -d --build
docker compose logs -f
```

El contenedor corre el bucle continuo y se reinicia solo (`restart: unless-stopped`). En Linux,
`./data` debe poder escribirlo el uid 1000 del contenedor (`sudo chown -R 1000 data`).

**Windows sin Docker**: `install_windows.cmd` crea el entorno, prueba en seco, deja `run_loop.cmd`
en el inicio de sesión y registra la tarea **"newsbot watchdog"** (`deploy\watchdog_task.ps1`).
Sin ventanas; todo queda en `data/newsbot.log` y `data/watchdog.log` (rotados).

- **Vigilante** (`newsbot/watchdog.py`, cada 5 min): si el bucle no está en marcha lo arranca, y si
  no escribe su latido en 20 min (colgado) lo reinicia; en ambos casos avisa por Telegram. Todo
  arranque pasa por él (también `run_loop.cmd`), así que nunca hay dos bucles.
- **Parar de verdad**: `stop_bot.cmd` (deja `data\stop` y el vigilante no lo toca). Volver a
  arrancar: `run_loop.cmd`. `/pausa` en Telegram solo detiene las decisiones; el proceso sigue.
- **Claude** (`ANTHROPIC_API_KEY` en `.env`): opcional. Reutiliza el resultado de cada moneda 2 h
  (`llm_min_minutes`) y no pasa de 60 llamadas al día (`llm_max_calls_day`, ≈ 1 $/mes con Haiku);
  sin clave, si falla o al agotar el tope, sigue con el léxico. El gasto del día sale en `/estado`.
  Pon también un límite de gasto mensual en la consola de Anthropic.

**Nunca dejes dos equipos ejecutando el bot con la misma cuenta**: operarían los dos. Antes de
arrancarlo en el portátil, desactiva las tareas del PC anterior.

Alternativa sin bucle: `deploy/crontab` o `deploy/newsbot.service` + `newsbot.timer` (09:05).

## Fiscalidad en España (contexto, no consejo)

Las ganancias por compraventa de cripto (incluidas las permutas cripto-cripto) tributan como
ganancias patrimoniales en la base del ahorro del IRPF (desde el 19 %), método FIFO; las pérdidas
compensan ganancias del ahorro y se arrastran 4 años. Con DAC8, los exchanges reportan a la AEAT
desde 2026. `newsbot.reporting.tax` exporta una fila por lote casado con las comisiones incluidas.
Bitvavo tiene licencia MiCA: usa siempre exchanges con licencia MiCA en la UE.

## Sesgo de look-ahead de los LLM

Un LLM puede "conocer" lo que pasó después de un titular antiguo, así que backtestear el
sentimiento con noticias anteriores a su knowledge cutoff infla los resultados (Glasserman y Lin,
2023, [arXiv 2309.17322](https://arxiv.org/abs/2309.17322); [arXiv 2512.23847](https://arxiv.org/abs/2512.23847)).
Por eso los titulares se guardan desde el primer día, y las entidades que no son el activo operado
se anonimizan. Además, como dice r/algotrading, la noticia llega antes a los HFT que a un RSS y
gran parte ya está en el precio: por eso el horizonte es diario.

## Limitaciones conocidas

- CryptoPanic solo se ha probado con respuestas de ejemplo (sin token real).
- No hay sandbox de Bitvavo: el modo live está probado con un exchange simulado, no con órdenes reales.
- La rejilla de reoptimización y el backtest no incluyen noticias (no hay histórico todavía).
- El backtest no usa vectorbt: la decisión depende del estado (stops, kill-switch, posición), así
  que recorre los días uno a uno con las mismas funciones que el bot.
