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
    files = request.files.getlist('file')  # Get multiple files
    if not files or all(f.filename == '' for f in files):
        return redirect(url_for('index'))

    for file in files:
        if file:
            # Sanitize the relative path (file.filename includes folder path)
            relative_path = file.filename.replace('\\', '/')

            from werkzeug.utils import secure_filename  # Import added here
            # Split path into segments and secure each against ../ attacks
            path_parts = relative_path.split('/')
            safe_parts = [secure_filename(part) for part in path_parts if part]
            safe_path = '/'.join(safe_parts)

            # Full path on server
            full_path = os.path.join(app.config['UPLOAD_FOLDER'], safe_path)

            # Create directory if it doesn't exist
            os.makedirs(os.path.dirname(full_path), exist_ok=True)

            # Save the file
            file.save(full_path)

            # Get size after saving
            file_size = os.path.getsize(full_path)

            # Store in DB using the original relative path as name
            new_file = File(original_name=relative_path, stored_name=safe_path, file_size=file_size)
            db.session.add(new_file)

    db.session.commit()
    return redirect(url_for('index'))

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
        logger.error(f"File not found: {file_path}")
        return "File not found", 404

    ext = os.path.splitext(file.original_name)[1].lower()
    mime_types = {
        '.mp4': 'video/mp4',
        '.mp3': 'audio/mpeg'
    }
    mimetype = mime_types.get(ext, 'application/octet-stream')

    range_header = request.headers.get('Range', None)
    size = os.path.getsize(file_path)

    if not range_header:
        def generate():
            with open(file_path, 'rb') as f:
                yield from iter(lambda: f.read(16384), b'')
        response = Response(generate(), mimetype=mimetype, status=200)
        response.headers['Content-Length'] = size
        response.headers['Accept-Ranges'] = 'bytes'
        return response

    match = re.match(r'bytes=(\d+)-(\d*)', range_header)
    if not match:
        logger.error("Invalid range header")
        return "Invalid range", 416

    start, end = match.groups()
    start = int(start)
    end = int(end) if end else size - 1

    if start >= size or end >= size:
        logger.error(f"Range out of bounds: {start}-{end}, file size: {size}")
        return "Range out of bounds", 416

    length = end - start + 1

    def generate():
        with open(file_path, 'rb') as f:
            f.seek(start)
            remaining = length
            while remaining > 0:
                chunk = f.read(min(16384, remaining))
                if not chunk:
                    break
                yield chunk
                remaining -= len(chunk)

    response = Response(generate(), status=206, mimetype=mimetype)
    response.headers['Content-Range'] = f'bytes {start}-{end}/{size}'
    response.headers['Content-Length'] = length
    response.headers['Accept-Ranges'] = 'bytes'
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
        old_files = File.query.filter(File.upload_time < datetime.utcnow() - timedelta(hours=24)).all()
        for file in old_files:
            file_path = os.path.join(app.config['UPLOAD_FOLDER'], file.stored_name)
            if os.path.exists(file_path):
                os.remove(file_path)
            db.session.delete(file)
        db.session.commit()

# Schedule cleanup
scheduler = BackgroundScheduler()
scheduler.add_job(cleanup_old_files, 'interval', hours=1)
scheduler.start()

if __name__ == '__main__':
    with app.app_context():
        db.create_all()
    app.run(host='0.0.0.0', port=5000)