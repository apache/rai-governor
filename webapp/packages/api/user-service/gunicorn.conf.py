"""gunicorn settings for the API container (ISSUE-017).

gunicorn supervises the uvicorn workers: it restarts a worker that dies and
shuts workers down gracefully on deploy. The values below keep what the old
``uvicorn --workers 4`` command line did.
"""

import os

bind = "0.0.0.0:8000"
workers = int(os.getenv("WEB_CONCURRENCY", "4"))
worker_class = "uvicorn_worker.UvicornWorker"

# Each worker must import the app itself. services.run_registry.WORKER_ID is
# generated at import and tells workers apart: which one owns a run, which
# heartbeats are alive. Preloading in the parent would give every worker the
# same id.
preload_app = False

# Do not recycle workers. Agent runs execute inside the worker that started
# them; restarting it ends them (the orphan sweep then marks them as error).
max_requests = 0

# How long the parent waits for a worker whose event loop has stopped
# answering before it kills that worker, and with it every run in it. Not a
# request limit: a healthy loop answers while SSE streams and runs go on.
# Some calls still block the loop (python-couchdb has no socket timeout), and
# the old `uvicorn --workers` never killed a stalled worker, so this is set
# well above any of them. 0 would disable it but makes uvicorn notify the
# parent every 0.1 s.
timeout = int(os.getenv("GUNICORN_TIMEOUT", "1800"))
# On deploy, time for in-flight requests to finish before workers are killed.
graceful_timeout = int(os.getenv("GUNICORN_GRACEFUL_TIMEOUT", "30"))

# Was --timeout-keep-alive 3600: SSE clients reconnect on the same connection.
keepalive = 3600
# Was --proxy-headers --forwarded-allow-ips=*: trust the front proxy's headers.
forwarded_allow_ips = "*"

accesslog = "-"
errorlog = "-"
