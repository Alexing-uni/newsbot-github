"""Vigilante del bucle (Windows): arranca `--loop` si no está en marcha y lo reinicia si se cuelga.

Lo lanza la tarea programada "newsbot watchdog" cada 5 min (deploy/watchdog_task.ps1) y también
run_loop.cmd, así que todo arranque pasa por aquí y nunca hay dos bucles a la vez.
- Sin proceso `newsbot.bot --loop`        → lo arranca y avisa por Telegram.
- Proceso vivo pero sin latido reciente    → lo mata, lo arranca de nuevo y avisa.
- `data/stop` existe (stop_bot.cmd)        → no hace nada: lo paraste tú a propósito.
pythonw -m newsbot.watchdog [--quiet]   (--quiet: sin aviso de Telegram al arrancar, p. ej. al iniciar sesión)
"""
import argparse
import logging
import os
import subprocess
import sys
import time
from contextlib import closing
from logging.handlers import RotatingFileHandler
from pathlib import Path

from newsbot import config as config_mod
from newsbot.storage import db

log = logging.getLogger("newsbot.watchdog")
ROOT = Path(__file__).resolve().parents[1]
PATTERN = "*newsbot.bot*--loop*"
NO_WINDOW = 0x08000000  # CREATE_NO_WINDOW: que powershell no abra una consola cada 5 min


def loop_pids() -> list[int]:
    """PIDs de los procesos del bucle (el lanzador de la venv y su hijo cuentan los dos)."""
    out = subprocess.run(
        ["powershell", "-NoProfile", "-Command",
         # solo python(w).exe: la propia consulta de powershell también contiene el patrón
         f"Get-CimInstance Win32_Process -Filter \"Name like 'python%'\" "
         f"| Where-Object {{ $_.CommandLine -like '{PATTERN}' }} "
         "| Select-Object -ExpandProperty ProcessId"],
        capture_output=True, text=True, timeout=60, creationflags=NO_WINDOW).stdout
    return [int(x) for x in out.split() if x.isdigit() and int(x) != os.getpid()]


def kill(pids: list[int]) -> None:
    for pid in pids:
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True,
                       timeout=30, creationflags=NO_WINDOW)


def start() -> None:
    pythonw = ROOT / ".venv" / "Scripts" / "pythonw.exe"
    subprocess.Popen([str(pythonw), "-m", "newsbot.bot", "--loop"], cwd=ROOT, close_fds=True,
                     creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP)


def heartbeat_age(cfg: dict, now: float) -> float | None:
    path = Path(cfg["general"]["data_dir"]) / "newsbot.db"
    if not path.exists():
        return None
    with closing(db.connect(path)) as con:
        beat = db.KV(con).get("loop:heartbeat")
    return now - int(beat) if beat else None


def extra_cfgs(cfg: dict, path: str | Path = ROOT / "config.toml") -> list[dict]:
    try:
        return config_mod.load_portfolios(cfg, path) if cfg["general"].get("portfolios") else []
    except Exception as exc:
        log.warning("No se pudieron leer las carteras extra (%s)", type(exc).__name__)
        return []


def extra_dirs(cfg: dict, path: str | Path = ROOT / "config.toml") -> list[Path]:
    """data_dir de las carteras de general.portfolios; vacío si no hay o no se pueden leer."""
    try:
        if not cfg["general"].get("portfolios"):
            return []
        return [Path(c["general"]["data_dir"]) for c in config_mod.load_portfolios(cfg, path)]
    except Exception as exc:
        log.warning("No se pudieron leer las carteras extra (%s)", type(exc).__name__)
        return []


def check(cfg: dict, now: float | None = None, pids=loop_pids, kill=kill, start=start,
          notify=None, quiet: bool = False) -> str:
    """Una pasada del vigilante. Devuelve lo que hizo: ok, stopped, started o restarted."""
    from newsbot.reporting import telegram
    notify = notify or telegram.send
    now = time.time() if now is None else now
    data = Path(cfg["general"]["data_dir"])
    if (data / "stop").exists():
        return "stopped"
    running = pids()
    stale = max(3 * cfg["general"]["tick_minutes"] * 60, 20 * 60)  # un tick lento no es un cuelgue
    age = heartbeat_age(cfg, now)
    for x in extra_cfgs(cfg):  # carteras extra (bolsa): su hilo late en su propia base de datos
        x_age, x_stale = heartbeat_age(x, now), max(3 * x["general"]["tick_minutes"] * 60, 50 * 60)
        if x_age is not None and x_age > x_stale and (age is None or x_age > age):
            age, stale = x_age, x_stale  # un hilo de bolsa colgado también reinicia el proceso
    if running and (age is None or age < stale):
        return "ok"
    if running:  # vivo pero sin latir: colgado (red, exchange...). Se reinicia limpio.
        kill(running)
        (data / ".lock").unlink(missing_ok=True)
        for d in extra_dirs(cfg):  # candados de las carteras extra (bolsa): murieron con el bucle
            (d / ".lock").unlink(missing_ok=True)
        start()
        mins = int(age // 60) if age is not None else "?"
        log.warning("Bucle colgado (último latido hace %s min): reiniciado", mins)
        notify(f"🔁 Vigilante: el bot no daba señales de vida desde hace {mins} min. "
               "Lo he reiniciado.")
        return "restarted"
    (data / ".lock").unlink(missing_ok=True)  # el proceso murió: sus candados no valen
    for d in extra_dirs(cfg):
        (d / ".lock").unlink(missing_ok=True)
    start()
    log.warning("Bucle no estaba en marcha: arrancado")
    if not quiet:
        notify("🔁 Vigilante: el bot no estaba en marcha (¿se cerró o se reinició el PC?). "
               "Lo he arrancado.")
    return "started"


def _single_run(data: Path) -> bool:
    """Evita dos vigilantes a la vez (tarea programada + doble clic en run_loop.cmd)."""
    lock = data / ".watchdog.lock"
    try:
        os.close(os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY))
        return True
    except FileExistsError:
        if time.time() - lock.stat().st_mtime > 120:  # abandonado
            lock.unlink(missing_ok=True)
            return _single_run(data)
        return False


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--quiet", action="store_true", help="sin aviso de Telegram si lo arranca")
    args = p.parse_args()
    os.chdir(ROOT)  # la tarea programada arranca en System32
    from newsbot.bot import load_dotenv
    load_dotenv()
    cfg = config_mod.load()
    data = Path(cfg["general"]["data_dir"])
    data.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(data / "watchdog.log", maxBytes=200_000, backupCount=1,
                                  encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logging.basicConfig(level=logging.INFO, handlers=[handler], force=True)
    if not _single_run(data):
        return
    try:
        log.info("Vigilante: %s", check(cfg, quiet=args.quiet))
    except Exception:
        log.exception("Vigilante falló")
        sys.exit(1)
    finally:
        (data / ".watchdog.lock").unlink(missing_ok=True)


if __name__ == "__main__":
    main()
