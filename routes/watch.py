import time
import threading
from collections import defaultdict

from flask import Blueprint, request
from flask_socketio import join_room, emit

from extensions import socketio
from utils import update_viewer_info,  get_device_string

watch_bp = Blueprint('watch', __name__)

# --- Session state ---
watch_sessions  = {}
watch_sequence  = defaultdict(int)
watch_lock      = threading.Lock()

# --- Rate limiting ---
client_last_update  = defaultdict(float)
client_update_lock  = threading.Lock()
RATE_LIMIT_SECONDS  = 0.5

# --- Viewer tracking ---
viewers_data  = defaultdict(dict)
viewers_lock  = threading.Lock()


@watch_bp.route('/watch/action/<int:file_id>', methods=['POST'])
def watch_action(file_id):
    data = request.get_json()
    if not data or 'action' not in data:
        return {'error': 'missing action'}, 400

    action     = data['action']
    now        = time.time()
    client_ip  = request.remote_addr or 'unknown'

    # Compute RTT from client_time (client sends Date.now()/1000). Falls back to 0.
    client_time = data.get('client_time')
    latency_ms  = round((now - client_time) * 1000) if client_time else 0
    
    # Register the baseline connection status
    update_viewer_info(viewers_data, viewers_lock, file_id, client_ip, latency_ms)
    
    # Store the device context safely inside our active viewers dictionary
    device_name = get_device_string()
    with viewers_lock:
        if file_id in viewers_data and client_ip in viewers_data[file_id]:
            viewers_data[file_id][client_ip]['device'] = device_name

    rate_key = f'{client_ip}:{file_id}'
    with client_update_lock:
        if now - client_last_update.get(rate_key, 0) < RATE_LIMIT_SECONDS:
            return {'status': 'rate_limited'}, 429
        client_last_update[rate_key] = now

    with watch_lock:
        sess = watch_sessions.get(file_id)
        if not sess:
            sess = {'playing': False, 'position': 0.0, 'updated_at': now,
                    'last_action': 'pause', 'seq': 0}
            watch_sessions[file_id] = sess

        if action == 'seek':
            sess['position']    = float(data.get('position', 0.0))
            sess['updated_at']  = now
            sess['last_action'] = 'seek'

        elif action == 'play':
            sess['playing']     = True
            sess['position']    = float(data.get('position', sess['position']))
            sess['updated_at']  = now
            sess['last_action'] = 'play'

        elif action == 'pause':
            sess['playing']     = False
            sess['position']    = float(data.get('position', sess['position']))
            sess['updated_at']  = now
            sess['last_action'] = 'pause'

        elif action == 'heartbeat':
            sess['last_active'] = now
            return {'status': 'ok'}

        else:
            return {'error': 'unknown action'}, 400

        watch_sequence[file_id] += 1
        sess['seq']         = watch_sequence[file_id]
        sess['last_active'] = now

        payload = {
            'playing':    sess['playing'],
            'position':   sess['position'],
            'updated_at': sess['updated_at'],
            'last_action': sess['last_action'],
            'seq':        sess['seq'],
            'server_now': now,
        }

    socketio.emit('watch_update', payload, room=f'watch_{file_id}')
    return {'status': 'ok'}


@watch_bp.route('/watch/viewers/<int:file_id>')
def watch_viewers(file_id):
    with viewers_lock:
        now    = time.time()
        active = {
            ip: info for ip, info in viewers_data.get(file_id, {}).items()
            if now - info.get('last_seen', 0) < 30
        }
    return {
        'count': len(active),
        'viewers': [
            {
                'ip':             ip,
                'latency':        round(info['latency'], 1),
                'active_seconds': round(now - info.get('first_seen', now), 1),
                'device': info.get('device', 'Unknown Device')
            }
            for ip, info in active.items()
        ],
    }


@socketio.on('join_watch')
def join_watch(data):
    file_id = data.get('file_id')
    if file_id is None:
        return

    join_room(f'watch_{file_id}')
    now       = time.time()
    client_ip = request.remote_addr or 'unknown'

    # Register viewer immediately on join
    update_viewer_info(viewers_data, viewers_lock, file_id, client_ip, 0)
    
    # Parse and store user-agent context for the initial socket connection
    device_name = get_device_string()
    with viewers_lock:
        if file_id in viewers_data and client_ip in viewers_data[file_id]:
            viewers_data[file_id][client_ip]['device'] = device_name

    with watch_lock:
        sess = watch_sessions.get(file_id)
        if not sess:
            sess = {'playing': False, 'position': 0.0, 'updated_at': now,
                    'last_action': 'pause', 'seq': 0}
            watch_sessions[file_id] = sess

    emit('watch_update', {
        'playing':    sess['playing'],
        'position':   sess['position'],
        'updated_at': sess['updated_at'],
        'last_action': sess['last_action'],
        'seq':        sess['seq'],
        'server_now': now,
    })