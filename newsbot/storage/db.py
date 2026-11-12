import json
import sqlite3
from datetime import datetime
from pathlib import Path

# Migraciones versionadas: se aplican en orden según PRAGMA user_version. Nunca edites una ya
# publicada; añade otra al final.
MIGRATIONS = [
    """
    CREATE TABLE headlines (
        id INTEGER PRIMARY KEY, day TEXT NOT NULL, source TEXT, title TEXT NOT NULL,
        summary TEXT, published TEXT, ingested TEXT, UNIQUE(day, title));
    CREATE TABLE sentiment_runs (
        id INTEGER PRIMARY KEY, day TEXT NOT NULL, created TEXT, method TEXT, score REAL,
        confidence REAL, reason TEXT, headlines_hash TEXT);
    CREATE TABLE decisions (
        day TEXT PRIMARY KEY, created TEXT, mode TEXT, action TEXT, signal REAL,
        target_exposure REAL, order_eur REAL, price REAL, reason TEXT, inputs_json TEXT,
        config_hash TEXT);
    CREATE TABLE orders (
        id INTEGER PRIMARY KEY, day TEXT, client_order_id TEXT UNIQUE, mode TEXT, side TEXT,
        qty REAL, price REAL, fee REAL, eur REAL, created TEXT);
    CREATE TABLE equity_daily (
        day TEXT PRIMARY KEY, cash REAL, qty REAL, avg_entry REAL, price REAL, equity REAL,
        peak REAL);
    CREATE TABLE params_history (
        id INTEGER PRIMARY KEY, created TEXT, params_json TEXT, status TEXT, note TEXT);
    """,
    """
    ALTER TABLE headlines ADD COLUMN url TEXT;
    ALTER TABLE headlines ADD COLUMN weight REAL;
    ALTER TABLE headlines ADD COLUMN votes_json TEXT;
    """,
    """
    CREATE TABLE kv (key TEXT PRIMARY KEY, value TEXT);
    ALTER TABLE decisions ADD COLUMN news_ema REAL;
    ALTER TABLE equity_daily ADD COLUMN high_since_entry REAL DEFAULT 0;
    ALTER TABLE orders ADD COLUMN status TEXT DEFAULT 'filled';
    """,
    """
    ALTER TABLE decisions ADD COLUMN symbol TEXT;
    ALTER TABLE orders ADD COLUMN symbol TEXT;
    ALTER TABLE equity_daily ADD COLUMN symbol TEXT;
    ALTER TABLE sentiment_runs ADD COLUMN symbol TEXT;
    UPDATE decisions SET symbol = 'BTC/EUR';
    UPDATE orders SET symbol = 'BTC/EUR';
    UPDATE equity_daily SET symbol = 'BTC/EUR' WHERE qty > 0;
    UPDATE sentiment_runs SET symbol = 'BTC/EUR';
    """,
    """
    CREATE TABLE decisions_archive AS SELECT * FROM decisions WHERE 0;
    ALTER TABLE decisions_archive ADD COLUMN archived_at TEXT;
    """,
    """
    CREATE TABLE IF NOT EXISTS ticks (
        id INTEGER PRIMARY KEY, ts INTEGER NOT NULL, created TEXT NOT NULL, symbol TEXT NOT NULL,
        price REAL, signal REAL, action TEXT, reason TEXT, held INTEGER, raw_json TEXT,
        contributions_json TEXT);
    CREATE INDEX IF NOT EXISTS ticks_symbol_ts ON ticks(symbol, ts);
    """,
    """
    CREATE INDEX IF NOT EXISTS ticks_ts ON ticks(ts);
    """,
    """
    CREATE TABLE IF NOT EXISTS positions (
        symbol TEXT PRIMARY KEY, qty REAL NOT NULL, avg_entry REAL, high REAL, opened TEXT,
        stop_price REAL, stop_order_id TEXT);
    CREATE TABLE IF NOT EXISTS proposals (
        id INTEGER PRIMARY KEY, created TEXT NOT NULL, expires TEXT NOT NULL, symbol TEXT NOT NULL,
        side TEXT NOT NULL, eur REAL, qty REAL, ref_price REAL, stop_price REAL, reason TEXT,
        signal REAL, urgent INTEGER DEFAULT 0, status TEXT NOT NULL DEFAULT 'proposed', decided TEXT,
        msg_id INTEGER, result TEXT);
    CREATE INDEX IF NOT EXISTS proposals_status ON proposals(status, expires);
    CREATE TABLE IF NOT EXISTS events (
        id INTEGER PRIMARY KEY, code TEXT NOT NULL, kind TEXT NOT NULL, title TEXT, date TEXT NOT NULL,
        effect TEXT, confidence REAL, why TEXT, source TEXT, created TEXT, UNIQUE(code, kind, date));
    """,
    """
    ALTER TABLE proposals ADD COLUMN sent_at TEXT;
    """,
    """
    ALTER TABLE proposals ADD COLUMN thought TEXT;
    """,
]


class KV:
    """Estado persistente pequeño: kill_switch, paused, offset de Telegram, caché del LLM..."""

    def __init__(self, con: sqlite3.Connection):
        self.con = con

    def get(self, key: str, default: str | None = None) -> str | None:
        row = self.con.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
        return row[0] if row else default

    def set(self, key: str, value: str) -> None:
        with self.con:
            self.con.execute("INSERT OR REPLACE INTO kv VALUES (?, ?)", (key, value))

    def delete(self, key: str) -> None:
        with self.con:
            self.con.execute("DELETE FROM kv WHERE key = ?", (key,))


def connect(path: str | Path) -> sqlite3.Connection:
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    version = con.execute("PRAGMA user_version").fetchone()[0]
    for i, sql in enumerate(MIGRATIONS[version:], start=version + 1):
        con.executescript(sql)
        con.execute(f"PRAGMA user_version = {i}")
    return con


def has_decision(con: sqlite3.Connection, day: str) -> bool:
    return con.execute("SELECT 1 FROM decisions WHERE day = ?", (day,)).fetchone() is not None


def last_state(con: sqlite3.Connection) -> sqlite3.Row | None:
    return con.execute("SELECT * FROM equity_daily ORDER BY day DESC LIMIT 1").fetchone()


def save_headlines(con: sqlite3.Connection, day: str, created: str, headlines, weights) -> None:
    """Histórico propio de titulares (la base para evaluar las noticias sin look-ahead)."""
    with con:
        con.executemany(
            "INSERT OR IGNORE INTO headlines(day, source, title, summary, published, ingested, "
            "url, weight, votes_json) VALUES (?,?,?,?,?,?,?,?,?)",
            [(day, h.source, h.title, h.summary, h.published.isoformat(), created, h.url, w,
              json.dumps(h.votes)) for h, w in zip(headlines, weights, strict=True)])


def orders_today(con: sqlite3.Connection, day: str) -> int:
    """Operaciones enviadas hoy (llenadas o pendientes)."""
    return con.execute("SELECT count(*) FROM orders WHERE day = ? AND status IN "
                       "('filled', 'pending')", (day,)).fetchone()[0]


def last_trade_time(con: sqlite3.Connection) -> datetime | None:
    """Hora de la última operación enviada (llenada o pendiente), para el enfriamiento."""
    row = con.execute("SELECT max(created) FROM orders WHERE status IN ('filled', 'pending')"
                      ).fetchone()
    return datetime.fromisoformat(row[0]) if row[0] else None


def archive_decision(con: sqlite3.Connection, day: str, now: str) -> None:
    """Guarda la decisión del día antes de que el vigilante la sustituya (trazabilidad)."""
    with con:  # decisions_archive = columnas de decisions (migración 5) + archived_at
        con.execute("INSERT INTO decisions_archive SELECT *, ? FROM decisions WHERE day = ?",
                    (now, day))


def save_ticks(con: sqlite3.Connection, now: datetime, rows: list[dict]) -> None:
    """Una fila por moneda y tick: la base del aprendizaje (research.learn) y de /senales.
    `raw` son los componentes sin ponderar en [-1, 1] (positivo = alcista), sombra incluida.
    Una fila con "ts" propio (relleno histórico) usa ese instante en vez de `now`."""
    def when(r):
        ts = r.get("ts") or int(now.timestamp())
        return ts, datetime.fromtimestamp(ts, now.tzinfo).isoformat()
    with con:
        con.executemany(
            "INSERT INTO ticks(ts, created, symbol, price, signal, action, reason, held, raw_json, "
            "contributions_json) VALUES (?,?,?,?,?,?,?,?,?,?)",
            [(*when(r), r["symbol"], r["price"], r["signal"],
              r["action"], r["reason"], int(r["held"]), json.dumps(r["raw"]),
              json.dumps(r["contributions"])) for r in rows])


def latest_ticks(con: sqlite3.Connection) -> list[sqlite3.Row]:
    """Última fila de cada moneda (la del último tick guardado), de mejor a peor señal."""
    return con.execute("SELECT * FROM ticks WHERE ts = (SELECT max(ts) FROM ticks WHERE action != "
                       "'BACKFILL') AND action != 'BACKFILL' ORDER BY signal DESC").fetchall()


def prune_ticks(con: sqlite3.Connection, before_ts: int) -> int:
    """Borra ticks más antiguos que `before_ts` (fuera de la ventana de aprendizaje)."""
    with con:
        return con.execute("DELETE FROM ticks WHERE ts < ?", (before_ts,)).rowcount


def open_positions(con: sqlite3.Connection) -> list[sqlite3.Row]:
    """Posiciones abiertas de una cartera multiposición (bolsa); la de cripto no usa esta tabla."""
    return con.execute("SELECT * FROM positions WHERE qty > 0 ORDER BY symbol").fetchall()


def pending_proposals(con: sqlite3.Connection, now_iso: str) -> list[sqlite3.Row]:
    """Propuestas esperando tu respuesta en Telegram (sin caducar), de la más reciente a la más antigua."""
    return con.execute("SELECT * FROM proposals WHERE status = 'proposed' AND expires > ? "
                       "ORDER BY id DESC", (now_iso,)).fetchall()


def last_decision(con: sqlite3.Connection) -> sqlite3.Row | None:
    return con.execute("SELECT * FROM decisions ORDER BY day DESC LIMIT 1").fetchone()


def active_params(con: sqlite3.Connection, mode: str | None = None) -> tuple[int | None, dict]:
    """Override efectivo: todas las propuestas aplicadas, en orden, una encima de otra (cada fila
    guarda solo sus secciones: reoptimize los umbrales, learn el vector completo de pesos).
    En live se ignoran las que se aplicaron solas en papel ("auto" con "mode" distinto de live):
    se decidieron con otro dinero y otras reglas. Las automáticas de live sí cuentan.
    Devuelve (id de la última que cuenta, override) o (None, {})."""
    last, out = None, {}
    for row in con.execute("SELECT id, params_json, note FROM params_history "
                           "WHERE status = 'applied' ORDER BY id"):
        try:
            note = json.loads(row["note"] or "{}")
        except ValueError:
            note = {}
        if mode == "live" and isinstance(note, dict) and note.get("auto") and \
                note.get("mode") != "live":
            continue
        for section, values in json.loads(row["params_json"]).items():
            out.setdefault(section, {}).update(values)
        last = row["id"]
    return last, out