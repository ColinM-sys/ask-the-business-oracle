"""Watch the swarm; exit (which alerts Claude) as soon as something needs fixing.
Every 60 s it appends a status line to watchdog.log. It exits with a reason when:
  - more than 30 Codex failures in the last minute (mass kill)
  - no Codex process running for 5 minutes while hammer.py is alive (stalled round)
  - master has no new commit for 30 minutes (pipeline producing nothing)
  - hammer.py or merge_bot.py is not running
    python watchdog.py"""
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
FAIL = os.path.join(HERE, "codex_failures.log")


def ps(match):
    out = subprocess.run(["powershell", "-c", "Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | "
                          f"? {{ $_.CommandLine -like '*{match}*' }} | measure | select -expand Count"],
                         capture_output=True, text=True).stdout.strip()
    return int(out or 0)


def codex_count():
    out = subprocess.run(["powershell", "-c", "(Get-Process | ? Name -eq 'codex' | measure).Count"],
                         capture_output=True, text=True).stdout.strip()
    return int(out or 0)


def lines(path):
    return sum(1 for _ in open(path, encoding="utf-8", errors="ignore")) if os.path.exists(path) else 0


def last_commit_age():
    subprocess.run(["git", "fetch", "-q"], cwd=os.path.join(HERE, "project"))
    ts = subprocess.run(["git", "log", "-1", "--format=%ct", "origin/master"], cwd=os.path.join(HERE, "project"),
                        capture_output=True, text=True).stdout.strip()
    return time.time() - int(ts or time.time())


def done(reason):
    with open(os.path.join(HERE, "watchdog.log"), "a", encoding="utf-8") as f:
        f.write(f"{time.strftime('%H:%M:%S')} ALERT {reason}\n")
    print("ALERT", reason)
    sys.exit(1)


fails, idle_since = lines(FAIL), None
while True:
    time.sleep(60)
    n_fail, n_codex, age = lines(FAIL), codex_count(), last_commit_age()
    hammer, merger = ps("hammer.py"), ps("merge_bot.py")
    with open(os.path.join(HERE, "watchdog.log"), "a", encoding="utf-8") as f:
        f.write(f"{time.strftime('%H:%M:%S')} codex={n_codex} new_failures={n_fail - fails} "
                f"last_commit={age / 60:.0f}m hammer={hammer} merge_bot={merger}\n")
    if n_fail - fails > 30:
        done(f"{n_fail - fails} Codex failures in the last minute")
    fails = n_fail
    if not hammer or not merger:
        done(f"process down: hammer={hammer} merge_bot={merger}")
    idle_since = (idle_since or time.time()) if n_codex == 0 else None
    if idle_since and time.time() - idle_since > 300:
        done("no Codex agents running for 5 minutes")
    if age > 1800:
        done(f"no new commit on master for {age / 60:.0f} minutes")
