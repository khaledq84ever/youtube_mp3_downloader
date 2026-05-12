import os

bind             = f"0.0.0.0:{os.environ.get('PORT', '8080')}"
workers          = 1
worker_class     = "gevent"
worker_connections = 200
timeout          = 360
graceful_timeout = 30
keepalive        = 5
loglevel         = "info"
accesslog        = "-"
errorlog         = "-"
