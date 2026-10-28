"""Local Flask UI: python app.py, then http://127.0.0.1:5055."""
import warnings
from getpass import GetPassWarning, getpass
from hmac import compare_digest

from flask import Flask, abort, render_template, request

from agent import QuestionValidationError, answer_question

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 16 * 1024


@app.before_request
def protect_local_server():
    host = request.headers.get("Host", "").lower()
    if host not in {"localhost", "localhost:5055", "127.0.0.1", "127.0.0.1:5055"}:
        abort(400)
    password = app.config.get("LOCAL_AUTH_PASSWORD")
    if not password:
        abort(503)
    auth = request.authorization
    if (not auth or auth.type != "basic" or auth.username != "atb"
            or not isinstance(auth.password, str)
            or not compare_digest(auth.password.encode("utf-8"), password.encode("utf-8"))):
        return "Authentication required", 401, {"WWW-Authenticate": 'Basic realm="Ask the Business", charset="UTF-8"'}


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
    return render_template(
        "index.html", result=None, question="",
        error="Submission too large. Shorten your question and try again.", question_error=True,
    ), 413


@app.route("/", methods=["GET", "POST"])
def index():
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
    with warnings.catch_warnings():
        warnings.simplefilter("error", GetPassWarning)
        try:
            password = getpass("Set a local web password (at least 16 characters): ")
        except GetPassWarning:
            raise SystemExit("A terminal that can hide password input is required.") from None
    if len(password) < 16:
        raise SystemExit("The local web password must contain at least 16 characters.")
    app.config["LOCAL_AUTH_PASSWORD"] = password
    app.run(host="127.0.0.1", port=5055, debug=False)
