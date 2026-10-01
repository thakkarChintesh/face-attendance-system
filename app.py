import os
import io
import csv
import random
import shutil
import sqlite3
import smtplib
import datetime
import functools
from email.message import EmailMessage

from flask import (Flask, render_template, request, redirect, url_for, session, flash, g,
                   jsonify, Response, send_file, abort)
from werkzeug.security import generate_password_hash as _gph, check_password_hash

from config import Config

app = Flask(__name__)
app.config.from_object(Config)

def generate_password_hash(pw):
    return _gph(pw, method="pbkdf2:sha256")  # works on every Python build


DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]

SCHEMA = """
CREATE TABLE IF NOT EXISTS departments(id INTEGER PRIMARY KEY, name TEXT UNIQUE NOT NULL, code TEXT);
CREATE TABLE IF NOT EXISTS classes(id INTEGER PRIMARY KEY, name TEXT NOT NULL, department_id INTEGER, semester INTEGER);
CREATE TABLE IF NOT EXISTS users(
  id INTEGER PRIMARY KEY, username TEXT UNIQUE NOT NULL, name TEXT NOT NULL,
  role TEXT NOT NULL CHECK(role IN ('admin','teacher','student')),
  email TEXT, phone TEXT, password_hash TEXT NOT NULL,
  department_id INTEGER, class_id INTEGER,
  face_enrolled INTEGER DEFAULT 0, photo_count INTEGER DEFAULT 0,
  created_at TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS subjects(id INTEGER PRIMARY KEY, name TEXT NOT NULL, code TEXT,
  class_id INTEGER, teacher_id INTEGER, days TEXT, start_time TEXT, end_time TEXT);
CREATE TABLE IF NOT EXISTS sessions(id INTEGER PRIMARY KEY, subject_id INTEGER, teacher_id INTEGER,
  date TEXT, start_time TEXT, end_time TEXT, status TEXT DEFAULT 'active');
CREATE TABLE IF NOT EXISTS attendance(id INTEGER PRIMARY KEY, session_id INTEGER, student_id INTEGER,
  time TEXT, method TEXT, note TEXT, UNIQUE(session_id, student_id));
CREATE TABLE IF NOT EXISTS unknown_logs(id INTEGER PRIMARY KEY, image_path TEXT, time TEXT, camera TEXT, kind TEXT);
CREATE TABLE IF NOT EXISTS activity(id INTEGER PRIMARY KEY, message TEXT, icon TEXT, time TEXT);
CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS face_requests(id INTEGER PRIMARY KEY, student_id INTEGER, reason TEXT,
  status TEXT DEFAULT 'pending', time TEXT);
CREATE TABLE IF NOT EXISTS notifications(id INTEGER PRIMARY KEY, user_id INTEGER, message TEXT, time TEXT);
"""


# ================================================================= database helpers
def raw_conn():
    con = sqlite3.connect(Config.DB_PATH)
    con.row_factory = sqlite3.Row
    return con


def db():
    if "db" not in g:
        g.db = raw_conn()
    return g.db


@app.teardown_appcontext
def close_db(_exc):
    con = g.pop("db", None)
    if con:
        con.close()


def q(sql, args=(), one=False):
    rows = db().execute(sql, args).fetchall()
    return (rows[0] if rows else None) if one else rows


def ex(sql, args=()):
    cur = db().execute(sql, args)
    db().commit()
    return cur.lastrowid


def now_str():
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def log(message, icon="activity"):
    ex("INSERT INTO activity(message, icon, time) VALUES(?,?,?)", (message, icon, now_str()))


SETTING_DEFAULTS = {
    "threshold": str(Config.THRESHOLD), "liveness": "1" if Config.LIVENESS else "0",
    "camera_index": str(Config.CAMERA_INDEX), "min_attendance": str(Config.MIN_ATTENDANCE),
    "smtp_host": "", "smtp_port": "587", "smtp_user": "", "smtp_password": "", "smtp_from": "",
}


def settings():
    s = dict(SETTING_DEFAULTS)
    s.update({r["key"]: r["value"] for r in q("SELECT * FROM settings")})
    return s


def min_att():
    return float(settings()["min_attendance"])


# ================================================================= auth
def current_user():
    uid = session.get("uid")
    return q("SELECT * FROM users WHERE id=?", (uid,), one=True) if uid else None


def role_required(*roles):
    def deco(fn):
        @functools.wraps(fn)
        def wrapper(*a, **kw):
            user = current_user()
            if not user:
                return redirect(url_for("login", next=request.path))
            if roles and user["role"] not in roles:
                abort(403)
            g.user = user
            return fn(*a, **kw)
        return wrapper
    return deco


@app.context_processor
def inject():
    user = current_user()
    unread = 0
    if user:
        unread = q("SELECT COUNT(*) c FROM notifications WHERE user_id=?", (user["id"],), one=True)["c"]
        if user["role"] == "admin":
            unread += q("SELECT COUNT(*) c FROM face_requests WHERE status='pending'", one=True)["c"]
    return {"user": user, "today": datetime.date.today(), "unread": unread, "min_att": Config.MIN_ATTENDANCE}


# ================================================================= stats helpers
def pct(p, t):
    return round(100.0 * p / t, 1) if t else 0.0


def class_students(class_id):
    return q("SELECT * FROM users WHERE role='student' AND class_id=? ORDER BY username", (class_id,))


def student_subject_stats(student):
    rows = q("""SELECT sub.id, sub.name, sub.code,
        (SELECT COUNT(*) FROM sessions s WHERE s.subject_id=sub.id AND s.status='completed') total,
        (SELECT COUNT(*) FROM attendance a JOIN sessions s ON s.id=a.session_id
           WHERE s.subject_id=sub.id AND s.status='completed' AND a.student_id=?) present
        FROM subjects sub WHERE sub.class_id=? ORDER BY sub.name""", (student["id"], student["class_id"]))
    return [dict(r, pct=pct(r["present"], r["total"])) for r in rows]


def student_overall(student):
    rows = student_subject_stats(student)
    p, t = sum(r["present"] for r in rows), sum(r["total"] for r in rows)
    return {"present": p, "total": t, "pct": pct(p, t)}


def subject_student_stats(subject_id):
    sub = q("SELECT * FROM subjects WHERE id=?", (subject_id,), one=True)
    total = q("SELECT COUNT(*) c FROM sessions WHERE subject_id=? AND status='completed'", (subject_id,), one=True)["c"]
    counts = {r["student_id"]: r["c"] for r in q("""SELECT a.student_id, COUNT(*) c FROM attendance a
        JOIN sessions s ON s.id=a.session_id WHERE s.subject_id=? AND s.status='completed'
        GROUP BY a.student_id""", (subject_id,))}
    out = []
    for st in class_students(sub["class_id"]):
        p = counts.get(st["id"], 0)
        out.append({"id": st["id"], "username": st["username"], "name": st["name"], "email": st["email"],
                    "present": p, "total": total, "pct": pct(p, total)})
    return out


def trend(days, teacher_id=None):
    out = []
    today = datetime.date.today()
    for i in range(days - 1, -1, -1):
        d = (today - datetime.timedelta(days=i)).isoformat()
        sql = """SELECT s.id, sub.class_id FROM sessions s JOIN subjects sub ON sub.id=s.subject_id
                 WHERE s.date=? AND s.status='completed'"""
        args = [d]
        if teacher_id:
            sql += " AND s.teacher_id=?"
            args.append(teacher_id)
        sess = q(sql, args)
        possible = sum(len(class_students(s["class_id"])) for s in sess)
        present = sum(q("SELECT COUNT(*) c FROM attendance WHERE session_id=?", (s["id"],), one=True)["c"] for s in sess)
        out.append({"date": d[5:], "pct": pct(present, possible), "sessions": len(sess)})
    return out


def today_summary():
    d = datetime.date.today().isoformat()
    class_ids = {r["class_id"] for r in q("""SELECT DISTINCT sub.class_id FROM sessions s
        JOIN subjects sub ON sub.id=s.subject_id WHERE s.date=?""", (d,))}
    expected = {st["id"] for c in class_ids for st in class_students(c)}
    present = {r["student_id"] for r in q("""SELECT DISTINCT a.student_id FROM attendance a
        JOIN sessions s ON s.id=a.session_id WHERE s.date=?""", (d,))}
    return {"present": len(present), "absent": len(expected - present), "pct": pct(len(present), len(expected))}


def roster(session_id):
    s = q("""SELECT s.*, sub.name subject, sub.class_id, c.name class_name, u.name teacher
        FROM sessions s JOIN subjects sub ON sub.id=s.subject_id JOIN classes c ON c.id=sub.class_id
        JOIN users u ON u.id=s.teacher_id WHERE s.id=?""", (session_id,), one=True)
    if not s:
        abort(404)
    marks = {r["student_id"]: r for r in q("SELECT * FROM attendance WHERE session_id=?", (session_id,))}
    students = []
    for st in class_students(s["class_id"]):
        m = marks.get(st["id"])
        students.append({"id": st["id"], "username": st["username"], "name": st["name"],
                         "enrolled": st["face_enrolled"], "present": bool(m),
                         "time": m["time"][11:16] if m else "", "method": m["method"] if m else "",
                         "note": m["note"] if m else ""})
    return s, students


def notify(user_row, message, subject="Attendance alert"):
    ex("INSERT INTO notifications(user_id, message, time) VALUES(?,?,?)", (user_row["id"], message, now_str()))
    s = settings()
    if s["smtp_host"] and user_row["email"]:
        try:
            msg = EmailMessage()
            msg["Subject"], msg["From"], msg["To"] = subject, s["smtp_from"] or s["smtp_user"], user_row["email"]
            msg.set_content(message)
            with smtplib.SMTP(s["smtp_host"], int(s["smtp_port"]), timeout=10) as smtp:
                smtp.starttls()
                if s["smtp_user"]:
                    smtp.login(s["smtp_user"], s["smtp_password"])
                smtp.send_message(msg)
        except Exception as e:  # keep the in-app notification even if email fails
            log(f"Email to {user_row['email']} failed: {e}", "alert-triangle")


# ================================================================= auth routes
@app.route("/")
def index():
    user = current_user()
    if not user:
        return redirect(url_for("login"))
    return redirect(url_for({"admin": "admin_dashboard", "teacher": "teacher_dashboard"}.get(user["role"], "student_dashboard")))


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        user = q("SELECT * FROM users WHERE username=?", (request.form["username"].strip(),), one=True)
        if user and check_password_hash(user["password_hash"], request.form["password"]):
            session.clear()
            session["uid"] = user["id"]
            nxt = request.args.get("next")
            return redirect(nxt if nxt and nxt.startswith("/") else url_for("index"))
        flash("Invalid username or password", "error")
    return render_template("login.html")


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


# ================================================================= ADMIN
@app.route("/admin")
@role_required("admin")
def admin_dashboard():
    counts = {
        "students": q("SELECT COUNT(*) c FROM users WHERE role='student'", one=True)["c"],
        "teachers": q("SELECT COUNT(*) c FROM users WHERE role='teacher'", one=True)["c"],
        "departments": q("SELECT COUNT(*) c FROM departments", one=True)["c"],
        "classes": q("SELECT COUNT(*) c FROM classes", one=True)["c"],
        "subjects": q("SELECT COUNT(*) c FROM subjects", one=True)["c"],
        "enrolled": q("SELECT COUNT(*) c FROM users WHERE role='student' AND face_enrolled=1", one=True)["c"],
    }
    return render_template("admin/dashboard.html", counts=counts,
                           summary=today_summary(),
                           activity=q("SELECT * FROM activity ORDER BY id DESC LIMIT 8"),
                           alerts=q("SELECT COUNT(*) c FROM unknown_logs WHERE date(time)=date('now','localtime')", one=True)["c"])


@app.route("/api/trend")
@role_required("admin", "teacher")
def api_trend():
    days = min(int(request.args.get("days", 7)), 90)
    tid = g.user["id"] if g.user["role"] == "teacher" else None
    return jsonify(trend(days, tid))


# ---------------------------------------------------------------- users
@app.route("/admin/users", methods=["GET", "POST"])
@role_required("admin")
def admin_users():
    if request.method == "POST":
        f = request.form
        try:
            ex("""INSERT INTO users(username,name,role,email,phone,password_hash,department_id,class_id)
                  VALUES(?,?,?,?,?,?,?,?)""",
               (f["username"].strip(), f["name"].strip(), f["role"], f.get("email"), f.get("phone"),
                generate_password_hash(f["password"]), f.get("department_id") or None,
                f.get("class_id") if f["role"] == "student" else None))
            log(f"New {f['role']} registered: {f['name']}", "user-plus")
            flash(f"{f['name']} added", "success")
        except sqlite3.IntegrityError:
            flash("That username / ID already exists", "error")
        return redirect(url_for("admin_users", role=f["role"]))

    role = request.args.get("role", "student")
    flt = request.args.get("filter")
    sql = """SELECT u.*, d.name dept, c.name class_name FROM users u
             LEFT JOIN departments d ON d.id=u.department_id LEFT JOIN classes c ON c.id=u.class_id
             WHERE u.role=?"""
    if flt == "not_enrolled":
        sql += " AND u.face_enrolled=0"
    users = q(sql + " ORDER BY u.username", (role,))
    return render_template("admin/users.html", users=users, role=role,
                           departments=q("SELECT * FROM departments ORDER BY name"),
                           classes=q("SELECT * FROM classes ORDER BY name"))


@app.route("/admin/users/<int:uid>/edit", methods=["POST"])
@role_required("admin")
def admin_user_edit(uid):
    f = request.form
    ex("UPDATE users SET name=?, email=?, phone=?, department_id=?, class_id=? WHERE id=?",
       (f["name"], f.get("email"), f.get("phone"), f.get("department_id") or None, f.get("class_id") or None, uid))
    if f.get("password"):
        ex("UPDATE users SET password_hash=? WHERE id=?", (generate_password_hash(f["password"]), uid))
    flash("User updated", "success")
    return redirect(request.referrer or url_for("admin_users"))


@app.route("/admin/users/<int:uid>/delete", methods=["POST"])
@role_required("admin")
def admin_user_delete(uid):
    u = q("SELECT * FROM users WHERE id=?", (uid,), one=True)
    if u and u["id"] != g.user["id"]:
        ex("DELETE FROM users WHERE id=?", (uid,))
        ex("DELETE FROM attendance WHERE student_id=?", (uid,))
        if u["role"] == "student":
            shutil.rmtree(os.path.join(Config.DATASET_DIR, u["username"]), ignore_errors=True)
            try:
                from modules import face_engine
                face_engine.remove_person(uid)
            except ImportError:
                pass
        log(f"Deleted {u['role']} {u['name']}", "trash-2")
        flash(f"{u['name']} deleted", "success")
    return redirect(request.referrer or url_for("admin_users"))


@app.route("/admin/enroll/<int:uid>", methods=["GET", "POST"])
@role_required("admin")
def admin_enroll(uid):
    st = q("SELECT u.*, c.name class_name FROM users u LEFT JOIN classes c ON c.id=u.class_id WHERE u.id=?",
           (uid,), one=True)
    if not st or st["role"] != "student":
        abort(404)
    folder = os.path.join(Config.DATASET_DIR, st["username"])
    if request.method == "POST":
        try:
            from modules import face_engine
        except ImportError as e:
            flash(f"Recognition libraries not installed: {e}", "error")
            return redirect(url_for("admin_enroll", uid=uid))
        action = request.form.get("action")
        os.makedirs(folder, exist_ok=True)
        if action == "reset":
            shutil.rmtree(folder, ignore_errors=True)
            os.makedirs(folder, exist_ok=True)
        elif action in ("upload", "camera"):  # camera = photos captured in the browser popup
            for i, file in enumerate(request.files.getlist("photos")):
                if file and file.filename.lower().endswith((".jpg", ".jpeg", ".png")):
                    file.save(os.path.join(folder, f"up_{datetime.datetime.now():%H%M%S}_{i}{os.path.splitext(file.filename)[1].lower()}"))
        usable = face_engine.enroll_from_folder(uid, folder)
        ex("UPDATE users SET face_enrolled=?, photo_count=? WHERE id=?", (1 if usable >= 3 else 0, usable, uid))
        ex("UPDATE face_requests SET status='done' WHERE student_id=? AND status='approved'", (uid,))
        log(f"Face data updated for {st['name']} ({usable} usable photos)", "scan-face")
        if usable < 3:
            flash("Need at least 3 clear single-face photos to enroll", "error")
        else:
            flash(f"{st['name']} enrolled with {usable} encodings", "success")
        return redirect(url_for("admin_enroll", uid=uid))
    photos = sorted(os.listdir(folder)) if os.path.isdir(folder) else []
    return render_template("admin/enroll.html", st=st, photos=photos, target=Config.PHOTOS_PER_STUDENT)


@app.route("/dataset/<username>/<path:filename>")
@role_required("admin", "student")
def dataset_file(username, filename):
    if g.user["role"] == "student" and g.user["username"] != username:
        abort(403)
    path = os.path.join(Config.DATASET_DIR, username, os.path.basename(filename))
    if not os.path.exists(path):
        abort(404)
    return send_file(path)


# ---------------------------------------------------------------- academics
@app.route("/admin/academics", methods=["GET", "POST"])
@role_required("admin")
def admin_academics():
    if request.method == "POST":
        f = request.form
        kind = f["kind"]
        try:
            if kind == "department":
                ex("INSERT INTO departments(name, code) VALUES(?,?)", (f["name"], f.get("code")))
            elif kind == "class":
                ex("INSERT INTO classes(name, department_id, semester) VALUES(?,?,?)",
                   (f["name"], f["department_id"], f.get("semester") or None))
            elif kind == "subject":
                ex("""INSERT INTO subjects(name, code, class_id, teacher_id, days, start_time, end_time)
                      VALUES(?,?,?,?,?,?,?)""",
                   (f["name"], f.get("code"), f["class_id"], f.get("teacher_id") or None,
                    ",".join(f.getlist("days")), f.get("start_time"), f.get("end_time")))
            elif kind == "assign":
                ex("UPDATE subjects SET teacher_id=? WHERE id=?", (f["teacher_id"] or None, f["subject_id"]))
            log(f"{kind.title()} saved: {f.get('name', '')}", "layers")
            flash(f"{kind.title()} saved", "success")
        except sqlite3.IntegrityError:
            flash("Already exists", "error")
        return redirect(url_for("admin_academics"))
    return render_template("admin/academics.html",
        departments=q("""SELECT d.*, (SELECT COUNT(*) FROM classes c WHERE c.department_id=d.id) n_classes,
            (SELECT COUNT(*) FROM users u WHERE u.department_id=d.id AND u.role='student') n_students
            FROM departments d ORDER BY name"""),
        classes=q("""SELECT c.*, d.name dept, (SELECT COUNT(*) FROM users u WHERE u.class_id=c.id) n_students
            FROM classes c LEFT JOIN departments d ON d.id=c.department_id ORDER BY c.name"""),
        subjects=q("""SELECT s.*, c.name class_name, u.name teacher FROM subjects s
            LEFT JOIN classes c ON c.id=s.class_id LEFT JOIN users u ON u.id=s.teacher_id ORDER BY c.name, s.name"""),
        teachers=q("SELECT * FROM users WHERE role='teacher' ORDER BY name"), days=DAYS)


@app.route("/admin/academics/<kind>/<int:item_id>/delete", methods=["POST"])
@role_required("admin")
def admin_academics_delete(kind, item_id):
    table = {"department": "departments", "class": "classes", "subject": "subjects"}.get(kind)
    if not table:
        abort(404)
    ex(f"DELETE FROM {table} WHERE id=?", (item_id,))
    flash(f"{kind.title()} deleted", "success")
    return redirect(url_for("admin_academics"))


# ---------------------------------------------------------------- attendance records
def session_filter_query(args, teacher_id=None):
    sql = """SELECT s.*, sub.name subject, c.name class_name, u.name teacher, sub.class_id,
        (SELECT COUNT(*) FROM attendance a WHERE a.session_id=s.id) present,
        (SELECT COUNT(*) FROM users st WHERE st.class_id=sub.class_id AND st.role='student') strength
        FROM sessions s JOIN subjects sub ON sub.id=s.subject_id JOIN classes c ON c.id=sub.class_id
        JOIN users u ON u.id=s.teacher_id WHERE 1=1"""
    params = []
    for key, col in (("class_id", "sub.class_id"), ("subject_id", "s.subject_id")):
        if args.get(key):
            sql += f" AND {col}=?"
            params.append(args[key])
    if args.get("date_from"):
        sql += " AND s.date>=?"
        params.append(args["date_from"])
    if args.get("date_to"):
        sql += " AND s.date<=?"
        params.append(args["date_to"])
    if teacher_id:
        sql += " AND s.teacher_id=?"
        params.append(teacher_id)
    return q(sql + " ORDER BY s.date DESC, s.start_time DESC LIMIT 300", params)


@app.route("/admin/attendance")
@role_required("admin")
def admin_attendance():
    return render_template("admin/attendance.html", sessions=session_filter_query(request.args),
                           classes=q("SELECT * FROM classes ORDER BY name"),
                           subjects=q("SELECT * FROM subjects ORDER BY name"), args=request.args)


@app.route("/session/<int:sid>")
@role_required("admin", "teacher")
def session_detail(sid):
    s, students = roster(sid)
    if g.user["role"] == "teacher" and s["teacher_id"] != g.user["id"]:
        abort(403)
    return render_template("session_detail.html", s=s, students=students)


@app.route("/session/<int:sid>/mark", methods=["POST"])
@role_required("admin", "teacher")
def session_mark(sid):
    s, _ = roster(sid)
    if g.user["role"] == "teacher" and s["teacher_id"] != g.user["id"]:
        abort(403)
    student_id = int(request.form["student_id"])
    status = request.form["status"]
    reason = request.form.get("reason", "").strip() or "Manual correction"
    st = q("SELECT name FROM users WHERE id=?", (student_id,), one=True)
    if status == "present":
        ex("""INSERT INTO attendance(session_id, student_id, time, method, note) VALUES(?,?,?,?,?)
              ON CONFLICT(session_id, student_id) DO UPDATE SET note=excluded.note""",
           (sid, student_id, now_str(), "manual", f"{reason} (by {g.user['name']})"))
    else:
        ex("DELETE FROM attendance WHERE session_id=? AND student_id=?", (sid, student_id))
    log(f"{g.user['name']} marked {st['name']} {status} in {s['subject']} ({s['date']}): {reason}", "edit-3")
    if request.headers.get("X-Requested-With") == "fetch":
        return jsonify(ok=True)
    flash(f"{st['name']} marked {status}", "success")
    return redirect(request.referrer or url_for("session_detail", sid=sid))


# ---------------------------------------------------------------- export
@app.route("/export")
@role_required("admin", "teacher")
def export():
    teacher_id = g.user["id"] if g.user["role"] == "teacher" else None
    rows = []
    for s in session_filter_query(request.args, teacher_id):
        _, students = roster(s["id"])
        for st in students:
            rows.append({"Date": s["date"], "Start": s["start_time"], "Class": s["class_name"],
                         "Subject": s["subject"], "Teacher": s["teacher"], "Student ID": st["username"],
                         "Name": st["name"], "Status": "Present" if st["present"] else "Absent",
                         "Time": st["time"], "Method": st["method"], "Note": st["note"]})
    fmt = request.args.get("fmt", "xlsx")
    stamp = datetime.date.today().isoformat()
    if fmt == "csv":
        buf = io.StringIO()
        w = csv.DictWriter(buf, fieldnames=list(rows[0].keys()) if rows else ["Date"])
        w.writeheader()
        w.writerows(rows)
        return Response(buf.getvalue(), mimetype="text/csv",
                        headers={"Content-Disposition": f"attachment; filename=attendance_{stamp}.csv"})
    import pandas as pd
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as xw:
        df = pd.DataFrame(rows)
        df.to_excel(xw, index=False, sheet_name="Attendance")
        if not df.empty:
            summ = df.assign(P=df.Status.eq("Present")).groupby(["Student ID", "Name", "Subject"]) \
                     .agg(Attended=("P", "sum"), Total=("P", "size")).reset_index()
            summ["Percentage"] = (100 * summ.Attended / summ.Total).round(1)
            summ.to_excel(xw, index=False, sheet_name="Summary")
    buf.seek(0)
    return send_file(buf, as_attachment=True, download_name=f"attendance_{stamp}.xlsx",
                     mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


# ---------------------------------------------------------------- reports
@app.route("/admin/reports")
@role_required("admin")
def admin_reports():
    threshold = min_att()
    class_rows, students_all = [], []
    for c in q("SELECT c.*, d.name dept FROM classes c LEFT JOIN departments d ON d.id=c.department_id ORDER BY c.name"):
        studs = class_students(c["id"])
        p = t = 0
        for st in studs:
            o = student_overall(st)
            p, t = p + o["present"], t + o["total"]
            students_all.append({**dict(st), **o, "class_name": c["name"]})
        class_rows.append({"name": c["name"], "dept": c["dept"], "students": len(studs), "pct": pct(p, t)})
    dept = {}
    for r in class_rows:
        d = dept.setdefault(r["dept"] or "—", [0, 0])
        d[0] += r["pct"] * r["students"]
        d[1] += r["students"]
    dept_rows = [{"name": k, "pct": round(v[0] / v[1], 1) if v[1] else 0} for k, v in dept.items()]
    subject_rows = []
    for sub in q("SELECT s.*, c.name class_name FROM subjects s JOIN classes c ON c.id=s.class_id ORDER BY s.name"):
        stats = subject_student_stats(sub["id"])
        p, t = sum(x["present"] for x in stats), sum(x["total"] for x in stats)
        subject_rows.append({"name": sub["name"], "class_name": sub["class_name"], "pct": pct(p, t)})
    month = q("""SELECT substr(s.date,1,7) m, COUNT(DISTINCT s.id) sessions, COUNT(a.id) present
        FROM sessions s LEFT JOIN attendance a ON a.session_id=s.id WHERE s.status='completed'
        GROUP BY m ORDER BY m DESC LIMIT 6""")
    defaulters = sorted([s for s in students_all if s["total"] and s["pct"] < threshold], key=lambda x: x["pct"])
    return render_template("admin/reports.html", class_rows=class_rows, dept_rows=dept_rows,
                           subject_rows=subject_rows, defaulters=defaulters, threshold=threshold, month=month)


# ---------------------------------------------------------------- security
@app.route("/admin/security")
@role_required("admin")
def admin_security():
    kind = request.args.get("kind")
    sql = "SELECT * FROM unknown_logs"
    logs = q(sql + (" WHERE kind=?" if kind else "") + " ORDER BY id DESC LIMIT 200", (kind,) if kind else ())
    return render_template("admin/security.html", logs=logs, kind=kind,
        n_unknown=q("SELECT COUNT(*) c FROM unknown_logs WHERE kind='unknown'", one=True)["c"],
        n_spoof=q("SELECT COUNT(*) c FROM unknown_logs WHERE kind='spoof'", one=True)["c"])


@app.route("/admin/security/clear", methods=["POST"])
@role_required("admin")
def admin_security_clear():
    ex("DELETE FROM unknown_logs")
    flash("Security log cleared", "success")
    return redirect(url_for("admin_security"))


# ---------------------------------------------------------------- face update requests
@app.route("/admin/requests", methods=["GET", "POST"])
@role_required("admin")
def admin_requests():
    if request.method == "POST":
        rid, decision = request.form["id"], request.form["decision"]
        r = q("SELECT * FROM face_requests WHERE id=?", (rid,), one=True)
        ex("UPDATE face_requests SET status=? WHERE id=?", (decision, rid))
        st = q("SELECT * FROM users WHERE id=?", (r["student_id"],), one=True)
        notify(st, f"Your face data update request was {decision}." +
               (" Please visit the admin office for photo capture." if decision == "approved" else ""))
        if decision == "approved":
            return redirect(url_for("admin_enroll", uid=st["id"]))
        return redirect(url_for("admin_requests"))
    reqs = q("""SELECT r.*, u.name, u.username FROM face_requests r JOIN users u ON u.id=r.student_id
                ORDER BY r.status='pending' DESC, r.id DESC""")
    return render_template("admin/requests.html", reqs=reqs)


# ---------------------------------------------------------------- settings / backup
@app.route("/admin/settings", methods=["GET", "POST"])
@role_required("admin")
def admin_settings():
    if request.method == "POST":
        f = request.form
        for key in SETTING_DEFAULTS:
            if key == "liveness":
                val = "1" if f.get("liveness") else "0"
            elif key == "smtp_password" and not f.get(key):
                continue  # keep existing password when field left blank
            else:
                val = f.get(key, SETTING_DEFAULTS[key])
            ex("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, val))
        log("Settings updated", "settings")
        flash("Settings saved", "success")
        return redirect(url_for("admin_settings"))
    return render_template("admin/settings.html", s=settings())


@app.route("/admin/backup")
@role_required("admin")
def admin_backup():
    buf = io.BytesIO()
    src = raw_conn()
    tmp = sqlite3.connect(":memory:")
    src.backup(tmp)
    src.close()
    buf.write("\n".join(tmp.iterdump()).encode())
    buf.seek(0)
    return send_file(buf, as_attachment=True, download_name=f"backup_{datetime.date.today()}.sql", mimetype="application/sql")


@app.route("/admin/restore", methods=["POST"])
@role_required("admin")
def admin_restore():
    file = request.files.get("backup")
    if not file:
        flash("Choose a .sql backup file", "error")
        return redirect(url_for("admin_settings"))
    script = file.read().decode()
    con = raw_conn()
    for (name,) in con.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall():
        con.execute(f"DROP TABLE IF EXISTS {name}")
    con.executescript(script)
    con.commit()
    con.close()
    flash("Database restored from backup", "success")
    return redirect(url_for("logout"))


# ================================================================= TEACHER
def teacher_subjects(tid):
    return q("""SELECT s.*, c.name class_name,
        (SELECT COUNT(*) FROM users u WHERE u.class_id=s.class_id AND u.role='student') strength
        FROM subjects s JOIN classes c ON c.id=s.class_id WHERE s.teacher_id=? ORDER BY s.start_time""", (tid,))


@app.route("/teacher")
@role_required("teacher")
def teacher_dashboard():
    day = DAYS[datetime.date.today().weekday()]
    subs = teacher_subjects(g.user["id"])
    today_iso = datetime.date.today().isoformat()
    timetable = []
    for s in subs:
        if day in (s["days"] or "").split(","):
            sess = q("SELECT * FROM sessions WHERE subject_id=? AND date=? ORDER BY id DESC", (s["id"], today_iso), one=True)
            present = q("SELECT COUNT(*) c FROM attendance WHERE session_id=?", (sess["id"],), one=True)["c"] if sess else 0
            timetable.append({**dict(s), "session": sess, "present": present})
    recent = session_filter_query({}, g.user["id"])[:8]
    defaulters = []
    for s in subs:
        defaulters += [{**d, "subject": s["name"]} for d in subject_student_stats(s["id"])
                       if d["total"] and d["pct"] < min_att()]
    return render_template("teacher/dashboard.html", timetable=timetable, subjects=subs, recent=recent,
                           defaulters=sorted(defaulters, key=lambda d: d["pct"])[:6], day=day,
                           n_sessions=q("SELECT COUNT(*) c FROM sessions WHERE teacher_id=?", (g.user["id"],), one=True)["c"])


@app.route("/teacher/session/start", methods=["POST"])
@role_required("teacher")
def teacher_session_start():
    sub = q("SELECT * FROM subjects WHERE id=? AND teacher_id=?", (request.form["subject_id"], g.user["id"]), one=True)
    if not sub:
        abort(403)
    now = datetime.datetime.now()
    sid = ex("INSERT INTO sessions(subject_id, teacher_id, date, start_time, status) VALUES(?,?,?,?, 'active')",
             (sub["id"], g.user["id"], now.date().isoformat(), now.strftime("%H:%M")))
    log(f"{g.user['name']} started attendance for {sub['name']}", "play-circle")
    return redirect(url_for("teacher_live", sid=sid))


@app.route("/teacher/session/<int:sid>/live")
@role_required("teacher")
def teacher_live(sid):
    s, students = roster(sid)
    if s["teacher_id"] != g.user["id"]:
        abort(403)
    if s["status"] != "active":
        return redirect(url_for("session_detail", sid=sid))
    return render_template("teacher/live.html", s=s, students=students, cfg=settings())


@app.route("/api/session/<int:sid>/frame", methods=["POST"])
@role_required("teacher")
def api_session_frame(sid):
    """The browser posts one JPEG camera frame; we return face boxes + names and mark attendance."""
    s, students = roster(sid)
    if s["teacher_id"] != g.user["id"] or s["status"] != "active":
        abort(403)
    try:
        from modules import face_engine
    except ImportError as e:
        return jsonify(error=f"Recognition libraries not installed: {e}"), 500
    frame = face_engine.decode_jpeg(request.get_data())
    if frame is None:
        return jsonify(error="bad frame"), 400
    rec = face_engine.RECOGNIZERS.get(sid)
    if rec is None:
        cfg = settings()
        rec = face_engine.LiveRecognizer({st["id"]: st["name"] for st in students}, cfg["threshold"],
                                         cfg["liveness"] == "1", [st["id"] for st in students if st["present"]])
        face_engine.RECOGNIZERS[sid] = rec
    faces, new_marks, events = rec.process(frame)
    for uid in new_marks:
        ex("INSERT OR IGNORE INTO attendance(session_id, student_id, time, method) VALUES(?,?,?, 'face')",
           (sid, uid, now_str()))
    for kind, img in events:
        ex("INSERT INTO unknown_logs(image_path, time, camera, kind) VALUES(?,?,?,?)",
           (face_engine.save_snapshot(img, kind), now_str(), s["class_name"], kind))
    return jsonify(faces=faces, width=frame.shape[1], height=frame.shape[0], marked=len(new_marks))


@app.route("/api/session/<int:sid>/status")
@role_required("teacher", "admin")
def api_session_status(sid):
    s, students = roster(sid)
    present = sum(1 for st in students if st["present"])
    return jsonify(present=present, total=len(students), status=s["status"], students=students)


@app.route("/teacher/session/<int:sid>/stop", methods=["POST"])
@role_required("teacher")
def teacher_session_stop(sid):
    s, students = roster(sid)
    if s["teacher_id"] != g.user["id"]:
        abort(403)
    try:
        from modules import face_engine
        face_engine.RECOGNIZERS.pop(sid, None)
    except ImportError:
        pass
    ex("UPDATE sessions SET status='completed', end_time=? WHERE id=?", (datetime.datetime.now().strftime("%H:%M"), sid))
    present = sum(1 for st in students if st["present"])
    log(f"{s['subject']} session saved: {present}/{len(students)} present", "check-circle")
    flash(f"Session saved — {present} of {len(students)} present", "success")
    return redirect(url_for("session_detail", sid=sid))


@app.route("/teacher/reports")
@role_required("teacher")
def teacher_reports():
    subs = teacher_subjects(g.user["id"])
    sel = request.args.get("subject_id", type=int) or (subs[0]["id"] if subs else None)
    stats = subject_student_stats(sel) if sel else []
    return render_template("teacher/reports.html", subjects=subs, sel=sel, stats=stats, threshold=min_att(),
                           sessions=session_filter_query({"subject_id": sel}, g.user["id"]) if sel else [])


@app.route("/teacher/notify", methods=["POST"])
@role_required("teacher")
def teacher_notify():
    sub = q("SELECT * FROM subjects WHERE id=? AND teacher_id=?", (request.form["subject_id"], g.user["id"]), one=True)
    if not sub:
        abort(403)
    custom = request.form.get("message", "").strip()
    sent = 0
    for d in subject_student_stats(sub["id"]):
        if d["total"] and d["pct"] < min_att():
            st = q("SELECT * FROM users WHERE id=?", (d["id"],), one=True)
            notify(st, custom or f"Your attendance in {sub['name']} is {d['pct']}% ({d['present']}/{d['total']}). "
                                 f"Minimum required is {min_att():.0f}%.", f"Low attendance: {sub['name']}")
            sent += 1
    log(f"{g.user['name']} sent low-attendance alerts to {sent} students ({sub['name']})", "bell")
    flash(f"Alert sent to {sent} students", "success")
    return redirect(url_for("teacher_reports", subject_id=sub["id"]))


# ================================================================= STUDENT
@app.route("/student")
@role_required("student")
def student_dashboard():
    st = q("SELECT u.*, c.name class_name, d.name dept FROM users u LEFT JOIN classes c ON c.id=u.class_id "
           "LEFT JOIN departments d ON d.id=u.department_id WHERE u.id=?", (g.user["id"],), one=True)
    subjects = student_subject_stats(st)
    overall = student_overall(st)
    # history: every completed session of the student's class
    history = q("""SELECT s.date, s.start_time, sub.name subject, a.time, a.method FROM sessions s
        JOIN subjects sub ON sub.id=s.subject_id
        LEFT JOIN attendance a ON a.session_id=s.id AND a.student_id=?
        WHERE sub.class_id=? AND s.status='completed' ORDER BY s.date DESC, s.start_time DESC""", (st["id"], st["class_id"]))
    # calendar for the selected month
    month = request.args.get("month") or datetime.date.today().strftime("%Y-%m")
    y, m = map(int, month.split("-"))
    first = datetime.date(y, m, 1)
    nxt = (first + datetime.timedelta(days=32)).replace(day=1)
    by_day = {}
    for h in history:
        if h["date"].startswith(month):
            d = by_day.setdefault(int(h["date"][8:]), [0, 0])
            d[0] += 1 if h["time"] else 0
            d[1] += 1
    cal = [None] * first.weekday() + list(range(1, (nxt - first).days + 1))
    prev = (first - datetime.timedelta(days=1)).strftime("%Y-%m")
    photos = []
    folder = os.path.join(Config.DATASET_DIR, st["username"])
    if os.path.isdir(folder):
        photos = sorted(os.listdir(folder))[:1]
    return render_template("student/dashboard.html", st=st, subjects=subjects, overall=overall,
        history=history[:50], cal=cal, by_day=by_day, month_label=first.strftime("%B %Y"),
        prev=prev, next=nxt.strftime("%Y-%m"), photo=photos[0] if photos else None, threshold=min_att(),
        notes=q("SELECT * FROM notifications WHERE user_id=? ORDER BY id DESC LIMIT 10", (st["id"],)),
        pending=q("SELECT * FROM face_requests WHERE student_id=? AND status='pending'", (st["id"],), one=True))


@app.route("/student/request", methods=["POST"])
@role_required("student")
def student_request():
    ex("INSERT INTO face_requests(student_id, reason, time) VALUES(?,?,?)",
       (g.user["id"], request.form.get("reason", ""), now_str()))
    log(f"{g.user['name']} requested a face data update", "refresh-cw")
    flash("Request sent to admin", "success")
    return redirect(url_for("student_dashboard"))


# ================================================================= init + demo data
def seed_demo(con):
    ph = generate_password_hash
    c = con.cursor()
    c.execute("INSERT INTO users(username,name,role,email,password_hash) VALUES(?,?,?,?,?)",
              ("admin", "Administrator", "admin", "admin@college.test", ph("admin123")))
    if not Config.SEED_DEMO:
        return
    c.execute("INSERT INTO departments(name, code) VALUES('Computer Engineering','CE')")
    c.execute("INSERT INTO departments(name, code) VALUES('Information Technology','IT')")
    c.execute("INSERT INTO classes(name, department_id, semester) VALUES('CE Sem 5 - A', 1, 5)")
    c.execute("INSERT INTO classes(name, department_id, semester) VALUES('IT Sem 3 - A', 2, 3)")
    c.execute("INSERT INTO users(username,name,role,email,password_hash,department_id) VALUES(?,?,?,?,?,1)",
              ("teacher", "Prof. Anjali Mehta", "teacher", "mehta@college.test", ph("teacher123")))
    t1 = c.lastrowid
    c.execute("INSERT INTO users(username,name,role,email,password_hash,department_id) VALUES(?,?,?,?,?,2)",
              ("teacher2", "Prof. Rakesh Shah", "teacher", "shah@college.test", ph("teacher123")))
    t2 = c.lastrowid
    all_days = "Mon,Tue,Wed,Thu,Fri,Sat"
    subjects = [("Machine Learning", "CE501", 1, t1, all_days, "09:00", "10:00"),
                ("Database Systems", "CE502", 1, t1, "Mon,Wed,Fri", "10:15", "11:15"),
                ("Computer Networks", "CE503", 1, t2, "Tue,Thu,Sat", "11:30", "12:30"),
                ("Data Structures", "IT301", 2, t2, all_days, "09:00", "10:00")]
    c.executemany("INSERT INTO subjects(name,code,class_id,teacher_id,days,start_time,end_time) VALUES(?,?,?,?,?,?,?)", subjects)
    names = ["Aarav Patel", "Diya Shah", "Rohan Desai", "Isha Joshi", "Karan Mehta", "Priya Nair",
             "Vivaan Rao", "Sneha Iyer", "Arjun Gupta", "Meera Pillai"]
    rng = random.Random(7)
    studs = []
    for i, n in enumerate(names):
        cls = 1 if i < 7 else 2
        user = f"21CE{i+1:03d}" if cls == 1 else f"22IT{i-6:03d}"
        c.execute("INSERT INTO users(username,name,role,email,password_hash,department_id,class_id) VALUES(?,?,?,?,?,?,?)",
                  (user, n, "student", f"{user.lower()}@college.test", ph("student123"), cls, cls))
        studs.append((c.lastrowid, cls, rng.choice([0.95, 0.9, 0.85, 0.8, 0.65, 0.6])))
    today = datetime.date.today()
    for back in range(21, 0, -1):
        d = today - datetime.timedelta(days=back)
        day = DAYS[d.weekday()]
        for sid_, (sname, _, cls, tid, days, st, en) in enumerate(subjects, start=1):
            if day not in days.split(","):
                continue
            c.execute("INSERT INTO sessions(subject_id,teacher_id,date,start_time,end_time,status) VALUES(?,?,?,?,?,'completed')",
                      (sid_, tid, d.isoformat(), st, en))
            sess = c.lastrowid
            for uid, ucls, prob in studs:
                if ucls == cls and rng.random() < prob:
                    c.execute("INSERT INTO attendance(session_id,student_id,time,method) VALUES(?,?,?,?)",
                              (sess, uid, f"{d.isoformat()} {st}:{rng.randint(10,59)}", "face" if rng.random() > 0.08 else "manual"))
    for msg, icon in [("Demo data created", "database"), ("10 students registered", "user-plus"),
                      ("Timetable configured for CE Sem 5 - A", "calendar")]:
        c.execute("INSERT INTO activity(message, icon, time) VALUES(?,?,?)", (msg, icon, now_str()))


def init_db():
    for d in (Config.DATASET_DIR, os.path.dirname(Config.ENCODINGS_FILE), Config.UNKNOWN_DIR):
        os.makedirs(d, exist_ok=True)
    con = raw_conn()
    con.executescript(SCHEMA)
    if not con.execute("SELECT 1 FROM users LIMIT 1").fetchone():
        seed_demo(con)
    # sessions left 'active' by a crash/restart are closed on startup
    con.execute("UPDATE sessions SET status='completed', end_time=COALESCE(end_time, start_time) WHERE status='active' AND date<date('now','localtime')")
    con.commit()
    con.close()


init_db()

if __name__ == "__main__":
    app.run(debug=True, threaded=True, port=5001)
