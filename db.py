
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
PROJECTS_DIR = DATA_DIR / "projects"
DB_PATH = DATA_DIR / "lecture2anki.db"

DATA_DIR.mkdir(exist_ok=True)
PROJECTS_DIR.mkdir(exist_ok=True)

PBKDF2_ITERATIONS = 310_000


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


@contextmanager
def connect():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    try:
        yield con
        con.commit()
    finally:
        con.close()


def init_db() -> None:
    with connect() as con:
        con.executescript(
            """
            PRAGMA foreign_keys = ON;

            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                email TEXT NOT NULL UNIQUE COLLATE NOCASE,
                password_hash TEXT NOT NULL,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS projects (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                name TEXT NOT NULL,
                source_filename TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'created',
                settings_json TEXT NOT NULL DEFAULT '{}',
                summary_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS cards (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                project_id INTEGER NOT NULL,
                card_id TEXT NOT NULL,
                source_id TEXT,
                question_type TEXT,
                front TEXT NOT NULL,
                back TEXT NOT NULL,
                tags TEXT,
                source TEXT,
                accepted INTEGER NOT NULL DEFAULT 1,
                requires_image INTEGER NOT NULL DEFAULT 0,
                image_region_id TEXT,
                image_path TEXT,
                occluded_image_path TEXT,
                occlusion_applied INTEGER NOT NULL DEFAULT 0,
                occlusion_target TEXT,
                anki_ready INTEGER NOT NULL DEFAULT 1,
                raw_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(project_id, card_id),
                FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS idx_projects_user_id ON projects(user_id);
            CREATE INDEX IF NOT EXISTS idx_cards_project_id ON cards(project_id);
            """
        )


def _hash_password(password: str, salt: bytes | None = None) -> str:
    if salt is None:
        salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt,
        PBKDF2_ITERATIONS,
    )
    return (
        f"pbkdf2_sha256${PBKDF2_ITERATIONS}$"
        f"{base64.b64encode(salt).decode()}$"
        f"{base64.b64encode(digest).decode()}"
    )


def _verify_password(password: str, encoded: str) -> bool:
    try:
        algo, iterations, salt_b64, digest_b64 = encoded.split("$", 3)
        if algo != "pbkdf2_sha256":
            return False
        salt = base64.b64decode(salt_b64)
        expected = base64.b64decode(digest_b64)
        actual = hashlib.pbkdf2_hmac(
            "sha256",
            password.encode("utf-8"),
            salt,
            int(iterations),
        )
        return hmac.compare_digest(actual, expected)
    except Exception:
        return False


def create_user(email: str, password: str) -> tuple[bool, str]:
    email = email.strip().lower()
    if "@" not in email or len(email) < 5:
        return False, "有効なメールアドレスを入力してください。"
    if len(password) < 8:
        return False, "パスワードは8文字以上にしてください。"

    try:
        with connect() as con:
            con.execute(
                "INSERT INTO users(email, password_hash, created_at) VALUES (?, ?, ?)",
                (email, _hash_password(password), utc_now()),
            )
        return True, "ユーザー登録が完了しました。"
    except sqlite3.IntegrityError:
        return False, "このメールアドレスは既に登録されています。"


def authenticate_user(email: str, password: str) -> dict[str, Any] | None:
    with connect() as con:
        row = con.execute(
            "SELECT id, email, password_hash, created_at FROM users WHERE email = ? COLLATE NOCASE",
            (email.strip(),),
        ).fetchone()
    if not row or not _verify_password(password, row["password_hash"]):
        return None
    return {"id": row["id"], "email": row["email"], "created_at": row["created_at"]}


def create_project(
    user_id: int,
    name: str,
    source_filename: str,
    settings: dict[str, Any],
) -> int:
    now = utc_now()
    with connect() as con:
        cur = con.execute(
            """
            INSERT INTO projects(
                user_id, name, source_filename, status,
                settings_json, summary_json, created_at, updated_at
            )
            VALUES (?, ?, ?, 'processing', ?, '{}', ?, ?)
            """,
            (user_id, name, source_filename, json.dumps(settings, ensure_ascii=False), now, now),
        )
        return int(cur.lastrowid)


def update_project(
    project_id: int,
    status: str | None = None,
    summary: dict[str, Any] | None = None,
) -> None:
    fields = ["updated_at = ?"]
    params: list[Any] = [utc_now()]
    if status is not None:
        fields.append("status = ?")
        params.append(status)
    if summary is not None:
        fields.append("summary_json = ?")
        params.append(json.dumps(summary, ensure_ascii=False))
    params.append(project_id)

    with connect() as con:
        con.execute(
            f"UPDATE projects SET {', '.join(fields)} WHERE id = ?",
            params,
        )


def list_projects(user_id: int) -> list[dict[str, Any]]:
    with connect() as con:
        rows = con.execute(
            """
            SELECT
                p.*,
                COUNT(c.id) AS card_count,
                SUM(CASE WHEN c.accepted = 1 THEN 1 ELSE 0 END) AS accepted_count
            FROM projects p
            LEFT JOIN cards c ON c.project_id = p.id
            WHERE p.user_id = ?
            GROUP BY p.id
            ORDER BY p.updated_at DESC
            """,
            (user_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def get_project(project_id: int, user_id: int) -> dict[str, Any] | None:
    with connect() as con:
        row = con.execute(
            "SELECT * FROM projects WHERE id = ? AND user_id = ?",
            (project_id, user_id),
        ).fetchone()
    return dict(row) if row else None


def delete_project(project_id: int, user_id: int) -> bool:
    project = get_project(project_id, user_id)
    if not project:
        return False

    with connect() as con:
        con.execute(
            "DELETE FROM projects WHERE id = ? AND user_id = ?",
            (project_id, user_id),
        )

    pdir = PROJECTS_DIR / str(project_id)
    if pdir.exists():
        import shutil
        shutil.rmtree(pdir, ignore_errors=True)
    return True


def project_dir(project_id: int) -> Path:
    p = PROJECTS_DIR / str(project_id)
    p.mkdir(parents=True, exist_ok=True)
    return p


def _bool_value(v: Any) -> int:
    if isinstance(v, bool):
        return int(v)
    if v is None:
        return 0
    s = str(v).strip().lower()
    return 1 if s in {"true", "1", "yes", "y", "採用"} else 0


def import_cards_dataframe(project_id: int, df: pd.DataFrame) -> None:
    now = utc_now()
    records = []
    for _, row in df.iterrows():
        raw = {}
        for col in df.columns:
            val = row.get(col)
            if pd.isna(val):
                val = ""
            elif hasattr(val, "item"):
                try:
                    val = val.item()
                except Exception:
                    pass
            raw[col] = val

        accepted = _bool_value(row.get("採用", row.get("AnkiReady", True)))
        records.append(
            (
                project_id,
                str(row.get("CardID", "")),
                str(row.get("SourceFactID", row.get("SourceID", ""))),
                str(row.get("QuestionType", "")),
                str(row.get("Front", "")),
                str(row.get("Back", "")),
                str(row.get("Tags", "")),
                str(row.get("Source", "")),
                accepted,
                _bool_value(row.get("RequiresImage", False)),
                str(row.get("ImageRegionID", "")),
                str(row.get("ImagePath", "")),
                str(row.get("OccludedImagePath", "")),
                _bool_value(row.get("OcclusionApplied", False)),
                str(row.get("OcclusionTarget", "")),
                _bool_value(row.get("AnkiReady", True)),
                json.dumps(raw, ensure_ascii=False, default=str),
                now,
                now,
            )
        )

    with connect() as con:
        con.execute("DELETE FROM cards WHERE project_id = ?", (project_id,))
        con.executemany(
            """
            INSERT INTO cards(
                project_id, card_id, source_id, question_type,
                front, back, tags, source, accepted,
                requires_image, image_region_id, image_path,
                occluded_image_path, occlusion_applied,
                occlusion_target, anki_ready, raw_json,
                created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            records,
        )


def load_cards_dataframe(project_id: int) -> pd.DataFrame:
    with connect() as con:
        rows = con.execute(
            "SELECT * FROM cards WHERE project_id = ? ORDER BY id",
            (project_id,),
        ).fetchall()

    output = []
    for row in rows:
        d = dict(row)
        try:
            raw = json.loads(d.get("raw_json") or "{}")
        except Exception:
            raw = {}
        raw["採用"] = bool(d["accepted"])
        raw["CardID"] = d["card_id"]
        raw["SourceFactID"] = d["source_id"]
        raw["QuestionType"] = d["question_type"]
        raw["Front"] = d["front"]
        raw["Back"] = d["back"]
        raw["Tags"] = d["tags"]
        raw["Source"] = d["source"]
        raw["RequiresImage"] = bool(d["requires_image"])
        raw["ImageRegionID"] = d["image_region_id"]
        raw["ImagePath"] = d["image_path"]
        raw["OccludedImagePath"] = d["occluded_image_path"]
        raw["OcclusionApplied"] = bool(d["occlusion_applied"])
        raw["OcclusionTarget"] = d["occlusion_target"]
        raw["AnkiReady"] = bool(d["anki_ready"])
        output.append(raw)

    return pd.DataFrame(output)


def save_edited_cards(project_id: int, df: pd.DataFrame) -> None:
    now = utc_now()
    with connect() as con:
        for _, row in df.iterrows():
            card_id = str(row.get("CardID", ""))
            if not card_id:
                continue

            existing = con.execute(
                "SELECT raw_json FROM cards WHERE project_id = ? AND card_id = ?",
                (project_id, card_id),
            ).fetchone()
            try:
                raw = json.loads(existing["raw_json"] or "{}") if existing else {}
            except Exception:
                raw = {}

            # Preserve edits in raw JSON too.
            for col in df.columns:
                val = row.get(col)
                if pd.isna(val):
                    val = ""
                elif hasattr(val, "item"):
                    try:
                        val = val.item()
                    except Exception:
                        pass
                raw[col] = val

            con.execute(
                """
                UPDATE cards SET
                    front = ?, back = ?, tags = ?, accepted = ?,
                    raw_json = ?, updated_at = ?
                WHERE project_id = ? AND card_id = ?
                """,
                (
                    str(row.get("Front", "")),
                    str(row.get("Back", "")),
                    str(row.get("Tags", "")),
                    _bool_value(row.get("採用", True)),
                    json.dumps(raw, ensure_ascii=False, default=str),
                    now,
                    project_id,
                    card_id,
                ),
            )

        con.execute(
            "UPDATE projects SET updated_at = ? WHERE id = ?",
            (now, project_id),
        )
