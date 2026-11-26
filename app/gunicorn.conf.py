bind = "0.0.0.0:8080"
workers = 2
threads = 4
timeout = 90
accesslog = None  # the app writes its own structured access log


def on_starting(server):
    from app import init_schema
    init_schema()
