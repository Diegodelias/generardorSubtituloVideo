import time
import requests
from dotenv import load_dotenv
import subprocess
import json
import uuid
from datetime import datetime
import tempfile
import os
import threading
import sqlite3
from database import init_db, DB_NAME

from flask import Flask, render_template, request, jsonify, send_file

load_dotenv()
ASSEMBLYAI_KEY = os.getenv("ASSEMBLYAI_KEY")
UPLOAD_URL = "https://api.assemblyai.com/v2/upload"
HEADERS = {"authorization": ASSEMBLYAI_KEY} if ASSEMBLYAI_KEY else {}

# Initialize the database when the app starts
init_db()

app = Flask(__name__)

def get_db_conn():
    """Creates a database connection. check_same_thread=False is safe for this app's usage pattern."""
    conn = sqlite3.connect(DB_NAME, check_same_thread=False)
    conn.row_factory = sqlite3.Row  # This allows accessing columns by name
    return conn

@app.route("/upload", methods=["POST"])
def upload():
    if "videoFile" not in request.files:
        return jsonify({"error": "No se envió archivo"}), 400
    file = request.files["videoFile"]
    if file.filename == "":
        return jsonify({"error": "Nombre de archivo vacío"}), 400
    
    temp_dir = tempfile.gettempdir()
    save_path = os.path.join(temp_dir, file.filename)
    file.save(save_path)

    if not ASSEMBLYAI_KEY:
        return jsonify({"error": "Falta ASSEMBLYAI_KEY en .env"}), 500

    def read_file(fn, chunk_size=5_242_880):
        with open(fn, "rb") as f:
            while True:
                data = f.read(chunk_size)
                if not data:
                    break
                yield data

    try:
        r = requests.post(UPLOAD_URL, headers=HEADERS, data=read_file(save_path))
        r.raise_for_status()
        upload_url = r.json().get("upload_url")

        transcribe_url = "https://api.assemblyai.com/v2/transcript"
        # Speaker labels are now always enabled as the UI switch has been removed.
        payload = {"audio_url": upload_url, "speaker_labels": True}
        lang = request.form.get("language")
        if lang in {"es", "en"}:
            payload["language_code"] = lang
        
        r2 = requests.post(transcribe_url, headers=HEADERS, json=payload)
        r2.raise_for_status()
        transcript_id = r2.json().get("id")
        status = r2.json().get("status")

        # Store video file info in the database
        conn = get_db_conn()
        conn.execute(
            "INSERT INTO video_files (transcript_id, original_path, filename, uploaded_at) VALUES (?, ?, ?, ?)",
            (transcript_id, save_path, file.filename, datetime.now().isoformat())
        )
        conn.commit()
        conn.close()

        return (
            jsonify(
                {
                    "message": "Archivo subido y transcripción iniciada",
                    "upload_url": upload_url,
                    "transcript_id": transcript_id,
                    "status": status,
                }
            ),
            200,
        )
    except Exception as e:
        return jsonify({"error": f"Error al subir a AssemblyAI: {str(e)}"}), 500

# Ruta principal sirve upload.html desde /templates
@app.route("/")
def index():
    return render_template("upload.html")

@app.route("/transcription_status", methods=["GET"])
def transcription_status():
    transcript_id = request.args.get("transcript_id")
    if not transcript_id:
        return jsonify({"error": "Falta transcript_id"}), 400
    
    transcribe_url = f"https://api.assemblyai.com/v2/transcript/{transcript_id}"
    try:
        r = requests.get(transcribe_url, headers=HEADERS)
        r.raise_for_status()
        return jsonify(r.json()), 200
    except Exception as e:
        return jsonify({"error": f"Error al consultar AssemblyAI: {str(e)}"}), 500

@app.route("/processing")
def processing():
    return render_template("processing.html")

@app.route("/results")
def results():
    return render_template("results.html")

@app.route("/burn_subtitles/<transcript_id>", methods=["POST"])
def burn_subtitles(transcript_id):
    """Start the subtitle burning process"""
    if not ASSEMBLYAI_KEY:
        return jsonify({"error": "Falta ASSEMBLYAI_KEY en .env"}), 500

    conn = get_db_conn()
    video_file = conn.execute("SELECT * FROM video_files WHERE transcript_id = ?", (transcript_id,)).fetchone()
    conn.close()

    if not video_file:
        return jsonify({"error": "Video original no encontrado"}), 404

    if not os.path.exists(video_file["original_path"]):
        return jsonify({"error": "Archivo de video no disponible"}), 404

    # Generate unique job ID
    job_id = str(uuid.uuid4())
    job_created_at = datetime.now().isoformat()

    # Initialize job status in the database
    conn = get_db_conn()
    conn.execute(
        "INSERT INTO video_jobs (job_id, transcript_id, status, progress, message, created_at) VALUES (?, ?, ?, ?, ?, ?)",
        (job_id, transcript_id, "started", 0, "Iniciando procesamiento de video...", job_created_at)
    )
    conn.commit()
    conn.close()

    try:
        thread = threading.Thread(target=process_video_with_subtitles, args=(job_id, transcript_id))
        thread.daemon = True
        thread.start()

        return jsonify({
            "job_id": job_id,
            "message": "Procesamiento de video iniciado",
            "status": "processing"
        }), 202
    except Exception as e:
        conn = get_db_conn()
        conn.execute("UPDATE video_jobs SET status = 'error', error = ? WHERE job_id = ?", (str(e), job_id))
        conn.commit()
        conn.close()
        return jsonify({"error": f"Error al iniciar procesamiento: {str(e)}"}), 500

@app.route("/video_status/<job_id>", methods=["GET"])
def video_status(job_id):
    """Check the status of video processing"""
    conn = get_db_conn()
    job = conn.execute("SELECT * FROM video_jobs WHERE job_id = ?", (job_id,)).fetchone()
    conn.close()

    if not job:
        return jsonify({"error": "Job no encontrado"}), 404

    return jsonify(dict(job))

@app.route("/get_job_by_transcript/<transcript_id>", methods=["GET"])
def get_job_by_transcript(transcript_id):
    """Find the latest job_id for a given transcript_id"""
    conn = get_db_conn()
    # Find the most recent job for this transcript ID, as a user might re-process the same video
    job = conn.execute(
        "SELECT * FROM video_jobs WHERE transcript_id = ? ORDER BY created_at DESC LIMIT 1",
        (transcript_id,)
    ).fetchone()
    conn.close()

    if not job:
        return jsonify({"error": "No se encontró un trabajo para esa transcripción"}), 404

    return jsonify(dict(job))

@app.route("/download_video/<job_id>")
def download_video(job_id):
    """Download the processed video with burned subtitles"""
    conn = get_db_conn()
    job = conn.execute("SELECT * FROM video_jobs WHERE job_id = ?", (job_id,)).fetchone()

    if not job:
        conn.close()
        return jsonify({"error": "Job no encontrado"}), 404

    if job["status"] != "completed" or not job["output_path"]:
        conn.close()
        return jsonify({"error": "Video aún no está listo"}), 400

    if not os.path.exists(job["output_path"]):
        conn.close()
        return jsonify({"error": "Archivo de video no encontrado"}), 404

    video_file = conn.execute("SELECT filename FROM video_files WHERE transcript_id = ?", (job["transcript_id"],)).fetchone()
    conn.close()

    output_filename = "video_con_subtitulos.mp4"
    if video_file:
        name, ext = os.path.splitext(video_file["filename"])
        output_filename = f"{name}_with_subtitles{ext}"

    return send_file(
        job["output_path"],
        as_attachment=True,
        download_name=output_filename,
        mimetype="video/mp4"
    )

def process_video_with_subtitles(job_id, transcript_id):
    """Process video with FFmpeg to burn subtitles"""
    conn = get_db_conn()
    video_info = conn.execute("SELECT * FROM video_files WHERE transcript_id = ?", (transcript_id,)).fetchone()
    conn.close()

    if not video_info:
        print(f"[VIDEO_PROCESSING] Error: Video info for {transcript_id} not found.")
        return

    def update_job_status(status, message, progress, error=None, output_path=None):
        conn = get_db_conn()
        conn.execute(
            "UPDATE video_jobs SET status=?, message=?, progress=?, error=?, output_path=? WHERE job_id=?",
            (status, message, progress, error, output_path, job_id)
        )
        conn.commit()
        conn.close()

    try:
        update_job_status("downloading_srt", "Descargando archivo SRT...", 10)

        srt_url = f"https://api.assemblyai.com/v2/transcript/{transcript_id}/srt"
        r = requests.get(srt_url, headers=HEADERS)
        r.raise_for_status()

        temp_dir = tempfile.gettempdir()
        srt_path = os.path.join(temp_dir, f"{job_id}.srt")
        with open(srt_path, 'w', encoding='utf-8') as f:
            f.write(r.text)

        update_job_status("processing", "Procesando video con subtítulos...", 30)

        original_path = video_info["original_path"]
        name, ext = os.path.splitext(original_path)
        output_path = f"{name}_with_subtitles{ext}"

        srt_path_escaped = srt_path.replace('\\', '/').replace(':', '\\:')

        cmd = [
            "ffmpeg",
            "-i", original_path,
            "-vf", f"subtitles='{srt_path_escaped}'",
            "-c:a", "copy",
            "-y",
            output_path
        ]

        update_job_status("processing", "Ejecutando FFmpeg...", 50)

        result = subprocess.run(cmd, capture_output=True, text=True, check=False)

        if result.returncode != 0:
            raise Exception(f"FFmpeg error: {result.stderr}")

        update_job_status("completed", "Video procesado exitosamente", 100, output_path=output_path)

        try:
            os.remove(srt_path)
        except:
            pass

    except Exception as e:
        error_message = str(e)
        update_job_status("error", f"Error: {error_message}", 0, error=error_message)
        print(f"[VIDEO_PROCESSING] Error processing job {job_id}: {error_message}")

@app.route("/download_srt/<transcript_id>")
def download_srt(transcript_id):
    if not ASSEMBLYAI_KEY:
        return jsonify({"error": "Falta ASSEMBLYAI_KEY en .env"}), 500

    srt_url = f"https://api.assemblyai.com/v2/transcript/{transcript_id}/srt"
    try:
        r = requests.get(srt_url, headers=HEADERS)
        r.raise_for_status()

        response = app.make_response(r.text)
        response.headers["Content-Disposition"] = f"attachment; filename={transcript_id}.srt"
        response.headers["Content-Type"] = "text/plain"
        return response
    except requests.exceptions.RequestException as e:
        return jsonify({"error": f"Error al descargar SRT de AssemblyAI: {str(e)}"}), 500
    except Exception as e:
        return jsonify({"error": f"Error inesperado: {str(e)}"}), 500

if __name__ == "__main__":
    app.run(debug=True)
