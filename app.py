import eventlet
eventlet.monkey_patch()

import os
import sys
import logging
from flask import Flask
from apscheduler.schedulers.background import BackgroundScheduler

from extensions import db, socketio

logging.basicConfig(level=logging.INFO)

# --- App setup ---
app = Flask(__name__)

# --- NEW: CLI Argument Parsing & Safety Flags ---
custom_folder = sys.argv[1] if len(sys.argv) > 1 else None

if custom_folder:
    if not os.path.isdir(custom_folder):
        print(f"Error: Directory '{custom_folder}' does not exist.")
        sys.exit(1)
    app.config['UPLOAD_FOLDER'] = os.path.abspath(custom_folder)
    app.config['CLEANUP_ENABLED'] = False
else:
    app.config['UPLOAD_FOLDER'] = 'uploads'
    app.config['CLEANUP_ENABLED'] = True

app.config['SQLALCHEMY_DATABASE_URI']        = 'sqlite:///database.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['MAX_CONTENT_LENGTH']             = 10_000 * 1024 * 1024  # 10 GB

db.init_app(app)
socketio.init_app(app, cors_allowed_origins='*', async_mode='eventlet')

# --- Blueprints ---
# Importing watch also registers the @socketio.on('join_watch') handler
from routes.files import files_bp
from routes.chat  import chat_bp
from routes.watch import (watch_bp,
                          watch_sessions, watch_lock,
                          viewers_data,   viewers_lock,
                          client_last_update, client_update_lock)

app.register_blueprint(files_bp)
app.register_blueprint(chat_bp)
app.register_blueprint(watch_bp)

# --- Scheduler ---
from utils import cleanup_old_files, cleanup_watch_sessions, cleanup_rate_limits

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

# --- Entrypoint ---
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

    print(f"Running on:\n  http://{local_ip}:5000\n  http://127.0.0.1:5000\n  http://localhost:5000")
    print("Press CTRL+C to quit")

    socketio.run(app, host='0.0.0.0', port=5000)