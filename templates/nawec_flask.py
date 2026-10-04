import base64, io, os, random, sqlite3
from datetime import datetime, timedelta

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import pandas as pd
import seaborn as sns
from flask import Flask, Response, flash, g, redirect, render_template, request, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "dev-only-change-me")
DB = os.environ.get("DB_PATH", "nawec.db")

REGIONS = ["Banjul", "Kanifing", "Brikama", "Kerewan", "Mansa Konko", "Janjanbureh", "Basse"]
TYPES = ["Residential", "Commercial", "Industrial"]
ERRORS = {"MR": "Missing reading", "SP": "Usage spike", "NG": "Negative usage",
          "DP": "Duplicate reading", "ST": "Stale reading", "CF": "Communication failure"}
BLUE, ORANGE, GREEN = "#2f6fb0", "#e08a2c", "#3f9b6a"
PAL = {"Residential": GREEN, "Commercial": BLUE, "Industrial": ORANGE}
sns.set_theme(style="whitegrid", font_scale=0.8,
              rc={"grid.color": "#e5ebf2", "axes.edgecolor": "#cbd5e1", "text.color": "#334155"})


def db():
    if "db" not in g:
        g.db = sqlite3.connect(DB)
        g.db.row_factory = sqlite3.Row
    return g.db


@app.teardown_appcontext
def close_db(_):
    conn = g.pop("db", None)
    if conn:
        conn.close()


def init_db():
    conn = sqlite3.connect(DB)
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS meters (
            id INTEGER PRIMARY KEY AUTOINCREMENT, customer_name TEXT,
            meter_number TEXT UNIQUE, location TEXT, meter_reading REAL,
            customer_type TEXT DEFAULT 'Residential');
        CREATE TABLE IF NOT EXISTS readings (
            id INTEGER PRIMARY KEY AUTOINCREMENT, meter_number TEXT,
            hour INTEGER, usage REAL, valid INTEGER);
        CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT, username TEXT UNIQUE, password_hash TEXT);
        CREATE TABLE IF NOT EXISTS exceptions (
            id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, meter_number TEXT,
            code TEXT, severity TEXT, resolved INTEGER DEFAULT 0);
    """)
    if conn.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0:
        conn.execute("INSERT INTO users (username, password_hash) VALUES (?, ?)",
                     (os.environ.get("ADMIN_USER", "admin"),
                      generate_password_hash(os.environ.get("ADMIN_PASSWORD", "admin123"))))
    if os.environ.get("SEED_DEMO", "1") == "1" and conn.execute("SELECT COUNT(*) FROM meters").fetchone()[0] == 0:
        random.seed(7)
        base = {"Residential": 1.2, "Commercial": 4, "Industrial": 11}
        nums = []
        for i in range(36):
            t, loc = random.choice(TYPES), random.choice(REGIONS)
            num = f"NW{100200 + i}"
            nums.append(num)
            conn.execute("INSERT INTO meters (customer_name, meter_number, location, meter_reading, customer_type) VALUES (?,?,?,?,?)",
                         (f"Customer {i + 1}", num, loc, round(random.uniform(500, 60000), 1), t))
            for h in range(24):
                peak = 1.8 if 18 <= h <= 22 else 0.6 if h < 6 else 1
                use = round(base[t] * peak * random.uniform(0.7, 1.3), 2)
                bad = random.random() < 0.04
                if bad:
                    use = round(use * random.uniform(4, 9), 2)
                conn.execute("INSERT INTO readings (meter_number, hour, usage, valid) VALUES (?,?,?,?)",
                             (num, h, use, 0 if bad else 1))
        now = datetime.now()
        for _ in range(70):
            conn.execute("INSERT INTO exceptions (ts, meter_number, code, severity, resolved) VALUES (?,?,?,?,?)",
                         ((now - timedelta(minutes=random.randint(5, 4000))).strftime("%Y-%m-%d %H:%M"),
                          random.choice(nums), random.choices(list(ERRORS), [3, 3, 1, 2, 2, 5])[0],
                          random.choices(["Critical", "Warning"], [4, 6])[0], int(random.random() < 0.15)))
    conn.commit()
    conn.close()


def png(fig):
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=120, bbox_inches="tight", transparent=True)
    plt.close(fig)
    return base64.b64encode(buf.getvalue()).decode()


def fig(w, h):
    f, ax = plt.subplots(figsize=(w, h))
    sns.despine(f)
    return f, ax


@app.route("/")
def dashboard():
    conn = db()
    sev, code = request.args.get("severity", ""), request.args.get("code", "")
    meters = pd.read_sql_query("SELECT * FROM meters", conn)
    reads = pd.read_sql_query("SELECT r.*, m.customer_type, m.meter_reading, m.location FROM readings r JOIN meters m USING (meter_number)", conn)
    exc = pd.read_sql_query("SELECT e.*, m.customer_type, m.location FROM exceptions e JOIN meters m USING (meter_number) ORDER BY ts DESC", conn)
    openx = exc[exc.resolved == 0]
    kpis = [("Total Meters", f"{len(meters):,}", ""),
            ("Readings Ingested", f"{len(reads):,}", "today"),
            ("Validation Pass", f"{reads.valid.mean() * 100:.2f}%" if len(reads) else "-", ""),
            ("Open Exceptions", f"{len(openx):,}", "pending"),
            ("Peak Ingestion", f"{reads.groupby('hour').size().max() if len(reads) else 0:,}/hr", ""),
            ("Meters Flagged", f"{openx.meter_number.nunique():,}", "")]
    charts = {}
    if len(reads):
        hourly = reads.groupby(["hour", "customer_type"], as_index=False).usage.sum()
        f, ax = fig(7, 2.8)
        sns.lineplot(data=hourly, x="hour", y="usage", hue="customer_type", palette=PAL, linewidth=2, ax=ax)
        ax.set(xlabel="Hour of day", ylabel="Usage (kWh)"); ax.legend(title=None, ncol=3, frameon=False)
        charts["ingestion"] = png(f)

        q = reads.groupby("hour").agg(n=("valid", "size"), ok=("valid", "mean")).reset_index()
        f, ax = fig(5, 2.8)
        ax.bar(q.hour, q.n, color=BLUE, alpha=.85); ax.set(xlabel="Hour of day", ylabel="Readings")
        ax2 = ax.twinx(); sns.lineplot(x=q.hour, y=q.ok * 100, color=ORANGE, linewidth=2, ax=ax2)
        ax2.set(ylabel="Pass rate (%)", ylim=(80, 101)); ax2.grid(False)
        charts["quality"] = png(f)

        f, ax = fig(3.2, 2.1)
        sns.scatterplot(data=reads, x="meter_reading", y="usage", s=14, alpha=.6, color=BLUE, ax=ax)
        ax.set(xlabel="Meter reading", ylabel="Usage"); charts["dist"] = png(f)

        f, ax = fig(3.2, 2.1)
        reads["status"] = reads.valid.map({1: "Valid", 0: "Outlier"})
        sns.scatterplot(data=reads, x="hour", y="usage", hue="status", s=14, alpha=.7,
                        palette={"Valid": BLUE, "Outlier": ORANGE}, ax=ax)
        ax.set(xlabel="Hour of day", ylabel="Usage"); ax.legend(title=None, frameon=False)
        charts["outliers"] = png(f)
    if len(exc):
        e = exc.assign(error=exc.code).groupby("error").size().reset_index(name="n")
        f, ax = fig(3.2, 2.1); sns.barplot(data=e, x="error", y="n", color=BLUE, ax=ax)
        ax.set(xlabel="", ylabel="Exceptions"); charts["errors"] = png(f)

        e = exc.groupby("customer_type").size().reset_index(name="n")
        f, ax = fig(3.2, 2.1)
        sns.barplot(data=e, x="customer_type", y="n", hue="customer_type", palette=PAL, legend=False, ax=ax)
        ax.set(xlabel="", ylabel="Exceptions"); charts["by_type"] = png(f)

        e = exc[exc.code == "CF"].groupby("location").size().reset_index(name="n").sort_values("n", ascending=False)
        f, ax = fig(3.2, 2.1); sns.barplot(data=e, x="location", y="n", color=BLUE, ax=ax)
        ax.set(xlabel="", ylabel="Gaps"); plt.setp(ax.get_xticklabels(), rotation=35, ha="right")
        charts["gaps"] = png(f)
    table = exc
    if sev: table = table[table.severity == sev]
    if code: table = table[table.code == code]
    return render_template("dashboard.html", kpis=kpis, charts=charts, errors=ERRORS,
                           rows=table.head(8).to_dict("records"), sev=sev, code=code,
                           active="dashboard", now=datetime.now().strftime("%b %d, %Y | %H:%M"))


@app.route("/meters")
def meters():
    q = request.args.get("q", "").strip()
    rows = db().execute("SELECT * FROM meters WHERE customer_name LIKE ? OR meter_number LIKE ? OR location LIKE ? ORDER BY id DESC",
                        (f"%{q}%",) * 3).fetchall()
    return render_template("meters.html", rows=rows, q=q, active="meters")


@app.route("/add", methods=["GET", "POST"])
def add_meter():
    if request.method == "POST":
        f = request.form
        name, num = f["customer_name"].strip(), f["meter_number"].strip()
        if not name or not num:
            flash("Enter the customer name and meter number.", "error")
        else:
            try:
                db().execute("INSERT INTO meters (customer_name, meter_number, location, meter_reading, customer_type) VALUES (?,?,?,?,?)",
                             (name, num, f["location"], float(f["meter_reading"] or 0), f["customer_type"]))
                db().commit()
                flash("Meter registered successfully.", "ok")
                return redirect(url_for("meters"))
            except sqlite3.IntegrityError:
                flash("This meter number already exists.", "error")
    return render_template("add.html", regions=REGIONS, types=TYPES, active="add")


@app.before_request
def require_login():
    if request.endpoint not in ("login", "static") and "user" not in session:
        return redirect(url_for("login", next=request.path))


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        row = db().execute("SELECT * FROM users WHERE username = ?", (request.form["username"].strip(),)).fetchone()
        if row and check_password_hash(row["password_hash"], request.form["password"]):
            session.clear()
            session["user"] = row["username"]
            nxt = request.args.get("next", "/")
            return redirect(nxt if nxt.startswith("/") and not nxt.startswith("//") else "/")
        flash("Wrong username or password.", "error")
    return render_template("login.html")


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/sample.csv")
def sample_csv():
    nums = [r[0] for r in db().execute("SELECT meter_number FROM meters LIMIT 3")] or ["NW100200"]
    lines = ["meter_number,hour,usage"] + [f"{n},{h},{round(random.uniform(0.5, 3), 2)}" for n in nums for h in (8, 9)]
    return Response("\n".join(lines) + "\n", mimetype="text/csv",
                    headers={"Content-Disposition": "attachment; filename=sample_readings.csv"})


def ingest(df):
    conn = db()
    known = {r[0] for r in conn.execute("SELECT meter_number FROM meters")}
    done = {(r[0], r[1]) for r in conn.execute("SELECT meter_number, hour FROM readings")}
    avg = {r[0]: r[1] for r in conn.execute("SELECT meter_number, AVG(usage) FROM readings WHERE valid = 1 GROUP BY meter_number")}
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    out = {"total": len(df), "added": 0, "flagged": 0, "skipped": []}
    for i, r in df.iterrows():
        line, num = i + 2, str(r["meter_number"]).strip()
        try:
            hour = int(r["hour"])
        except (TypeError, ValueError):
            hour = -1
        if num not in known:
            out["skipped"].append(f"Row {line}: unknown meter {num}"); continue
        if not 0 <= hour <= 23:
            out["skipped"].append(f"Row {line}: hour must be 0-23"); continue
        if (num, hour) in done:
            conn.execute("INSERT INTO exceptions (ts, meter_number, code, severity) VALUES (?,?,?,?)", (now, num, "DP", "Warning"))
            out["flagged"] += 1
            out["skipped"].append(f"Row {line}: {num} already has a reading for hour {hour}"); continue
        try:
            use = float(r["usage"])
        except (TypeError, ValueError):
            use = None
        code = None
        if use is None or pd.isna(use):
            code, use = "MR", 0.0
        elif use < 0:
            code = "NG"
        elif avg.get(num) and use > float(setting("spike_factor")) * avg[num]:
            code = "SP"
        conn.execute("INSERT INTO readings (meter_number, hour, usage, valid) VALUES (?,?,?,?)", (num, hour, use, 0 if code else 1))
        done.add((num, hour))
        out["added"] += 1
        if code:
            conn.execute("INSERT INTO exceptions (ts, meter_number, code, severity) VALUES (?,?,?,?)",
                         (now, num, code, "Critical" if code in ("NG", "SP") else "Warning"))
            out["flagged"] += 1
        else:
            conn.execute("UPDATE meters SET meter_reading = meter_reading + ? WHERE meter_number = ?", (use, num))
    conn.commit()
    return out


@app.route("/upload", methods=["GET", "POST"])
def upload():
    summary = None
    if request.method == "POST":
        file = request.files.get("file")
        if not file or not file.filename.lower().endswith(".csv"):
            flash("Choose a .csv file.", "error")
        else:
            try:
                df = pd.read_csv(file, dtype=str).rename(columns=lambda c: c.strip().lower())
            except Exception:
                df = None
            if df is None or not {"meter_number", "hour", "usage"} <= set(df.columns):
                flash("The CSV needs the columns meter_number, hour and usage.", "error")
            else:
                summary = ingest(df)
    return render_template("upload.html", summary=summary, active="upload")


START = datetime.now()
DEFAULTS = {"rate_Residential": "1.5", "rate_Commercial": "2.5", "rate_Industrial": "3.5", "spike_factor": "5"}


def setting(key):
    r = db().execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return r[0] if r else DEFAULTS.get(key, "0")


def billing_rows():
    df = pd.read_sql_query("""
        SELECT m.meter_number, m.customer_name, m.customer_type, m.location,
               COALESCE(SUM(r.usage), 0) AS kwh,
               (SELECT COUNT(*) FROM exceptions e WHERE e.meter_number = m.meter_number AND e.resolved = 0) AS open_ex
        FROM meters m LEFT JOIN readings r ON r.meter_number = m.meter_number AND r.valid = 1 GROUP BY m.id""", db())
    df["rate"] = df.customer_type.map(lambda t: float(setting(f"rate_{t}")))
    df["amount"] = (df.kwh * df.rate).round(2)
    df["status"] = df.open_ex.map(lambda n: "Hold" if n else "Ready")
    return df


def system_status():
    n = lambda q: db().execute(q).fetchone()[0]
    meters, reads, ok = n("SELECT COUNT(*) FROM meters"), n("SELECT COUNT(*) FROM readings"), n("SELECT COUNT(*) FROM readings WHERE valid = 1")
    ex, openx = n("SELECT COUNT(*) FROM exceptions"), n("SELECT COUNT(*) FROM exceptions WHERE resolved = 0")
    return {"meters": meters, "readings": reads, "open": openx,
            "pass": round(ok / reads * 100, 1) if reads else 0,
            "coverage": round(min(reads / (meters * 24), 1) * 100, 1) if meters else 0,
            "resolved": round((ex - openx) / ex * 100, 1) if ex else 100,
            "db_kb": round(os.path.getsize(DB) / 1024),
            "uptime": str(datetime.now() - START).split(".")[0], "time": datetime.now().strftime("%H:%M:%S")}


@app.route("/monitoring")
def monitoring():
    return render_template("monitoring.html", s=system_status(), active="monitoring")


@app.route("/api/status")
def api_status():
    return system_status()


@app.route("/billing")
def billing():
    df = billing_rows()
    ready, hold = df[df.status == "Ready"], df[df.status == "Hold"]
    return render_template("billing.html", rows=df.to_dict("records"), ready_amt=ready.amount.sum(),
                           hold_amt=hold.amount.sum(), n_ready=len(ready), n_hold=len(hold), active="billing")


@app.route("/reports")
def reports():
    df = pd.read_sql_query("SELECT m.location, m.customer_type, SUM(r.usage) AS kwh FROM readings r JOIN meters m USING (meter_number) WHERE r.valid = 1 GROUP BY 1, 2", db())
    chart = None
    if len(df):
        f, ax = fig(7, 3)
        sns.barplot(data=df, x="location", y="kwh", hue="customer_type", palette=PAL, ax=ax)
        ax.set(xlabel="", ylabel="Consumption (kWh)"); ax.legend(title=None, frameon=False)
        plt.setp(ax.get_xticklabels(), rotation=20, ha="right")
        chart = png(f)
    return render_template("reports.html", chart=chart, active="reports")


@app.route("/download/<name>.csv")
def download(name):
    q = {"readings": "SELECT * FROM readings", "exceptions": "SELECT * FROM exceptions", "meters": "SELECT * FROM meters"}
    df = billing_rows() if name == "billing" else pd.read_sql_query(q[name], db()) if name in q else None
    if df is None:
        return redirect(url_for("reports"))
    return Response(df.to_csv(index=False), mimetype="text/csv",
                    headers={"Content-Disposition": f"attachment; filename={name}.csv"})


@app.route("/settings", methods=["GET", "POST"])
def settings():
    if request.method == "POST":
        f = request.form
        if f["action"] == "tariffs":
            try:
                vals = {k: float(f[k]) for k in DEFAULTS}
                if any(v < 0 for v in vals.values()) or vals["spike_factor"] < 1:
                    raise ValueError
                for k, v in vals.items():
                    db().execute("INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value", (k, str(v)))
                db().commit()
                flash("Settings saved.", "ok")
            except ValueError:
                flash("Enter valid numbers (the spike factor must be at least 1).", "error")
        else:
            row = db().execute("SELECT * FROM users WHERE username = ?", (session["user"],)).fetchone()
            if not check_password_hash(row["password_hash"], f["current"]):
                flash("Current password is wrong.", "error")
            elif len(f["new"]) < 8 or f["new"] != f["confirm"]:
                flash("The new password must match and be at least 8 characters.", "error")
            else:
                db().execute("UPDATE users SET password_hash = ? WHERE username = ?", (generate_password_hash(f["new"]), session["user"]))
                db().commit()
                flash("Password changed.", "ok")
        return redirect(url_for("settings"))
    return render_template("settings.html", v={k: setting(k) for k in DEFAULTS}, active="settings")


@app.route("/exceptions")
def exceptions():
    rows = db().execute("SELECT e.*, m.customer_name FROM exceptions e JOIN meters m USING (meter_number) ORDER BY resolved, ts DESC").fetchall()
    return render_template("exceptions.html", rows=rows, errors=ERRORS, active="exceptions")


@app.post("/exceptions/<int:eid>/resolve")
def resolve(eid):
    db().execute("UPDATE exceptions SET resolved = 1 WHERE id = ?", (eid,))
    db().commit()
    return redirect(url_for("exceptions"))


init_db()
if __name__ == "__main__":
    app.run(debug=os.environ.get("FLASK_DEBUG") == "1")
