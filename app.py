from flask import Flask, request, render_template
from werkzeug.middleware.proxy_fix import ProxyFix
import sqlite3
from datetime import datetime, timedelta
import json
import random
import os
import threading
from sendgrid import SendGridAPIClient
from sendgrid.helpers.mail import Mail

app = Flask(__name__)
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1)

DB_FILE = "feedback.db"


# ---------------- DATABASE INIT ----------------
def init_db():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()

    c.execute("""
        CREATE TABLE IF NOT EXISTS feedback (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            toilet_id TEXT,
            rating INTEGER,
            smell TEXT,
            supplies TEXT,
            first_name TEXT,
            last_name TEXT,
            contact TEXT,
            ip_address TEXT,
            timestamp TEXT
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS alerts (
            toilet_id TEXT PRIMARY KEY,
            last_alert_time TEXT
        )
    """)

    conn.commit()
    conn.close()


init_db()


# ---------------- CAPTCHA ----------------
def generate_captcha():
    a, b = random.randint(1, 9), random.randint(1, 9)
    return a, b, a + b


# ---------------- ALERT CHECK ----------------
def should_send_alert(toilet_id):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()

    # son 3 kayıt
    c.execute("""
        SELECT rating FROM feedback
        WHERE toilet_id = ?
        ORDER BY id DESC
        LIMIT 3
    """, (toilet_id,))
    rows = c.fetchall()

    if len(rows) < 3 or not all(r[0] <= 3 for r in rows):
        conn.close()
        return False

    # cooldown kontrol
    c.execute("SELECT last_alert_time FROM alerts WHERE toilet_id=?", (toilet_id,))
    result = c.fetchone()

    if result:
        last_time = datetime.strptime(result[0], "%Y-%m-%d %H:%M:%S")
        if datetime.now() - last_time < timedelta(minutes=10):
            conn.close()
            return False

    # alert kaydı güncelle
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    c.execute("""
        INSERT OR REPLACE INTO alerts (toilet_id, last_alert_time)
        VALUES (?, ?)
    """, (toilet_id, now_str))

    conn.commit()
    conn.close()
    return True


def reset_alert_if_needed(toilet_id, rating):
    if rating >= 4:
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute("DELETE FROM alerts WHERE toilet_id=?", (toilet_id,))
        conn.commit()
        conn.close()


# ---------------- MAIL ----------------
def send_alert(toilet_id):
    api_key = os.environ.get("SENDGRID_API_KEY")
    to_email = os.environ.get("ALERT_EMAIL_TO")
    from_email = os.environ.get("ALERT_EMAIL_FROM")

    if not api_key or not to_email or not from_email:
        print("SendGrid env eksik")
        return

    message = Mail(
        from_email=from_email,
        to_emails=to_email,
        subject="WC ACİL UYARI",
        html_content=f"<strong>WC {toilet_id} için art arda 3 düşük puan alındı!</strong>"
    )

    try:
        sg = SendGridAPIClient(api_key)
        sg.send(message)
        print("Alert mail gönderildi")
    except Exception as e:
        print("SendGrid hata:", e)


# ---------------- ROUTE ----------------
@app.route("/toilet/<toilet_id>", methods=["GET", "POST"])
def toilet(toilet_id):
    captcha_a, captcha_b, captcha_answer = generate_captcha()

    if request.method == "POST":
        try:
            rating = int(request.form["rating"])
            smell = request.form["smell"]
            supplies = request.form["supplies"]
            first_name = request.form["first_name"].strip()
            last_name = request.form["last_name"].strip()
            contact = request.form["contact"].strip()
            user_captcha = int(request.form["captcha"])
            expected_captcha = int(request.form["expected_captcha"])
            ip_address = request.headers.get("X-Forwarded-For", request.remote_addr)
            timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

            if not (first_name and last_name and contact):
                return "<h3>Lütfen tüm alanları doldurun!</h3>"

            if user_captcha != expected_captcha:
                return "<h3>CAPTCHA yanlış!</h3>"

            conn = sqlite3.connect(DB_FILE)
            c = conn.cursor()
            c.execute("""
                INSERT INTO feedback
                (toilet_id, rating, smell, supplies,
                 first_name, last_name, contact,
                 ip_address, timestamp)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                toilet_id, rating, smell, supplies,
                first_name, last_name, contact,
                ip_address, timestamp
            ))
            conn.commit()
            conn.close()

            reset_alert_if_needed(toilet_id, rating)

            if should_send_alert(toilet_id):
                threading.Thread(
                    target=send_alert,
                    args=(toilet_id,),
                    daemon=True
                ).start()

            return "<h3>Teşekkür ederiz!</h3>"

        except Exception as e:
            return f"<h3>Hata: {str(e)}</h3>"

    return render_template(
        "toilet.html",
        toilet_id=toilet_id,
        captcha_a=captcha_a,
        captcha_b=captcha_b,
        captcha_answer=captcha_answer
    )


# ---------------- ADMIN ----------------
@app.route("/admin")
def admin():
    if request.args.get("key") != os.environ.get("ADMIN_KEY"):
        return "Unauthorized", 401

    toilet_filter = request.args.get("toilet")

    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()

    # Filtreli veya filtresiz veri çek
    if toilet_filter:
        c.execute("""
            SELECT id,toilet_id,rating,smell,supplies,
                   first_name,last_name,contact,ip_address,timestamp
            FROM feedback
            WHERE toilet_id = ?
            ORDER BY id DESC
        """, (toilet_filter,))
    else:
        c.execute("""
            SELECT id,toilet_id,rating,smell,supplies,
                   first_name,last_name,contact,ip_address,timestamp
            FROM feedback
            ORDER BY id DESC
        """)

    rows = c.fetchall()

    # Ortalama hesapla
    ratings = [r[2] for r in rows]
    average = round(sum(ratings) / len(ratings), 2) if ratings else 0

    # ---------------- GRAFİK VERİSİ ----------------
    c.execute("""
        SELECT toilet_id,
               DATE(timestamp),
               AVG(rating)
        FROM feedback
        GROUP BY toilet_id, DATE(timestamp)
        ORDER BY DATE(timestamp)
    """)

    raw_chart = c.fetchall()

    chart_data = {}
    dates = set()

    for toilet_id, day, avg_rating in raw_chart:
        dates.add(day)
        if toilet_id not in chart_data:
            chart_data[toilet_id] = []
        chart_data[toilet_id].append({
            "day": day,
            "avg": round(avg_rating, 2)
        })

    dates = sorted(list(dates))

    conn.close()

    return render_template(
        "admin.html",
        rows=rows,
        average=average,
        avg=average,   # html'deki kart için
        chart_data=json.dumps(chart_data),
        dates=json.dumps(dates),
        selected_toilet=toilet_filter
    )


# ---------------- HOME ----------------
@app.route("/")
def home():
    return "Toilet Feedback System Running"


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)