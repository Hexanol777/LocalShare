import os
import uuid
import re
from datetime import datetime, timedelta
from flask import Flask, render_template, request, redirect, url_for, send_from_directory, Response
from flask_sqlalchemy import SQLAlchemy
from apscheduler.schedulers.background import BackgroundScheduler

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
    if 'file' not in request.files:
        return redirect(url_for('index'))
    file = request.files['file']
    if file.filename == '':
        return redirect(url_for('index'))
    if file:
        stored_name = str(uuid.uuid4()) + os.path.splitext(file.filename)[1]
        file_path = os.path.join(app.config['UPLOAD_FOLDER'], stored_name)
        with open(file_path, 'wb') as f:
            chunk_size = 8192
            while True:
                chunk = file.stream.read(chunk_size)
                if not chunk:
                    break
                f.write(chunk)
        file_size = os.path.getsize(file_path)
        new_file = File(original_name=file.filename, stored_name=stored_name, file_size=file_size)
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

    # Determine MIME type based on extension
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

    def generate():
        with open(file_path, 'rb') as f:
            f.seek(0)
            while True:
                chunk = f.read(8192)
                if not chunk:
                    break
                yield chunk

    range_header = request.headers.get('Range', None)
    if not range_header:
        return Response(generate(), mimetype=mimetype)

    size = os.path.getsize(file_path)
    byte1, byte2 = 0, None
    match = re.match(r'bytes=(\d+)-(\d*)', range_header)
    if match:
        byte1, byte2 = match.groups()
        byte1 = int(byte1)
        byte2 = int(byte2) if byte2 else size - 1

    length = byte2 - byte1 + 1
    resp = Response(
        generate(),
        status=206,
        mimetype=mimetype,
        content_type=mimetype,
        direct_passthrough=True
    )
    resp.headers.add('Content-Range', f'bytes {byte1}-{byte2}/{size}')
    resp.headers.add('Accept-Ranges', 'bytes')
    return resp

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