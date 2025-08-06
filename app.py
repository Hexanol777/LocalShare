import os
import uuid
from datetime import datetime, timedelta
from flask import Flask, render_template, request, redirect, url_for, send_from_directory
from flask_sqlalchemy import SQLAlchemy
from apscheduler.schedulers.background import BackgroundScheduler

app = Flask(__name__)
app.config['UPLOAD_FOLDER'] = 'uploads'
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///database.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['MAX_CONTENT_LENGTH'] = 2000 * 1024 * 1024  # 2GB upload limit


db = SQLAlchemy(app)

# Ensure upload folder exists
if not os.path.exists(app.config['UPLOAD_FOLDER']):
    os.makedirs(app.config['UPLOAD_FOLDER'])

# Database model
class File(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    original_name = db.Column(db.String(255), nullable=False)
    stored_name = db.Column(db.String(255), nullable=False)
    upload_time = db.Column(db.DateTime, nullable=False, default=datetime.now)

# Routes
@app.route('/')
def index():
    recent_files = File.query.filter(File.upload_time >= datetime.now() - timedelta(hours=24)).order_by(File.upload_time.desc()).all()
    return render_template('index.html', files=recent_files)

@app.route('/upload', methods=['POST'])
def upload_file():
    if 'file' not in request.files:
        return redirect(url_for('index'))
    file = request.files['file']
    if file.filename == '':
        return redirect(url_for('index'))
    if file:
        stored_name = str(uuid.uuid4()) + os.path.splitext(file.filename)[1]
        file.save(os.path.join(app.config['UPLOAD_FOLDER'], stored_name))
        new_file = File(original_name=file.filename, stored_name=stored_name)
        db.session.add(new_file)
        db.session.commit()
        return redirect(url_for('index'))

@app.route('/download/<int:file_id>')
def download_file(file_id):
    file = File.query.get_or_404(file_id)
    return send_from_directory(app.config['UPLOAD_FOLDER'], file.stored_name, as_attachment=True, download_name=file.original_name)

# Cleanup function
def cleanup_old_files():
    with app.app_context():
        old_files = File.query.filter(File.upload_time < datetime.utcnow() - timedelta(hours=24)).all()
        for file in old_files:
            file_path = os.path.join(app.config['UPLOAD_FOLDER'], file.stored_name)
            if os.path.exists(file_path):
                os.remove(file_path)
            db.session.delete(file)
        db.session.commit()

# Schedule cleanup
scheduler = BackgroundScheduler()
scheduler.add_job(cleanup_old_files, 'interval', hours=1)
scheduler.start()

if __name__ == '__main__':
    with app.app_context():
        db.create_all()
    app.run(host='0.0.0.0', port=5000)