#!/usr/bin/env python3
"""LOHO Quiz Server — supports Firebase Firestore, PostgreSQL, or SQLite.

Priority: FIREBASE_CREDENTIALS → DATABASE_URL (postgres) → SQLite (local dev).
"""
import json
import os
import queue
import re
import sqlite3
import threading
import time
from datetime import datetime, timezone
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

ROOT_DIR = Path(__file__).resolve().parent
DATA_DIR = ROOT_DIR / "data"
SQLITE_DB_PATH = DATA_DIR / "quiz_results.db"

FIREBASE_CREDENTIALS = os.environ.get("FIREBASE_CREDENTIALS", "")
DATABASE_URL = os.environ.get("DATABASE_URL", "")

USE_FIREBASE = bool(FIREBASE_CREDENTIALS)
USE_POSTGRES = (not USE_FIREBASE) and DATABASE_URL.startswith("postgres")
USE_SQLITE = (not USE_FIREBASE) and (not USE_POSTGRES)

EMAIL_PATTERN = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
VALID_OUTCOMES = {"victory", "game_over"}
DB_TIMEOUT_SECONDS = 30
DB_BUSY_TIMEOUT_MS = 7000
DB_LOCK_RETRIES = 6
DB_RETRY_BASE_DELAY_SECONDS = 0.05
DEFAULT_RESULT_LIMIT = 5000
MAX_RESULT_LIMIT = 20000
SSE_QUEUE_SIZE = 128
SSE_HEARTBEAT_SECONDS = 15

# Module-level Firestore client — set in init_db()
_fs_db = None
_FS_COLLECTION = "game_results"


# ---------------------------------------------------------------------------
# Database initialisation
# ---------------------------------------------------------------------------

def init_db() -> None:
    global _fs_db

    if USE_FIREBASE:
        import base64
        import firebase_admin
        from firebase_admin import credentials, firestore

        raw = FIREBASE_CREDENTIALS
        try:
            cred_dict = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            cred_dict = json.loads(base64.b64decode(raw).decode("utf-8"))

        cred = credentials.Certificate(cred_dict)
        firebase_admin.initialize_app(cred)
        _fs_db = firestore.client()
        # Firestore creates collections on demand — nothing else to do.

    elif USE_POSTGRES:
        import psycopg2

        conn = psycopg2.connect(DATABASE_URL)
        try:
            conn.autocommit = False
            with conn.cursor() as cur:
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS game_results (
                        id SERIAL PRIMARY KEY,
                        player_name TEXT NOT NULL,
                        player_email TEXT NOT NULL,
                        score INTEGER NOT NULL,
                        correct_count INTEGER NOT NULL,
                        total_answered INTEGER NOT NULL,
                        accuracy INTEGER NOT NULL,
                        outcome TEXT NOT NULL,
                        created_at TEXT NOT NULL
                    )
                    """
                )
                cur.execute(
                    "CREATE INDEX IF NOT EXISTS idx_gr_created "
                    "ON game_results(created_at DESC)"
                )
                cur.execute(
                    "CREATE INDEX IF NOT EXISTS idx_gr_rank "
                    "ON game_results(score DESC, accuracy DESC, created_at ASC, id ASC)"
                )
            conn.commit()
        finally:
            conn.close()

    else:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(SQLITE_DB_PATH, timeout=DB_TIMEOUT_SECONDS) as conn:
            conn.execute(f"PRAGMA busy_timeout={DB_BUSY_TIMEOUT_MS}")
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS game_results (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    player_name TEXT NOT NULL,
                    player_email TEXT NOT NULL,
                    score INTEGER NOT NULL,
                    correct_count INTEGER NOT NULL,
                    total_answered INTEGER NOT NULL,
                    accuracy INTEGER NOT NULL,
                    outcome TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_gr_created "
                "ON game_results(created_at DESC)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_gr_rank "
                "ON game_results(score DESC, accuracy DESC, created_at ASC, id ASC)"
            )


# ---------------------------------------------------------------------------
# Firestore helpers
# ---------------------------------------------------------------------------

def _fs_insert(player_name, player_email, score, correct_count,
               total_answered, accuracy, outcome, created_at):
    """Insert a document into Firestore and return a numeric ID."""
    numeric_id = int(time.time() * 1_000_000)  # microsecond-precision ID
    doc = {
        "id": numeric_id,
        "player_name": player_name,
        "player_email": player_email,
        "score": score,
        "correct_count": correct_count,
        "total_answered": total_answered,
        "accuracy": accuracy,
        "outcome": outcome,
        "created_at": created_at,
    }
    _fs_db.collection(_FS_COLLECTION).document(str(numeric_id)).set(doc)
    return numeric_id


def _fs_to_api(doc_dict):
    """Convert a Firestore document dict to the API response format."""
    d = doc_dict
    return {
        "id": d.get("id", 0),
        "playerName": d.get("player_name", ""),
        "playerEmail": d.get("player_email", ""),
        "score": d.get("score", 0),
        "correctCount": d.get("correct_count", 0),
        "totalAnswered": d.get("total_answered", 0),
        "accuracy": d.get("accuracy", 0),
        "outcome": d.get("outcome", ""),
        "createdAt": d.get("created_at", ""),
    }


def _fs_list(sort_mode, limit_val):
    """Fetch all documents, sort in Python, and return API-formatted dicts."""
    docs = _fs_db.collection(_FS_COLLECTION).stream()
    results = [_fs_to_api(doc.to_dict()) for doc in docs]

    if sort_mode == "leaderboard":
        results.sort(key=lambda r: (
            -r["score"], -r["accuracy"], r["createdAt"], r["id"]
        ))
    else:
        results.sort(key=lambda r: -r["id"])

    if limit_val is not None:
        results = results[:limit_val]

    return results


def _fs_clear():
    """Delete every document in the collection. Returns count deleted."""
    docs = list(_fs_db.collection(_FS_COLLECTION).stream())
    count = len(docs)
    # Firestore batch limit is 500
    for i in range(0, count, 500):
        batch = _fs_db.batch()
        for doc in docs[i : i + 500]:
            batch.delete(doc.reference)
        batch.commit()
    return count


# ---------------------------------------------------------------------------
# PostgreSQL helpers
# ---------------------------------------------------------------------------

def _open_pg():
    import psycopg2

    conn = psycopg2.connect(DATABASE_URL)
    conn.autocommit = False
    return conn


def _pg_insert_row(conn, sql, params):
    with conn.cursor() as cur:
        cur.execute(sql, params)
        row = cur.fetchone()
        conn.commit()
        return row[0] if row else None


def _pg_fetchall(conn, sql, params=None):
    import psycopg2.extras

    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(sql, params or ())
        return cur.fetchall()


def _pg_execute(conn, sql, params=None):
    with conn.cursor() as cur:
        cur.execute(sql, params or ())
        rowcount = cur.rowcount
        conn.commit()
        return rowcount


# ---------------------------------------------------------------------------
# SQLite helpers
# ---------------------------------------------------------------------------

def _open_sqlite():
    conn = sqlite3.connect(SQLITE_DB_PATH, timeout=DB_TIMEOUT_SECONDS)
    conn.execute(f"PRAGMA busy_timeout={DB_BUSY_TIMEOUT_MS}")
    return conn


# ---------------------------------------------------------------------------
# Retry helper
# ---------------------------------------------------------------------------

def _is_retryable(exc: Exception) -> bool:
    if isinstance(exc, sqlite3.OperationalError):
        msg = str(exc).lower()
        return "database is locked" in msg or "database is busy" in msg
    if USE_POSTGRES:
        import psycopg2
        return isinstance(exc, (psycopg2.OperationalError, psycopg2.InterfaceError))
    return False


def with_db_retry(operation):
    delay = DB_RETRY_BASE_DELAY_SECONDS
    for attempt in range(DB_LOCK_RETRIES):
        try:
            return operation()
        except Exception as exc:
            if not _is_retryable(exc) or attempt == DB_LOCK_RETRIES - 1:
                raise
            time.sleep(delay)
            delay *= 2


# ---------------------------------------------------------------------------
# Unified database operations
# ---------------------------------------------------------------------------

def db_insert_result(player_name, player_email, score, correct_count,
                     total_answered, accuracy, outcome, created_at):
    """Insert a game result and return the new ID."""
    params = (player_name, player_email, score, correct_count,
              total_answered, accuracy, outcome, created_at)

    if USE_FIREBASE:
        return _fs_insert(*params)

    if USE_POSTGRES:
        def do_insert():
            conn = _open_pg()
            try:
                return _pg_insert_row(
                    conn,
                    """
                    INSERT INTO game_results (
                        player_name, player_email, score,
                        correct_count, total_answered, accuracy,
                        outcome, created_at
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                    RETURNING id
                    """,
                    params,
                )
            finally:
                conn.close()
        return with_db_retry(do_insert)

    # SQLite
    def do_insert():
        with _open_sqlite() as conn:
            cursor = conn.execute(
                """
                INSERT INTO game_results (
                    player_name, player_email, score,
                    correct_count, total_answered, accuracy,
                    outcome, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                params,
            )
            return cursor.lastrowid
    return with_db_retry(do_insert)


def db_list_results(sort_mode, limit_val):
    """Return a list of result dicts with camelCase keys."""
    if USE_FIREBASE:
        return _fs_list(sort_mode, limit_val)

    order_clause = (
        "score DESC, accuracy DESC, created_at ASC, id ASC"
        if sort_mode == "leaderboard"
        else "id DESC"
    )
    base_sql = f"""
        SELECT
            id,
            player_name  AS "playerName",
            player_email AS "playerEmail",
            score,
            correct_count  AS "correctCount",
            total_answered AS "totalAnswered",
            accuracy,
            outcome,
            created_at AS "createdAt"
        FROM game_results
        ORDER BY {order_clause}
    """

    if USE_POSTGRES:
        def do_fetch():
            conn = _open_pg()
            try:
                if limit_val is None:
                    return _pg_fetchall(conn, base_sql)
                return _pg_fetchall(conn, f"{base_sql} LIMIT %s", (limit_val,))
            finally:
                conn.close()
        rows = with_db_retry(do_fetch)
        return [dict(r) for r in rows]

    # SQLite
    def do_fetch():
        with _open_sqlite() as conn:
            conn.row_factory = sqlite3.Row
            if limit_val is None:
                return conn.execute(base_sql).fetchall()
            return conn.execute(f"{base_sql} LIMIT ?", (limit_val,)).fetchall()
    rows = with_db_retry(do_fetch)
    return [dict(r) for r in rows]


def db_clear_results():
    """Delete all results and return the count of deleted records."""
    if USE_FIREBASE:
        return _fs_clear()

    if USE_POSTGRES:
        def do_clear():
            conn = _open_pg()
            try:
                return _pg_execute(conn, "DELETE FROM game_results")
            finally:
                conn.close()
        return with_db_retry(do_clear)

    # SQLite
    def do_clear():
        with _open_sqlite() as conn:
            cursor = conn.execute("DELETE FROM game_results")
            return cursor.rowcount
    return with_db_retry(do_clear)


# ---------------------------------------------------------------------------
# JSON body helpers
# ---------------------------------------------------------------------------

def read_json_body(handler: SimpleHTTPRequestHandler) -> dict:
    raw_length = handler.headers.get("Content-Length")
    if raw_length is None:
        raise ValueError("Missing Content-Length header")

    length = int(raw_length)
    body = handler.rfile.read(length)
    try:
        payload = json.loads(body.decode("utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError("Invalid JSON payload") from exc

    if not isinstance(payload, dict):
        raise ValueError("JSON body must be an object")
    return payload


def parse_int_field(payload: dict, field: str, min_value: int = 0) -> int:
    value = payload.get(field)
    if isinstance(value, bool):
        raise ValueError(f"{field} must be a number")
    if isinstance(value, int):
        number = value
    elif isinstance(value, str) and value.strip().isdigit():
        number = int(value.strip())
    else:
        raise ValueError(f"{field} must be a number")

    if number < min_value:
        raise ValueError(f"{field} must be >= {min_value}")
    return number


# ---------------------------------------------------------------------------
# HTTP Handler
# ---------------------------------------------------------------------------

BLOCKED_PREFIXES = (
    "/.git", "/.env", "/.venv", "/__pycache__",
    "/server.py", "/render.yaml", "/requirements.txt",
    "/DB_SETUP.md", "/.gitignore", "/data/",
)


class QuizHandler(SimpleHTTPRequestHandler):
    subscribers = set()
    subscribers_lock = threading.Lock()

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(ROOT_DIR), **kwargs)

    def end_headers(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        super().end_headers()

    def _is_blocked(self, path: str) -> bool:
        return any(path.startswith(p) for p in BLOCKED_PREFIXES)

    def do_OPTIONS(self):
        self.send_response(204)
        self.end_headers()

    def do_GET(self):
        parsed = urlparse(self.path)

        # Redirect root to the main game page
        if parsed.path in ("", "/"):
            self.send_response(302)
            self.send_header("Location", "/Zoho-Logo2.html")
            self.end_headers()
            return

        # Block sensitive files
        if self._is_blocked(parsed.path):
            self.send_json(403, {"error": "Forbidden"})
            return

        if parsed.path == "/api/results/stream":
            return self.handle_results_stream()
        if parsed.path == "/api/results":
            return self.handle_list_results(parsed)
        super().do_GET()

    def do_POST(self):
        parsed = urlparse(self.path)
        if parsed.path == "/api/results":
            return self.handle_create_result()
        self.send_json(404, {"error": "Not found"})

    def do_DELETE(self):
        parsed = urlparse(self.path)
        if parsed.path == "/api/results":
            return self.handle_clear_results()
        self.send_json(404, {"error": "Not found"})

    # ---- SSE live updates ----

    @classmethod
    def add_subscriber(cls, q):
        with cls.subscribers_lock:
            cls.subscribers.add(q)

    @classmethod
    def remove_subscriber(cls, q):
        with cls.subscribers_lock:
            cls.subscribers.discard(q)

    @classmethod
    def broadcast_event(cls, event_name: str, payload: dict):
        msg = f"event: {event_name}\ndata: {json.dumps(payload, separators=(',', ':'))}\n\n"
        with cls.subscribers_lock:
            subs = list(cls.subscribers)
        for q in subs:
            try:
                q.put_nowait(msg)
            except queue.Full:
                try:
                    q.get_nowait()
                except queue.Empty:
                    pass
                try:
                    q.put_nowait(msg)
                except queue.Full:
                    pass

    def handle_results_stream(self):
        q = queue.Queue(maxsize=SSE_QUEUE_SIZE)
        self.add_subscriber(q)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        try:
            self.wfile.write(b"retry: 2000\n\n")
            self.wfile.flush()
            while True:
                try:
                    msg = q.get(timeout=SSE_HEARTBEAT_SECONDS)
                except queue.Empty:
                    msg = ": ping\n\n"
                self.wfile.write(msg.encode("utf-8"))
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            self.remove_subscriber(q)

    # ---- Create result ----

    def handle_create_result(self):
        try:
            payload = read_json_body(self)

            player_name = str(payload.get("playerName", "")).strip()
            player_email = str(payload.get("playerEmail", "")).strip()
            score = parse_int_field(payload, "score", min_value=0)
            correct_count = parse_int_field(payload, "correctCount", min_value=0)
            total_answered = parse_int_field(payload, "totalAnswered", min_value=0)
            outcome = str(payload.get("outcome", "")).strip()

            if len(player_name) < 2:
                raise ValueError("playerName must be at least 2 characters")
            if not EMAIL_PATTERN.match(player_email):
                raise ValueError("playerEmail is invalid")
            if total_answered == 0 and correct_count > 0:
                raise ValueError("correctCount cannot be greater than totalAnswered")
            if correct_count > total_answered:
                raise ValueError("correctCount cannot be greater than totalAnswered")
            if outcome not in VALID_OUTCOMES:
                raise ValueError("outcome must be victory or game_over")

            accuracy = round((correct_count / total_answered) * 100) if total_answered > 0 else 0
            created_at = datetime.now(timezone.utc).isoformat(timespec="milliseconds")

            result_id = db_insert_result(
                player_name, player_email, score,
                correct_count, total_answered, accuracy,
                outcome, created_at,
            )

            result = {
                "id": result_id,
                "playerName": player_name,
                "playerEmail": player_email,
                "score": score,
                "correctCount": correct_count,
                "totalAnswered": total_answered,
                "accuracy": accuracy,
                "outcome": outcome,
                "createdAt": created_at,
            }

            self.send_json(201, result)
            self.broadcast_event("result_created", {"result": result})
        except ValueError as exc:
            self.send_json(400, {"error": str(exc)})
        except Exception:
            self.send_json(500, {"error": "Internal server error"})

    # ---- List results ----

    def handle_list_results(self, parsed):
        try:
            qs = parse_qs(parsed.query)
            raw_limit = qs.get("limit", [str(DEFAULT_RESULT_LIMIT)])[0].strip().lower()
            sort_mode = qs.get("sort", ["latest"])[0].strip().lower()

            limit_val = None
            if raw_limit != "all":
                try:
                    limit_val = max(1, min(MAX_RESULT_LIMIT, int(raw_limit)))
                except ValueError:
                    limit_val = DEFAULT_RESULT_LIMIT

            results = db_list_results(sort_mode, limit_val)
            self.send_json(200, {"results": results})
        except Exception:
            self.send_json(500, {"error": "Internal server error"})

    # ---- Clear results ----

    def handle_clear_results(self):
        try:
            deleted = db_clear_results()
            self.send_json(200, {
                "deleted": deleted,
                "message": "All leaderboard records were cleared.",
            })
            self.broadcast_event("results_cleared", {"deleted": deleted})
        except Exception:
            self.send_json(500, {"error": "Internal server error"})

    # ---- JSON response ----

    def send_json(self, status_code: int, payload: dict):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


# ---------------------------------------------------------------------------
# Server
# ---------------------------------------------------------------------------

class HighCapacityHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 256


def _db_label() -> str:
    if USE_FIREBASE:
        return "Firebase Firestore"
    if USE_POSTGRES:
        host = DATABASE_URL.split("@")[-1].split("/")[0]
        return f"PostgreSQL ({host})"
    return f"SQLite ({SQLITE_DB_PATH})"


def run_server():
    init_db()
    port = int(os.environ.get("PORT", 8000))
    server = HighCapacityHTTPServer(("0.0.0.0", port), QuizHandler)
    print(f"Server running at: http://localhost:{port}")
    print(f"Database: {_db_label()}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nServer stopped.")
    finally:
        server.server_close()


if __name__ == "__main__":
    run_server()
