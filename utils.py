import os
import time
import logging
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

STREAMABLE_EXTENSIONS = {'.mp4', '.mkv', '.mp3', '.flac', '.webm', '.ogg', '.m4b', '.m4a', '.ts', '.gif'}


def human_readable_size(size):
    for unit in ['B', 'KB', 'MB', 'GB']:
        if size < 1024:
            return f"{size:.2f} {unit}"
        size /= 1024
    return f"{size:.2f} TB"


def update_viewer_info(viewers_data, viewers_lock, file_id, client_ip, latency_ms):
    with viewers_lock:
        now = time.time()
        viewers = viewers_data[file_id]
        if client_ip not in viewers:
            viewers[client_ip] = {'last_seen': now, 'latency': latency_ms, 'first_seen': now}
        else:
            old = viewers[client_ip]['latency']
            viewers[client_ip]['latency'] = old * 0.7 + latency_ms * 0.3
            viewers[client_ip]['last_seen'] = now


def cleanup_old_files():
    """Must be called within an active Flask application context."""
    from extensions import db
    from models import File, ChatMessage
    from flask import current_app  # <--- Added current_app

    cutoff = datetime.utcnow() - timedelta(hours=24)
    
    # Only delete files if cleanup is enabled (i.e., using default 'uploads' folder)
    if current_app.config.get('CLEANUP_ENABLED', True):
        old_files = File.query.filter(File.upload_time < cutoff).all()
        upload_folder = current_app.config.get('UPLOAD_FOLDER', 'uploads')
        
        for file in old_files:
            file_path = os.path.join(upload_folder, file.stored_name)
            if os.path.exists(file_path):
                os.remove(file_path)
            db.session.delete(file)
            
    # Always clean up old chat messages regardless of directory mode
    ChatMessage.query.filter(ChatMessage.timestamp < cutoff).delete()
    db.session.commit()


def cleanup_watch_sessions(watch_sessions, watch_lock, viewers_data, viewers_lock):
    now = time.time()
    with watch_lock:
        expired = [fid for fid, s in watch_sessions.items()
                   if now - s.get('last_active', 0) > 600]
        for fid in expired:
            del watch_sessions[fid]
        if expired:
            logger.info(f"Cleaned up {len(expired)} inactive watch sessions")

    with viewers_lock:
        for fid in list(viewers_data.keys()):
            viewers = viewers_data[fid]
            stale = [ip for ip, info in viewers.items()
                     if now - info.get('last_seen', 0) > 30]
            for ip in stale:
                del viewers[ip]
            if not viewers and fid not in watch_sessions:
                del viewers_data[fid]


def cleanup_rate_limits(client_last_update, client_update_lock):
    now = time.time()
    with client_update_lock:
        expired = [k for k, ts in client_last_update.items() if now - ts > 60]
        for k in expired:
            del client_last_update[k]
        if expired:
            logger.info(f"Cleaned up {len(expired)} rate limit entries")