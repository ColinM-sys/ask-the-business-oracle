"""Local Flask UI: python app.py, then http://127.0.0.1:5055."""
import os
import traceback

from flask import Flask, render_template, request

from agent import answer_question

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 16 * 1024


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
        except Exception as exc:
            # Log locations and types only: messages, source lines and locals may contain secrets.
            chain, seen = [], set()
            while exc is not None and id(exc) not in seen:
                seen.add(id(exc))
                frames = [(os.path.basename(frame.f_code.co_filename), frame.f_code.co_name, line)
                          for frame, line in traceback.walk_tb(exc.__traceback__)]
                chain.append((type(exc).__name__, frames))
                exc = exc.__cause__ if exc.__cause__ is not None else (
                    None if exc.__suppress_context__ else exc.__context__)
            app.logger.error("Analysis failed (exception types and traceback frames): %r", chain)
            error, status = "Analysis unavailable. Check the local Oracle connection and policy setup, then retry.", 503
    return render_template("index.html", result=result, error=error, question=question), status


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5055, debug=False)
