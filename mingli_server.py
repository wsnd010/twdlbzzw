#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
命理解读师 · 多步骤 API 服务（紫微斗数 + 八字合参）
====================================================

把「排盘 → 出题/作答 → 手相上传比对 → 大模型解读」拆成多个 HTTP 接口，
用 MySQL 存储每个会话状态。第一步返回 id，后续步骤都带同一个 id 往下走。
同时集成八字（BaZi）：四柱/大运/流年/神煞排盘 + DeepSeek 解读。

启动：
    py -m uvicorn mingli_server:app --host 127.0.0.1 --port 8000

打开：
    可视化调用面板（网页）   http://127.0.0.1:8000/
    在线接口文档（Swagger）  http://127.0.0.1:8000/docs
    接口列表 JSON            http://127.0.0.1:8000/api

依赖：
    pip install iztro-py fastapi "uvicorn[standard]" pymysql python-multipart

MySQL（默认，可用环境变量覆盖）：
    DB_HOST=127.0.0.1  DB_PORT=3306  DB_USER=root  DB_PASSWORD=你的数据库密码  DB_NAME=mingli

密钥（环境变量）：
    DEEPSEEK_API_KEY=sk-xxx          # DeepSeek（解读/交叉比对）
    DASHSCOPE_API_KEY=sk-xxx         # 通义千问 qwen-vl（手相照片识别）

接口一览（紫微斗数）：
    POST /api/chart              排盘（公历/农历），返回 id
    GET  /api/{id}/questions     出题（校准问题，可选）
    POST /api/{id}/answers       提交答案（可选）
    POST /api/{id}/palm          手相照片/描述 → 特征提取 + 交叉比对（可选）
    POST /api/{id}/interpret     大模型解读 → reading + HTML
    GET  /api/{id} /chart /html  会话状态 / 排盘 JSON / 命盘 HTML

接口一览（八字）：
    POST /api/bazi/chart         四柱/大运/流年/神煞排盘，返回 id
    POST /api/bazi/{id}/interpret DeepSeek 八字解读 → 报告 HTML
    GET  /api/bazi/{id} /html    会话状态 / 报告 HTML

辅助：
    GET  /api/hours              时辰索引对照表（可 ?time=HH:MM 反查）
"""

import base64
import datetime
import hashlib
import json
import os
import random
import sys
import uuid
import urllib.error
import urllib.request
from typing import Any, Optional

import pymysql
from fastapi import FastAPI, File, Form, Header, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)
sys.path.insert(0, os.path.join(SCRIPT_DIR, "scripts"))

import mingli_api as engine                      # 复用紫微排盘 / 解读 / HTML 生成能力
from calibration import CALIBRATION_QUESTIONS    # 复用校准题库

# 八字模块（来自 bazi-skill，纯标准库）
sys.path.insert(0, os.path.join(SCRIPT_DIR, "bazi"))
from pai_pan import (  # noqa: E402
    compute as bazi_compute,
    format_report as bazi_format_report,
    format_lunar as bazi_format_lunar,
    lunar_to_solar as bazi_lunar_to_solar,
)
BAZI_REF_DIR = os.path.join(SCRIPT_DIR, "bazi", "references")

# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------
DB_CONFIG = {
    "host": os.environ.get("DB_HOST", "127.0.0.1"),
    "port": int(os.environ.get("DB_PORT", "3306")),
    "user": os.environ.get("DB_USER", "root"),
    "password": os.environ.get("DB_PASSWORD", ""),
    "database": os.environ.get("DB_NAME", "mingli"),
    "charset": "utf8mb4",
}
DEEPSEEK_API_KEY = os.environ.get("DEEPSEEK_API_KEY", "")
DEEPSEEK_MODEL = os.environ.get("DEEPSEEK_MODEL", "deepseek-v4-pro")
# 手相照片识别用通义千问 qwen-vl（DeepSeek 无图片输入能力）
DASHSCOPE_API_KEY = os.environ.get("DASHSCOPE_API_KEY", "")
QWEN_VL_MODEL = os.environ.get("QWEN_VL_MODEL", "qwen-vl-max")
QWEN_BASE_URL = os.environ.get("QWEN_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1")
WECHAT_APPID = os.environ.get("WECHAT_APPID", "")
WECHAT_SECRET = os.environ.get("WECHAT_SECRET", "")
USER_KEY_SALT = os.environ.get("USER_KEY_SALT", "mingli-miniapp")
MINIAPP_DAILY_INTERPRET_LIMIT = int(os.environ.get("MINIAPP_DAILY_INTERPRET_LIMIT", "1"))
REQUIRE_HTTPS = os.environ.get("REQUIRE_HTTPS", "0") == "1"

# 时辰索引对照（排盘接口的 hour 参数用）
HOUR_INDEX = [
    {"index": 0, "name": "早子时", "time": "23:00-00:00"},
    {"index": 1, "name": "丑时", "time": "01:00-03:00"},
    {"index": 2, "name": "寅时", "time": "03:00-05:00"},
    {"index": 3, "name": "卯时", "time": "05:00-07:00"},
    {"index": 4, "name": "辰时", "time": "07:00-09:00"},
    {"index": 5, "name": "巳时", "time": "09:00-11:00"},
    {"index": 6, "name": "午时", "time": "11:00-13:00"},
    {"index": 7, "name": "未时", "time": "13:00-15:00"},
    {"index": 8, "name": "申时", "time": "15:00-17:00"},
    {"index": 9, "name": "酉时", "time": "17:00-19:00"},
    {"index": 10, "name": "戌时", "time": "19:00-21:00"},
    {"index": 11, "name": "亥时", "time": "21:00-23:00"},
    {"index": 12, "name": "晚子时", "time": "23:00-00:00"},
]

UPLOAD_DIR = os.path.join(SCRIPT_DIR, "uploads")
OUTPUT_DIR = os.path.join(SCRIPT_DIR, "output")
# WEB_INDEX = os.path.join(SCRIPT_DIR, "web", "index.html")
os.makedirs(UPLOAD_DIR, exist_ok=True)
os.makedirs(OUTPUT_DIR, exist_ok=True)


# ---------------------------------------------------------------------------
# MySQL
# ---------------------------------------------------------------------------
def get_conn():
    return pymysql.connect(**DB_CONFIG, autocommit=True, cursorclass=pymysql.cursors.DictCursor)


def init_db():
    base = {k: v for k, v in DB_CONFIG.items() if k != "database"}
    conn = pymysql.connect(**base, autocommit=True)
    cur = conn.cursor()
    cur.execute(
        "CREATE DATABASE IF NOT EXISTS " + DB_CONFIG["database"] +
        " DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"
    )
    conn.close()

    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS sessions (
            id BIGINT AUTO_INCREMENT PRIMARY KEY,
            session_token VARCHAR(64) UNIQUE,
            user_key VARCHAR(96),
            wx_openid VARCHAR(96),
            solar_date VARCHAR(32),
            lunar_date VARCHAR(64),
            is_lunar TINYINT(1) DEFAULT 0,
            hour_index INT NOT NULL,
            gender VARCHAR(4) NOT NULL,
            is_leap TINYINT(1) DEFAULT 0,
            target_year INT,
            chart_json LONGTEXT,
            gezhi_name VARCHAR(64),
            answers_json LONGTEXT,
            palm_features LONGTEXT,
            hand_reading_json LONGTEXT,
            reading_json LONGTEXT,
            html_path VARCHAR(255),
            status VARCHAR(24) DEFAULT 'created',
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS mini_users (
            id BIGINT AUTO_INCREMENT PRIMARY KEY,
            user_key VARCHAR(96) NOT NULL UNIQUE,
            wx_openid VARCHAR(96) UNIQUE,
            session_key_hash VARCHAR(128),
            nickname VARCHAR(80),
            avatar_url VARCHAR(512),
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS bazi_sessions (
            id BIGINT AUTO_INCREMENT PRIMARY KEY,
            session_token VARCHAR(64) UNIQUE,
            user_key VARCHAR(96),
            wx_openid VARCHAR(96),
            solar_date VARCHAR(64),
            lunar_date VARCHAR(64),
            sex VARCHAR(4),
            bazi_json LONGTEXT,
            report LONGTEXT,
            reading_json LONGTEXT,
            html_path VARCHAR(255),
            status VARCHAR(24) DEFAULT 'created',
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """)
    ensure_column(cur, "sessions", "session_token", "VARCHAR(64) UNIQUE")
    ensure_column(cur, "sessions", "user_key", "VARCHAR(96)")
    ensure_column(cur, "sessions", "wx_openid", "VARCHAR(96)")
    ensure_column(cur, "bazi_sessions", "session_token", "VARCHAR(64) UNIQUE")
    ensure_column(cur, "bazi_sessions", "user_key", "VARCHAR(96)")
    ensure_column(cur, "bazi_sessions", "wx_openid", "VARCHAR(96)")
    conn.close()


def ensure_column(cur, table_name, column_name, column_def):
    cur.execute(
        """
        SELECT COUNT(*) AS cnt
        FROM information_schema.COLUMNS
        WHERE TABLE_SCHEMA=%s AND TABLE_NAME=%s AND COLUMN_NAME=%s
        """,
        (DB_CONFIG["database"], table_name, column_name),
    )
    row = cur.fetchone()
    exists = row[0] if not isinstance(row, dict) else row["cnt"]
    if not exists:
        cur.execute("ALTER TABLE " + table_name + " ADD COLUMN " + column_name + " " + column_def)


def get_session(sid: int) -> dict:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT * FROM sessions WHERE id=%s", (sid,))
    row = cur.fetchone()
    conn.close()
    if not row:
        raise HTTPException(status_code=404, detail="会话 id=" + str(sid) + " 不存在")
    return row


def update_session(sid: int, **fields):
    if not fields:
        return
    cols = ", ".join(k + "=%s" for k in fields)
    vals = list(fields.values()) + [sid]
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("UPDATE sessions SET " + cols + " WHERE id=%s", vals)
    conn.close()


def new_session_token() -> str:
    return "mp_" + uuid.uuid4().hex + uuid.uuid4().hex[:8]


def ensure_session_token(row: dict) -> str:
    token = row.get("session_token")
    if token:
        return token
    token = new_session_token()
    update_session(row["id"], session_token=token)
    row["session_token"] = token
    return token


def get_session_by_token(token: str) -> dict:
    token = (token or "").strip()
    if not token:
        raise HTTPException(status_code=404, detail="命盘不存在")
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT * FROM sessions WHERE session_token=%s", (token,))
    row = cur.fetchone()
    conn.close()
    if not row:
        raise HTTPException(status_code=404, detail="命盘不存在")
    return row


def get_owned_session(sid: int, user_key: Optional[str]) -> dict:
    row = get_session(sid)
    owner = row.get("user_key")
    if user_key and owner and owner != user_key:
        raise HTTPException(status_code=403, detail="该命盘不属于当前小程序用户")
    if user_key and not owner:
        update_session(sid, user_key=user_key)
        row["user_key"] = user_key
    return row


def get_owned_session_by_token(token: str, user_key: Optional[str]) -> dict:
    row = get_session_by_token(token)
    owner = row.get("user_key")
    if user_key and owner and owner != user_key:
        raise HTTPException(status_code=403, detail="该命盘不属于当前小程序用户")
    if user_key and not owner:
        update_session(row["id"], user_key=user_key)
        row["user_key"] = user_key
    return row


def get_bazi_session(sid: int) -> dict:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT * FROM bazi_sessions WHERE id=%s", (sid,))
    row = cur.fetchone()
    conn.close()
    if not row:
        raise HTTPException(status_code=404, detail="八字会话 id=" + str(sid) + " 不存在")
    return row


def update_bazi_session(sid: int, **fields):
    if not fields:
        return
    cols = ", ".join(k + "=%s" for k in fields)
    vals = list(fields.values()) + [sid]
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("UPDATE bazi_sessions SET " + cols + " WHERE id=%s", vals)
    conn.close()


def ensure_bazi_session_token(row: dict) -> str:
    token = row.get("session_token")
    if token:
        return token
    token = new_session_token()
    update_bazi_session(row["id"], session_token=token)
    row["session_token"] = token
    return token


def get_bazi_session_by_token(token: str) -> dict:
    token = (token or "").strip()
    if not token:
        raise HTTPException(status_code=404, detail="八字命盘不存在")
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT * FROM bazi_sessions WHERE session_token=%s", (token,))
    row = cur.fetchone()
    conn.close()
    if not row:
        raise HTTPException(status_code=404, detail="八字命盘不存在")
    return row


def get_owned_bazi_session_by_token(token: str, user_key: Optional[str]) -> dict:
    row = get_bazi_session_by_token(token)
    owner = row.get("user_key")
    if user_key and owner and owner != user_key:
        raise HTTPException(status_code=403, detail="该八字命盘不属于当前小程序用户")
    if user_key and not owner:
        update_bazi_session(row["id"], user_key=user_key)
        row["user_key"] = user_key
    return row


def decode_json(text):
    if not text:
        return None
    try:
        return json.loads(text)
    except Exception:
        return text


def _hash_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _public_user_key(openid: str) -> str:
    return "wx_" + _hash_text(openid + USER_KEY_SALT)[:32]


def _exchange_wechat_code(code: str) -> dict:
    if not WECHAT_APPID or not WECHAT_SECRET:
        raise HTTPException(status_code=400, detail="服务端未配置 WECHAT_APPID / WECHAT_SECRET，无法换取微信 openid")
    url = (
        "https://api.weixin.qq.com/sns/jscode2session"
        "?appid=" + WECHAT_APPID +
        "&secret=" + WECHAT_SECRET +
        "&js_code=" + code +
        "&grant_type=authorization_code"
    )
    try:
        with urllib.request.urlopen(url, timeout=20) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        raise HTTPException(status_code=502, detail="微信登录接口调用失败：" + str(e))
    if data.get("errcode"):
        raise HTTPException(status_code=400, detail="微信登录失败：" + data.get("errmsg", str(data)))
    if not data.get("openid"):
        raise HTTPException(status_code=400, detail="微信登录未返回 openid")
    return data


def upsert_mini_user(user_key: str, wx_openid: Optional[str] = None,
                     session_key: Optional[str] = None, nickname: Optional[str] = None,
                     avatar_url: Optional[str] = None):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO mini_users (user_key, wx_openid, session_key_hash, nickname, avatar_url)
        VALUES (%s,%s,%s,%s,%s)
        ON DUPLICATE KEY UPDATE
            wx_openid=COALESCE(VALUES(wx_openid), wx_openid),
            session_key_hash=COALESCE(VALUES(session_key_hash), session_key_hash),
            nickname=COALESCE(VALUES(nickname), nickname),
            avatar_url=COALESCE(VALUES(avatar_url), avatar_url)
        """,
        (
            user_key,
            wx_openid,
            _hash_text(session_key) if session_key else None,
            nickname,
            avatar_url,
        ),
    )
    conn.close()


def resolve_user_key(x_mingli_user: Optional[str]) -> str:
    user_key = (x_mingli_user or "").strip()
    if not user_key:
        raise HTTPException(status_code=401, detail="缺少 X-Mingli-User，请先调用 /api/miniapp/login")
    return user_key


def build_chart_payload(sid: int, chart: dict, row: Optional[dict] = None) -> dict:
    palaces = []
    for palace in chart.get("palaces", []):
        palaces.append({
            "name": palace.get("name"),
            "branch": palace.get("dizhi"),
            "major_stars": palace.get("major_stars", []),
            "minor_stars": palace.get("minor_stars", [])[:6],
            "tags": palace.get("tags", []),
            "is_empty_palace": palace.get("is_empty_palace", False),
            "borrowed_major_stars": palace.get("borrowed_major_stars", []),
            "energy_coefficient": palace.get("energy_coefficient"),
        })
    gz = chart.get("gezhi_analysis") or {}
    payload = {
        "status": row.get("status") if row else "created",
        "basic": {
            "solar": chart.get("solar_date"),
            "lunar": chart.get("lunar_date"),
            "gender": chart.get("gender"),
            "five_elements": chart.get("five_elements"),
            "soul_palace_branch": chart.get("soul_palace_branch"),
            "body_palace_branch": chart.get("body_palace_branch"),
        },
        "gezhi": {
            "gezhi_name": gz.get("gezhi_name"),
            "tech_alias": gz.get("tech_alias"),
            "description": gz.get("description"),
            "quant_metrics": gz.get("quant_metrics", {}),
            "stress_warnings": gz.get("stress_warnings", []),
        },
        "time_travel": chart.get("time_travel_analysis") or {},
        "palaces": palaces,
        "summary": engine.summarize_chart(chart),
    }
    if row:
        payload["token"] = ensure_session_token(row)
    else:
        payload["id"] = sid
    return payload


def build_reading_payload(reading: Any) -> dict:
    if not isinstance(reading, dict):
        return {"cards": [], "hand_reading": {"items": []}, "calibration_questions": []}
    reading.setdefault("cards", [])
    reading.setdefault("hand_reading", {"items": []})
    reading.setdefault("calibration_questions", [])
    return reading


def reading_preview(reading: Any) -> str:
    reading = build_reading_payload(reading)
    cards = reading.get("cards") or []
    if not cards:
        return ""
    first = cards[0]
    if isinstance(first, str):
        text = first
    else:
        text = str(first.get("body") or first.get("text") or first.get("title") or "")
    text = text.replace("<br>", " ").replace("<br/>", " ").replace("<br />", " ")
    while "<" in text and ">" in text:
        start = text.find("<")
        end = text.find(">", start)
        if end == -1:
            break
        text = text[:start] + text[end + 1:]
    return text[:96]


def bazi_reading_preview(reading: Any) -> str:
    if not isinstance(reading, dict):
        return ""
    for key in ("summary", "day_master", "geju", "suggestions"):
        if reading.get(key):
            return str(reading.get(key))[:96]
    return ""


def _bazi_chart(row: dict):
    """解析 bazi_json 为结构化命盘数据，供小程序端渲染。"""
    chart = decode_json(row.get("bazi_json")) or {}
    if not isinstance(chart, dict) or not chart:
        return None
    # 剔除对前端无用的非 JSON 原生字段（datetime 会被序列化成字符串，无需返回）
    chart.pop("birth_dt", None)
    return chart


def build_bazi_payload(row: dict, include_report: bool = True, include_reading: bool = False) -> dict:
    data = {
        "token": ensure_bazi_session_token(row),
        "status": row.get("status"),
        "solar": row.get("solar_date"),
        "lunar": row.get("lunar_date"),
        "sex": row.get("sex"),
        "created_at": str(row.get("created_at")),
        "updated_at": str(row.get("updated_at")),
    }
    if include_report:
        data["report"] = row.get("report") or ""
        data["chart"] = _bazi_chart(row)
    if include_reading:
        data["reading"] = decode_json(row.get("reading_json")) or {}
        data["html_url"] = "/api/miniapp/bazi/" + ensure_bazi_session_token(row) + "/html" if row.get("html_path") else None
    return data


def miniapp_today_usage(user_key: str) -> dict:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT COUNT(*) AS cnt
        FROM sessions
        WHERE user_key=%s
          AND reading_json IS NOT NULL
          AND DATE(updated_at)=CURDATE()
        """,
        (user_key,),
    )
    row = cur.fetchone()
    ziwei_used = int(row["cnt"] if isinstance(row, dict) else row[0])
    cur.execute(
        """
        SELECT COUNT(*) AS cnt
        FROM bazi_sessions
        WHERE user_key=%s
          AND reading_json IS NOT NULL
          AND DATE(updated_at)=CURDATE()
        """,
        (user_key,),
    )
    row = cur.fetchone()
    conn.close()
    used = ziwei_used + int(row["cnt"] if isinstance(row, dict) else row[0])
    return {
        "used": used,
        "limit": MINIAPP_DAILY_INTERPRET_LIMIT,
        "remaining": max(0, MINIAPP_DAILY_INTERPRET_LIMIT - used),
    }


def assert_miniapp_interpret_quota(user_key: str):
    usage = miniapp_today_usage(user_key)
    if usage["remaining"] <= 0:
        raise HTTPException(
            status_code=429,
            detail="今天的免费解读次数已用完。服务器算力有限，每个用户每天限解读一次，明天再来继续看。",
        )


# ---------------------------------------------------------------------------
# 请求模型
# ---------------------------------------------------------------------------
class ChartRequest(BaseModel):
    calendar: str = "solar"      # solar（公历） | lunar（农历）
    date: str                    # 如 "1991-8-15"
    hour: int                    # 时辰索引 0-12
    gender: str                  # 男 | 女
    leap: bool = False           # 农历闰月
    year: Optional[int] = None   # 目标流年


class AnswersRequest(BaseModel):
    answers: Any = None          # [{"id","answer"}] 或 {"q1":"答案"}


class MiniLoginRequest(BaseModel):
    code: Optional[str] = None
    nickname: Optional[str] = None
    avatar_url: Optional[str] = None


class BaziChartRequest(BaseModel):
    calendar: str = "solar"        # solar | lunar
    date: str                      # YYYY-M-D 或 YYYY-MM-DD
    sex: str                       # 男 | 女
    hour: Optional[str] = None     # 出生钟点 HH:MM（北京时间）
    shichen: Optional[str] = None  # 时辰地支：子丑寅卯辰巳午未申酉戌亥
    leap: bool = False             # 农历闰月
    place: Optional[str] = None    # 出生地（仅展示/警示）
    deceased_year: Optional[int] = None


# ---------------------------------------------------------------------------
# DeepSeek 调用
# ---------------------------------------------------------------------------
def _deepseek_post(messages, response_format=None, temperature=0.7):
    url = "https://api.deepseek.com/chat/completions"
    payload = {
        "model": DEEPSEEK_MODEL,
        "messages": messages,
        "temperature": temperature,
        "stream": False,
    }
    if response_format:
        payload["response_format"] = response_format

    def _do(body):
        req = urllib.request.Request(url, data=json.dumps(body).encode("utf-8"), method="POST")
        req.add_header("Authorization", "Bearer " + DEEPSEEK_API_KEY)
        req.add_header("Content-Type", "application/json")
        with urllib.request.urlopen(req, timeout=300) as resp:
            return json.loads(resp.read().decode("utf-8"))["choices"][0]["message"]["content"]

    try:
        return _do(payload)
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "ignore")
        if e.code == 401:
            raise HTTPException(status_code=502, detail="DeepSeek API Key 无效或已失效，请在服务端重新配置 DEEPSEEK_API_KEY")
        if e.code == 400 and "response_format" in detail and response_format:
            payload.pop("response_format", None)
            return _do(payload)
        raise HTTPException(status_code=502, detail="DeepSeek API 调用失败：" + detail)


# ---------------------------------------------------------------------------
# 手相：特征提取 + 交叉比对
# ---------------------------------------------------------------------------
def extract_palm_via_vision(image_path):
    """用通义千问 qwen-vl 识别手相照片，提取掌纹特征（DeepSeek 无图片能力）。"""
    if not DASHSCOPE_API_KEY:
        raise HTTPException(status_code=500, detail="未配置 DASHSCOPE_API_KEY，无法识别照片，可改用文字描述")
    ext = os.path.splitext(image_path)[1].lower()
    mime = "image/jpeg"
    if ext in (".png",):
        mime = "image/png"
    elif ext in (".webp",):
        mime = "image/webp"
    elif ext in (".gif",):
        mime = "image/gif"
    with open(image_path, "rb") as f:
        b64 = base64.b64encode(f.read()).decode("ascii")
    data_url = "data:" + mime + ";base64," + b64
    prompt = (
        "请从这张手掌照片提取掌纹特征，只输出一个 JSON 对象，字段："
        '{"life_line":"生命线：弧度/长度/清晰度","head_line":"智慧线：起点/走向/长度",'
        '"heart_line":"感情线：深浅/终点位置","texture":"整体纹路粗细/简洁程度","palm_shape":"掌型/手指粗细"}'
    )
    body = {
        "model": QWEN_VL_MODEL,
        "messages": [{"role": "user", "content": [
            {"type": "image_url", "image_url": {"url": data_url}},
            {"type": "text", "text": prompt},
        ]}],
        "stream": False,
    }
    url = QWEN_BASE_URL.rstrip("/") + "/chat/completions"
    try:
        req = urllib.request.Request(url, data=json.dumps(body).encode("utf-8"), method="POST")
        req.add_header("Authorization", "Bearer " + DASHSCOPE_API_KEY)
        req.add_header("Content-Type", "application/json")
        with urllib.request.urlopen(req, timeout=180) as resp:
            result = json.loads(resp.read().decode("utf-8"))
        content = result["choices"][0]["message"]["content"]
        parsed = engine.extract_json(content)
        return json.dumps(parsed, ensure_ascii=False, indent=2)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=400,
            detail="照片识别失败（qwen-vl）：" + str(e) + "。可改用手相文字描述。",
        )


def cross_compare_palm(chart, features):
    if not DEEPSEEK_API_KEY:
        raise HTTPException(status_code=500, detail="未配置 DEEPSEEK_API_KEY，无法交叉比对")
    system = (
        "你是命理咨询师，负责把手相特征与紫微斗数命盘交叉比对，标注吻合/矛盾之处，"
        "矛盾处要给出取舍逻辑。只输出一个 JSON 对象，不要输出任何解释。"
    )
    user = (
        "命盘摘要：\n" + engine.summarize_chart(chart) + "\n\n"
        "手相特征：\n" + features + "\n\n"
        '请输出格式为 {"items":[...]} 的 JSON，每个 item 字段：'
        '{"title":"生命线","body":"描述","status":"match 或 conflict",'
        '"status_text":"与命盘XX共振 ✓ 或 与XX有表面矛盾","resolution":"仅 conflict 时填取舍逻辑"}'
    )
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]
    content = _deepseek_post(messages, response_format={"type": "json_object"})
    data = engine.extract_json(content)
    if isinstance(data, list):
        items = data
    else:
        items = data.get("items", [])
    return {"items": items}


# ---------------------------------------------------------------------------
# 紫微解读 Prompt
# ---------------------------------------------------------------------------
def build_interpret_messages(chart, row):
    target_year = row.get("target_year") or datetime.date.today().year
    msgs = engine.build_messages(chart, target_year)
    system, user = msgs[0], msgs[1]

    extra = []
    answers = decode_json(row.get("answers_json"))
    if answers:
        extra.append("【用户校准回答】（用于校准解读、修正取象偏差）\n" + json.dumps(answers, ensure_ascii=False, indent=2))

    palm = row.get("palm_features")
    if palm:
        extra.append("【手相特征】\n" + palm)

    hand = decode_json(row.get("hand_reading_json"))
    if hand and hand.get("items"):
        extra.append("【手相互证结果】\n" + json.dumps(hand, ensure_ascii=False, indent=2) +
                     "\n请把这段手相互证结果原样放进输出 JSON 的 hand_reading 字段。")

    if answers:
        extra.append("用户已经作答了校准问题，请根据回答校准解读（例如调整置信度、修正矛盾点）；"
                     "输出里的 calibration_questions 可以留空数组，或只给 1-2 个进一步追问。")

    if extra:
        user["content"] = user["content"] + "\n\n" + "\n\n".join(extra)
    return [system, user]


# ---------------------------------------------------------------------------
# 八字：排盘 + 解读
# ---------------------------------------------------------------------------
def load_bazi_reference(name):
    path = os.path.join(BAZI_REF_DIR, name)
    if not os.path.exists(path):
        return ""
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def _parse_date(text):
    parts = text.strip().replace("/", "-").split("-")
    if len(parts) != 3:
        raise ValueError("日期格式应为 YYYY-M-D")
    return int(parts[0]), int(parts[1]), int(parts[2])


def bazi_paipan(req: BaziChartRequest):
    if req.calendar not in ("solar", "lunar"):
        raise HTTPException(status_code=400, detail="calendar 只能是 solar 或 lunar")
    if req.sex not in ("男", "女"):
        raise HTTPException(status_code=400, detail="sex 只能是 男 或 女")
    if req.hour and req.shichen:
        raise HTTPException(status_code=400, detail="hour 与 shichen 二选一")
    y, m, d = _parse_date(req.date)
    hour = minute = None
    if req.hour:
        try:
            hh, mm = req.hour.strip().split(":")
            hour, minute = int(hh), int(mm)
        except Exception:
            raise HTTPException(status_code=400, detail="hour 格式应为 HH:MM")
    lunar_display = None
    if req.calendar == "lunar":
        try:
            solar_date = bazi_lunar_to_solar(y, m, d, leap=req.leap)
            lunar_display = bazi_format_lunar(y, m, d, req.leap)
        except Exception as e:
            raise HTTPException(status_code=400, detail="农历转换失败：" + str(e))
    else:
        solar_date = datetime.date(y, m, d)
    try:
        result = bazi_compute(
            solar_date=solar_date,
            hour=hour,
            minute=minute,
            shichen=req.shichen,
            sex=req.sex,
            place=req.place,
            deceased_year=req.deceased_year,
            lunar_display=lunar_display,
        )
    except Exception as e:
        raise HTTPException(status_code=400, detail="八字排盘失败：" + str(e))
    return result, bazi_format_report(result)


def build_bazi_interpret_messages(report):
    system = (
        "你是一位熟悉中国传统干支历法与八字知识、说话风趣的趣味解说员。"
        "你的任务是把四柱八字排盘结果，用轻松通俗的方式介绍给用户，"
        "定位是「传统文化小知识 + 娱乐消遣」，而不是命理预测或人生指导。\n\n"
        "【重要边界】\n"
        "1. 不预测未来，不判断吉凶祸福，不下「你一定会…」这类结论。\n"
        "2. 不指导任何现实决策（事业、婚恋、投资、健康、就医等），只做趣味性介绍。\n"
        "3. 不制造焦虑或恐惧，遇到「冲、刑、克、害」等术语只做客观知识说明，不作吉凶评判。\n"
        "4. 语气像朋友闲聊讲冷知识，先讲积极有趣的一面，再带一句轻松提醒。\n\n"
        "【经典典籍规则摘要】\n" + load_bazi_reference("classical-texts.md") + "\n\n"
        "【五行/十神表】\n" + load_bazi_reference("wuxing-tables.md") + "\n\n"
        "【大运规则】\n" + load_bazi_reference("dayun-rules.md") + "\n\n"
        "【神煞表】\n" + load_bazi_reference("shensha-table.md") + "\n\n"
        "【输出要求】\n"
        "1. 只输出一个合法 JSON 对象，不要任何解释或 markdown。\n"
        '2. 结构：{"summary":"整体印象一句话","day_master":"日主特点的趣味介绍",'
        '"wuxing":"五行构成与搭配","shishen":"十神搭配的趣味解释","geju":"格局的趣味欣赏",'
        '"dayun":"不同人生阶段的大运小知识（含当前阶段）",'
        '"liunian":"当前流年干支与四柱的趣味关系（如 2026 丙午年）",'
        '"aspects":{"career":"事业趣谈","wealth":"财富趣谈","relationship":"缘分趣谈","health":"健康小贴士"},'
        '"suggestions":"温馨提示"}\n'
        "3. 通篇使用「传统上认为」「趣味角度」「打个比方」这类表述，明确这是文化解读而非预测。\n"
        "4. suggestions 结尾要强调：仅供娱乐，理性看待，不构成任何决策依据。"
    )
    user = "以下是四柱八字排盘结果，请用娱乐化的方式介绍它：\n\n" + report
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def build_bazi_html(report, reading):
    def esc(s):
        return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    secs = []
    mapping = [
        ("summary", "整体印象"),
        ("day_master", "日主特点"),
        ("wuxing", "五行构成"),
        ("shishen", "十神搭配"),
        ("geju", "格局欣赏"),
        ("dayun", "人生阶段"),
        ("liunian", "流年"),
    ]
    for key, title in mapping:
        v = reading.get(key)
        if v:
            secs.append("<h2>" + title + "</h2><p>" + esc(v) + "</p>")
    aspects = reading.get("aspects") or {}
    amap = [("career", "事业趣谈"), ("wealth", "财富趣谈"), ("relationship", "缘分趣谈"), ("health", "健康小贴士")]
    items = []
    for k, t in amap:
        if aspects.get(k):
            items.append("<h3>" + t + "</h3><p>" + esc(aspects[k]) + "</p>")
    if items:
        secs.append("<h2>趣味解读</h2>" + "".join(items))
    if reading.get("suggestions"):
        secs.append("<h2>温馨提示</h2><p>" + esc(reading["suggestions"]) + "</p>")

    html = (
        "<!DOCTYPE html><html lang='zh-CN'><head><meta charset='utf-8'>"
        "<title>四柱干支 · 趣味解读</title><style>"
        "body{font-family:'PingFang SC','Microsoft YaHei',sans-serif;max-width:780px;margin:24px auto;padding:0 16px;color:#2a2418;background:#f7f3ea;line-height:1.8}"
        "h1{color:#a8841a;border-bottom:2px solid #c9a227;padding-bottom:8px}"
        "h2{color:#a8841a;margin-top:26px;border-left:4px solid #c9a227;padding-left:10px}"
        "h3{color:#6b5b2e;margin:16px 0 6px}"
        "pre{background:#fffdf8;border:1px solid #e6ddc8;border-radius:8px;padding:16px;font-size:13px;overflow:auto;line-height:1.6}"
        "p{margin:6px 0}"
        "</style></head><body>"
        "<h1>四柱干支 · 趣味解读</h1>"
        "<h2>排盘</h2><pre>" + esc(report) + "</pre>"
        + "".join(secs) +
        "<p style='margin-top:28px;color:#8a8172;font-size:12px'>本结果仅供传统文化学习与娱乐参考，不构成任何决策依据。</p>"
        "</body></html>"
    )
    return html


# ---------------------------------------------------------------------------
# FastAPI 应用
# ---------------------------------------------------------------------------
app = FastAPI(title="命理解读师 · 多步骤 API", version="2.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def require_https_for_miniapp(request, call_next):
    if REQUIRE_HTTPS and request.url.path.startswith("/api/miniapp"):
        proto = request.headers.get("x-forwarded-proto", request.url.scheme)
        if proto != "https":
            return JSONResponse(status_code=403, content={"detail": "小程序接口必须通过 HTTPS 访问"})
    return await call_next(request)


@app.on_event("startup")
def _startup():
    init_db()

#
# @app.get("/")
# def index():
#     """首页：返回可视化调用面板（web/index.html）。"""
#     if os.path.exists(WEB_INDEX):
#         with open(WEB_INDEX, "r", encoding="utf-8") as f:
#             return HTMLResponse(content=f.read())
#     return api_index()


@app.get("/api")
def api_index():
    return {
        "name": "命理解读师 · 多步骤 API",
        "docs": "/docs",
        "web": "/",
        "steps": [
            {"step": 1, "method": "POST", "path": "/api/chart", "desc": "紫微排盘（公历/农历），返回 id"},
            {"step": 2, "method": "GET", "path": "/api/{id}/questions", "desc": "出题（校准问题，可选）"},
            {"step": 2, "method": "POST", "path": "/api/{id}/answers", "desc": "提交答案（可选）"},
            {"step": 3, "method": "POST", "path": "/api/{id}/palm", "desc": "上传手相照片/描述，交叉比对（可选）"},
            {"step": 4, "method": "POST", "path": "/api/{id}/interpret", "desc": "大模型解读，生成命盘"},
        ],
        "bazi": [
            {"step": 1, "method": "POST", "path": "/api/bazi/chart", "desc": "八字排盘（四柱/大运/流年/神煞），返回 id"},
            {"step": 2, "method": "POST", "path": "/api/bazi/{id}/interpret", "desc": "DeepSeek 八字解读，生成报告"},
        ],
        "helpers": [
            {"method": "GET", "path": "/api/hours", "desc": "时辰索引对照表（可 ?time=HH:MM 反查）"},
        ],
    }


# 时辰索引查询
@app.get("/api/hours")
def get_hours(time: Optional[str] = None):
    """返回时辰索引对照表；可选 ?time=HH:MM 反查某时刻对应的时辰。"""
    if time:
        try:
            hh = time.strip().split(":")[0]
            h = int(hh)
            if not (0 <= h <= 23):
                raise ValueError
        except Exception:
            raise HTTPException(status_code=400, detail="time 格式应为 HH:MM，如 02:30")
        idx = ((h + 1) // 2) % 12
        hit = next(x for x in HOUR_INDEX if x["index"] == idx)
        return {
            "time": time,
            "index": hit["index"],
            "name": hit["name"],
            "time_range": hit["time"],
            "note": "紫微排盘接口 hour 参数填 index；子时(23:00-01:00)按 0 处理，若需晚子时用 12",
        }
    return {
        "hours": HOUR_INDEX,
        "usage": "POST /api/chart 的 hour 参数填 index；也可 GET /api/hours?time=02:30 反查",
    }


# ===========================================================================
# 微信小程序专用接口（紫微斗数）
# ===========================================================================
@app.post("/api/miniapp/login")
def miniapp_login(req: MiniLoginRequest):
    """
    小程序登录/绑定。

    生产环境传 wx.login() 得到的 code，并配置 WECHAT_APPID / WECHAT_SECRET。
    本地调试没有微信配置时，可以不传 code，服务端会返回一个临时 user_key。
    """
    wx_openid = None
    session_key = None
    if req.code:
        data = _exchange_wechat_code(req.code)
        wx_openid = data.get("openid")
        session_key = data.get("session_key")
        user_key = _public_user_key(wx_openid)
    else:
        user_key = "guest_" + uuid.uuid4().hex

    upsert_mini_user(
        user_key=user_key,
        wx_openid=wx_openid,
        session_key=session_key,
        nickname=req.nickname,
        avatar_url=req.avatar_url,
    )
    return {
        "user_key": user_key,
        "has_openid": bool(wx_openid),
        "debug_guest": not bool(wx_openid),
    }


@app.get("/api/miniapp/me")
def miniapp_me(x_mingli_user: Optional[str] = Header(None)):
    user_key = resolve_user_key(x_mingli_user)
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "SELECT user_key, wx_openid, nickname, avatar_url, created_at, updated_at FROM mini_users WHERE user_key=%s",
        (user_key,),
    )
    user = cur.fetchone()
    cur.execute(
        """
        SELECT id, session_token, solar_date, lunar_date, hour_index, gender, target_year, gezhi_name, status, created_at, updated_at
        FROM sessions
        WHERE user_key=%s
        ORDER BY id DESC
        LIMIT 20
        """,
        (user_key,),
    )
    rows = cur.fetchall()
    conn.close()
    sessions = []
    for row in rows:
        sessions.append({
            "token": ensure_session_token(row),
            "solar": row.get("solar_date"),
            "lunar": row.get("lunar_date"),
            "hour_index": row.get("hour_index"),
            "gender": row.get("gender"),
            "target_year": row.get("target_year"),
            "gezhi_name": row.get("gezhi_name"),
            "status": row.get("status"),
            "created_at": str(row.get("created_at")),
            "updated_at": str(row.get("updated_at")),
        })
    return {
        "user": user or {"user_key": user_key},
        "sessions": sessions,
        "today_usage": miniapp_today_usage(user_key),
    }


@app.get("/api/miniapp/history")
def miniapp_history(x_mingli_user: Optional[str] = Header(None)):
    user_key = resolve_user_key(x_mingli_user)
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT id, session_token, solar_date, lunar_date, hour_index, gender, target_year, gezhi_name,
               reading_json, created_at, updated_at
        FROM sessions
        WHERE user_key=%s AND reading_json IS NOT NULL
        ORDER BY updated_at DESC, id DESC
        LIMIT 50
        """,
        (user_key,),
    )
    rows = cur.fetchall()
    conn.close()
    records = []
    for row in rows:
        token = ensure_session_token(row)
        reading = decode_json(row.get("reading_json"))
        records.append({
            "token": token,
            "solar": row.get("solar_date"),
            "lunar": row.get("lunar_date"),
            "hour_index": row.get("hour_index"),
            "gender": row.get("gender"),
            "target_year": row.get("target_year"),
            "gezhi_name": row.get("gezhi_name"),
            "preview": reading_preview(reading),
            "created_at": str(row.get("created_at")),
            "interpreted_at": str(row.get("updated_at")),
        })
    return {
        "today_usage": miniapp_today_usage(user_key),
        "records": records,
    }


@app.get("/api/miniapp/bazi/history")
def miniapp_bazi_history(x_mingli_user: Optional[str] = Header(None)):
    user_key = resolve_user_key(x_mingli_user)
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT id, session_token, solar_date, lunar_date, sex, reading_json, created_at, updated_at
        FROM bazi_sessions
        WHERE user_key=%s AND reading_json IS NOT NULL
        ORDER BY updated_at DESC, id DESC
        LIMIT 50
        """,
        (user_key,),
    )
    rows = cur.fetchall()
    conn.close()
    records = []
    for row in rows:
        reading = decode_json(row.get("reading_json"))
        records.append({
            "token": ensure_bazi_session_token(row),
            "solar": row.get("solar_date"),
            "lunar": row.get("lunar_date"),
            "sex": row.get("sex"),
            "preview": bazi_reading_preview(reading),
            "created_at": str(row.get("created_at")),
            "interpreted_at": str(row.get("updated_at")),
        })
    return {
        "today_usage": miniapp_today_usage(user_key),
        "records": records,
    }


@app.post("/api/miniapp/bazi/chart")
def miniapp_create_bazi_chart(req: BaziChartRequest, x_mingli_user: Optional[str] = Header(None)):
    user_key = resolve_user_key(x_mingli_user)
    result, report = bazi_paipan(req)
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO bazi_sessions (session_token, user_key, solar_date, lunar_date, sex, bazi_json, report, status) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s,'created')",
        (
            new_session_token(),
            user_key,
            result.get("solar_text"),
            result.get("lunar_text"),
            req.sex,
            json.dumps(result, ensure_ascii=False, default=str),
            report,
        ),
    )
    sid = cur.lastrowid
    conn.close()
    row = get_bazi_session(sid)
    return {
        **build_bazi_payload(row, include_report=True),
        "today_usage": miniapp_today_usage(user_key),
        "next": "POST /api/miniapp/bazi/" + ensure_bazi_session_token(row) + "/interpret",
    }


@app.get("/api/miniapp/bazi/{token}/record")
def miniapp_bazi_record(token: str, x_mingli_user: Optional[str] = Header(None)):
    user_key = resolve_user_key(x_mingli_user)
    row = get_owned_bazi_session_by_token(token, user_key)
    if not row.get("reading_json"):
        raise HTTPException(status_code=404, detail="该八字命盘还没有生成分析")
    return {
        **build_bazi_payload(row, include_report=True, include_reading=True),
        "today_usage": miniapp_today_usage(user_key),
    }


@app.post("/api/miniapp/bazi/{token}/interpret")
def miniapp_bazi_interpret(token: str, x_mingli_user: Optional[str] = Header(None)):
    user_key = resolve_user_key(x_mingli_user)
    row = get_owned_bazi_session_by_token(token, user_key)
    if row.get("reading_json"):
        return miniapp_bazi_record(token, x_mingli_user)
    assert_miniapp_interpret_quota(user_key)
    data = interpret_bazi(row["id"])
    row = get_bazi_session(row["id"])
    return {
        **build_bazi_payload(row, include_report=True, include_reading=True),
        "reading": data.get("reading") or {},
        "html_url": "/api/miniapp/bazi/" + ensure_bazi_session_token(row) + "/html",
        "today_usage": miniapp_today_usage(user_key),
    }


@app.get("/api/miniapp/bazi/{token}/html")
def miniapp_bazi_view_html(token: str, x_mingli_user: Optional[str] = Header(None)):
    user_key = resolve_user_key(x_mingli_user)
    row = get_owned_bazi_session_by_token(token, user_key)
    if not row.get("html_path") or not os.path.exists(row["html_path"]):
        raise HTTPException(status_code=404, detail="该八字命盘还没有生成分析")
    with open(row["html_path"], "r", encoding="utf-8") as f:
        return HTMLResponse(content=f.read())


@app.post("/api/miniapp/chart")
def miniapp_create_chart(req: ChartRequest, x_mingli_user: Optional[str] = Header(None)):
    user_key = resolve_user_key(x_mingli_user)
    data = create_chart(req)
    update_session(data["id"], user_key=user_key)
    row = get_session(data["id"])
    chart = json.loads(row["chart_json"])
    payload = build_chart_payload(data["id"], chart, row)
    token = ensure_session_token(row)
    payload["next"] = [
        "GET /api/miniapp/" + token + "/questions",
        "POST /api/miniapp/" + token + "/interpret",
    ]
    return payload


@app.get("/api/miniapp/{token}/chart")
def miniapp_view_chart(token: str, x_mingli_user: Optional[str] = Header(None)):
    user_key = resolve_user_key(x_mingli_user)
    row = get_owned_session_by_token(token, user_key)
    if not row.get("chart_json"):
        raise HTTPException(status_code=404, detail="该会话还没有排盘数据")
    return build_chart_payload(row["id"], json.loads(row["chart_json"]), row)


@app.get("/api/miniapp/{token}/record")
def miniapp_record(token: str, x_mingli_user: Optional[str] = Header(None)):
    user_key = resolve_user_key(x_mingli_user)
    row = get_owned_session_by_token(token, user_key)
    if not row.get("chart_json"):
        raise HTTPException(status_code=404, detail="该会话还没有排盘数据")
    if not row.get("reading_json"):
        raise HTTPException(status_code=404, detail="该命盘还没有生成解读")
    return {
        "token": ensure_session_token(row),
        "chart": build_chart_payload(row["id"], json.loads(row["chart_json"]), row),
        "reading": build_reading_payload(decode_json(row.get("reading_json"))),
        "html_url": "/api/miniapp/" + ensure_session_token(row) + "/html" if row.get("html_path") else None,
        "created_at": str(row.get("created_at")),
        "interpreted_at": str(row.get("updated_at")),
        "today_usage": miniapp_today_usage(user_key),
    }


@app.get("/api/miniapp/{token}/html")
def miniapp_view_html(token: str, x_mingli_user: Optional[str] = Header(None)):
    user_key = resolve_user_key(x_mingli_user)
    row = get_owned_session_by_token(token, user_key)
    if not row.get("html_path") or not os.path.exists(row["html_path"]):
        raise HTTPException(status_code=404, detail="该命盘还没有生成解读")
    with open(row["html_path"], "r", encoding="utf-8") as f:
        return HTMLResponse(content=f.read())


@app.get("/api/miniapp/{token}/questions")
def miniapp_get_questions(token: str, x_mingli_user: Optional[str] = Header(None)):
    user_key = resolve_user_key(x_mingli_user)
    row = get_owned_session_by_token(token, user_key)
    questions = [
        {"id": q["id"], "text": q["text"], "hint": q.get("hint", ""), "priority": q["priority"]}
        for q in CALIBRATION_QUESTIONS
    ]
    count = min(len(questions), random.randint(3, 5))
    picked = random.sample(questions, count) if len(questions) > count else questions
    picked.sort(key=lambda q: 0 if q.get("priority") == "high" else 1)
    return {"token": ensure_session_token(row), "questions": picked, "next": "POST /api/miniapp/" + token + "/answers"}


@app.post("/api/miniapp/{token}/answers")
def miniapp_submit_answers(token: str, req: AnswersRequest, x_mingli_user: Optional[str] = Header(None)):
    user_key = resolve_user_key(x_mingli_user)
    row = get_owned_session_by_token(token, user_key)
    return submit_answers(row["id"], req)


@app.post("/api/miniapp/{token}/interpret")
def miniapp_interpret(token: str, x_mingli_user: Optional[str] = Header(None)):
    user_key = resolve_user_key(x_mingli_user)
    row = get_owned_session_by_token(token, user_key)
    if row.get("reading_json"):
        return miniapp_record(token, x_mingli_user)
    assert_miniapp_interpret_quota(user_key)
    data = interpret(row["id"])
    row = get_session(row["id"])
    chart_payload = build_chart_payload(row["id"], json.loads(row["chart_json"]), row)
    reading = build_reading_payload(data.get("reading") or {})
    return {
        "token": ensure_session_token(row),
        "chart": chart_payload,
        "reading": reading,
        "html_url": "/api/miniapp/" + ensure_session_token(row) + "/html",
        "today_usage": miniapp_today_usage(user_key),
    }


# ===========================================================================
# 紫微斗数接口
# ===========================================================================
@app.post("/api/chart")
def create_chart(req: ChartRequest):
    if req.calendar not in ("solar", "lunar"):
        raise HTTPException(status_code=400, detail="calendar 只能是 solar（公历）或 lunar（农历）")
    if req.gender not in ("男", "女"):
        raise HTTPException(status_code=400, detail="gender 只能是 男 或 女")
    if not (0 <= req.hour <= 12):
        raise HTTPException(status_code=400, detail="hour 必须在 0-12 之间")
    if not req.date:
        raise HTTPException(status_code=400, detail="date 不能为空")

    is_lunar = req.calendar == "lunar"
    try:
        chart = engine.build_chart(req.date, req.hour, req.gender, is_lunar=is_lunar, is_leap=req.leap)
        chart = engine.enrich_chart(chart, req.year or datetime.date.today().year)
    except Exception as e:
        raise HTTPException(status_code=400, detail="排盘失败：" + str(e))

    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO sessions "
        "(session_token, solar_date, lunar_date, is_lunar, hour_index, gender, is_leap, target_year, chart_json, gezhi_name, status) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'created')",
        (
            new_session_token(),
            chart.get("solar_date"), chart.get("lunar_date"), int(is_lunar),
            req.hour, req.gender, int(req.leap), req.year,
            json.dumps(chart, ensure_ascii=False),
            (chart.get("gezhi_analysis") or {}).get("gezhi_name", ""),
        ),
    )
    sid = cur.lastrowid
    conn.close()

    gz = chart.get("gezhi_analysis") or {}
    return {
        "id": sid,
        "status": "created",
        "basic": {
            "solar": chart.get("solar_date"),
            "lunar": chart.get("lunar_date"),
            "five_elements": chart.get("five_elements"),
            "soul_palace_branch": chart.get("soul_palace_branch"),
            "body_palace_branch": chart.get("body_palace_branch"),
        },
        "gezhi": gz,
        "summary": engine.summarize_chart(chart),
        "next": [
            "GET /api/" + str(sid) + "/questions  (可选)",
            "POST /api/" + str(sid) + "/palm  (可选)",
            "POST /api/" + str(sid) + "/interpret  (第四步)",
        ],
    }


@app.get("/api/{sid}/questions")
def get_questions(sid: int):
    get_session(sid)
    questions = [
        {"id": q["id"], "text": q["text"], "hint": q.get("hint", ""), "priority": q["priority"]}
        for q in CALIBRATION_QUESTIONS
    ]
    return {"id": sid, "questions": questions, "next": "POST /api/" + str(sid) + "/answers"}


@app.post("/api/{sid}/answers")
def submit_answers(sid: int, req: AnswersRequest):
    get_session(sid)
    answers = req.answers
    if isinstance(answers, dict):
        answers = [{"id": k, "answer": v} for k, v in answers.items()]
    if answers is None:
        answers = []
    update_session(sid, answers_json=json.dumps(answers, ensure_ascii=False), status="answered")
    return {
        "id": sid,
        "received": len(answers),
        "next": ["POST /api/" + str(sid) + "/palm", "POST /api/" + str(sid) + "/interpret"],
    }


@app.post("/api/{sid}/palm")
def upload_palm(sid: int, image: Optional[UploadFile] = File(None), description: str = Form("")):
    row = get_session(sid)
    chart = json.loads(row["chart_json"])

    image_path = None
    if image is not None:
        data = image.file.read()
        if data:
            ext = os.path.splitext(image.filename or "")[1] or ".jpg"
            image_path = os.path.join(UPLOAD_DIR, "palm_" + str(sid) + "_" + str(int(datetime.datetime.now().timestamp())) + ext)
            with open(image_path, "wb") as f:
                f.write(data)

    if description and description.strip():
        features = "用户手相文字描述：" + description.strip()
    elif image_path:
        features = extract_palm_via_vision(image_path)
    else:
        raise HTTPException(status_code=400, detail="请上传手相照片，或在 description 字段提供手相文字描述")

    comparison = cross_compare_palm(chart, features)

    update_session(
        sid,
        palm_features=features,
        hand_reading_json=json.dumps(comparison, ensure_ascii=False),
        status="palm",
    )
    return {
        "id": sid,
        "palm_features": features,
        "hand_reading": comparison,
        "next": "POST /api/" + str(sid) + "/interpret",
    }


@app.post("/api/{sid}/interpret")
def interpret(sid: int):
    row = get_session(sid)
    if not row.get("chart_json"):
        raise HTTPException(status_code=400, detail="该会话还没有排盘数据")
    if not DEEPSEEK_API_KEY:
        raise HTTPException(status_code=500, detail="未配置 DEEPSEEK_API_KEY 环境变量")

    chart = json.loads(row["chart_json"])
    messages = build_interpret_messages(chart, row)
    content = _deepseek_post(messages, response_format={"type": "json_object"})
    reading = engine.extract_json(content)

    reading.setdefault("cards", [])
    reading.setdefault("calibration_questions", [])
    hand = decode_json(row.get("hand_reading_json"))
    reading["hand_reading"] = hand if hand else {"items": []}

    html = engine.generate_html(chart, reading, engine.TEMPLATE_PATH)
    session_dir = os.path.join(OUTPUT_DIR, "session_" + str(sid))
    os.makedirs(session_dir, exist_ok=True)
    html_path = os.path.join(session_dir, "mingpan.html")
    with open(html_path, "w", encoding="utf-8") as f:
        f.write(html)

    update_session(
        sid,
        reading_json=json.dumps(reading, ensure_ascii=False),
        html_path=html_path,
        status="interpreted",
    )
    return {
        "id": sid,
        "reading": reading,
        "html_url": "/api/" + str(sid) + "/html",
    }


@app.get("/api/{sid}")
def view_session(sid: int):
    row = get_session(sid)
    return {
        "id": sid,
        "status": row.get("status"),
        "basic": {
            "solar": row.get("solar_date"),
            "lunar": row.get("lunar_date"),
            "hour_index": row.get("hour_index"),
            "gender": row.get("gender"),
            "target_year": row.get("target_year"),
        },
        "gezhi_name": row.get("gezhi_name"),
        "has_chart": bool(row.get("chart_json")),
        "has_answers": bool(row.get("answers_json")),
        "has_palm": bool(row.get("palm_features")),
        "has_reading": bool(row.get("reading_json")),
        "html_url": "/api/" + str(sid) + "/html" if row.get("html_path") else None,
        "created_at": str(row.get("created_at")),
        "updated_at": str(row.get("updated_at")),
    }


@app.get("/api/{sid}/chart")
def view_chart(sid: int):
    row = get_session(sid)
    if not row.get("chart_json"):
        raise HTTPException(status_code=404, detail="该会话还没有排盘数据")
    return JSONResponse(content=json.loads(row["chart_json"]))


@app.get("/api/{sid}/html")
def view_html(sid: int):
    row = get_session(sid)
    if not row.get("html_path") or not os.path.exists(row["html_path"]):
        raise HTTPException(status_code=404, detail="该会话还没有生成命盘 HTML，请先调用 POST /api/" + str(sid) + "/interpret")
    with open(row["html_path"], "r", encoding="utf-8") as f:
        return HTMLResponse(content=f.read())


# ===========================================================================
# 八字接口
# ===========================================================================
@app.post("/api/bazi/chart")
def create_bazi_chart(req: BaziChartRequest):
    result, report = bazi_paipan(req)
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO bazi_sessions (solar_date, lunar_date, sex, bazi_json, report, status) "
        "VALUES (%s,%s,%s,%s,%s,'created')",
        (
            result.get("solar_text"), result.get("lunar_text"), req.sex,
            json.dumps(result, ensure_ascii=False, default=str),
            report,
        ),
    )
    sid = cur.lastrowid
    conn.close()
    return {
        "id": sid,
        "status": "created",
        "report": report,
        "next": "POST /api/bazi/" + str(sid) + "/interpret",
    }


@app.get("/api/bazi/{sid}")
def view_bazi_session(sid: int):
    row = get_bazi_session(sid)
    return {
        "id": sid,
        "status": row.get("status"),
        "solar": row.get("solar_date"),
        "lunar": row.get("lunar_date"),
        "sex": row.get("sex"),
        "has_reading": bool(row.get("reading_json")),
        "html_url": "/api/bazi/" + str(sid) + "/html" if row.get("html_path") else None,
        "created_at": str(row.get("created_at")),
        "updated_at": str(row.get("updated_at")),
    }


@app.post("/api/bazi/{sid}/interpret")
def interpret_bazi(sid: int):
    row = get_bazi_session(sid)
    if not row.get("report"):
        raise HTTPException(status_code=400, detail="该会话还没有排盘数据")
    if not DEEPSEEK_API_KEY:
        raise HTTPException(status_code=500, detail="未配置 DEEPSEEK_API_KEY 环境变量")

    messages = build_bazi_interpret_messages(row["report"])
    content = _deepseek_post(messages, response_format={"type": "json_object"})
    reading = engine.extract_json(content)

    html = build_bazi_html(row["report"], reading)
    session_dir = os.path.join(OUTPUT_DIR, "bazi_session_" + str(sid))
    os.makedirs(session_dir, exist_ok=True)
    html_path = os.path.join(session_dir, "bazi.html")
    with open(html_path, "w", encoding="utf-8") as f:
        f.write(html)

    update_bazi_session(
        sid,
        reading_json=json.dumps(reading, ensure_ascii=False),
        html_path=html_path,
        status="interpreted",
    )
    return {
        "id": sid,
        "reading": reading,
        "html_url": "/api/bazi/" + str(sid) + "/html",
    }


@app.get("/api/bazi/{sid}/html")
def view_bazi_html(sid: int):
    row = get_bazi_session(sid)
    if not row.get("html_path") or not os.path.exists(row["html_path"]):
        raise HTTPException(status_code=404, detail="该会话还没有生成八字报告，请先调用 POST /api/bazi/" + str(sid) + "/interpret")
    with open(row["html_path"], "r", encoding="utf-8") as f:
        return HTMLResponse(content=f.read())


if __name__ == "__main__":
    import uvicorn
    init_db()
    uvicorn.run(app, host="127.0.0.1", port=8000)
