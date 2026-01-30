import os
import uuid
import re
from datetime import datetime, timedelta
from flask import Flask, render_template, request, redirect, url_for, send_from_directory, Response
from flask_sqlalchemy import SQLAlchemy
from apscheduler.schedulers.background import BackgroundScheduler


import logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


app = Flask(__name__)
app.config['UPLOAD_FOLDER'] = 'uploads'
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///database.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['MAX_CONTENT_LENGTH'] = 2000 * 1024 * 1024  # 2GB limit

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


# Helper function for human-readable file size
def human_readable_size(size):
    for unit in ['B', 'KB', 'MB', 'GB']:
        if size < 1024:
            return f"{size:.2f} {unit}"
        size /= 1024
    return f"{size:.2f} TB"

# Cleanup function
def cleanup_old_files():
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

# Schedule cleanup
scheduler = BackgroundScheduler()
scheduler.add_job(cleanup_old_files, 'interval', hours=1)
scheduler.start()

if __name__ == '__main__':
    with app.app_context():
        db.create_all()
    app.run(host='0.0.0.0', port=5000)