# LocalShare

LocalShare is a lightweight homeserver management interface that is made with simplicity and performance in mind.
A lightweight, web-based file-sharing application designed for temporary uploads and downloads with real-time streaming support. Built using Flask, SQLite, HTML, CSS, and JavaScript, this project enables users to upload files, stream media (e.g., MP4, MKV, MP3, FLAC), and download them within a 24-hour window.

## Features
- Upload files with real-time progress tracking (up to 2 GB limit, adjustable).
- Download files with a green-themed button.
- Stream video and audio files (MP4, MKV, MP3, FLAC, WebM, OGG).
- Automatic file cleanup after 24 hours.
- Cross-device compatibility with troubleshooting for streaming issues (e.g., black frames, playback interruptions).

## Installation
1. **Clone the repository**:
   ```bash
   git clone https://github.com/Hexanol777/LocalShare.git
   cd LocalShare
   ```


2. **Install dependencies**:
```bash
   pip install -r requirements.txt
```

3. **Set up the environment**:
   - Ensure the `instance/` and `uploads/` directories exist (created automatically on first run).
   - No additional configuration is needed for the SQLite database (`instance/database.db`).

4. **Run the application**:
   ```bash
   python app.py
   ```
   Access the server at `http://0.0.0.0:5000` or your local IP (e.g., `http://192.168.1.100:5000`).


## Configuration
- **Streamable Extensions**: Edit the `STREAMABLE_EXTENSIONS` list in `app.py` to add or remove supported file types (e.g., `['.mp4', '.mkv', '.mp3', '.flac', '.webm', '.ogg']`).
- **MIME Types**: Update the `mime_types` dictionary in the `/stream/<file_id>` route to support new extensions with appropriate MIME types.
- **Port**: Change the `port` in `app.run(host='0.0.0.0', port=5000)` if needed.

