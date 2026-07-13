import os
import time
import psutil

from flask import Blueprint, render_template, jsonify, current_app

from utils import admin_required, human_readable_size, activity_log
from routes.watch import viewers_data, viewers_lock

dashboard_bp = Blueprint('dashboard', __name__)


@dashboard_bp.route('/dashboard')
@admin_required
def dashboard():
    return render_template('dashboard.html')


@dashboard_bp.route('/api/stats')
@admin_required
def api_stats():

    # --- System ---
    cpu    = psutil.cpu_percent(interval=None)
    mem    = psutil.virtual_memory()
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
            'cpu':       round(cpu, 1),
            'ram_used':  mem.used,
            'ram_total': mem.total,
            'uptime':    int(uptime),
        },
        'storage': {
            'used_hr': human_readable_size(total_size),
            'files':   file_count,
        },
        'viewers': viewer_list,
        'logs':    list(activity_log),
    })