"""Customer datasets: import tabular data and let agents query it with read-only SQL.

Each customer's datasets live in their own SQLite file. Agents can only run SELECT
statements, enforced by SQLite's authorizer (not by inspecting the SQL text), and only on
the tables listed in the tool's config.
"""

import csv
import io
import json
import re
import sqlite3
from pathlib import Path

from .store import Store

_NAME = re.compile(r"^[a-z][a-z0-9_]{0,62}$")
MAX_ROWS = 200
MAX_COLUMNS = 500
_IDENTIFIER = re.compile(r"^(\+|0\d)")
# Plain decimal numbers only: Python's int()/float() also accept "2026_01", "nan", "inf" and
# non-ASCII digits, which would silently turn codes and words into numbers.
_INTEGER = re.compile(r"^[+-]?[0-9]+$")
_REAL = re.compile(r"^[+-]?([0-9]+\.?[0-9]*|\.[0-9]+)([eE][+-]?[0-9]+)?$")


def _ident(name: str) -> str:
    name = re.sub(r"[^a-z0-9_]+", "_", name.strip().lower()).strip("_") or "col"
    return name if name[0].isalpha() else f"c_{name}"


def _coerce(values: list[str]) -> tuple[str, list]:
    """Pick INTEGER, REAL or TEXT for a column and convert its values. Identifier-like values
    (leading zeros or '+', e.g. apartment 0904 or phone numbers) stay text so nothing is lost."""
    if any(_IDENTIFIER.match(v.strip()) for v in values):
        return "TEXT", [v if v != "" else None for v in values]
    filled = [v.strip() for v in values if v.strip() != ""]
    for sql_type, pattern, cast in (("INTEGER", _INTEGER, int), ("REAL", _REAL, float)):
        if all(pattern.match(v) for v in filled):
            return sql_type, [cast(v.strip()) if v.strip() != "" else None for v in values]
    return "TEXT", [v if v != "" else None for v in values]


MAX_IMPORT_ROWS = 200_000


def import_csv(store: Store, customer_id: int, name: str, csv_path: str | Path) -> tuple[list[str], int]:
    """Create or replace dataset `name` for the customer from a CSV file."""
    return import_csv_text(store, customer_id, name, Path(csv_path).read_text(encoding="utf-8-sig"))


def import_csv_text(store: Store, customer_id: int, name: str, text: str) -> tuple[list[str], int]:
    """Create or replace dataset `name` for the customer from CSV text."""
    if not _NAME.match(name):
        raise ValueError("Dataset names use lowercase letters, digits and underscores, starting with a letter")
    if name.startswith("sqlite_"):
        raise ValueError("Dataset names can't start with 'sqlite_': the database reserves that name. Choose another name.")
    text = text.lstrip("\ufeff")
    if not text.strip():
        raise ValueError("The CSV file is empty")
    try:
        dialect = csv.Sniffer().sniff(text[:4096], delimiters=",;\t")
    except csv.Error:
        dialect = csv.excel
    rows = [r for r in csv.reader(io.StringIO(text), dialect) if any(cell.strip() for cell in r)]
    if len(rows) < 2:
        raise ValueError("The CSV needs a header row and at least one data row")
    if len(rows) - 1 > MAX_IMPORT_ROWS:
        raise ValueError(f"Too many rows (limit {MAX_IMPORT_ROWS})")
    header, body = rows[0], rows[1:]
    if len(header) > MAX_COLUMNS:
        raise ValueError(f"Too many columns ({len(header)}; limit {MAX_COLUMNS})")
    for n, r in enumerate(body, start=2):
        if len(r) > len(header) and any(cell.strip() for cell in r[len(header):]):
            raise ValueError(f"Row {n} has {len(r)} values but the header has {len(header)} columns. A value probably "
                             "contains an unquoted separator (for example 'Acme, Inc'); put such values in quotes.")
    columns, seen = [], set()
    for h in header:
        col = _ident(h)
        while col in seen:
            col += "_2"
        seen.add(col)
        columns.append(col)
    body = [(r + [""] * (len(columns) - len(r)))[:len(columns)] for r in body]
    typed = [_coerce([r[i] for r in body]) for i in range(len(columns))]

    # Replace the table in one transaction, so a failed import leaves the existing data untouched.
    conn = sqlite3.connect(store.customer_data_path(customer_id), isolation_level=None)
    try:
        conn.execute("BEGIN")
        conn.execute(f'DROP TABLE IF EXISTS "{name}"')
        col_defs = ", ".join(f'"{c}" {t}' for c, (t, _) in zip(columns, typed))
        conn.execute(f'CREATE TABLE "{name}" ({col_defs})')
        values = list(zip(*(vals for _, vals in typed))) if body else []
        conn.executemany(f'INSERT INTO "{name}" VALUES ({", ".join("?" * len(columns))})', values)
        conn.execute("COMMIT")
    except sqlite3.Error as e:
        conn.execute("ROLLBACK")
        raise ValueError(f"The data could not be stored: {e}") from e
    finally:
        conn.close()
    store.register_dataset(customer_id, name, [f"{c} {t}" for c, (t, _) in zip(columns, typed)], len(body))
    return columns, len(body)


def describe(store: Store, customer_id: int, allowed: list[str]) -> str:
    """Schema text for the agent's tool description."""
    lines = []
    for ds in store.datasets(customer_id):
        if ds["name"] in allowed:
            lines.append(f"- {ds['name']} ({ds['row_count']} rows): {', '.join(json.loads(ds['columns_json']))}")
    return "\n".join(lines) or "- (no datasets imported yet)"


def query(store: Store, customer_id: int, allowed: list[str], sql: str) -> str:
    """Run a read-only query restricted to `allowed` tables. Returns a text table."""
    path = store.customer_data_path(customer_id)
    if not path.exists():
        return "Error: no datasets have been imported for this customer."
    allowed_set = set(allowed)

    def authorizer(action, arg1, arg2, db_name, trigger):
        if action == sqlite3.SQLITE_SELECT or action == sqlite3.SQLITE_FUNCTION:
            return sqlite3.SQLITE_OK
        if action == sqlite3.SQLITE_READ:
            return sqlite3.SQLITE_OK if arg1 in allowed_set else sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_DENY

    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.set_authorizer(authorizer)
    try:
        cur = conn.execute(sql)
        cols = [d[0] for d in cur.description or []]
        rows = cur.fetchmany(MAX_ROWS + 1)
    except sqlite3.DatabaseError as e:
        return f"Error: {e}. Only SELECT queries on these tables are allowed: {', '.join(sorted(allowed_set))}."
    finally:
        conn.close()
    if not cols:
        return "Query returned no columns."
    truncated = len(rows) > MAX_ROWS
    rows = rows[:MAX_ROWS]
    out = [" | ".join(cols)] + [" | ".join("" if v is None else str(v) for v in r) for r in rows]
    if not rows:
        out.append("(no rows)")
    if truncated:
        out.append(f"(showing first {MAX_ROWS} rows; add filters or aggregates)")
    return "\n".join(out)


def preview(store: Store, customer_id: int, name: str, limit: int = 20) -> dict:
    """Column names and the first rows of a dataset (for people, not agents)."""
    if not _NAME.match(name) or name not in {d["name"] for d in store.datasets(customer_id)}:
        raise KeyError(f"No dataset {name!r}")
    conn = sqlite3.connect(f"file:{store.customer_data_path(customer_id)}?mode=ro", uri=True)
    try:
        cur = conn.execute(f'SELECT * FROM "{name}" LIMIT ?', (limit,))
        return {"columns": [d[0] for d in cur.description], "rows": [list(r) for r in cur.fetchall()]}
    finally:
        conn.close()


def delete(store: Store, customer_id: int, name: str) -> None:
    if not _NAME.match(name) or name not in {d["name"] for d in store.datasets(customer_id)}:
        raise KeyError(f"No dataset {name!r}")
    conn = sqlite3.connect(store.customer_data_path(customer_id))
    try:
        conn.execute(f'DROP TABLE IF EXISTS "{name}"')
        conn.commit()
    finally:
        conn.close()
    store.conn.execute("DELETE FROM datasets WHERE customer_id = ? AND name = ?", (customer_id, name))
    store.conn.commit()
