import sqlite3

DB_NAME = "app_data.db"

def init_db():
    """Initializes the database and creates tables if they don't exist."""
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()

    # Create a table for storing original video file information
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS video_files (
            transcript_id TEXT PRIMARY KEY,
            original_path TEXT NOT NULL,
            filename TEXT NOT NULL,
            uploaded_at TEXT NOT NULL
        )
    ''')

    # Create a table for storing video processing job status
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS video_jobs (
            job_id TEXT PRIMARY KEY,
            transcript_id TEXT NOT NULL,
            status TEXT NOT NULL,
            progress INTEGER NOT NULL,
            message TEXT,
            created_at TEXT NOT NULL,
            output_path TEXT,
            error TEXT,
            FOREIGN KEY (transcript_id) REFERENCES video_files (transcript_id)
        )
    ''')

    conn.commit()
    conn.close()
