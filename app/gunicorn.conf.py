import threading

bind = "0.0.0.0:8080"
workers = 2
threads = 4
timeout = 90
accesslog = None  # the app writes its own structured access log


def when_ready(server):
    """Initialise the schema in the background once the port is bound, so /api/health
    answers the ALB immediately even if RDS is slow to accept connections."""
    from app import init_schema
    threading.Thread(target=init_schema, daemon=True).start()
