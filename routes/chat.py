from flask import Blueprint, render_template, request

from extensions import db
from models import ChatMessage

chat_bp = Blueprint('chat', __name__)


@chat_bp.route('/chat')
def chat():
    return render_template('chat.html')


@chat_bp.route('/chat/send', methods=['POST'])
def chat_send():
    data = request.get_json()
    if not data or not data.get('message'):
        return {'status': 'error'}, 400

    msg = data['message'].strip()
    if not msg:
        return {'status': 'ok'}

    db.session.add(ChatMessage(
        sender_ip=request.remote_addr or 'unknown',
        content=msg,
    ))
    db.session.commit()
    return {'status': 'ok'}


@chat_bp.route('/chat/messages')
def chat_messages():
    since_id = request.args.get('since', type=int, default=0)
    messages = (
        ChatMessage.query
        .filter(ChatMessage.id > since_id)
        .order_by(ChatMessage.id.asc())
        .all()
    )
    return {
        'messages': [
            {
                'id': m.id,
                'sender': f'{m.sender_ip}:',
                'content': m.content,
                'timestamp': m.timestamp.isoformat(),
            }
            for m in messages
        ]
    }