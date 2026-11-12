import csv
from pathlib import Path


def append(path: str | Path, row: dict) -> None:
    """journal.csv: exportación legible; la fuente de verdad es SQLite."""
    path = Path(path)
    if path.exists():
        with path.open(encoding="utf-8") as f:
            header = f.readline().strip().split(",")
        if header != list(row):  # columnas nuevas (p. ej. un componente más): fichero nuevo
            n = 1
            while (old := path.with_name(f"{path.stem}.{n}{path.suffix}")).exists():
                n += 1
            path.rename(old)
    new = not path.exists()
    with path.open("a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(row))
        if new:
            writer.writeheader()
        writer.writerow(row)
