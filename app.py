"""Local Flask UI: python app.py, then http://127.0.0.1:5055."""
import os
from hmac import compare_digest

from flask import Flask, render_template, request

from agent import answer_question

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 16 * 1024
app.config["TRUSTED_HOSTS"] = ["127.0.0.1", "localhost"]


@app.before_request
def require_authentication():
    secret = os.environ.get("APP_AUTH_SECRET")
    if not secret:
        return "Set APP_AUTH_SECRET before starting the server.", 503
    auth = request.authorization
    if (auth is None or auth.type != "basic" or auth.username != "atb"
            or auth.password is None
            or not compare_digest(auth.password.encode("utf-8"), secret.encode("utf-8"))):
        return "Authentication required.", 401, {
            "WWW-Authenticate": 'Basic realm="Ask the Business", charset="UTF-8"',
        }


@app.errorhandler(413)
def submission_too_large(_):
    return render_template(
        "index.html", result=None, question="",
        error="Submission too large. Shorten your question and try again.",
    ), 413


@app.route("/", methods=["GET", "POST"])
def index():
    result, error = None, None
    question = request.form.get("question", "") if request.method == "POST" else "Why did West margin fall since July 2025?"
    status = 200
    if request.method == "POST":
        try:
            result = answer_question(question)
        except ValueError as exc:
            error, status = str(exc), 400
        except Exception:
            # Do not expose connection settings, driver errors or credentials.
            error, status = "Analysis unavailable. Check the local Oracle connection and policy setup, then retry.", 503
    return render_template("index.html", result=result, error=error, question=question), status


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5055, debug=False)
