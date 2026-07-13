import os
import time
import psutil

from flask import Blueprint, render_template, jsonify, make_response, current_app

from utils import admin_required, human_readable_size, activity_log
from routes.watch import viewers_data, viewers_lock

dashboard_bp = Blueprint('dashboard', __name__)

# Grab handle to current process once at startup — avoids repeated PID lookups
_proc = psutil.Process()


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

    uptime = time.time() - psutil.boot_time()

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