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


def start_virtual_mdns(hostname="share", port=5000):
    """
    Spins up a background worker thread to broadcast a custom local domain 
    alias (e.g., http://share.local:5000) using multicast DNS (mDNS), 
    tricking local devices into finding this machine without modifying the 
    host OS computer name.
    """
    # Use raw socket routing through a custom import name to bypass scoped re-import collision
    import sys
    import socket
    from zeroconf import Zeroconf, ServiceInfo

    # Dynamically retrieve the current primary LAN IP address
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)    
    try:
        s.connect(('8.8.8.8', 80))
        local_ip = s.getsockname()[0]
    except Exception:
        local_ip = '127.0.0.1'
    finally:
        s.close()

    # Convert the text IP into raw bytes required by network packets
    try:
        raw_ip = socket.inet_aton(local_ip)
    except Exception:
        raw_ip = socket.inet_aton('127.0.0.1')

    # Formulate the service broadcast parameters
    service_type = "_http._tcp.local."
    service_name = f"Share-WebServer.{service_type}"
    server_domain = f"{hostname}.local."

    info = ServiceInfo(
        type_=service_type,
        name=service_name,
        addresses=[raw_ip],
        port=port,
        properties={},
        server=server_domain
    )

    # Initialize Zeroconf by strictly binding it to your local IP address interface.
    # Passing the individual IP inside a list ([local_ip]) works perfectly across ALL 
    zeroconf_instance = Zeroconf(interfaces=[local_ip])    

    logger.info(f"Registering virtual mDNS host mapping: http://{hostname}.local:{port} -> {local_ip}")
    zeroconf_instance.register_service(info)

    return zeroconf_instance, info, local_ip