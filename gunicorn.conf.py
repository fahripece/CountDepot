import os

# Bind
bind        = f"0.0.0.0:{os.environ.get('PORT', '5000')}"

# Workers — 2-4x CPU cores is the standard rule
workers     = int(os.environ.get("WEB_CONCURRENCY", 4))
worker_class = "sync"
threads     = 2
timeout     = 60
keepalive   = 5

# Logging
accesslog   = "-"   # stdout
errorlog    = "-"   # stderr
loglevel    = os.environ.get("LOG_LEVEL", "info")

# Process naming
proc_name   = "countdepot"

# Reload in development only
reload      = os.environ.get("FLASK_ENV") == "development"
