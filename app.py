import os
import sys
import secrets
import logging
from datetime import timedelta

from flask import Flask
from apscheduler.schedulers.background import BackgroundScheduler

from extensions import db, socketio

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ============================================================
# APP SETUP
# ============================================================

app = Flask(__name__)

# CLI Argument Parsing & Safety Flag
custom_folder = sys.argv[1] if len(sys.argv) > 1 else None

if custom_folder:
    if not os.path.isdir(custom_folder):
        print(f"Error: Directory '{custom_folder}' does not exist.")
        sys.exit(1)
    app.config['UPLOAD_FOLDER']   = os.path.abspath(custom_folder)
    app.config['CLEANUP_ENABLED'] = False
else:
    app.config['UPLOAD_FOLDER']   = 'uploads'
    app.config['CLEANUP_ENABLED'] = True

app.config['SQLALCHEMY_DATABASE_URI']        = 'sqlite:///database.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['MAX_CONTENT_LENGTH']             = 10_000 * 1024 * 1024  # 10 GB

# ============================================================
# AUTH CONFIG
# ============================================================

_SECRET_KEY     = os.environ.get('SECRET_KEY')
_ADMIN_PASSWORD = os.environ.get('ADMIN_PASSWORD', 'admin123')

if not _SECRET_KEY:
    _SECRET_KEY = secrets.token_hex(32)
    logger.warning(
        "SECRET_KEY not set — using a randomly generated key. "
        "Sessions will be invalidated on every restart. "
        "Set SECRET_KEY as an environment variable for persistence."
    )

if _ADMIN_PASSWORD == 'admin123':
    logger.warning(
        "\n" + "=" * 62 +
        "\n  WARNING: Default admin password in use!" +
        "\n  Set ADMIN_PASSWORD as an environment variable before" +
        "\n  exposing this server to your network." +
        "\n" + "=" * 62
    )

app.config['SECRET_KEY']                 = _SECRET_KEY
app.config['ADMIN_PASSWORD']             = _ADMIN_PASSWORD  # read by routes/auth.py
app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(days=30)

# ============================================================
# EXTENSIONS
# ============================================================

db.init_app(app)
socketio.init_app(app, cors_allowed_origins='*', async_mode='threading')

# ============================================================
# BLUEPRINTS
# ============================================================

# Importing watch also registers the @socketio.on('join_watch') handler
from routes.files import files_bp
from routes.chat  import chat_bp
from routes.auth  import auth_bp
from routes.dashboard  import dashboard_bp
from routes.watch import (watch_bp,
                          watch_sessions, watch_lock,
                          viewers_data,   viewers_lock,
                          client_last_update, client_update_lock)

app.register_blueprint(files_bp)
app.register_blueprint(chat_bp)
app.register_blueprint(auth_bp)
app.register_blueprint(dashboard_bp)
app.register_blueprint(watch_bp)

# ============================================================
# CONTEXT PROCESSOR
# Context processors are app-level — they must be registered on
# the app itself to inject into templates from all blueprints.
# ============================================================

from utils import is_admin

@app.context_processor
def inject_auth_status():
    """Makes admin_mode available in every template automatically."""
    return dict(admin_mode=is_admin())

# ============================================================
# SCHEDULER
# ============================================================

from utils import cleanup_old_files, cleanup_watch_sessions, cleanup_rate_limits, start_virtual_mdns

def _cleanup_files():
    with app.app_context():
        cleanup_old_files()

scheduler = BackgroundScheduler()
scheduler.add_job(_cleanup_files, 'interval', hours=1)
scheduler.add_job(
    lambda: cleanup_watch_sessions(watch_sessions, watch_lock, viewers_data, viewers_lock),
    'interval', minutes=15,
)
scheduler.add_job(
    lambda: cleanup_rate_limits(client_last_update, client_update_lock),
    'interval', minutes=5,
)
scheduler.start()

# ============================================================
# ENTRYPOINT
# ============================================================

if __name__ == '__main__':
    os.makedirs('instance', exist_ok=True)
    os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)

    with app.app_context():
        db.create_all()

    import socket as _socket
    s = _socket.socket(_socket.AF_INET, _socket.SOCK_DGRAM)
    try:
        s.connect(('8.8.8.8', 80))
        local_ip = s.getsockname()[0]
    except Exception:
        local_ip = '127.0.0.1'
    finally:
        s.close()

    # --- TRICK mDNS USING ZEROCONF ---
    zeroconf, service_info, machine_ip = start_virtual_mdns(hostname="share", port=80)

    print("Press CTRL+C to quit")

    try:
        socketio.run(app, host='0.0.0.0', port=80)
    finally:
        logger.info("De-registering broadcast parameters from local subnet routing tables...")
        zeroconf.unregister_service(service_info)
        zeroconf.close()