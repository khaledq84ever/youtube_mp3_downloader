import os

bind             = f"0.0.0.0:{os.environ.get('PORT', '8080')}"
workers          = 1
worker_class     = "gthread"
threads          = 32
timeout          = 360
graceful_timeout = 30
keepalive        = 5
loglevel         = "info"
accesslog        = "-"
errorlog         = "-"
