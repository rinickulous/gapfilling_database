import os
import sys
import traceback
import mariadb
from pathlib import Path
from datetime import datetime # Import datetime for footer year

from flask import (
    Flask, render_template, request, jsonify,
    url_for, send_from_directory, abort, current_app
)
from flask_cors import CORS
from werkzeug.utils import secure_filename

# --- Configuration ---

BASE_DIR = Path(__file__).parent.resolve()
UPLOAD_FOLDER = BASE_DIR / "uploads"
UPLOAD_FOLDER.mkdir(exist_ok=True, parents=True) # Create uploads folder if it doesn't exist
ALLOWED_EXTENSIONS = {".xml"}

# --- Hardcoded Database Credentials (as requested) ---
# WARNING: Hardcoding credentials is NOT recommended for production environments.
# Consider using environment variables or configuration files instead.
DB_CONFIG = {
    "host":     "bioed-new.bu.edu",
    "port":     4253,
    "user":     "npetruni",
    "password": "moesIsgooD1125##",
    "database": "Team11",
}

# --- App & DB Initialization ---

app = Flask(__name__)
# If your frontend and backend are served from the same origin, CORS might not be strictly necessary.
# However, it doesn't hurt to leave it for flexibility, especially during development.
CORS(app)
app.config['UPLOAD_FOLDER'] = str(UPLOAD_FOLDER) # Store path as string for Flask config

# Establish Database Connection
try:
    conn = mariadb.connect(**DB_CONFIG)
    conn.autocommit = False # Start with autocommit off for transaction control
    print("Successfully connected to MariaDB.")
except mariadb.Error as e:
    print(f"FATAL: Could not connect to MariaDB: {e}", file=sys.stderr)
    sys.exit(1) # Exit if DB connection fails on startup

# --- Database Helper Functions ---

def get_db_cursor():
    """ Returns a new cursor for the existing connection. """
    try:
        # Optional: Ping the connection to ensure it's still alive
        # Call ping() without arguments for mariadb version 1.1.11
        conn.ping()
        return conn.cursor()
    except mariadb.Error as e:
        print(f"Error getting DB cursor or connection lost: {e}", file=sys.stderr)
        # Attempt to reconnect if ping fails or other connection error occurs
        print("Attempting to reconnect to DB...")
        try:
            conn.connect(**DB_CONFIG) # Use connect method to re-establish
            conn.autocommit = False # Reset autocommit if needed
            print("Successfully reconnected to MariaDB.")
            return conn.cursor()
        except mariadb.Error as reconn_e:
             print(f"FATAL: Reconnection failed: {reconn_e}", file=sys.stderr)
             # If reconnection fails, re-raise the original error or a new one
             raise e # Re-raise the original error that caused the ping failure
    except Exception as e:
        # Catch other potential errors during cursor creation
        print(f"Unexpected error getting cursor: {e}", file=sys.stderr)
        raise

def dict_rows(cur):
    """ Converts cursor fetch results into a list of dictionaries. """
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]

def insert_gapfill_row(cur, meta):
    """ Inserts a new row into the gapfill_models table. """
    sql = """
    INSERT INTO gapfill_models
      (growth_media, gapfill_algorithm, annotation_tool,
       biomass_type, file_name, file_link, growth_yes_or_no)
    VALUES (?, ?, ?, ?, ?, ?, ?)
    """
    try:
        cur.execute(sql, (
            meta.get("growth_media"), # Use .get for safety, though form validation helps
            meta.get("gapfill_algorithm"),
            meta.get("annotation_tool"),
            meta.get("biomass_type"),
            meta.get("file_name"),
            meta.get("file_link"),
            meta.get("growth_yes_or_no"),
        ))
        return cur.lastrowid # Return the ID of the inserted row
    except mariadb.Error as e:
        print(f"Error inserting data: {e}", file=sys.stderr)
        raise # Re-raise the exception to be caught by the route handler


# --- Teardown Function ---
@app.teardown_appcontext
def close_db_connection(exception=None):
    """ Closes the database connection when the app context tears down. """
    # This is generally good practice, although the connection might persist
    # if the script doesn't exit cleanly. The global `conn` object is simple
    # but less robust than per-request connections or pooling.
    # For this simple app, we won't explicitly close the global `conn` here
    # as it's intended to stay open for the app's lifetime.
    # If using connection pooling or per-request connections, close them here.
    pass


# --- Health check ---
@app.route("/ping")
def ping():
    """ Simple health check endpoint. """
    return "pong", 200


# --- Web UI Routes ---

@app.route("/")
def index():
    """ Renders the main page, displaying all models by default. """
    models = []
    error_message = None
    try:
        with get_db_cursor() as cur:
            cur.execute("SELECT * FROM gapfill_models ORDER BY id DESC")
            models = dict_rows(cur)
    except mariadb.Error as e:
        current_app.logger.error(f"Database error in index(): {e}\n{traceback.format_exc()}")
        error_message = "Could not retrieve models from the database."
    except Exception as e:
        current_app.logger.error(f"Unexpected error in index(): {e}\n{traceback.format_exc()}")
        error_message = "An unexpected server error occurred."
        # In a real app, you might render a specific error page instead of abort(500)
        # abort(500)

    return render_template("index.html",
                           search_results=models,
                           media_search=None,
                           current_year=datetime.now().year, # Pass current year
                           error_message=error_message)


@app.route("/search", methods=["POST"])
def search():
    """ Handles searching models by growth media and renders the results. """
    models = []
    term = request.form.get("media_search", "").strip()
    error_message = None
    try:
        with get_db_cursor() as cur:
            # Use parameter binding to prevent SQL injection
            cur.execute(
                "SELECT * FROM gapfill_models WHERE growth_media LIKE ? ORDER BY id DESC",
                (f"%{term}%",) # Comma makes it a tuple
            )
            models = dict_rows(cur)
    except mariadb.Error as e:
        current_app.logger.error(f"Database error in search(): {e}\n{traceback.format_exc()}")
        error_message = f"Could not perform search for '{term}'."
    except Exception as e:
        current_app.logger.error(f"Unexpected error in search(): {e}\n{traceback.format_exc()}")
        error_message = "An unexpected server error occurred during search."
        # abort(500)

    return render_template("index.html",
                           search_results=models,
                           media_search=term,
                           current_year=datetime.now().year, # Pass current year
                           error_message=error_message)


# --- File Download Route ---

@app.route("/download/<path:filename>")
def download(filename):
    """ Serves uploaded files for download. """
    # Sanitize filename just in case, though secure_filename should handle it on upload
    safe_filename = secure_filename(filename)
    if not safe_filename: # If filename is empty or potentially dangerous after sanitizing
        abort(400, "Invalid filename.")

    file_path = UPLOAD_FOLDER / safe_filename
    current_app.logger.info(f"Attempting to download file from: {file_path}")

    if not file_path.is_file(): # More robust check than exists()
         current_app.logger.warning(f"Download failed: File not found at {file_path}")
         abort(404, "File not found.")

    try:
        return send_from_directory(
            directory=str(UPLOAD_FOLDER),
            path=safe_filename,
            as_attachment=True # Force download dialog
        )
    except Exception as e:
        current_app.logger.error(f"Error sending file {safe_filename}: {e}\n{traceback.format_exc()}")
        abort(500, "Could not send file.")


# --- JSON API Routes ---

@app.route("/api/models", methods=["GET"])
def api_list_models():
    """ API endpoint to list all models in JSON format. """
    models = []
    try:
        with get_db_cursor() as cur:
            cur.execute("SELECT * FROM gapfill_models ORDER BY id DESC")
            models = dict_rows(cur)
        return jsonify(models)
    except mariadb.Error as e:
        current_app.logger.error(f"API DB error in api_list_models(): {e}\n{traceback.format_exc()}")
        return jsonify(error=f"Database error: {e}"), 500
    except Exception as e:
        current_app.logger.error(f"API Exception in api_list_models(): {e}\n{traceback.format_exc()}")
        return jsonify(error="Internal server error"), 500


@app.route("/api/models", methods=["POST"])
def api_create_model():
    """ API endpoint to upload an XML file and create a new model entry. """
    dest = None # Initialize dest path variable
    try:
        # --- 1. File Validation and Saving ---
        if "xmlUpload" not in request.files:
            return jsonify(error="No file part in the request ('xmlUpload' expected)"), 400

        file = request.files["xmlUpload"]
        if file.filename == "":
            return jsonify(error="No file selected for upload"), 400

        # Check file extension
        ext = Path(file.filename).suffix.lower()
        if ext not in ALLOWED_EXTENSIONS:
            return jsonify(
                error=f"Invalid file type '{ext}'. Only {', '.join(ALLOWED_EXTENSIONS)} allowed."
            ), 400

        # Secure the filename and create destination path
        filename = secure_filename(file.filename)
        if not filename: # Handle cases where filename becomes empty after securing
             return jsonify(error="Invalid filename provided"), 400

        dest = UPLOAD_FOLDER / filename

        # Prevent overwriting existing files (optional, but good practice)
        if dest.exists():
            return jsonify(error=f"File '{filename}' already exists on the server."), 409 # Conflict

        # Save the file
        file.save(dest)
        current_app.logger.info(f"File '{filename}' saved successfully to {dest}")

        # --- 2. Form Data Validation ---
        required_fields = ["growth_media", "gapfill_algorithm", "annotation_tool", "biomass_type", "growth_yes_or_no"]
        form_data = {}
        missing_fields = []
        for field in required_fields:
            value = request.form.get(field)
            if value is None or value.strip() == "":
                missing_fields.append(field)
            else:
                form_data[field] = value.strip()

        if missing_fields:
             # Clean up saved file if form data is invalid
            dest.unlink(missing_ok=True)
            return jsonify(error=f"Missing required form fields: {', '.join(missing_fields)}"), 400

        # Specific validation for 'growth_yes_or_no'
        if form_data["growth_yes_or_no"] not in ("Yes", "No"):
             # Clean up saved file
            dest.unlink(missing_ok=True)
            return jsonify(error="Invalid value for 'Predicted Growth?'. Must be 'Yes' or 'No'."), 400

        # --- 3. Database Insertion ---
        # Generate the download link dynamically using url_for
        # Use _external=True if the API consumer needs an absolute URL
        file_link = url_for("download", filename=filename, _external=False) # Relative URL is usually fine

        meta = {
            "growth_media":       form_data["growth_media"],
            "gapfill_algorithm":  form_data["gapfill_algorithm"],
            "annotation_tool":    form_data["annotation_tool"],
            "biomass_type":       form_data["biomass_type"],
            "growth_yes_or_no":   form_data["growth_yes_or_no"],
            "file_name":          filename,
            "file_link":          file_link,
        }

        # Use a cursor within a 'with' block if possible, or manage manually
        cur = None
        try:
            cur = get_db_cursor()
            new_id = insert_gapfill_row(cur, meta)
            conn.commit() # Commit the transaction
            meta["id"] = new_id
            current_app.logger.info(f"Successfully inserted model ID {new_id} for file '{filename}'.")
            return jsonify(meta), 201 # 201 Created status

        except mariadb.Error as db_e:
            conn.rollback() # Rollback on database error
            # Clean up the uploaded file if DB insert fails
            if dest and dest.exists():
                 dest.unlink(missing_ok=True)
                 current_app.logger.info(f"Deleted file '{filename}' due to DB error.")
            current_app.logger.error(f"DB error in api_create_model(): {db_e}\n{traceback.format_exc()}")
            return jsonify(error=f"Database error: {db_e}"), 500
        finally:
            if cur:
                cur.close()

    except FileNotFoundError as fnf_e: # Error during file save perhaps
        current_app.logger.error(f"File operation error in api_create_model(): {fnf_e}\n{traceback.format_exc()}")
        return jsonify(error=f"Server file system error: {fnf_e}"), 500
    except Exception as e:
        # General catch-all, try to rollback and clean up file
        try:
            conn.rollback()
        except mariadb.Error as rb_e:
             current_app.logger.error(f"Rollback failed after exception: {rb_e}")

        if dest and dest.exists():
            try:
                dest.unlink(missing_ok=True)
                current_app.logger.info(f"Deleted file '{filename}' due to unexpected error.")
            except OSError as del_e:
                 current_app.logger.error(f"Could not delete file '{filename}' after error: {del_e}")

        current_app.logger.error(f"Unexpected exception in api_create_model(): {e}\n{traceback.format_exc()}")
        return jsonify(error="An unexpected internal server error occurred."), 500


# --- Run the App ---

if __name__ == "__main__":
    # Set debug=False for production
    # host='0.0.0.0' makes it accessible on your network
    app.run(host="0.0.0.0", port=5001, debug=True)