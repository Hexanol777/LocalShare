import os
import time
import psutil
from collections import deque

from flask import Blueprint, render_template, jsonify, make_response, current_app, request

from utils import admin_required, human_readable_size, activity_log, log_activity
from routes.watch import viewers_data, viewers_lock, watch_sessions, watch_lock

dashboard_bp = Blueprint('dashboard', __name__)

# Grab handle to current process once at startup — avoids repeated PID lookups
_proc = psutil.Process()

# --- Network throughput tracking ---
# psutil.net_io_counters() returns cumulative byte counters, so we diff
# against the previous poll to get an instantaneous rate. System-wide
# (not per-process) since there's no reliable per-process network
# counter cross-platform.
_net_last_counters = psutil.net_io_counters()
_net_last_time     = time.time()
_net_history        = deque(maxlen=30)   # ~2.5 min of samples at 4s poll interval


@dashboard_bp.route('/dashboard')
@admin_required
def dashboard():
    return render_template('dashboard.html')


@dashboard_bp.route('/api/stats')
@admin_required
def api_stats():

    # --- LocalShare process stats (not system-wide) ---
    # cpu_percent(interval=None) is non-blocking; returns usage since last call.
    # Divide by cpu_count() to normalise across cores → 0-100 % of total capacity.
    cpu_raw   = _proc.cpu_percent(interval=None)
    cpu_norm  = round(cpu_raw / psutil.cpu_count(), 1)

    # RSS = physical RAM pages actually mapped by this process
    ram_proc  = _proc.memory_info().rss
    ram_total = psutil.virtual_memory().total   # system total for context/bar


    uptime = time.time() - _proc.create_time()

    # --- Storage: walk UPLOAD_FOLDER ---
    upload_folder = current_app.config['UPLOAD_FOLDER']
    total_size    = 0
    file_count    = 0
    for dirpath, _, filenames in os.walk(upload_folder):
        for fname in filenames:
            try:
                total_size += os.path.getsize(os.path.join(dirpath, fname))
                file_count += 1
            except OSError:
                pass

    # --- Get hard drive storage metrics for the drive containing uploads
    try:
        disk_info = psutil.disk_usage(upload_folder)
        disk_total = disk_info.total
        disk_free = disk_info.free
        disk_percent = disk_info.percent
    except OSError:
        disk_total = disk_free = disk_percent = 0

    # --- Network throughput (system-wide, rate since last poll) ---
    global _net_last_counters, _net_last_time

    net_now      = psutil.net_io_counters()
    net_now_time = time.time()
    net_dt       = max(net_now_time - _net_last_time, 0.001)

    upload_bps   = max((net_now.bytes_sent - _net_last_counters.bytes_sent) / net_dt, 0)
    download_bps = max((net_now.bytes_recv - _net_last_counters.bytes_recv) / net_dt, 0)

    _net_last_counters = net_now
    _net_last_time     = net_now_time

    _net_history.append({
        't':    int(net_now_time),
        'up':   round(upload_bps),
        'down': round(download_bps),
    })

    # --- Active viewers (last seen within 30 s) ---
    now         = time.time()
    viewer_list = []
    with viewers_lock:
        for file_id, viewers in viewers_data.items():
            for ip, info in viewers.items():
                if now - info.get('last_seen', 0) < 30:
                    viewer_list.append({
                        'ip':      ip.replace('::ffff:', ''),
                        'file_id': file_id,
                        'latency': round(info.get('latency', 0), 1),
                        'device': info.get('device', 'Unknown Device')
                    })

    # --- Ops-card metrics (lightweight; computed every poll) ---
    from routes.files import THUMBNAIL_DIR
    from extensions import db
    from models import File

    # Thumbnail cache
    thumb_count, thumb_size = 0, 0
    if os.path.isdir(THUMBNAIL_DIR):
        for fname in os.listdir(THUMBNAIL_DIR):
            try:
                thumb_size += os.path.getsize(os.path.join(THUMBNAIL_DIR, fname))
                thumb_count += 1
            except OSError:
                pass

    # Orphan files — only meaningful in uploads (cleanup-enabled) mode
    orphan_count = 0
    if current_app.config.get('CLEANUP_ENABLED', False):
        try:
            db_names   = {f.stored_name for f in File.query.with_entities(File.stored_name).all()}
            disk_names = set(os.listdir(upload_folder))
            orphan_count = len(disk_names - db_names)
        except Exception:
            pass

    # Active watch rooms and total connected peers
    with watch_lock:
        room_count = len(watch_sessions)
    with viewers_lock:
        peer_count = sum(len(v) for v in viewers_data.values())

    return jsonify({
        'system': {
            'cpu':       cpu_norm,
            'ram_used':  ram_proc,
            'ram_total': ram_total,
            'uptime':    int(uptime),
        },
        'storage': {
            'used_hr': human_readable_size(total_size),
            'files':   file_count,
            'free_hr': human_readable_size(disk_free),
            'total_hr': human_readable_size(disk_total),
            'percent': disk_percent,
        },
        'network': {
            'upload_bps':   round(upload_bps),
            'download_bps': round(download_bps),
            'history':      list(_net_history),
        },
        'ops': {
            'thumb_count':   thumb_count,
            'thumb_size_hr': human_readable_size(thumb_size),
            'log_count':     len(activity_log),
            'orphan_count':  orphan_count,
            'room_count':    room_count,
            'peer_count':    peer_count,
        },
        'viewers': viewer_list,
        'logs':    list(activity_log),
    })


@dashboard_bp.route('/api/logs/dump')
@admin_required
def logs_dump():
    """Return the current activity log as a downloadable JSON file."""
    import json
    from datetime import datetime

    payload = {
        'exported_at': datetime.utcnow().isoformat() + 'Z',
        'count':       len(activity_log),
        'entries':     list(activity_log),
    }

    filename = f"localshare-log-{datetime.utcnow().strftime('%Y%m%d-%H%M%S')}.json"
    resp = make_response(json.dumps(payload, indent=2))
    resp.headers['Content-Type']        = 'application/json'
    resp.headers['Content-Disposition'] = f'attachment; filename="{filename}"'
    return resp

# ============================================================
# SYSTEM OPERATIONS  — four POST endpoints, all admin-only
# ============================================================

@dashboard_bp.route('/admin/api/clear-thumbnails', methods=['POST'])
@admin_required
def ops_clear_thumbnails():
    """Purge all cached thumbnail files. They regenerate lazily on next view."""
    from routes.files import THUMBNAIL_DIR

    cleared, size = 0, 0
    if os.path.isdir(THUMBNAIL_DIR):
        for fname in os.listdir(THUMBNAIL_DIR):
            fpath = os.path.join(THUMBNAIL_DIR, fname)
            try:
                size += os.path.getsize(fpath)
                os.remove(fpath)
                cleared += 1
            except OSError:
                pass

    log_activity(request.remote_addr, 'Purge Thumbnails', THUMBNAIL_DIR,
                 'ops_clear_thumbnails', f'{cleared} removed')
    return jsonify({'status': 'ok', 'cleared': cleared,
                    'thumb_count': 0, 'thumb_size_hr': '0.00 B'})


@dashboard_bp.route('/admin/api/flush-logs', methods=['POST'])
@admin_required
def ops_flush_logs():
    """Clear the in-memory activity log ring buffer."""
    count = len(activity_log)
    activity_log.clear()
    # Log the flush itself so the buffer isn't completely empty after the op
    log_activity(request.remote_addr, 'Flush Logs', '/admin/api/flush-logs',
                 'ops_flush_logs', f'{count} entries cleared')
    return jsonify({'status': 'ok', 'log_count': 1})


@dashboard_bp.route('/admin/api/clean-orphans', methods=['POST'])
@admin_required
def ops_clean_orphans():
    """
    Remove files present on disk but absent from the database.
    Only runs in uploads (cleanup-enabled) mode; safe no-ops otherwise.
    """
    if not current_app.config.get('CLEANUP_ENABLED', False):
        return jsonify({'status': 'skipped', 'reason': 'custom folder mode',
                        'orphan_count': 0})

    from extensions import db
    from models import File

    upload_folder = current_app.config['UPLOAD_FOLDER']
    removed = 0
    try:
        db_names   = {f.stored_name for f in File.query.with_entities(File.stored_name).all()}
        disk_names = set(os.listdir(upload_folder))
        orphans    = disk_names - db_names
        for fname in orphans:
            try:
                os.remove(os.path.join(upload_folder, fname))
                removed += 1
            except OSError:
                pass
    except Exception as e:
        return jsonify({'status': 'error', 'detail': str(e)}), 500

    log_activity(request.remote_addr, 'Clean Orphans', upload_folder,
                 'ops_clean_orphans', f'{removed} removed')
    return jsonify({'status': 'ok', 'removed': removed, 'orphan_count': 0})


@dashboard_bp.route('/admin/api/reset-rooms', methods=['POST'])
@admin_required
def ops_reset_rooms():
    """
    Clear all active Watch Together sessions and viewer presence records.
    Connected clients will re-sync automatically on their next action.
    """
    with watch_lock:
        room_count = len(watch_sessions)
        watch_sessions.clear()

    with viewers_lock:
        peer_count = sum(len(v) for v in viewers_data.values())
        viewers_data.clear()

    log_activity(request.remote_addr, 'Reset Rooms', '/admin/api/reset-rooms',
                 'ops_reset_rooms', f'{room_count} rooms, {peer_count} peers cleared')
    return jsonify({'status': 'ok', 'room_count': 0, 'peer_count': 0})