from __future__ import annotations
import json
import os
import sqlite3
import sys
import traceback
from pathlib import Path
from typing import Any


BASE_DIR = Path(__file__).resolve().parent
DATABASE_PATH = Path(os.environ.get("KANTHARI_DB_PATH", BASE_DIR / "kanthari.db"))
ALLOW_WRITE = os.environ.get("KANTHARI_MCP_ALLOW_WRITE", "").lower() in {"1", "true", "yes"}


def connect() -> sqlite3.Connection:
    connection = sqlite3.connect(DATABASE_PATH)
    connection.row_factory = sqlite3.Row
    return connection


def rows_to_dicts(rows: list[sqlite3.Row]) -> list[dict[str, Any]]:
    return [dict(row) for row in rows]


def list_tables() -> dict[str, Any]:
    with connect() as db:
        rows = db.execute(
            """
            SELECT name, type
            FROM sqlite_master
            WHERE type IN ('table', 'view') AND name NOT LIKE 'sqlite_%'
            ORDER BY type, name
            """
        ).fetchall()
    return {"database": str(DATABASE_PATH), "tables": rows_to_dicts(rows)}


def describe_table(table: str) -> dict[str, Any]:
    if not table.replace("_", "").isalnum():
        raise ValueError("Invalid table name.")

    with connect() as db:
        exists = db.execute(
            """
            SELECT name
            FROM sqlite_master
            WHERE type IN ('table', 'view') AND name = ?
            """,
            (table,),
        ).fetchone()
        if exists is None:
            raise ValueError(f"Unknown table: {table}")

        columns = db.execute(f'PRAGMA table_info("{table}")').fetchall()
        foreign_keys = db.execute(f'PRAGMA foreign_key_list("{table}")').fetchall()
        indexes = db.execute(f'PRAGMA index_list("{table}")').fetchall()

    return {
        "table": table,
        "columns": rows_to_dicts(columns),
        "foreign_keys": rows_to_dicts(foreign_keys),
        "indexes": rows_to_dicts(indexes),
    }


def database_summary() -> dict[str, Any]:
    summary = list_tables()
    with connect() as db:
        for table in summary["tables"]:
            table_name = table["name"]
            count = db.execute(f'SELECT COUNT(*) AS count FROM "{table_name}"').fetchone()
            table["row_count"] = count["count"]
    return summary


def is_read_only_sql(sql: str) -> bool:
    stripped = sql.strip().lower()
    return stripped.startswith(("select", "with", "pragma", "explain"))


def query_database(sql: str, params: list[Any] | None = None, max_rows: int = 100) -> dict[str, Any]:
    if not ALLOW_WRITE and not is_read_only_sql(sql):
        raise ValueError("Only read-only SQL is enabled. Set KANTHARI_MCP_ALLOW_WRITE=1 to allow writes.")

    max_rows = max(1, min(int(max_rows), 500))
    with connect() as db:
        cursor = db.execute(sql, params or [])
        if cursor.description is None:
            db.commit()
            return {"row_count": cursor.rowcount, "rows": []}

        rows = cursor.fetchmany(max_rows)
        columns = [column[0] for column in cursor.description]
        return {"columns": columns, "rows": rows_to_dicts(rows), "max_rows": max_rows}


TOOLS = {
    "database_summary": {
        "description": "Show the Kanthari SQLite database path, tables, and row counts.",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
        "handler": lambda _args: database_summary(),
    },
    "list_tables": {
        "description": "List user tables and views in the Kanthari SQLite database.",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
        "handler": lambda _args: list_tables(),
    },
    "describe_table": {
        "description": "Describe columns, foreign keys, and indexes for a table.",
        "inputSchema": {
            "type": "object",
            "properties": {"table": {"type": "string"}},
            "required": ["table"],
            "additionalProperties": False,
        },
        "handler": lambda args: describe_table(args["table"]),
    },
    "query_database": {
        "description": "Run SQL against the Kanthari SQLite database. Read-only SQL is enabled by default.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "sql": {"type": "string"},
                "params": {"type": "array", "items": {}},
                "max_rows": {"type": "integer", "minimum": 1, "maximum": 500},
            },
            "required": ["sql"],
            "additionalProperties": False,
        },
        "handler": lambda args: query_database(args["sql"], args.get("params"), args.get("max_rows", 100)),
    },
}


def response(message_id: Any, result: Any) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": message_id, "result": result}


def error_response(message_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": message_id, "error": {"code": code, "message": message}}


def text_content(value: Any) -> list[dict[str, str]]:
    return [{"type": "text", "text": json.dumps(value, indent=2, default=str)}]


def handle_request(message: dict[str, Any]) -> dict[str, Any] | None:
    method = message.get("method")
    message_id = message.get("id")
    params = message.get("params") or {}

    if method == "initialize":
        return response(
            message_id,
            {
                "protocolVersion": params.get("protocolVersion", "2024-11-05"),
                "capabilities": {"tools": {}, "resources": {}},
                "serverInfo": {"name": "kanthari-sqlite", "version": "1.0.0"},
            },
        )

    if method == "notifications/initialized":
        return None

    if method == "tools/list":
        return response(
            message_id,
            {
                "tools": [
                    {
                        "name": name,
                        "description": tool["description"],
                        "inputSchema": tool["inputSchema"],
                    }
                    for name, tool in TOOLS.items()
                ]
            },
        )

    if method == "tools/call":
        tool_name = params.get("name")
        arguments = params.get("arguments") or {}
        if tool_name not in TOOLS:
            return error_response(message_id, -32602, f"Unknown tool: {tool_name}")

        try:
            result = TOOLS[tool_name]["handler"](arguments)
            return response(message_id, {"content": text_content(result)})
        except Exception as exc:
            return response(message_id, {"content": text_content({"error": str(exc)}), "isError": True})

    if method == "resources/list":
        return response(
            message_id,
            {
                "resources": [
                    {
                        "uri": "sqlite://kanthari/schema",
                        "name": "Kanthari SQLite schema",
                        "description": "Tables, columns, keys, indexes, and row counts for kanthari.db.",
                        "mimeType": "application/json",
                    }
                ]
            },
        )

    if method == "resources/read":
        uri = params.get("uri")
        if uri != "sqlite://kanthari/schema":
            return error_response(message_id, -32602, f"Unknown resource: {uri}")

        summary = database_summary()
        summary["schema"] = [describe_table(table["name"]) for table in summary["tables"]]
        return response(
            message_id,
            {
                "contents": [
                    {
                        "uri": uri,
                        "mimeType": "application/json",
                        "text": json.dumps(summary, indent=2, default=str),
                    }
                ]
            },
        )

    if message_id is None:
        return None
    return error_response(message_id, -32601, f"Method not found: {method}")


def main() -> int:
    if not DATABASE_PATH.exists():
        print(f"Database not found: {DATABASE_PATH}", file=sys.stderr)

    for line in sys.stdin:
        if not line.strip():
            continue
        try:
            message = json.loads(line)
            result = handle_request(message)
        except Exception:
            result = error_response(None, -32603, traceback.format_exc())

        if result is not None:
            print(json.dumps(result), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
