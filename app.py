import os
import re
import sqlite3
from datetime import datetime, date, timedelta

from flask import Flask, g, render_template, request, redirect, url_for, session, abort

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "data", "wrkshp.db")
SECRET_KEY_PATH = os.path.join(BASE_DIR, "data", "secret_key.txt")
ADMIN_CODE = "AICENTER"
REGION_CODE = "TFA27"

app = Flask(__name__)


def get_secret_key():
    if os.path.exists(SECRET_KEY_PATH):
        with open(SECRET_KEY_PATH, "r") as f:
            return f.read().strip()
    key = os.urandom(24).hex()
    with open(SECRET_KEY_PATH, "w") as f:
        f.write(key)
    return key


app.secret_key = get_secret_key()


def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
    return g.db


@app.teardown_appcontext
def close_db(exception=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db():
    os.makedirs(os.path.join(BASE_DIR, "data"), exist_ok=True)
    db = sqlite3.connect(DB_PATH)
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS challenges (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            number INTEGER UNIQUE NOT NULL,
            slug TEXT UNIQUE NOT NULL,
            title TEXT NOT NULL,
            subtitle TEXT,
            body TEXT,
            difficulty TEXT,
            status TEXT NOT NULL DEFAULT 'active',
            created_at TEXT NOT NULL
        )
        """
    )
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS page_views (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            path TEXT NOT NULL,
            ts TEXT NOT NULL
        )
        """
    )
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS submissions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            challenge_id INTEGER NOT NULL,
            region_user_id INTEGER NOT NULL,
            url TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(challenge_id, region_user_id)
        )
        """
    )
    db.commit()

    page_view_cols = {row[1] for row in db.execute("PRAGMA table_info(page_views)")}
    if "region_user_id" not in page_view_cols:
        db.execute("ALTER TABLE page_views ADD COLUMN region_user_id INTEGER")
    if "actor" not in page_view_cols:
        db.execute("ALTER TABLE page_views ADD COLUMN actor TEXT")
    db.commit()

    tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if "region_users" in tables and "regions" not in tables:
        # Login model changed from individual accounts to a shared location + access
        # code. Old per-account test data (name/email/password) no longer applies.
        db.execute(
            """
            CREATE TABLE regions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT UNIQUE NOT NULL,
                created_at TEXT NOT NULL,
                last_active TEXT
            )
            """
        )
        db.execute("DELETE FROM submissions")
        db.execute("DELETE FROM page_views WHERE region_user_id IS NOT NULL")
        db.execute("DROP TABLE region_users")
        db.commit()
    else:
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS regions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT UNIQUE NOT NULL,
                created_at TEXT NOT NULL,
                last_active TEXT
            )
            """
        )
        db.commit()

    count = db.execute("SELECT COUNT(*) FROM challenges").fetchone()[0]
    if count == 0:
        seed = [
            (
                "Prompt the Impossible",
                "Get a model to solve a task it was never told how to do.",
                "Design a single prompt that gets an LLM to reliably complete a task "
                "outside its obvious training distribution. Document your approach, "
                "the failures along the way, and the final prompt.",
                "EASY",
            ),
            (
                "Build a Tool-Using Agent",
                "Wire an LLM up to real tools and let it loose.",
                "Build an agent that can call at least three external tools (search, "
                "code execution, a custom API) to complete a multi-step task end to end.",
                "MEDIUM",
            ),
            (
                "Jailbreak Your Own Guardrails",
                "Red-team a system you built to find where it breaks.",
                "Take a model-backed feature you control and attempt to break its "
                "safety or business-logic guardrails. Write up what worked and how "
                "you'd patch it.",
                "HARD",
            ),
            (
                "RAG Without the Slop",
                "Retrieval-augmented generation that actually cites its sources.",
                "Build a retrieval pipeline that answers questions from a document "
                "set with verifiable, accurate citations and no hallucinated sources.",
                "MEDIUM",
            ),
            (
                "Fine-Tune on a Shoestring",
                "Get a meaningful capability bump out of a tiny compute budget.",
                "Fine-tune or otherwise adapt an open model to noticeably improve at "
                "a narrow task, using the smallest budget you can get away with.",
                "HARD",
            ),
            (
                "Ship an AI Feature in a Day",
                "Idea to working demo before the clock runs out.",
                "Pick a real workflow, scope an AI-assisted feature for it, and ship "
                "a working demo in a single day. Speed and taste both count.",
                "EASY",
            ),
        ]
        now = datetime.utcnow().isoformat()
        for i, (title, subtitle, body, difficulty) in enumerate(seed, start=1):
            db.execute(
                "INSERT INTO challenges (number, slug, title, subtitle, body, difficulty, status, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, 'active', ?)",
                (i, slugify(title), title, subtitle, body, difficulty, now),
            )
        db.commit()
    db.close()


def slugify(text):
    slug = text.lower()
    slug = re.sub(r"[^a-z0-9]+", "-", slug).strip("-")
    return slug or "challenge"


def unique_slug(db, base_slug):
    slug = base_slug
    n = 2
    while db.execute("SELECT 1 FROM challenges WHERE slug = ?", (slug,)).fetchone():
        slug = f"{base_slug}-{n}"
        n += 1
    return slug


def next_number(db):
    row = db.execute("SELECT MAX(number) FROM challenges").fetchone()
    return (row[0] or 0) + 1


def is_admin():
    return session.get("admin") is True


def is_region_user():
    return session.get("region_id") is not None


def logged_in():
    return is_admin() or is_region_user()


ONLINE_WINDOW = timedelta(minutes=5)

TRACK_SKIP_PREFIXES = ("/static/",)
TRACK_SKIP_PATHS = ("/favicon.ico",)


@app.before_request
def track_traffic():
    if request.method != "GET":
        return
    if request.path.startswith(TRACK_SKIP_PREFIXES) or request.path in TRACK_SKIP_PATHS:
        return

    db = get_db()
    now = datetime.utcnow().isoformat()
    region_id = session.get("region_id")

    if region_id:
        actor = session.get("region_label")
    elif is_admin():
        actor = "ADMIN"
    else:
        actor = None

    db.execute(
        "INSERT INTO page_views (path, ts, region_user_id, actor) VALUES (?, ?, ?, ?)",
        (request.path, now, region_id, actor),
    )
    if region_id:
        db.execute("UPDATE regions SET last_active = ? WHERE id = ?", (now, region_id))
    db.commit()


def render_landing(admin_error=None, region_login_error=None):
    return render_template(
        "landing.html",
        admin_error=admin_error,
        region_login_error=region_login_error,
    )


@app.route("/")
def landing():
    if is_admin():
        return redirect(url_for("admin_dashboard"))
    if is_region_user():
        return redirect(url_for("challenge_list"))
    return render_landing()


@app.route("/challenges")
def challenge_list():
    if not logged_in():
        return redirect(url_for("landing"))
    db = get_db()
    challenges = db.execute(
        "SELECT * FROM challenges WHERE status IN ('active', 'hidden') ORDER BY number ASC"
    ).fetchall()
    return render_template("index.html", challenges=challenges)


@app.route("/challenges/<slug>")
def challenge_detail(slug):
    if not logged_in():
        return redirect(url_for("landing"))
    db = get_db()
    challenge = db.execute(
        "SELECT * FROM challenges WHERE slug = ?", (slug,)
    ).fetchone()
    if challenge is None:
        abort(404)
    if challenge["status"] == "hidden" and not is_admin():
        abort(404)

    submission = None
    if is_region_user():
        submission = db.execute(
            "SELECT * FROM submissions WHERE challenge_id = ? AND region_user_id = ?",
            (challenge["id"], session["region_id"]),
        ).fetchone()

    return render_template("challenge.html", challenge=challenge, submission=submission)


@app.route("/challenges/<slug>/submit", methods=["POST"])
def submit_challenge(slug):
    if not is_region_user():
        return redirect(url_for("landing"))

    db = get_db()
    challenge = db.execute(
        "SELECT * FROM challenges WHERE slug = ?", (slug,)
    ).fetchone()
    if challenge is None or challenge["status"] == "hidden":
        abort(404)

    url = request.form.get("url", "").strip()
    if not url or not re.match(r"^https?://", url, re.IGNORECASE):
        abort(400)

    now = datetime.utcnow().isoformat()
    region_id = session["region_id"]

    existing = db.execute(
        "SELECT id FROM submissions WHERE challenge_id = ? AND region_user_id = ?",
        (challenge["id"], region_id),
    ).fetchone()
    if existing:
        db.execute(
            "UPDATE submissions SET url = ?, updated_at = ? WHERE id = ?",
            (url, now, existing["id"]),
        )
    else:
        db.execute(
            "INSERT INTO submissions (challenge_id, region_user_id, url, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (challenge["id"], region_id, url, now, now),
        )
    db.commit()
    return redirect(url_for("challenge_detail", slug=slug))


@app.route("/admin")
def admin_root():
    if is_admin():
        return redirect(url_for("admin_dashboard"))
    return redirect(url_for("landing"))


@app.route("/admin/login", methods=["POST"])
def admin_login():
    code = request.form.get("code", "")
    if code == ADMIN_CODE:
        session["admin"] = True
        return redirect(url_for("admin_dashboard"))
    return render_landing(admin_error="INVALID CODE")


@app.route("/admin/logout", methods=["POST"])
def admin_logout():
    session.pop("admin", None)
    return redirect(url_for("landing"))


@app.route("/regions/login", methods=["POST"])
def region_login():
    name = request.form.get("region", "").strip()
    code = request.form.get("code", "")

    if not name or code != REGION_CODE:
        return render_landing(region_login_error="INVALID LOCATION OR ACCESS CODE")

    db = get_db()
    now = datetime.utcnow().isoformat()
    row = db.execute(
        "SELECT * FROM regions WHERE name = ? COLLATE NOCASE", (name,)
    ).fetchone()
    if row is None:
        cur = db.execute(
            "INSERT INTO regions (name, created_at, last_active) VALUES (?, ?, ?)",
            (name, now, now),
        )
        db.commit()
        region_id = cur.lastrowid
    else:
        region_id = row["id"]
        db.execute("UPDATE regions SET last_active = ? WHERE id = ?", (now, region_id))
        db.commit()

    session["region_id"] = region_id
    session["region_label"] = name
    return redirect(url_for("challenge_list"))


@app.route("/regions/logout", methods=["POST"])
def region_logout():
    session.pop("region_id", None)
    session.pop("region_label", None)
    return redirect(url_for("landing"))


@app.route("/admin/dashboard")
def admin_dashboard():
    if not is_admin():
        return redirect(url_for("admin_root"))

    db = get_db()

    total_views = db.execute(
        "SELECT COUNT(*) FROM page_views WHERE path LIKE '/challenges/%'"
    ).fetchone()[0]

    today_str = date.today().isoformat()
    views_today = db.execute(
        "SELECT COUNT(*) FROM page_views WHERE path LIKE '/challenges/%' AND ts LIKE ?",
        (f"{today_str}%",),
    ).fetchone()[0]

    unique_paths = db.execute(
        "SELECT COUNT(DISTINCT path) FROM page_views WHERE path LIKE '/challenges/%'"
    ).fetchone()[0]

    top_pages = db.execute(
        "SELECT path, COUNT(*) as hits FROM page_views "
        "WHERE path LIKE '/challenges/%' "
        "GROUP BY path ORDER BY hits DESC LIMIT 10"
    ).fetchall()

    recent_hits = db.execute(
        "SELECT path, ts, actor FROM page_views "
        "WHERE path LIKE '/challenges/%' "
        "ORDER BY id DESC LIMIT 25"
    ).fetchall()

    challenges = db.execute(
        "SELECT * FROM challenges ORDER BY number ASC"
    ).fetchall()

    total_challenges = len(challenges)
    active_challenges = sum(1 for c in challenges if c["status"] == "active")
    visible_challenges = sum(1 for c in challenges if c["status"] in ("active", "hidden"))

    regions = db.execute(
        "SELECT * FROM regions ORDER BY last_active DESC, created_at DESC"
    ).fetchall()

    view_counts = db.execute(
        "SELECT region_user_id, COUNT(DISTINCT path) as cnt FROM page_views "
        "WHERE path LIKE '/challenges/%' AND region_user_id IS NOT NULL "
        "GROUP BY region_user_id"
    ).fetchall()
    challenge_counts = {row["region_user_id"]: row["cnt"] for row in view_counts}

    online_cutoff = (datetime.utcnow() - ONLINE_WINDOW).isoformat()
    online_ids = {
        r["id"] for r in regions if r["last_active"] and r["last_active"] >= online_cutoff
    }

    total_regions = len(regions)
    online_now = len(online_ids)

    return render_template(
        "admin_dashboard.html",
        total_views=total_views,
        views_today=views_today,
        unique_paths=unique_paths,
        top_pages=top_pages,
        recent_hits=recent_hits,
        challenges=challenges,
        total_challenges=total_challenges,
        active_challenges=active_challenges,
        visible_challenges=visible_challenges,
        regions=regions,
        challenge_counts=challenge_counts,
        online_ids=online_ids,
        total_regions=total_regions,
        online_now=online_now,
    )


@app.route("/admin/challenges", methods=["POST"])
def admin_create_challenge():
    if not is_admin():
        return redirect(url_for("admin_root"))

    title = request.form.get("title", "").strip()
    subtitle = request.form.get("subtitle", "").strip()
    body = request.form.get("body", "").strip()
    difficulty = request.form.get("difficulty", "").strip().upper()

    if not title:
        return redirect(url_for("admin_dashboard"))

    db = get_db()
    slug = unique_slug(db, slugify(title))
    number = next_number(db)
    now = datetime.utcnow().isoformat()

    db.execute(
        "INSERT INTO challenges (number, slug, title, subtitle, body, difficulty, status, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, 'active', ?)",
        (number, slug, title, subtitle, body, difficulty, now),
    )
    db.commit()
    return redirect(url_for("admin_dashboard"))


VALID_STATUSES = ("active", "hidden", "retired")


@app.route("/admin/challenges/<int:challenge_id>/status", methods=["POST"])
def admin_set_challenge_status(challenge_id):
    if not is_admin():
        return redirect(url_for("admin_root"))

    new_status = request.form.get("status", "")
    if new_status not in VALID_STATUSES:
        abort(400)

    db = get_db()
    row = db.execute(
        "SELECT id FROM challenges WHERE id = ?", (challenge_id,)
    ).fetchone()
    if row is None:
        abort(404)
    db.execute(
        "UPDATE challenges SET status = ? WHERE id = ?", (new_status, challenge_id)
    )
    db.commit()
    return redirect(url_for("admin_dashboard"))


@app.route("/admin/challenges/<int:challenge_id>/delete", methods=["POST"])
def admin_delete_challenge(challenge_id):
    if not is_admin():
        return redirect(url_for("admin_root"))

    db = get_db()
    db.execute("DELETE FROM challenges WHERE id = ?", (challenge_id,))
    db.commit()
    return redirect(url_for("admin_dashboard"))


init_db()

if __name__ == "__main__":
    app.run(debug=True, port=5050)
