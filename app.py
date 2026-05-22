import os
import uuid
import threading
import time
import re
from datetime import datetime, timedelta
from flask import Flask, render_template, request, redirect, url_for, send_from_directory, Response
from flask_sqlalchemy import SQLAlchemy
from apscheduler.schedulers.background import BackgroundScheduler
from collections import defaultdict

import logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


app = Flask(__name__)
app.config['UPLOAD_FOLDER'] = 'uploads'
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///database.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['MAX_CONTENT_LENGTH'] = 2000 * 1024 * 1024  # 2GB limit


# Watch Together states
watch_sessions = {}
watch_lock = threading.Lock()
client_last_update = defaultdict(float)
client_update_lock = threading.Lock()
RATE_LIMIT_SECONDS = 0.5  # Minimum 500ms between updates from same client for same file to prevent seek storms
viewers_data = defaultdict(dict)  # {file_id: {client_ip: {'last_seen': timestamp, 'latency': avg_ms}}}
viewers_lock = threading.Lock()


# List of streamable file extensions (modify this to add/remove formats)
STREAMABLE_EXTENSIONS = ['.mp4', '.mkv', '.mp3', '.flac', '.webm', '.ogg']

# Ensure folders exist
if not os.path.exists('instance'):
    os.makedirs('instance')
if not os.path.exists(app.config['UPLOAD_FOLDER']):
    os.makedirs(app.config['UPLOAD_FOLDER'])

db = SQLAlchemy(app)

# Database model
class File(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    original_name = db.Column(db.String(255), nullable=False)
    stored_name = db.Column(db.String(255), nullable=False)
    upload_time = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    file_size = db.Column(db.Integer, nullable=False)

# chat Database model
class ChatMessage(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    sender_ip = db.Column(db.String(45), nullable=False)
    content = db.Column(db.Text, nullable=False)
    timestamp = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)


# Routes
@app.route('/')
def index():
    recent_files = File.query.filter(File.upload_time >= datetime.utcnow() - timedelta(hours=24)).order_by(File.upload_time.desc()).all()
    for file in recent_files:
        file.display_size = human_readable_size(file.file_size)
        # Compute file extension
        file.extension = os.path.splitext(file.original_name)[1].lower()
    return render_template('index.html', files=recent_files, streamable_extensions=STREAMABLE_EXTENSIONS)


# ----- DOWNLOAD/UPLOAD ENDPOINTS -----
@app.route('/upload', methods=['POST'])
def upload_file():
    files = request.files.getlist('file')
    if not files or all(f.filename == '' for f in files):
        return redirect(url_for('index'))

    from werkzeug.utils import secure_filename

    # One UUID per upload request
    upload_id = uuid.uuid4().hex

    for file in files:
        if not file:
            continue

        # Normalize and sanitize relative path
        relative_path = file.filename.replace('\\', '/')

        path_parts = relative_path.split('/')
        safe_parts = [secure_filename(part) for part in path_parts if part]

        if not safe_parts:
            continue

        # Prefix with UUID to avoid collisions
        safe_path = os.path.join(upload_id, *safe_parts)

        full_path = os.path.join(app.config['UPLOAD_FOLDER'], safe_path)

        # Creaate directory if it doesn't exist
        os.makedirs(os.path.dirname(full_path), exist_ok=True)

        # Save file
        file.save(full_path)

        file_size = os.path.getsize(full_path)

        # save original relative path for display and UUID path for storage
        new_file = File(
            original_name=relative_path,
            stored_name=safe_path,
            file_size=file_size
        )
        db.session.add(new_file)

    db.session.commit()
    return redirect(url_for('index'))


@app.route('/download/<int:file_id>')
def download_file(file_id):
    file = File.query.get_or_404(file_id)
    return send_from_directory(app.config['UPLOAD_FOLDER'], file.stored_name, as_attachment=True, download_name=file.original_name)


# ----- STREAM ENDPOINTS -----
@app.route('/stream/<int:file_id>')
def stream_file(file_id):
    file = File.query.get_or_404(file_id)
    file_path = os.path.join(app.config['UPLOAD_FOLDER'], file.stored_name)
    
    if not os.path.exists(file_path):
        return "File not found", 404

    file_size = os.path.getsize(file_path)
    
    # Determine MIME type
    ext = os.path.splitext(file.original_name)[1].lower()
    mime_types = {
        '.mp4': 'video/mp4',
        '.mkv': 'video/x-matroska',
        '.webm': 'video/webm',
        '.ogg': 'video/ogg',
        '.mp3': 'audio/mpeg',
        '.flac': 'audio/flac'
    }
    mimetype = mime_types.get(ext, 'application/octet-stream')

    # Safely encode filename for Content-Disposition (handles Unicode like Japanese characters)
    from urllib.parse import quote
    encoded_filename = quote(file.original_name)

    range_header = request.headers.get('Range', None)
    
    if not range_header:
        # Full file request
        def generate():
            with open(file_path, 'rb') as f:
                while chunk := f.read(8192):
                    yield chunk
        
        response = Response(generate(), mimetype=mimetype)
        response.headers['Content-Length'] = file_size
        response.headers['Accept-Ranges'] = 'bytes'
        response.headers['Content-Disposition'] = f'inline; filename="{encoded_filename}"'
        return response

    # Parse Range header
    import re
    match = re.match(r'bytes=(\d+)-(\d*)', range_header)
    if not match:
        return "Invalid Range Header", 416

    start = int(match.group(1))
    end = int(match.group(2)) if match.group(2) else file_size - 1

    if start >= file_size or end >= file_size or start > end:
        return "Range Not Satisfiable", 416

    length = end - start + 1

    def generate_range():
        with open(file_path, 'rb') as f:
            f.seek(start)
            remaining = length
            while remaining > 0:
                chunk_size = min(8192, remaining)
                chunk = f.read(chunk_size)
                if not chunk:
                    break
                yield chunk
                remaining -= len(chunk)

    response = Response(generate_range(), status=206, mimetype=mimetype)
    response.headers['Content-Range'] = f'bytes {start}-{end}/{file_size}'
    response.headers['Content-Length'] = length
    response.headers['Accept-Ranges'] = 'bytes'
    response.headers['Content-Disposition'] = f'inline; filename="{encoded_filename}"'
    return response

@app.route('/stream_page/<int:file_id>')
def stream_page(file_id):
    file = File.query.get_or_404(file_id)
    ext = os.path.splitext(file.original_name)[1].lower()
    mime_types = {
        '.mp4': 'video/mp4',
        '.mkv': 'video/x-matroska',
        '.webm': 'video/webm',
        '.ogg': 'video/ogg',
        '.mp3': 'audio/mpeg',
        '.flac': 'audio/flac'
    }
    mimetype = mime_types.get(ext, 'application/octet-stream')
    return render_template('stream.html', file_id=file_id, mimetype=mimetype)

# ----- CHAT ENDPOINTS -----
# Main endpoint for the LAN chatroom
@app.route('/chat')
def chat():
    return render_template('chat.html')

# POST endpoint to send messages
@app.route('/chat/send', methods=['POST'])
def chat_send():
    data = request.get_json()
    if not data or not data.get('message'):
        return {'status': 'error'}, 400

    msg = data['message'].strip()
    if not msg:
        return {'status': 'ok'}

    sender_ip = request.remote_addr or 'unknown'

    chat_msg = ChatMessage(
        sender_ip=sender_ip,
        content=msg
    )
    db.session.add(chat_msg)
    db.session.commit()

    return {'status': 'ok'}

# Message pollind endpoint
@app.route('/chat/messages')
def chat_messages():
    since_id = request.args.get('since', type=int, default=0)

    messages = ChatMessage.query \
        .filter(ChatMessage.id > since_id) \
        .order_by(ChatMessage.id.asc()) \
        .all()

    return {
        'messages': [
            {
                'id': m.id,
                'sender': f"{m.sender_ip}:",
                'content': m.content,
                'timestamp': m.timestamp.isoformat()
            }
            for m in messages
        ]
    }


# ----- WATCH ALONG ENDPOINTS -----
@app.route('/watch/state/<int:file_id>')
def watch_state(file_id):
    """Get the current watch state for a file."""
    with watch_lock:
        sess = watch_sessions.get(file_id)
        if not sess:
            # Return default state (paused at 0)
            return {
                'playing': False,
                'position': 0.0,
                'updated_at': time.time(),
                'last_action': 'pause'
            }
        # Return a copy to avoid accidental modification
        return {
            'playing': sess['playing'],
            'position': sess['position'],
            'updated_at': sess['updated_at'],
            'last_action': sess.get('last_action', 'pause')
        } 

@app.route('/watch/action/<int:file_id>', methods=['POST'])
def watch_action(file_id):
    data = request.get_json()
    if not data or 'action' not in data:
        return {'error': 'missing action'}, 400

    action = data['action']
    now = time.time()
    
    # Rate limiting
    client_ip = request.remote_addr or 'unknown'
    rate_limit_key = f"{client_ip}:{file_id}"
    
    # Track viewer latency (estimate from RTT if available)
    client_latency = data.get('latency', 50)  # Default 50ms if not sent
    update_viewer_info(file_id, client_ip, client_latency)
    
    with client_update_lock:
        last_update = client_last_update.get(rate_limit_key, 0)
        if now - last_update < RATE_LIMIT_SECONDS:
            return {'status': 'rate_limited'}, 429
        client_last_update[rate_limit_key] = now
    
    # Optional: Clean up old rate limit entries periodically (add to cleanup_watch_sessions)
    # This prevents the dict from growing forever

    with watch_lock:
        sess = watch_sessions.get(file_id)
        if not sess:
            sess = {
                'playing': False,
                'position': 0.0,
                'updated_at': now,
                'last_action': 'pause'
            }
            watch_sessions[file_id] = sess

        # For seek actions, only update if the position change is significant
        if action == 'seek':
            new_pos = data.get('position', 0.0)
            if not isinstance(new_pos, (int, float)) or new_pos < 0:
                return {'error': 'invalid position'}, 400
            
            # Don't update if the position change is less than 0.5 seconds (prevents micro-seeks)
            if abs(sess['position'] - new_pos) < 0.5 and sess['playing']:
                return {'status': 'ignored', 'message': 'Position change too small'}

            sess['position'] = new_pos
            sess['updated_at'] = now
            sess['last_action'] = 'seek'
            # Keep playing state as-is (do not change)

        elif action == 'play':
            sess['playing'] = True
            # Only update position if provided and significantly different
            if 'position' in data:
                new_pos = float(data['position'])
                if abs(sess['position'] - new_pos) > 0.3:
                    sess['position'] = new_pos
            sess['updated_at'] = now
            sess['last_action'] = 'play'

        elif action == 'pause':
            sess['playing'] = False
            # Update position to the current video time when paused
            if 'position' in data:
                sess['position'] = float(data['position'])
            sess['updated_at'] = now
            sess['last_action'] = 'pause'

        else:
            return {'error': 'unknown action'}, 400

        sess['last_active'] = now

    return {'status': 'ok'}

@app.route('/watch/viewers/<int:file_id>')
def watch_viewers(file_id):
    """Get list of active viewers for a watch session"""
    with viewers_lock:
        viewers = viewers_data.get(file_id, {})
        now = time.time()
        # Only include viewers active in last 30 seconds
        active = {
            ip: info for ip, info in viewers.items()
            if now - info.get('last_seen', 0) < 30
        }
        return {
            'count': len(active),
            'viewers': [
                {
                    'ip': ip,
                    'latency': round(info['latency'], 1),
                    'active_seconds': round(now - info.get('first_seen', now), 1)
                }
                for ip, info in active.items()
            ]
        }

# Helper function for human-readable file size
def human_readable_size(size):
    for unit in ['B', 'KB', 'MB', 'GB']:
        if size < 1024:
            return f"{size:.2f} {unit}"
        size /= 1024
    return f"{size:.2f} TB"

def update_viewer_info(file_id, client_ip, latency_ms):
    """Track active viewers and their latency"""
    with viewers_lock:
        now = time.time()
        viewers = viewers_data[file_id]
        
        if client_ip not in viewers:
            viewers[client_ip] = {'last_seen': now, 'latency': latency_ms, 'first_seen': now}
        else:
            # Exponential moving average for latency
            old_latency = viewers[client_ip]['latency']
            viewers[client_ip]['latency'] = old_latency * 0.7 + latency_ms * 0.3
            viewers[client_ip]['last_seen'] = now

# Cleanup functions
def cleanup_old_files():
    """Remove old files after a specified amount of time"""
    with app.app_context():
        cutoff = datetime.utcnow() - timedelta(hours=24)

        # Files
        old_files = File.query.filter(File.upload_time < cutoff).all()
        for file in old_files:
            file_path = os.path.join(app.config['UPLOAD_FOLDER'], file.stored_name)
            if os.path.exists(file_path):
                os.remove(file_path)
            db.session.delete(file)

        # Chat messages
        ChatMessage.query.filter(ChatMessage.timestamp < cutoff).delete()

        db.session.commit()

def cleanup_watch_sessions():
    """Remove watch sessions and viewers inactive for > 10 minutes"""
    now = time.time()
    
    # Clean sessions
    with watch_lock:
        expired_sessions = [fid for fid, sess in watch_sessions.items()
                           if now - sess.get('last_active', 0) > 600]
        for fid in expired_sessions:
            del watch_sessions[fid]
        if expired_sessions:
            logger.info(f"Cleaned up {len(expired_sessions)} inactive watch sessions")
    
    # Clean stale viewers (inactive > 30 seconds)
    with viewers_lock:
        for fid in list(viewers_data.keys()):
            viewers = viewers_data[fid]
            stale = [ip for ip, info in viewers.items() 
                    if now - info.get('last_seen', 0) > 30]
            for ip in stale:
                del viewers[ip]
            if not viewers and fid not in watch_sessions:
                del viewers_data[fid]

def cleanup_rate_limits():
    """Remove rate limit entries older than 1 minute"""
    now = time.time()
    with client_update_lock:
        expired = [key for key, timestamp in client_last_update.items()
                   if now - timestamp > 60]  # 1 minute expiration
        for key in expired:
            del client_last_update[key]
        if expired:
            logger.info(f"Cleaned up {len(expired)} rate limit entries")


# Schedule cleanup
scheduler = BackgroundScheduler()
scheduler.add_job(cleanup_old_files, 'interval', hours=1)
scheduler.add_job(cleanup_watch_sessions, 'interval', minutes=15)
scheduler.start()

if __name__ == '__main__':
    with app.app_context():
        db.create_all()
    app.run(host='0.0.0.0', port=5000)