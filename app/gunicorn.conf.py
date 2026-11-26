import threading

bind = "0.0.0.0:8080"
workers = 2
threads = 4
timeout = 90
accesslog = None  # the app writes its own structured access log


def post_worker_init(worker):
    """Initialise the schema from inside each worker, in a background thread.

    It must not run in the master: a thread that is alive when the master forks its workers
    leaves locks (logging, imports, PyMySQL) held forever in the children, so random requests
    hang. Running it after the fork, in the worker, avoids that, and /api/health answers the ALB
    straight away. init_schema takes a MySQL named lock, so two workers never seed twice.
    """
    from app import init_schema
    threading.Thread(target=init_schema, daemon=True).start()
