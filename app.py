import os
import sys
import traceback
import mariadb
import shutil
from pathlib import Path
from datetime import datetime # Import datetime for footer year

from flask import (
    Flask, render_template, request, jsonify,
    url_for, send_from_directory, abort, current_app # Make sure send_from_directory is imported
)
from flask_cors import CORS
from werkzeug.utils import secure_filename

# --- Configuration ---

BASE_DIR = Path(__file__).parent.resolve()
# Store uploads in an 'uploads' folder next to app.py
UPLOAD_FOLDER = BASE_DIR / "uploads"
UPLOAD_FOLDER.mkdir(exist_ok=True, parents=True) # Create uploads folder if it doesn't exist
ALLOWED_EXTENSIONS = {".xml"}

# --- Hardcoded Database Credentials ---
# WARNING: Hardcoding credentials is NOT recommended for production environments.
DB_CONFIG = {
    "host":     "bioed-new.bu.edu",
    "port":     4253,
    "user":     "npetruni", # Using credentials from the first app
    "password": "moesIsgooD1125##", # Using credentials from the first app
    "database": "Team11",
}

# --- App & DB Initialization ---

app = Flask(__name__)
# Limit uploads to 16 Megabytes (adjust value if needed)
app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024
app.config['UPLOAD_FOLDER'] = str(UPLOAD_FOLDER) # Store path string in config if needed elsewhere
CORS(app)

# Establish Database Connection
conn = None # Initialize conn to None
try:
    conn = mariadb.connect(**DB_CONFIG)
    conn.autocommit = False # Start with autocommit off for transaction control
    print("Successfully connected to MariaDB.")
except mariadb.Error as e:
    print(f"FATAL: Could not connect to MariaDB: {e}", file=sys.stderr)
    sys.exit(1) # Exit if DB connection fails on startup

# --- Database Helper Functions ---

def get_db_cursor():
    """ Returns a new cursor for the existing connection. Handles potential disconnects. """
    global conn # Need to modify the global connection object if reconnecting
    try:
        if conn is None:
            print("Connection is None, attempting to establish initial connection.")
            conn = mariadb.connect(**DB_CONFIG)
            conn.autocommit = False
            print("Successfully connected to MariaDB.")
            return conn.cursor()

        # Ping the connection to ensure it's still alive
        # Call ping() without arguments for mariadb version 1.1.11
        conn.ping()
        return conn.cursor()
    except mariadb.Error as e:
        print(f"Error getting DB cursor or connection lost: {e}", file=sys.stderr)
        # Attempt to reconnect if ping fails or other connection error occurs
        print("Attempting to reconnect to DB...")
        try:
            # Close the potentially broken connection first, if it exists
            if conn:
                try:
                    conn.close()
                    print("Closed potentially broken DB connection.")
                except mariadb.Error as close_e:
                    print(f"Error closing broken connection: {close_e}", file=sys.stderr)
            # Establish a new connection
            conn = mariadb.connect(**DB_CONFIG) # Use connect method to re-establish
            conn.autocommit = False # Reset autocommit
            print("Successfully reconnected to MariaDB.")
            return conn.cursor()
        except mariadb.Error as reconn_e:
             print(f"FATAL: Reconnection failed: {reconn_e}", file=sys.stderr)
             conn = None # Set conn back to None if reconnection fails
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
            meta.get("growth_media"),
            meta.get("gapfill_algorithm"),
            meta.get("annotation_tool"),
            meta.get("biomass_type"),
            meta.get("file_name"),
            meta.get("file_link"), # This is the URL like /download/filename.xml
            meta.get("growth_yes_or_no"),
        ))
        return cur.lastrowid # Return the ID of the inserted row
    except mariadb.Error as e:
        print(f"Error inserting data: {e}", file=sys.stderr)
        raise # Re-raise the exception to be caught by the route handler


# --- Teardown Function ---
@app.teardown_appcontext
def close_db_connection(exception=None):
    """ Closes the database cursor and connection if needed (for robust cleanup). """
    # While the global `conn` exists, explicitly closing isn't strictly necessary
    # if the app runs continuously. However, in Flask, it's better practice
    # to manage resources within the app context if possible, although
    # reconnect logic complicates this slightly for a single global `conn`.
    # For now, we'll keep the global connection alive as intended by the original code.
    # If you were using Flask-SQLAlchemy or connection pooling, cleanup would happen here.
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
    cur = None
    try:
        cur = get_db_cursor() # Get cursor (handles connection logic)
        cur.execute("SELECT * FROM gapfill_models ORDER BY id DESC")
        models = dict_rows(cur)
    except mariadb.Error as e:
        current_app.logger.error(f"Database error in index(): {e}\n{traceback.format_exc()}")
        error_message = "Could not retrieve models from the database."
    except Exception as e:
        current_app.logger.error(f"Unexpected error in index(): {e}\n{traceback.format_exc()}")
        error_message = "An unexpected server error occurred."
        # abort(500) # Consider uncommenting for production
    finally:
        if cur:
            cur.close() # Always close the cursor

    return render_template("index.html",
                           search_results=models,
                           media_search=None,
                           current_year=datetime.now().year,
                           error_message=error_message)


@app.route("/search", methods=["POST"])
def search():
    """ Handles searching models by growth media and renders the results. """
    models = []
    term = request.form.get("media_search", "").strip()
    error_message = None
    cur = None
    try:
        cur = get_db_cursor()
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
        # abort(500) # Consider uncommenting for production
    finally:
        if cur:
            cur.close() # Always close the cursor

    return render_template("index.html",
                           search_results=models,
                           media_search=term,
                           current_year=datetime.now().year,
                           error_message=error_message)


# --- File Download Route ---

# Make sure UPLOAD_FOLDER is defined correctly *before* this route
if not UPLOAD_FOLDER or not os.path.isdir(UPLOAD_FOLDER):
     print(f"WARNING: UPLOAD_FOLDER '{UPLOAD_FOLDER}' is not configured or does not exist.", file=sys.stderr)
     # Depending on your needs, you might want to exit or handle this differently
     # For now, we'll let it proceed, but downloads will fail.

@app.route("/download/<path:filename>")
def download(filename):
    """ Serves uploaded files for download. """
    # Use the UPLOAD_FOLDER defined globally
    upload_dir = current_app.config.get('UPLOAD_FOLDER', UPLOAD_FOLDER) # Get from config or global

    # Sanitize filename again for safety, though secure_filename on upload helps
    safe_filename = secure_filename(filename)
    if not safe_filename or safe_filename != filename: # Check if sanitization changed it or it's empty
        current_app.logger.warning(f"Download attempt rejected for potentially unsafe filename: {filename}")
        abort(400, "Invalid filename.")

    file_path = Path(upload_dir) / safe_filename
    current_app.logger.info(f"Attempting to download file from: {file_path}")

    if not file_path.is_file(): # Check if the file actually exists
         current_app.logger.warning(f"Download failed: File not found at {file_path}")
         abort(404, "File not found.")

    try:
        # send_from_directory handles Content-Disposition and MIME types
        return send_from_directory(
            directory=str(upload_dir), # Pass the directory path as string
            path=safe_filename,        # Pass the sanitized filename
            as_attachment=True         # IMPORTANT: This triggers the download dialog
        )
    except FileNotFoundError: # Might occur if file disappears between check and send
        current_app.logger.error(f"File not found during send_from_directory for {safe_filename}")
        abort(404, "File not found.")
    except Exception as e:
        current_app.logger.error(f"Error sending file {safe_filename}: {e}\n{traceback.format_exc()}")
        abort(500, "Could not send file.")


# --- JSON API Routes ---

@app.route("/api/models", methods=["GET"])
def api_list_models():
    """ API endpoint to list all models in JSON format. """
    models = []
    cur = None
    try:
        cur = get_db_cursor()
        cur.execute("SELECT * FROM gapfill_models ORDER BY id DESC")
        models = dict_rows(cur)
        return jsonify(models)
    except mariadb.Error as e:
        current_app.logger.error(f"API DB error in api_list_models(): {e}\n{traceback.format_exc()}")
        return jsonify(error=f"Database error: {e}"), 500
    except Exception as e:
        current_app.logger.error(f"API Exception in api_list_models(): {e}\n{traceback.format_exc()}")
        return jsonify(error="Internal server error"), 500
    finally:
        if cur:
            cur.close()


@app.route("/api/models", methods=["POST"])
def api_create_model():
    """ API endpoint to upload an XML file and create a new model entry. """
    dest = None # Initialize dest path variable
    filename = None # Initialize filename
    try:
        # --- 1. File Validation and Saving ---
        if "xmlUpload" not in request.files:
            return jsonify(error="No file part in the request ('xmlUpload' expected)"), 400

        file = request.files["xmlUpload"]
        if file.filename == "":
            return jsonify(error="No file selected for upload"), 400

        # Secure the filename *before* checking extension
        filename = secure_filename(file.filename)
        if not filename: # Handle cases where filename becomes empty after securing
             return jsonify(error="Invalid filename provided"), 400

        # Check file extension
        ext = Path(filename).suffix.lower() # Check extension on the secured name
        if ext not in ALLOWED_EXTENSIONS:
            return jsonify(
                error=f"Invalid file type '{ext}'. Only {', '.join(ALLOWED_EXTENSIONS)} allowed."
            ), 400

        dest = UPLOAD_FOLDER / filename

        # Prevent overwriting existing files
        if dest.exists():
            # Consider adding a timestamp or unique ID if overwriting is not desired
            # For now, return error
            return jsonify(error=f"File '{filename}' already exists on the server."), 409 # Conflict

        # --- Stream-save the file ---
        try:
            current_app.logger.info(f"Attempting to stream-save file to: {dest}")
            with open(dest, 'wb') as f_dest:
                # Use file.save() which handles streaming internally for Werkzeug FileStorage
                file.save(f_dest)
            current_app.logger.info(f"File '{filename}' saved successfully to {dest}")
        except Exception as e:
             current_app.logger.error(f"Error during file saving {filename}: {e}\n{traceback.format_exc()}")
             if dest.exists():
                 try:
                     dest.unlink(missing_ok=True)
                     current_app.logger.info(f"Deleted partially written file {filename} after save error.")
                 except OSError as del_e:
                      current_app.logger.error(f"Could not delete partially written file {filename}: {del_e}")
             return jsonify(error=f"Failed to save file on server: {e}"), 500

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
            dest.unlink(missing_ok=True) # Clean up saved file
            return jsonify(error=f"Missing required form fields: {', '.join(missing_fields)}"), 400

        if form_data["growth_yes_or_no"] not in ("Yes", "No"):
            dest.unlink(missing_ok=True) # Clean up saved file
            return jsonify(error="Invalid value for 'Predicted Growth?'. Must be 'Yes' or 'No'."), 400

        # --- 3. Database Insertion ---
        # Generate the relative download link using url_for
        # This will produce something like "/download/yourfile.xml"
        file_link = url_for("download", filename=filename) # _external=False is default

        meta = {
            "growth_media":       form_data["growth_media"],
            "gapfill_algorithm":  form_data["gapfill_algorithm"],
            "annotation_tool":    form_data["annotation_tool"],
            "biomass_type":       form_data["biomass_type"],
            "growth_yes_or_no":   form_data["growth_yes_or_no"],
            "file_name":          filename, # Store the actual filename
            "file_link":          file_link, # Store the generated URL path
        }

        cur = None
        try:
            cur = get_db_cursor()
            new_id = insert_gapfill_row(cur, meta)
            conn.commit() # Commit the transaction
            meta["id"] = new_id # Add the new ID to the response
            current_app.logger.info(f"Successfully inserted model ID {new_id} for file '{filename}'.")
            # Return the full metadata including the ID and relative link
            return jsonify(meta), 201 # 201 Created status

        except mariadb.Error as db_e:
            if conn: conn.rollback() # Rollback on database error
            if dest and dest.exists():
                 dest.unlink(missing_ok=True)
                 current_app.logger.info(f"Deleted file '{filename}' due to DB error.")
            current_app.logger.error(f"DB error in api_create_model(): {db_e}\n{traceback.format_exc()}")
            return jsonify(error=f"Database error: {db_e}"), 500
        finally:
            if cur:
                cur.close()

    except Exception as e:
        # General catch-all
        if conn:
            try:
                conn.rollback()
            except mariadb.Error as rb_e:
                 current_app.logger.error(f"Rollback failed after exception: {rb_e}")

        if dest and dest.exists():
            try:
                dest.unlink(missing_ok=True)
                current_app.logger.info(f"Deleted file '{filename or 'unknown'}' due to unexpected error.")
            except OSError as del_e:
                 current_app.logger.error(f"Could not delete file '{filename or 'unknown'}' after error: {del_e}")

        current_app.logger.error(f"Unexpected exception in api_create_model(): {e}\n{traceback.format_exc()}")
        return jsonify(error="An unexpected internal server error occurred."), 500


# --- Run the App ---

if __name__ == "__main__":
    # Enable logging for debugging
    logging.basicConfig(level=logging.INFO)
    # Set debug=False for production
    # host='0.0.0.0' makes it accessible on your network
    app.run(host="0.0.0.0", port=5001, debug=True) # Using port 5001 as in your first app