from datetime import datetime
from extensions import db


class File(db.Model):
    id            = db.Column(db.Integer, primary_key=True)
    original_name = db.Column(db.String(255), nullable=False)
    stored_name   = db.Column(db.String(255), nullable=False)
    upload_time   = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    file_size     = db.Column(db.Integer, nullable=False)


class ChatMessage(db.Model):
    id         = db.Column(db.Integer, primary_key=True)
    sender_ip  = db.Column(db.String(45), nullable=False)
    content    = db.Column(db.Text, nullable=False)
    timestamp  = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)