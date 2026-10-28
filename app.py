"""Local Flask UI: python app.py, then http://127.0.0.1:5055."""
from flask import Flask, render_template, request

from agent import QuestionValidationError, answer_question

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 16 * 1024


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
        except QuestionValidationError as exc:
            error, status = str(exc), 400
        except Exception:
            # Do not expose connection settings, driver errors or credentials.
            error, status = "Analysis unavailable. Check the local Oracle connection and policy setup, then retry.", 503
    return render_template("index.html", result=result, error=error, question=question), status


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5055, debug=False)
