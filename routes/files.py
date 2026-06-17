import os
import re
import uuid
from urllib.parse import quote

from flask import Blueprint, render_template, request, redirect, url_for, send_file, Response, current_app
from werkzeug.utils import secure_filename

from extensions import db
from models import File
from utils import human_readable_size, STREAMABLE_EXTENSIONS

files_bp = Blueprint('files', __name__)

MIME_TYPES = {
    '.mp4':  'video/mp4',
    '.mkv':  'video/x-matroska',
    '.webm': 'video/webm',
    '.ogg':  'video/ogg',
    '.mp3':  'audio/mpeg',
    '.flac': 'audio/flac',
}


@files_bp.route('/')
def index():
    from datetime import datetime, timedelta
    recent_files = (
        File.query
        .filter(File.upload_time >= datetime.utcnow() - timedelta(hours=72))
        .order_by(File.upload_time.desc())
        .all()
    )
    for file in recent_files:
        file.display_size = human_readable_size(file.file_size)
        file.extension = os.path.splitext(file.original_name)[1].lower()
    return render_template('index.html', files=recent_files, streamable_extensions=STREAMABLE_EXTENSIONS)


@files_bp.route('/upload', methods=['POST'])
def upload_file():
    files = request.files.getlist('file')
    if not files or all(f.filename == '' for f in files):
        return redirect(url_for('files.index'))

    upload_folder = current_app.config['UPLOAD_FOLDER']
    upload_id = uuid.uuid4().hex

    for file in files:
        if not file:
            continue

        relative_path = file.filename.replace('\\', '/')
        path_parts = relative_path.split('/')
        safe_parts = [secure_filename(part) for part in path_parts if part]
        if not safe_parts:
            continue

        safe_path = os.path.join(upload_id, *safe_parts)
        full_path = os.path.join(upload_folder, safe_path)
        os.makedirs(os.path.dirname(full_path), exist_ok=True)
        file.save(full_path)

        db.session.add(File(
            original_name=relative_path,
            stored_name=safe_path,
            file_size=os.path.getsize(full_path),
        ))

        print(relative_path, safe_path)
    db.session.commit()
    return redirect(url_for('files.index'))


@files_bp.route('/download/<int:file_id>')
def download_file(file_id):
    file = File.query.get_or_404(file_id)

    # Normalize path
    stored_name = file.stored_name.replace('\\', '/')
    file_path = os.path.join(current_app.config['UPLOAD_FOLDER'], stored_name)

    if not os.path.exists(file_path):
        # If folder is deleted
        return f'File not found: {file_path}', 404

    return send_file(
        file_path,
        as_attachment=True,
        download_name=file.original_name,
    )


@files_bp.route('/stream/<int:file_id>')
def stream_file(file_id):
    file = File.query.get_or_404(file_id)
    file_path = os.path.join(current_app.config['UPLOAD_FOLDER'], file.stored_name)

    if not os.path.exists(file_path):
        return 'File not found', 404

    file_size = os.path.getsize(file_path)
    ext = os.path.splitext(file.original_name)[1].lower()
    mimetype = MIME_TYPES.get(ext, 'application/octet-stream')
    encoded_filename = quote(file.original_name)
    range_header = request.headers.get('Range')

    if not range_header:
        def generate():
            with open(file_path, 'rb') as f:
                while chunk := f.read(8192):
                    yield chunk

        resp = Response(generate(), mimetype=mimetype)
        resp.headers['Content-Length'] = file_size
        resp.headers['Accept-Ranges'] = 'bytes'
        resp.headers['Content-Disposition'] = f'inline; filename="{encoded_filename}"'
        return resp

    match = re.match(r'bytes=(\d+)-(\d*)', range_header)
    if not match:
        return 'Invalid Range Header', 416

    start = int(match.group(1))
    end = int(match.group(2)) if match.group(2) else file_size - 1

    if start >= file_size or end >= file_size or start > end:
        return 'Range Not Satisfiable', 416

    length = end - start + 1

    def generate_range():
        with open(file_path, 'rb') as f:
            f.seek(start)
            remaining = length
            while remaining > 0:
                chunk = f.read(min(256 * 1024, remaining))
                if not chunk:
                    break
                yield chunk
                remaining -= len(chunk)

    resp = Response(generate_range(), status=206, mimetype=mimetype)
    resp.headers['Content-Range'] = f'bytes {start}-{end}/{file_size}'
    resp.headers['Content-Length'] = length
    resp.headers['Accept-Ranges'] = 'bytes'
    resp.headers['Content-Disposition'] = f'inline; filename="{encoded_filename}"'
    return resp


@files_bp.route('/stream_page/<int:file_id>')
def stream_page(file_id):
    file = File.query.get_or_404(file_id)
    ext = os.path.splitext(file.original_name)[1].lower()
    mimetype = MIME_TYPES.get(ext, 'application/octet-stream')
    return render_template('stream.html', file_id=file_id, mimetype=mimetype)