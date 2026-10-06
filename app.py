"""
London grad jobs: Vercel edition.

Differences from the laptop version:
  * Data lives in Postgres (Vercel's filesystem is wiped between requests).
  * Checks are triggered by a schedule calling /api/cron, not a background loop.
  * The whole site sits behind a password, because it's on the open internet.

Environment variables (set in Vercel -> Project -> Settings -> Environment Variables):
  DATABASE_URL   added automatically when you connect a Neon Postgres database
  APP_PASSWORD   the password for the site
  CRON_SECRET    a long random string; scheduled checks must present it
"""
import copy
import hmac
import os
import smtplib
from datetime import datetime, timedelta, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from html import escape

import psycopg
from flask import Flask, Response, jsonify, render_template, request
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

import scanner

DB_URL = os.environ.get("DATABASE_URL") or os.environ.get("POSTGRES_URL", "")
APP_PASSWORD = os.environ.get("APP_PASSWORD", "")
CRON_SECRET = os.environ.get("CRON_SECRET", "")

STATUSES = {"new", "seen", "saved", "applied", "hidden"}
LIST_KEYS = ["greenhouse", "lever", "ashby", "smartrecruiters", "jsonld_pages",
             "search_terms", "include", "exclude", "locations", "location_exclude"]

app = Flask(__name__)
_db_ready = False


# ------------------------------------------------------------- database

def db():
    global _db_ready
    con = psycopg.connect(DB_URL, row_factory=dict_row, autocommit=True)
    if not _db_ready:
        con.execute("""CREATE TABLE IF NOT EXISTS jobs (
            id SERIAL PRIMARY KEY, key TEXT UNIQUE NOT NULL,
            company TEXT, title TEXT, location TEXT, url TEXT, posted TEXT, source TEXT,
            first_seen TIMESTAMPTZ DEFAULT now(), status TEXT DEFAULT 'new')""")
        con.execute("CREATE TABLE IF NOT EXISTS kv (name TEXT PRIMARY KEY, value JSONB)")
        _db_ready = True
    return con


def kv_get(con, name, default):
    row = con.execute("SELECT value FROM kv WHERE name=%s", (name,)).fetchone()
    return row["value"] if row else default


def kv_set(con, name, value):
    con.execute("INSERT INTO kv (name, value) VALUES (%s, %s) "
                "ON CONFLICT (name) DO UPDATE SET value = EXCLUDED.value",
                (name, Jsonb(value)))


def load_settings(con):
    s = copy.deepcopy(scanner.DEFAULT_SETTINGS)
    saved = kv_get(con, "settings", {})
    email = {**s["email"], **saved.pop("email", {})}
    s.update(saved)
    s["email"] = email
    return s


# ------------------------------------------------------------- security

@app.before_request
def require_password():
    if request.path == "/api/cron":
        return None                      # protected by CRON_SECRET instead
    if not APP_PASSWORD:
        return Response("Set the APP_PASSWORD environment variable in Vercel, then redeploy.",
                        503, mimetype="text/plain")
    auth = request.authorization
    if not auth or not hmac.compare_digest((auth.password or "").encode(), APP_PASSWORD.encode()):
        return Response("Password required", 401,
                        {"WWW-Authenticate": 'Basic realm="London grad jobs"'})
    return None


# ------------------------------------------------------------- scanning

def send_email(s, jobs):
    e = s["email"]
    rows = "".join(
        f"<tr><td style='padding:6px 10px'>{escape(j['company'])}</td>"
        f"<td style='padding:6px 10px'><a href='{escape(j['url'])}'>{escape(j['title'])}</a></td>"
        f"<td style='padding:6px 10px'>{escape(j['location'])}</td></tr>" for j in jobs)
    subject = f"{len(jobs)} new graduate job{'s' if len(jobs) != 1 else ''} in London"
    body = (f"<h2 style='font-family:sans-serif'>{subject}</h2>"
            f"<table style='font-family:sans-serif;border-collapse:collapse'>{rows}</table>")
    to = [a.strip() for a in str(e["to"]).split(",") if a.strip()]
    msg = MIMEMultipart("alternative")
    msg["Subject"], msg["From"], msg["To"] = subject, e["username"], ", ".join(to)
    msg.attach(MIMEText(body, "html"))
    with smtplib.SMTP(e["smtp_host"], int(e["smtp_port"]), timeout=30) as smtp:
        smtp.starttls()
        smtp.login(e["username"], e["password"])
        smtp.sendmail(e["username"], to, msg.as_string())


def do_scan():
    """Run one check synchronously and return the stored status."""
    con = db()
    status = kv_get(con, "status", {})
    started = status.get("running_since")
    if started and datetime.fromisoformat(started) > datetime.now(timezone.utc) - timedelta(minutes=6):
        return status                     # another check is already running
    status = {**status, "running_since": datetime.now(timezone.utc).isoformat()}
    kv_set(con, "status", status)

    lines = []
    log = lines.append
    new_count = 0
    try:
        s = load_settings(con)
        found = scanner.run_scan(s, log)
        new = []
        for j in found:
            row = con.execute(
                "INSERT INTO jobs (key, company, title, location, url, posted, source) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (key) DO NOTHING RETURNING id",
                (j["key"], j["company"], j["title"], j["location"], j["url"],
                 j["posted"], j["source"])).fetchone()
            if row:
                new.append(j)
        new_count = len(new)
        log(f"{new_count} new role{'s' if new_count != 1 else ''} added.")
        if new and s["email"]["enabled"]:
            try:
                send_email(s, new)
                log("Email digest sent.")
            except Exception as e:
                log(f"Email failed ({e}). Check the email settings.")
    except Exception as e:
        log(f"Check failed ({e})")
    status = {"running_since": None, "last_run": datetime.now(timezone.utc).isoformat(),
              "last_new": new_count, "log": lines}
    kv_set(con, "status", status)
    con.close()
    return status


# ------------------------------------------------------------- routes

@app.get("/")
def index():
    return render_template("index.html")


@app.get("/api/cron")
def api_cron():
    if not CRON_SECRET:
        return jsonify(error="CRON_SECRET is not set"), 503
    sent = request.headers.get("Authorization", "")
    if not hmac.compare_digest(sent.encode(), f"Bearer {CRON_SECRET}".encode()):
        return jsonify(error="Unauthorized"), 401
    status = do_scan()
    return jsonify(ok=True, new=status.get("last_new", 0))


@app.post("/api/check")
def api_check():
    return jsonify(public_status(do_scan()))


def public_status(st):
    return {"running": bool(st.get("running_since")), "last_run": st.get("last_run"),
            "last_new": st.get("last_new", 0), "log": st.get("log", [])}


@app.get("/api/status")
def api_status():
    with db() as con:
        return jsonify(public_status(kv_get(con, "status", {})))


@app.get("/api/jobs")
def api_jobs():
    with db() as con:
        rows = con.execute("SELECT * FROM jobs ORDER BY first_seen DESC, company").fetchall()
    for r in rows:
        r["first_seen"] = r["first_seen"].isoformat() if r["first_seen"] else None
    return jsonify(rows)


@app.post("/api/jobs/<int:job_id>/status")
def api_set_status(job_id):
    status = (request.get_json(silent=True) or {}).get("status")
    if status not in STATUSES:
        return jsonify(error="Unknown status"), 400
    with db() as con:
        con.execute("UPDATE jobs SET status=%s WHERE id=%s", (status, job_id))
    return jsonify(ok=True)


@app.post("/api/jobs/mark-read")
def api_mark_read():
    with db() as con:
        con.execute("UPDATE jobs SET status='seen' WHERE status='new'")
    return jsonify(ok=True)


@app.get("/api/settings")
def api_get_settings():
    with db() as con:
        s = load_settings(con)
    s["email"] = {**s["email"], "password": "", "has_password": bool(s["email"]["password"])}
    return jsonify(s)


@app.post("/api/settings")
def api_save_settings():
    incoming = request.get_json(silent=True) or {}
    with db() as con:
        s = load_settings(con)
        for k in LIST_KEYS:
            if k in incoming:
                s[k] = [x.strip() for x in incoming[k] if str(x).strip()]
        for k in ["adzuna_app_id", "adzuna_app_key", "reed_api_key", "search_location"]:
            if k in incoming:
                s[k] = str(incoming[k]).strip()
        e = incoming.get("email") or {}
        for k in ["enabled", "smtp_host", "smtp_port", "username", "to"]:
            if k in e:
                s["email"][k] = e[k]
        if e.get("password"):            # blank means "keep the existing password"
            s["email"]["password"] = e["password"]
        kv_set(con, "settings", s)
    return jsonify(ok=True)


if __name__ == "__main__":               # local testing: python app.py
    app.run(host="127.0.0.1", port=5000, debug=True)
