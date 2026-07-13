import hmac
import time
from collections import defaultdict

from flask import (Blueprint, render_template, request,
                   redirect, url_for, session, current_app)

from utils import is_admin, log_activity

auth_bp = Blueprint('auth', __name__)

# ============================================================
# RATE LIMITER  (in-memory, per-IP)
# ============================================================

_login_attempts: dict[str, list[float]] = defaultdict(list)
_LOGIN_MAX = 10
_LOGIN_WIN = 300   # 5 minutes


def _is_rate_limited(ip: str) -> bool:
    now  = time.time()
    hits = [t for t in _login_attempts[ip] if now - t < _LOGIN_WIN]
    _login_attempts[ip] = hits
    return len(hits) >= _LOGIN_MAX


def _record_attempt(ip: str) -> None:
    _login_attempts[ip].append(time.time())


# ============================================================
# ROUTES
# ============================================================

@auth_bp.route('/login', methods=['GET', 'POST'])
def login():
    if is_admin():
        return redirect(url_for('files.browse'))

    error = None

    if request.method == 'POST':
        ip             = request.remote_addr
        admin_password = current_app.config['ADMIN_PASSWORD']

        if _is_rate_limited(ip):
            error = 'Too many failed attempts — try again in 5 minutes.'
            log_activity(ip, 'Login', '/login', 'login', 'Rate limited')
        else:
            password = request.form.get('password', '')
            if hmac.compare_digest(password, admin_password):
                session.permanent   = True
                session['is_admin'] = True
                log_activity(ip, 'Login', '/login', 'login', 'Success')
                return redirect(url_for('files.browse'))
            else:
                _record_attempt(ip)
                error = 'Incorrect password.'
                log_activity(ip, 'Login', '/login', 'login', 'Failed')

    return render_template('login.html', error=error)


@auth_bp.route('/logout')
def logout():
    log_activity(request.remote_addr, 'Logout', '/logout', 'logout', 'Success')
    session.pop('is_admin', None)
    return redirect(url_for('files.browse'))