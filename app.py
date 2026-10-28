"""Local Flask UI: run python app.py to open a secure browser session."""
import re
import secrets
import threading
import webbrowser

from flask import Flask, abort, render_template, request, session
from werkzeug.serving import make_server

from agent import QuestionValidationError, answer_question

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 16 * 1024
app.config["BOOTSTRAP_TOKEN"] = secrets.token_urlsafe(32)
app.config["SESSION_COOKIE_NAME"] = "atb_session"
app.config["SESSION_COOKIE_SAMESITE"] = "Strict"
app.secret_key = secrets.token_bytes(32)


@app.before_request
def validate_host():
    host = re.fullmatch(r"(?:localhost|127\.0\.0\.1|\[::1\])(?::([0-9]{1,5}))?",
                        request.headers.get("Host", "") or "", re.IGNORECASE)
    if not host or (host.group(1) and not 1 <= int(host.group(1)) <= 65535):
        abort(400)


@app.after_request
def prevent_sensitive_caching(response):
    response.headers["Cache-Control"] = "no-store"
    response.headers["Referrer-Policy"] = "no-referrer"
    return response


@app.post("/session")
def start_session():
    supplied = request.form.get("token", "").encode("utf-8")
    if not secrets.compare_digest(supplied, app.config["BOOTSTRAP_TOKEN"].encode("ascii")):
        abort(403)
    session.clear()
    session["token"] = secrets.token_urlsafe(32)
    return "", 204


@app.template_filter()
def browser_evidence(evidence):
    """Keep integer cells exact across JSON/JavaScript without changing SQL evidence."""
    return {
        key: {**item, "rows": [
            {column: str(value) if isinstance(value, int) and not isinstance(value, bool) else value
             for column, value in row.items()}
            for row in item["rows"]
        ]}
        for key, item in evidence.items()
    }


@app.errorhandler(413)
def submission_too_large(_):
    if not session.get("token"):
        return "Submission too large.", 413
    return render_template(
        "index.html", result=None, question="",
        error="Submission too large. Shorten your question and try again.", question_error=True,
    ), 413


@app.route("/", methods=["GET", "POST"])
def index():
    token = session.get("token")
    if request.method == "GET" and not token:
        return render_template("bootstrap.html")
    if request.method == "POST":
        if not token or not secrets.compare_digest(
                token.encode("ascii"), request.form.get("session_token", "").encode("utf-8")):
            abort(403)
    result, error = None, None
    question = request.form.get("question", "") if request.method == "POST" else "Why did West margin fall since July 2025?"
    status = 200
    if request.method == "POST":
        try:
            result = answer_question(question)
        except QuestionValidationError as exc:
            error, status = str(exc), 400
        except Exception:
            # Do not expose connection settings, driver errors or credentials.
            error, status = "Analysis unavailable. Check the local Oracle connection and policy setup, then retry.", 503
    return render_template("index.html", result=result, error=error, question=question,
                           question_error=status == 400), status


if __name__ == "__main__":
    server = make_server("127.0.0.1", 5055, app, threaded=True)
    url = "http://127.0.0.1:5055/#" + app.config["BOOTSTRAP_TOKEN"]
    threading.Thread(target=webbrowser.open, args=(url,), daemon=True).start()
    server.serve_forever()
