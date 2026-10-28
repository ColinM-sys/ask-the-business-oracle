"""Local Flask UI: python app.py, then http://127.0.0.1:5055."""
import builtins
from decimal import Decimal
from pathlib import Path
import secrets

from flask import Flask, abort, render_template, request, session
import oracledb

from agent import (DataQualityError, DiscountDataQualityError, MissingCostsError,
                   MissingDiscountInputsError, QuestionValidationError,
                   RevenueDataQualityError, answer_question)

app = Flask(__name__)
app.secret_key = secrets.token_hex(32)
app.config["MAX_CONTENT_LENGTH"] = 16 * 1024
app.config["TRUSTED_HOSTS"] = ["localhost", "127.0.0.1"]
app.config["SESSION_COOKIE_SAMESITE"] = "Strict"


@app.template_global()
def csrf_token():
    if "csrf_token" not in session:
        session["csrf_token"] = secrets.token_urlsafe(32)
    return session["csrf_token"]


@app.template_filter()
def browser_evidence(evidence):
    """Keep integer and Decimal cells exact in JavaScript without changing SQL evidence."""
    return {
        key: {**item, "rows": [
            {column: str(value) if isinstance(value, (int, Decimal)) and not isinstance(value, bool) else value
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
    if request.method == "POST":
        if request.headers.get("Origin") != request.host_url.rstrip("/"):
            abort(403)
        expected = session.get("csrf_token")
        submitted = request.form.get("csrf_token", "")
        if not expected or not secrets.compare_digest(expected.encode(), submitted.encode()):
            abort(403)
    question = request.form.get("question", "") if request.method == "POST" else "Why did West margin fall since July 2025?"
    status = 200
    if request.method == "POST":
        try:
            result = answer_question(question)
        except QuestionValidationError as exc:
            error, status = str(exc), 400
        except MissingCostsError:
            error, status = (f"Analysis unavailable. {MissingCostsError.message} "
                             "Correct missing, incomplete, or duplicate monthly cost records, then retry."), 503
        except MissingDiscountInputsError:
            error, status = (f"Analysis unavailable. {MissingDiscountInputsError.message} "
                             "Correct missing discount, quantity, or unit price inputs, then retry."), 503
        except DiscountDataQualityError:
            error, status = "Analysis unavailable. Correct missing discount, quantity, or unit price inputs, then retry.", 503
        except RevenueDataQualityError:
            error, status = "Analysis unavailable. Correct missing net revenue for sales, then retry.", 503
        except DataQualityError:
            error, status = "Analysis unavailable. Correct missing, incomplete, or duplicate monthly cost records, then retry.", 503
        except Exception as exc:
            # Do not expose connection settings, driver errors or credentials.
            error_type = type(exc)
            name = error_type.__name__
            if (getattr(builtins, name, None) is not error_type
                    and getattr(oracledb, name, None) is not error_type):
                name = "OtherError"
            location = "unknown"
            tb = exc.__traceback__
            while tb:
                filename = Path(tb.tb_frame.f_code.co_filename)
                if (filename.parent == Path(__file__).resolve().parent
                        and filename.name in {"app.py", "agent.py", "tools.py", "policies.py", "db.py", "critic.py"}):
                    location = f"{filename.name}:{tb.tb_lineno}"
                tb = tb.tb_next
            code = getattr(exc.args[0], "code", None) if isinstance(exc, oracledb.DatabaseError) and exc.args else None
            if type(code) is int and 0 < code < 100000:
                app.logger.error("Analysis failed: %s at %s (ORA-%05d)", name, location, code)
            else:
                app.logger.error("Analysis failed: %s at %s", name, location)
            error, status = "Analysis unavailable. Check the local Oracle connection and policy setup, then retry.", 503
    return render_template("index.html", result=result, error=error, question=question,
                           question_error=status == 400), status


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5055, debug=False)
