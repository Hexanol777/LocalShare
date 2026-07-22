import os
import re
import hashlib
import subprocess
import shutil
from urllib.parse import quote

from flask import (Blueprint, render_template, request, redirect,
                   url_for, send_file, Response, current_app, abort, jsonify)
from werkzeug.utils import secure_filename

from extensions import db
from models import File
from utils import human_readable_size, STREAMABLE_EXTENSIONS, admin_required, log_activity

files_bp = Blueprint('files', __name__)

IMAGE_EXTENSIONS = {'.jpg', '.jpeg', '.png', '.webp', '.gif'}
VIDEO_EXTENSIONS = {'.mp4', '.mkv', '.webm', '.ts', '.mov', '.avi', '.m4v'}

THUMBNAIL_DIR = '.thumbnails'
FFMPEG_PATH   = shutil.which('ffmpeg')

try:
    from PIL import Image
    PILLOW_AVAILABLE = True
except ImportError:
    PILLOW_AVAILABLE = False

MIME_TYPES = {
    # Native video
    '.mp4':  'video/mp4',
    '.webm': 'video/webm',
    '.ogg':  'video/ogg',
    '.mov':  'video/mp4',
    # TS — mpegts.js
    '.ts':   'video/mp2t',
    # Unsupported video (correct MIME, player shows download prompt)
    '.mkv':  'video/x-matroska',
    '.avi':  'video/x-msvideo',
    '.wmv':  'video/x-ms-wmv',
    # Native audio
    '.mp3':  'audio/mpeg',
    '.flac': 'audio/flac',
    '.wav':  'audio/wav',
    '.aac':  'audio/aac',
    '.m4a':  'audio/mp4',
    '.m4b':  'audio/mp4',
    '.opus': 'audio/ogg',
    '.oga':  'audio/ogg',
}

# Player routing
PLAYER_NATIVE_VIDEO = {'.mp4', '.webm', '.ogg', '.mov'}
PLAYER_TS_VIDEO     = {'.ts'}
PLAYER_NATIVE_AUDIO = {'.mp3', '.flac', '.wav', '.aac', '.m4a', '.m4b', '.opus', '.oga'}


# ---------- Helpers ----------

def is_safe_path(base_dir, rel):
    base   = os.path.realpath(base_dir)
    target = os.path.realpath(os.path.join(base_dir, rel))
    return target == base or target.startswith(base + os.sep)


def natural_sort_key(s):
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r'(\d+)', s)]


def _resolve_subpath(subpath):
    p = os.path.normpath(subpath).lstrip('/') if subpath.strip() else ''
    return '' if p == '.' else p


def _get_or_register(rel_path, entry_name, entry_size):
    obj = File.query.filter_by(stored_name=rel_path).first()
    if obj is None:
        obj = File(original_name=entry_name, stored_name=rel_path, file_size=entry_size)
        db.session.add(obj)
        try:
            db.session.flush()
        except Exception:
            db.session.rollback()
            obj = File.query.filter_by(stored_name=rel_path).first()
    return obj


# ---------- Routes ----------

@files_bp.route('/')
def index():
    return redirect(url_for('files.browse'))


@files_bp.route('/browse')
def browse():
    upload_folder = current_app.config['UPLOAD_FOLDER']
    os.makedirs(upload_folder, exist_ok=True)

    safe_path = _resolve_subpath(request.args.get('path', ''))

    if safe_path and not is_safe_path(upload_folder, safe_path):
        abort(403)

    full_path = os.path.join(upload_folder, safe_path) if safe_path else upload_folder
    if not os.path.isdir(full_path):
        abort(404)

    items      = []
    image_only = True
    has_files  = False

    try:
        entries = sorted(os.scandir(full_path),
                         key=lambda e: (not e.is_dir(), natural_sort_key(e.name)))
    except PermissionError:
        abort(403)

    for entry in entries:
        rel = (os.path.join(safe_path, entry.name) if safe_path else entry.name).replace('\\', '/')

        if entry.is_dir():
            image_only = False
            items.append({'name': entry.name, 'type': 'dir', 'path': rel, 'size': ''})
        else:
            has_files = True
            ext = os.path.splitext(entry.name)[1].lower()
            if ext not in IMAGE_EXTENSIONS:
                image_only = False

            db_file = _get_or_register(rel, entry.name, entry.stat().st_size)

            items.append({
                'name':          entry.name,
                'type':          'file',
                'path':          rel,
                'size':          human_readable_size(db_file.file_size),
                'streamable':    ext in STREAMABLE_EXTENSIONS,
                'extension':     ext,
                'has_thumbnail': ext in IMAGE_EXTENSIONS or ext in VIDEO_EXTENSIONS,
                'file_id':       db_file.id,
            })

    db.session.commit()

    if not has_files:
        image_only = False

    breadcrumbs = []
    if safe_path:
        cumulative = ''
        for part in safe_path.split('/'):
            cumulative = (cumulative + '/' + part).lstrip('/')
            breadcrumbs.append({'name': part, 'path': cumulative})

    parent_path = '/'.join(safe_path.split('/')[:-1]) if safe_path else ''

    return render_template(
        'browse.html',
        items=items,
        current_path=safe_path,
        breadcrumbs=breadcrumbs,
        parent_path=parent_path,
        image_only=image_only,
    )


@files_bp.route('/upload', methods=['POST'])
@admin_required
def upload_file():
    upload_folder = current_app.config['UPLOAD_FOLDER']
    os.makedirs(upload_folder, exist_ok=True)

    safe_path = _resolve_subpath(request.form.get('path', ''))

    if safe_path and not is_safe_path(upload_folder, safe_path):
        abort(403)

    target_dir = os.path.join(upload_folder, safe_path) if safe_path else upload_folder
    os.makedirs(target_dir, exist_ok=True)

    files = request.files.getlist('file')
    if not files or all(f.filename == '' for f in files):
        return redirect(url_for('files.browse', path=safe_path))

    for file in files:
        if not file or not file.filename:
            continue

        parts = [secure_filename(p)
                 for p in file.filename.replace('\\', '/').split('/')
                 if p]
        if not parts:
            continue

        stored_name = '/'.join(([safe_path] + parts) if safe_path else parts)

        if not is_safe_path(upload_folder, stored_name):
            continue

        dest = os.path.join(upload_folder, stored_name)
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        file.save(dest)

        file_size = os.path.getsize(dest)
        existing  = File.query.filter_by(stored_name=stored_name).first()
        if existing:
            from datetime import datetime
            existing.file_size   = file_size
            existing.upload_time = datetime.utcnow()
        else:
            db.session.add(File(
                original_name=parts[-1],
                stored_name=stored_name,
                file_size=file_size,
            ))

    db.session.commit()
    log_activity(request.remote_addr, 'Upload', safe_path or '/', 'upload_file', 'Success')
    return redirect(url_for('files.browse', path=safe_path))


@files_bp.route('/delete/<int:file_id>', methods=['POST'])
@admin_required
def delete_file(file_id):
    file      = File.query.get_or_404(file_id)
    file_path = os.path.join(current_app.config['UPLOAD_FOLDER'], file.stored_name)

    # Remember which folder to return to before deleting the record
    folder = '/'.join(file.stored_name.replace('\\', '/').split('/')[:-1])

    if os.path.exists(file_path):
        os.remove(file_path)

    log_activity(request.remote_addr, 'Delete', file.stored_name, 'delete_file', 'Success')
    db.session.delete(file)
    db.session.commit()

    return redirect(url_for('files.browse', path=folder))


@files_bp.route('/rename/<int:file_id>', methods=['POST'])
@admin_required
def rename_file(file_id):
    file     = File.query.get_or_404(file_id)
    new_name = request.form.get('name', '').strip()

    if not new_name:
        return redirect(url_for('files.browse'))

    safe_name = secure_filename(new_name)
    if not safe_name:
        return redirect(url_for('files.browse'))

    old_path = os.path.join(current_app.config['UPLOAD_FOLDER'], file.stored_name)

    # Preserve subfolder, replace only the filename component
    parts       = file.stored_name.replace('\\', '/').split('/')
    parts[-1]   = safe_name
    new_stored  = '/'.join(parts)
    new_path    = os.path.join(current_app.config['UPLOAD_FOLDER'], new_stored)

    folder = '/'.join(parts[:-1])

    if os.path.exists(old_path) and not os.path.exists(new_path):
        os.rename(old_path, new_path)
        file.original_name = safe_name
        file.stored_name   = new_stored
        db.session.commit()
        log_activity(request.remote_addr, 'Rename',
                     f'{old_path.split(os.sep)[-1]} → {safe_name}',
                     'rename_file', 'Success')

    return redirect(url_for('files.browse', path=folder))


@files_bp.route('/download/<int:file_id>')
def download_file(file_id):
    file = File.query.get_or_404(file_id)
    path = os.path.join(current_app.config['UPLOAD_FOLDER'], file.stored_name)
    if not os.path.exists(path):
        return 'File not found', 404
    log_activity(request.remote_addr, 'Download', file.stored_name, 'download_file', '200')
    return send_file(path, as_attachment=True, download_name=file.original_name)


@files_bp.route('/stream/<int:file_id>')
def stream_file(file_id):
    file      = File.query.get_or_404(file_id)
    file_path = os.path.join(current_app.config['UPLOAD_FOLDER'], file.stored_name)
    if not os.path.exists(file_path):
        return 'File not found', 404

    file_size        = os.path.getsize(file_path)
    ext              = os.path.splitext(file.original_name)[1].lower()
    mimetype         = MIME_TYPES.get(ext, 'application/octet-stream')
    encoded_filename = quote(file.original_name)
    range_header     = request.headers.get('Range')

    if not range_header:
        def generate():
            with open(file_path, 'rb') as f:
                while chunk := f.read(8192):
                    yield chunk
        resp = Response(generate(), mimetype=mimetype)
        resp.headers['Content-Length']      = file_size
        resp.headers['Accept-Ranges']       = 'bytes'
        resp.headers['Content-Disposition'] = f'inline; filename="{encoded_filename}"'
        return resp

    match = re.match(r'bytes=(\d+)-(\d*)', range_header)
    if not match:
        return 'Invalid Range Header', 416

    start = int(match.group(1))
    end   = int(match.group(2)) if match.group(2) else file_size - 1

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
    resp.headers['Content-Range']       = f'bytes {start}-{end}/{file_size}'
    resp.headers['Content-Length']      = length
    resp.headers['Accept-Ranges']       = 'bytes'
    resp.headers['Content-Disposition'] = f'inline; filename="{encoded_filename}"'
    return resp


@files_bp.route('/stream_page/<int:file_id>')
def stream_page(file_id):
    file     = File.query.get_or_404(file_id)
    ext      = os.path.splitext(file.original_name)[1].lower()
    mimetype = MIME_TYPES.get(ext, 'application/octet-stream')

    if ext in PLAYER_NATIVE_VIDEO:
        player_type = 'video'
    elif ext in PLAYER_TS_VIDEO:
        player_type = 'ts'
    elif ext in PLAYER_NATIVE_AUDIO:
        player_type = 'audio'
    else:
        player_type = 'unsupported'

    return render_template('stream.html',
                           file_id=file_id,
                           file_name=file.original_name,
                           mimetype=mimetype,
                           player_type=player_type,
                           ext=ext)


@files_bp.route('/api/thumbnails/clear', methods=['POST'])
@admin_required
def clear_thumbnails():
    """Delete all cached thumbnails. They regenerate lazily on next view."""
    cleared = 0
    if os.path.isdir(THUMBNAIL_DIR):
        for fname in os.listdir(THUMBNAIL_DIR):
            try:
                os.remove(os.path.join(THUMBNAIL_DIR, fname))
                cleared += 1
            except OSError:
                pass

    log_activity(request.remote_addr, 'Clear Thumbnails', THUMBNAIL_DIR,
                 'clear_thumbnails', f'{cleared} removed')
    return {'status': 'ok', 'cleared': cleared}


@files_bp.route('/thumbnail/<int:file_id>')
def thumbnail(file_id):
    if not PILLOW_AVAILABLE:
        abort(501)

    file      = File.query.get_or_404(file_id)
    file_path = os.path.join(current_app.config['UPLOAD_FOLDER'], file.stored_name)

    if not os.path.exists(file_path):
        abort(404)

    ext = os.path.splitext(file.original_name)[1].lower()
    if ext not in IMAGE_EXTENSIONS and ext not in VIDEO_EXTENSIONS:
        abort(404)

    os.makedirs(THUMBNAIL_DIR, exist_ok=True)
    stat       = os.stat(file_path)
    thumb_hash = hashlib.md5(f"{file_path}:{stat.st_mtime}".encode()).hexdigest()
    thumb_path = os.path.join(THUMBNAIL_DIR, thumb_hash + '.webp')

    if os.path.exists(thumb_path):
        return send_file(thumb_path, mimetype='image/webp')

    try:
        if ext in IMAGE_EXTENSIONS:
            img = Image.open(file_path)
            try:
                rotations = {3: 180, 6: 270, 8: 90}
                orientation = img.getexif().get(274)
                if orientation in rotations:
                    img = img.rotate(rotations[orientation], expand=True)
            except Exception:
                pass
            img.thumbnail((96, 96))
            if img.mode not in ('RGB', 'RGBA'):
                img = img.convert('RGB')
            img.save(thumb_path, 'WEBP', quality=80)

        elif ext in VIDEO_EXTENSIONS:
            if not FFMPEG_PATH:
                abort(501)
            temp = os.path.join(THUMBNAIL_DIR, thumb_hash + '.jpg')
            result = subprocess.run(
                [FFMPEG_PATH, '-ss', '5', '-i', file_path,
                 '-frames:v', '1', '-q:v', '2', '-y', temp],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
            )
            if result.returncode != 0 or not os.path.exists(temp):
                abort(500)
            img = Image.open(temp)
            img.thumbnail((96, 96))
            if img.mode not in ('RGB', 'RGBA'):
                img = img.convert('RGB')
            img.save(thumb_path, 'WEBP', quality=80)
            os.remove(temp)
    except Exception:
        abort(500)

    return send_file(thumb_path, mimetype='image/webp')


@files_bp.route('/raw/<int:file_id>')
def raw_file(file_id):
    file = File.query.get_or_404(file_id)
    path = os.path.join(current_app.config['UPLOAD_FOLDER'], file.stored_name)
    if not os.path.exists(path):
        abort(404)
    return send_file(path)


@files_bp.route('/reader')
def reader():
    upload_folder = current_app.config['UPLOAD_FOLDER']
    safe_path     = _resolve_subpath(request.args.get('path', ''))

    if safe_path and not is_safe_path(upload_folder, safe_path):
        abort(403)

    full_path = os.path.join(upload_folder, safe_path) if safe_path else upload_folder
    if not os.path.isdir(full_path):
        abort(404)

    images = []
    for entry in os.scandir(full_path):
        if not entry.is_file():
            continue
        ext = os.path.splitext(entry.name)[1].lower()
        if ext not in IMAGE_EXTENSIONS:
            continue
        rel     = (os.path.join(safe_path, entry.name) if safe_path else entry.name).replace('\\', '/')
        db_file = _get_or_register(rel, entry.name, entry.stat().st_size)
        images.append({'name': entry.name, 'file_id': db_file.id})

    db.session.commit()
    images.sort(key=lambda x: natural_sort_key(x['name']))
    return render_template('reader.html', images=images, folder=safe_path)

# ============================================================
# FILE INFO  — metadata endpoint for the info popup
# ============================================================

def _fmt_duration(seconds):
    try:
        s = int(float(seconds))
        h, rem = divmod(s, 3600)
        m, sec = divmod(rem, 60)
        return f'{h}:{m:02d}:{sec:02d}' if h else f'{m}:{sec:02d}'
    except Exception:
        return '—'


def _ffprobe(path):
    try:
        r = subprocess.run(
            ['ffprobe', '-v', 'quiet', '-print_format', 'json',
             '-show_streams', '-show_format', path],
            capture_output=True, text=True, timeout=10
        )
        import json as _json
        return _json.loads(r.stdout)
    except Exception:
        return {}


def _video_meta(path):
    data = _ffprobe(path)
    if not data:
        return {}
    streams = data.get('streams', [])
    fmt     = data.get('format', {})
    video   = next((s for s in streams if s.get('codec_type') == 'video'), None)
    audio   = next((s for s in streams if s.get('codec_type') == 'audio'), None)
    meta    = {}

    dur = fmt.get('duration') or (video or {}).get('duration')
    if dur:
        meta['Duration'] = _fmt_duration(dur)

    if video:
        w, h = video.get('width'), video.get('height')
        if w and h:
            meta['Resolution'] = f'{w} × {h}'
        fps_str = video.get('r_frame_rate', '')
        if '/' in fps_str:
            num, den = fps_str.split('/')
            if int(den):
                meta['Frame Rate'] = f'{round(int(num)/int(den), 3)} fps'
        codec = video.get('codec_name', '').upper()
        if codec:
            meta['Video Codec'] = codec

    if audio:
        codec = audio.get('codec_name', '').upper()
        if codec:
            meta['Audio Codec'] = codec
        ch = audio.get('channels')
        if ch:
            meta['Audio'] = {1:'Mono', 2:'Stereo', 6:'5.1', 8:'7.1'}.get(ch, f'{ch}ch')

    br = fmt.get('bit_rate')
    if br:
        meta['Bitrate'] = f'{int(br)//1000} kbps'

    return meta


def _audio_meta(path):
    data = _ffprobe(path)
    if not data:
        return {}
    streams = data.get('streams', [])
    fmt     = data.get('format', {})
    audio   = next((s for s in streams if s.get('codec_type') == 'audio'), None)
    meta    = {}

    dur = fmt.get('duration') or (audio or {}).get('duration')
    if dur:
        meta['Duration'] = _fmt_duration(dur)

    if audio:
        codec = audio.get('codec_name', '').upper()
        if codec:
            meta['Codec'] = codec
        sr = audio.get('sample_rate')
        if sr:
            meta['Sample Rate'] = f'{int(sr):,} Hz'
        ch = audio.get('channels')
        if ch:
            meta['Channels'] = {1:'Mono', 2:'Stereo', 6:'5.1', 8:'7.1'}.get(ch, f'{ch}ch')

    br = fmt.get('bit_rate')
    if br:
        meta['Bitrate'] = f'{int(br)//1000} kbps'

    return meta


def _image_meta(path):
    try:
        from PIL import Image
        with Image.open(path) as img:
            return {'Dimensions': f'{img.width} × {img.height} px', 'Format': img.format or '—'}
    except Exception:
        return {}


_VIDEO_EXT = {'.mp4', '.mkv', '.webm', '.ts', '.m4v', '.avi', '.mov'}
_AUDIO_EXT = {'.mp3', '.flac', '.m4a', '.m4b', '.ogg', '.wav', '.aac'}
_IMAGE_EXT = {'.jpg', '.jpeg', '.png', '.gif', '.webp', '.bmp'}


@files_bp.route('/file/<int:file_id>/info')
def file_info(file_id):
    f    = File.query.get_or_404(file_id)
    path = os.path.join(current_app.config['UPLOAD_FOLDER'], f.stored_name)
    ext  = os.path.splitext(f.original_name)[1].lower()

    if ext in _VIDEO_EXT:
        meta = _video_meta(path)
    elif ext in _AUDIO_EXT:
        meta = _audio_meta(path)
    elif ext in _IMAGE_EXT:
        meta = _image_meta(path)
    else:
        meta = {}

    return jsonify({
        'name':          f.original_name,
        'size':          human_readable_size(f.file_size),
        'added':         f.upload_time.strftime('%B %d, %Y · %H:%M'),
        'has_thumbnail': ext in _IMAGE_EXT or ext in _VIDEO_EXT,
        'file_id':       file_id,
        'meta':          meta,
    })