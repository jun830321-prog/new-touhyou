import sqlite3
import random
import uuid
import threading
import time
import os
from datetime import datetime, timedelta
from flask import Flask, render_template, request, jsonify, redirect, g, make_response

app = Flask(__name__)
DATABASE = "votefocus.db"


def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DATABASE)
        g.db.row_factory = sqlite3.Row
    return g.db


@app.teardown_appcontext
def close_db(exception):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db():
    db = sqlite3.connect(DATABASE)
    db.execute("""
        CREATE TABLE IF NOT EXISTS groups (
            group_number TEXT PRIMARY KEY,
            group_name TEXT NOT NULL,
            password TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'setup',
            participant_count INTEGER NOT NULL DEFAULT 1,
            votes_per_person INTEGER NOT NULL DEFAULT 5,
            voted_count INTEGER NOT NULL DEFAULT 0,
            final_decision TEXT,
            final_votes INTEGER,
            shuffle_options INTEGER NOT NULL DEFAULT 1,
            invite_token TEXT,
            created_at TEXT,
            result_started_at TEXT,
            closed_at TEXT
        )
    """)
    db.execute("""
        CREATE TABLE IF NOT EXISTS options (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            group_number TEXT NOT NULL,
            name TEXT NOT NULL DEFAULT '',
            placeholder TEXT NOT NULL DEFAULT '',
            position INTEGER NOT NULL
        )
    """)
    db.execute("""
        CREATE TABLE IF NOT EXISTS chat_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            group_number TEXT NOT NULL,
            text TEXT NOT NULL
        )
    """)
    db.execute("""
        CREATE TABLE IF NOT EXISTS participants (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            group_number TEXT NOT NULL,
            token TEXT NOT NULL,
            is_owner INTEGER NOT NULL DEFAULT 0,
            has_voted INTEGER NOT NULL DEFAULT 0,
            UNIQUE(group_number, token)
        )
    """)
    db.execute("""
        CREATE TABLE IF NOT EXISTS votes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            group_number TEXT NOT NULL,
            token TEXT NOT NULL,
            option_id INTEGER NOT NULL,
            count INTEGER NOT NULL DEFAULT 0,
            UNIQUE(token, option_id)
        )
    """)
    db.commit()
    db.close()


init_db()


def generate_group_number():
    db = get_db()
    while True:
        candidate = str(random.randint(0, 9999)).zfill(4)
        exists = db.execute("SELECT 1 FROM groups WHERE group_number = ?", (candidate,)).fetchone()
        if not exists:
            return candidate


def get_results(group_number, db=None):
    if db is None:
        db = get_db()

    options_rows = db.execute(
        "SELECT id, name FROM options WHERE group_number = ? ORDER BY position",
        (group_number,)
    ).fetchall()

    results = []
    for opt in options_rows:
        total = db.execute(
            "SELECT COALESCE(SUM(count), 0) AS total FROM votes WHERE group_number = ? AND option_id = ?",
            (group_number, opt["id"])
        ).fetchone()["total"]
        results.append({"id": opt["id"], "name": opt["name"], "votes": total})

    results.sort(key=lambda r: r["votes"], reverse=True)
    total_votes = sum(r["votes"] for r in results)
    max_votes = results[0]["votes"] if results else 0
    tied_ids = [r["id"] for r in results if r["votes"] == max_votes and max_votes > 0]
    is_tie = len(tied_ids) > 1

    for i, r in enumerate(results):
        r["rank"] = i + 1
        r["percentage"] = round((r["votes"] / total_votes) * 100) if total_votes > 0 else 0
        r["is_tie"] = r["id"] in tied_ids and is_tie

    return {
        "results": results,
        "total_votes": total_votes,
        "is_tie": is_tie,
        "tied_option_ids": tied_ids if is_tie else [],
    }


def _delete_group_completely(group_number, db=None):
    if db is None:
        db = get_db()
    db.execute("DELETE FROM votes WHERE group_number = ?", (group_number,))
    db.execute("DELETE FROM participants WHERE group_number = ?", (group_number,))
    db.execute("DELETE FROM chat_messages WHERE group_number = ?", (group_number,))
    db.execute("DELETE FROM options WHERE group_number = ?", (group_number,))
    db.execute("DELETE FROM groups WHERE group_number = ?", (group_number,))
    db.commit()


def apply_auto_timers(group_number, db=None):
    if db is None:
        db = get_db()

    row = db.execute("SELECT * FROM groups WHERE group_number = ?", (group_number,)).fetchone()
    if row is None:
        return

    now = datetime.utcnow()

    if row["status"] == "setup" and row["created_at"]:
        if now - datetime.fromisoformat(row["created_at"]) > timedelta(days=30):
            _delete_group_completely(group_number, db)
            return

    elif row["status"] == "result" and row["result_started_at"]:
        if now - datetime.fromisoformat(row["result_started_at"]) > timedelta(days=1):
            result_data = get_results(group_number, db)
            winner = result_data["results"][0] if result_data["results"] else None
            if winner:
                db.execute(
                    "UPDATE groups SET status = 'closed', final_decision = ?, final_votes = ?, closed_at = ? WHERE group_number = ?",
                    (winner["name"], winner["votes"], now.isoformat(), group_number)
                )
                db.commit()

    elif row["status"] == "closed" and row["closed_at"]:
        if now - datetime.fromisoformat(row["closed_at"]) > timedelta(days=1):
            _delete_group_completely(group_number, db)


def background_cleanup_loop():
    while True:
        try:
            conn = sqlite3.connect(DATABASE)
            conn.row_factory = sqlite3.Row
            rows = conn.execute("SELECT group_number FROM groups").fetchall()
            for row in rows:
                apply_auto_timers(row["group_number"], conn)
            conn.close()
        except Exception as e:
            print("見回り処理でエラー:", e)
        time.sleep(3600)


def get_room(group_number):
    apply_auto_timers(group_number)
    db = get_db()
    group_row = db.execute("SELECT * FROM groups WHERE group_number = ?", (group_number,)).fetchone()
    if group_row is None:
        return None

    options_rows = db.execute(
        "SELECT id, name, placeholder FROM options WHERE group_number = ? ORDER BY position",
        (group_number,)
    ).fetchall()
    chat_rows = db.execute(
        "SELECT text FROM chat_messages WHERE group_number = ? ORDER BY id",
        (group_number,)
    ).fetchall()

    return {
        "group_name": group_row["group_name"],
        "password": group_row["password"],
        "status": group_row["status"],
        "participant_count": group_row["participant_count"],
        "votes_per_person": group_row["votes_per_person"],
        "voted_count": group_row["voted_count"],
        "shuffle_options": bool(group_row["shuffle_options"]),
        "options": [{"id": r["id"], "name": r["name"], "placeholder": r["placeholder"]} for r in options_rows],
        "chat": [r["text"] for r in chat_rows],
    }


@app.route("/")
def home():
    return render_template("index.html")


@app.route("/create", methods=["GET", "POST"])
def create_group():
    if request.method == "POST":
        group_name = request.form.get("group_name")
        password = request.form.get("password")
        votes_per_person = int(request.form.get("votes_per_person", 5))
        period_days = request.form.get("period_days", 7)

        group_number = generate_group_number()

        db = get_db()
        shuffle_options = 1 if request.form.get("shuffle_options") else 0
        invite_token = uuid.uuid4().hex[:10]
        created_at = datetime.utcnow().isoformat()
        db.execute(
            "INSERT INTO groups (group_number, group_name, password, status, participant_count, votes_per_person, voted_count, shuffle_options, invite_token, created_at) VALUES (?, ?, ?, 'setup', 1, ?, 0, ?, ?, ?)",
            (group_number, group_name, password, votes_per_person, shuffle_options, invite_token, created_at)
        )

        default_options = [
            ("例:週末の午後開催",),
            ("例:平日夜に開催",),
            ("例:オンライン開催",),
        ]
        for i, (placeholder,) in enumerate(default_options):
            db.execute(
                "INSERT INTO options (group_number, name, placeholder, position) VALUES (?, '', ?, ?)",
                (group_number, placeholder, i)
            )

        token = request.cookies.get("participant_token") or str(uuid.uuid4())
        db.execute(
            "INSERT INTO participants (group_number, token, is_owner) VALUES (?, ?, 1)",
            (group_number, token)
        )
        db.commit()

        resp = make_response(render_template(
            "group_created.html",
            group_name=group_name,
            password=password,
            votes_per_person=votes_per_person,
            period_days=period_days,
            group_number=group_number,
            invite_token=invite_token
        ))
        resp.set_cookie("participant_token", token, max_age=60 * 60 * 24 * 365)
        return resp
    return render_template("create_group.html")


@app.route("/room/<group_number>")
def room_setup(group_number):
    token = request.cookies.get("participant_token")
    db = get_db()
    participant = None
    if token:
        participant = db.execute(
            "SELECT is_owner, has_voted FROM participants WHERE group_number = ? AND token = ?",
            (group_number, token)
        ).fetchone()
    is_owner = bool(participant and participant["is_owner"])

    room = get_room(group_number)
    if room is None:
        return "グループが見つかりません", 404

    if room["status"] == "voting":
        my_votes = [0] * len(room["options"])
        has_voted = bool(participant and participant["has_voted"])

        if token:
            vote_rows = db.execute(
                "SELECT option_id, count FROM votes WHERE group_number = ? AND token = ?",
                (group_number, token)
            ).fetchall()
            vote_map = {r["option_id"]: r["count"] for r in vote_rows}
            for i, opt in enumerate(room["options"]):
                my_votes[i] = vote_map.get(opt["id"], 0)

        options_for_vote = room["options"][:]
        if room["shuffle_options"]:
            random.shuffle(options_for_vote)

        return render_template(
            "room_voting.html",
            group_number=group_number,
            group_name=room["group_name"],
            participant_count=room["participant_count"],
            is_owner=is_owner,
            options=options_for_vote,
            votes_per_person=room["votes_per_person"],
            voted_count=room["voted_count"],
            my_votes=my_votes,
            has_voted=has_voted
        )

    if room["status"] == "result":
        result_data = get_results(group_number)
        return render_template(
            "room_result.html",
            group_number=group_number,
            group_name=room["group_name"],
            participant_count=room["participant_count"],
            is_owner=is_owner,
            **result_data
        )

    if room["status"] == "closed":
        group_row = db.execute(
            "SELECT final_decision, final_votes FROM groups WHERE group_number = ?", (group_number,)
        ).fetchone()
        return render_template(
            "room_closed.html",
            group_name=room["group_name"],
            final_decision=group_row["final_decision"],
            final_votes=group_row["final_votes"]
        )

    return render_template(
        "room_setup.html",
        group_number=group_number,
        group_name=room["group_name"],
        participant_count=room["participant_count"],
        is_owner=is_owner,
        options=room["options"]
    )


@app.route("/join", methods=["GET", "POST"])
def join_group():
    error = None
    if request.method == "POST":
        group_number = request.form.get("group_number", "").strip()
        password = request.form.get("password", "").strip()

        db = get_db()
        group_row = db.execute(
            "SELECT password FROM groups WHERE group_number = ?", (group_number,)
        ).fetchone()

        if group_row is None:
            error = "グループが見つかりません。番号を確認してください"
        elif group_row["password"] != password:
            error = "パスワードが正しくありません"
        else:
            token = request.cookies.get("participant_token") or str(uuid.uuid4())
            existing = db.execute(
                "SELECT id FROM participants WHERE group_number = ? AND token = ?",
                (group_number, token)
            ).fetchone()

            if existing is None:
                db.execute(
                    "INSERT INTO participants (group_number, token, is_owner) VALUES (?, ?, 0)",
                    (group_number, token)
                )
                db.execute(
                    "UPDATE groups SET participant_count = participant_count + 1 WHERE group_number = ?",
                    (group_number,)
                )
                db.commit()

            resp = make_response(redirect(f"/room/{group_number}"))
            resp.set_cookie("participant_token", token, max_age=60 * 60 * 24 * 365)
            return resp

    return render_template("join_group.html", error=error)


@app.route("/j/<invite_token>")
def join_by_token(invite_token):
    db = get_db()
    group_row = db.execute(
        "SELECT group_number FROM groups WHERE invite_token = ?", (invite_token,)
    ).fetchone()

    if group_row is None:
        return "リンクが無効です", 404

    group_number = group_row["group_number"]
    token = request.cookies.get("participant_token") or str(uuid.uuid4())

    existing = db.execute(
        "SELECT id FROM participants WHERE group_number = ? AND token = ?",
        (group_number, token)
    ).fetchone()

    if existing is None:
        db.execute(
            "INSERT INTO participants (group_number, token, is_owner) VALUES (?, ?, 0)",
            (group_number, token)
        )
        db.execute(
            "UPDATE groups SET participant_count = participant_count + 1 WHERE group_number = ?",
            (group_number,)
        )
        db.commit()

    resp = make_response(redirect(f"/room/{group_number}"))
    resp.set_cookie("participant_token", token, max_age=60 * 60 * 24 * 365)
    return resp


@app.route("/api/my-groups")
def api_my_groups():
    token = request.cookies.get("participant_token")
    if not token:
        return jsonify([])

    db = get_db()
    rows = db.execute(
        """SELECT g.group_number, g.group_name, g.status, p.is_owner
           FROM participants p
           JOIN groups g ON p.group_number = g.group_number
           WHERE p.token = ? AND g.status != 'closed'
           ORDER BY g.group_number DESC""",
        (token,)
    ).fetchall()

    return jsonify([
        {
            "group_number": r["group_number"],
            "group_name": r["group_name"],
            "status": r["status"],
            "is_owner": bool(r["is_owner"]),
        }
        for r in rows
    ])


@app.route("/api/room/<group_number>")
def api_room_status(group_number):
    room = get_room(group_number)
    if room is None:
        return jsonify({"error": "not found"}), 404
    return jsonify(room)


@app.route("/api/room/<group_number>/option", methods=["POST"])
def api_add_option(group_number):
    db = get_db()
    name = request.json.get("name", "").strip()
    if name:
        max_pos = db.execute(
            "SELECT COALESCE(MAX(position), -1) AS m FROM options WHERE group_number = ?",
            (group_number,)
        ).fetchone()["m"]
        db.execute(
            "INSERT INTO options (group_number, name, placeholder, position) VALUES (?, ?, '', ?)",
            (group_number, name, max_pos + 1)
        )
        db.commit()
    return jsonify(get_room(group_number))


@app.route("/api/room/<group_number>/option/update", methods=["POST"])
def api_update_option(group_number):
    db = get_db()
    index = request.json.get("index")
    name = request.json.get("name", "").strip()
    rows = db.execute(
        "SELECT id FROM options WHERE group_number = ? ORDER BY position",
        (group_number,)
    ).fetchall()
    if index is not None and 0 <= index < len(rows):
        option_id = rows[index]["id"]
        db.execute("UPDATE options SET name = ? WHERE id = ?", (name, option_id))
        db.commit()
    return jsonify(get_room(group_number))


@app.route("/api/room/<group_number>/option/delete", methods=["POST"])
def api_delete_option(group_number):
    db = get_db()
    index = request.json.get("index")
    rows = db.execute(
        "SELECT id FROM options WHERE group_number = ? ORDER BY position",
        (group_number,)
    ).fetchall()
    if index is not None and 0 <= index < len(rows):
        option_id = rows[index]["id"]
        db.execute("DELETE FROM options WHERE id = ?", (option_id,))
        db.commit()
    return jsonify(get_room(group_number))


@app.route("/api/room/<group_number>/chat", methods=["POST"])
def api_add_chat(group_number):
    db = get_db()
    text = request.json.get("text", "").strip()
    if text:
        db.execute(
            "INSERT INTO chat_messages (group_number, text) VALUES (?, ?)",
            (group_number, text)
        )
        db.commit()
    return jsonify(get_room(group_number))


@app.route("/api/room/<group_number>/start", methods=["POST"])
def api_start_voting(group_number):
    db = get_db()
    db.execute("UPDATE groups SET status = 'voting' WHERE group_number = ?", (group_number,))
    db.commit()
    return jsonify(get_room(group_number))


@app.route("/api/room/<group_number>/vote/save", methods=["POST"])
def api_vote_save(group_number):
    token = request.cookies.get("participant_token")
    if not token:
        return jsonify({"error": "no token"}), 400

    db = get_db()
    votes = request.json.get("votes", [])

    db.execute("DELETE FROM votes WHERE group_number = ? AND token = ?", (group_number, token))
    for v in votes:
        if v.get("count", 0) > 0:
            db.execute(
                "INSERT INTO votes (group_number, token, option_id, count) VALUES (?, ?, ?, ?)",
                (group_number, token, v["option_id"], v["count"])
            )

    participant = db.execute(
        "SELECT has_voted FROM participants WHERE group_number = ? AND token = ?",
        (group_number, token)
    ).fetchone()

    if participant and not participant["has_voted"]:
        db.execute(
            "UPDATE participants SET has_voted = 1 WHERE group_number = ? AND token = ?",
            (group_number, token)
        )
        db.execute(
            "UPDATE groups SET voted_count = voted_count + 1 WHERE group_number = ?",
            (group_number,)
        )

    db.commit()
    return jsonify(get_room(group_number))


@app.route("/api/room/<group_number>/vote/undo", methods=["POST"])
def api_vote_undo(group_number):
    token = request.cookies.get("participant_token")
    if not token:
        return jsonify({"error": "no token"}), 400

    db = get_db()
    participant = db.execute(
        "SELECT has_voted FROM participants WHERE group_number = ? AND token = ?",
        (group_number, token)
    ).fetchone()

    if participant and participant["has_voted"]:
        db.execute(
            "UPDATE participants SET has_voted = 0 WHERE group_number = ? AND token = ?",
            (group_number, token)
        )
        db.execute(
            "UPDATE groups SET voted_count = MAX(voted_count - 1, 0) WHERE group_number = ?",
            (group_number,)
        )

    db.commit()
    return jsonify(get_room(group_number))


@app.route("/api/room/<group_number>/leave", methods=["POST"])
def api_leave_room(group_number):
    token = request.cookies.get("participant_token")
    if not token:
        return jsonify({"ok": True})

    db = get_db()
    participant = db.execute(
        "SELECT id, has_voted FROM participants WHERE group_number = ? AND token = ?",
        (group_number, token)
    ).fetchone()

    if participant:
        db.execute("DELETE FROM votes WHERE group_number = ? AND token = ?", (group_number, token))
        if participant["has_voted"]:
            db.execute(
                "UPDATE groups SET voted_count = MAX(voted_count - 1, 0) WHERE group_number = ?",
                (group_number,)
            )
        db.execute(
            "UPDATE groups SET participant_count = MAX(participant_count - 1, 0) WHERE group_number = ?",
            (group_number,)
        )
        db.execute("DELETE FROM participants WHERE id = ?", (participant["id"],))
        db.commit()

    return jsonify({"ok": True})


@app.route("/api/room/<group_number>/close", methods=["POST"])
def api_close_voting(group_number):
    db = get_db()
    db.execute(
        "UPDATE groups SET status = 'result', result_started_at = ? WHERE group_number = ?",
        (datetime.utcnow().isoformat(), group_number)
    )
    db.commit()
    return jsonify({"ok": True})


@app.route("/api/room/<group_number>/revote", methods=["POST"])
def api_revote(group_number):
    db = get_db()
    result_data = get_results(group_number)
    tied_ids = result_data["tied_option_ids"]
    if len(tied_ids) < 2:
        return jsonify({"error": "no tie"}), 400

    placeholders = ",".join("?" for _ in tied_ids)
    db.execute(
        f"DELETE FROM options WHERE group_number = ? AND id NOT IN ({placeholders})",
        (group_number, *tied_ids)
    )
    db.execute("DELETE FROM votes WHERE group_number = ?", (group_number,))
    db.execute("UPDATE participants SET has_voted = 0 WHERE group_number = ?", (group_number,))
    db.execute(
        "UPDATE groups SET status = 'voting', votes_per_person = 3, voted_count = 0 WHERE group_number = ?",
        (group_number,)
    )
    db.commit()
    return jsonify({"ok": True})


@app.route("/api/room/<group_number>/lottery", methods=["POST"])
def api_lottery_close(group_number):
    db = get_db()
    result_data = get_results(group_number)
    candidates = [r for r in result_data["results"] if r["id"] in result_data["tied_option_ids"]] \
        if result_data["is_tie"] else result_data["results"][:1]
    winner = random.choice(candidates) if candidates else None
    if winner:
        db.execute(
            "UPDATE groups SET status = 'closed', final_decision = ?, final_votes = ?, closed_at = ? WHERE group_number = ?",
            (winner["name"], winner["votes"], datetime.utcnow().isoformat(), group_number)
        )
        db.commit()
    return jsonify({"ok": True})


@app.route("/api/room/<group_number>/final_close", methods=["POST"])
def api_final_close(group_number):
    db = get_db()
    result_data = get_results(group_number)
    winner = result_data["results"][0] if result_data["results"] else None
    if winner:
        db.execute(
            "UPDATE groups SET status = 'closed', final_decision = ?, final_votes = ?, closed_at = ? WHERE group_number = ?",
            (winner["name"], winner["votes"], datetime.utcnow().isoformat(), group_number)
        )
        db.commit()
    return jsonify({"ok": True})


@app.route("/api/room/<group_number>/delete", methods=["POST"])
def api_delete_group(group_number):
    _delete_group_completely(group_number)
    return jsonify({"ok": True})


if os.environ.get("WERKZEUG_RUN_MAIN") == "true":
    threading.Thread(target=background_cleanup_loop, daemon=True).start()

if __name__ == "__main__":
    app.run(debug=True)