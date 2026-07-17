import os
import time
import psutil
from collections import deque

from flask import Blueprint, render_template, jsonify, make_response, current_app

from utils import admin_required, human_readable_size, activity_log
from routes.watch import viewers_data, viewers_lock

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

    return jsonify({
        'system': {
            'cpu':       cpu_norm,
            'ram_used':  ram_proc,    # LocalShare RSS in bytes
            'ram_total': ram_total,   # system total in bytes (for context bar)
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