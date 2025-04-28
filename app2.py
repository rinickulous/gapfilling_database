import os
import sys
import traceback
import mariadb
import shutil
import logging # Make sure logging is imported
from pathlib import Path
from datetime import datetime

from flask import (
    Flask, render_template, request, jsonify,
    url_for, send_from_directory, abort, current_app
)
from flask_cors import CORS
from werkzeug.utils import secure_filename
# Import specific exceptions for better handling (optional but good practice)
from werkzeug.exceptions import NotFound, BadRequest, InternalServerError

# --- Configuration ---

BASE_DIR = Path(__file__).parent.resolve()
UPLOAD_FOLDER = BASE_DIR / "uploads"
UPLOAD_FOLDER.mkdir(exist_ok=True, parents=True)
# Allow XML and TSV uploads
ALLOWED_EXTENSIONS = {".xml", ".tsv"}

# --- Hardcoded Database Credentials ---
# WARNING: Hardcoding credentials is NOT recommended for production. Use environment variables or config files.
DB_CONFIG = {
    "host":     "bioed-new.bu.edu",
    "port":     4253,
    "user":     "npetruni",
    "password": "moesIsgooD1125##",
    "database": "Team11",
}

# --- App & DB Initialization ---

app = Flask(__name__)
# Set the name of the script here if it's not 'app' or 'wsgi' for cleaner logs
# app.name = 'students_25.Team11.web_application2.app2'
app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024
app.config['UPLOAD_FOLDER'] = str(UPLOAD_FOLDER)
CORS(app)

# Configure Logging
# Ensure logs are written to stderr (common for WSGI setups like Apache's)
# Use app.logger for Flask context-aware logging
logging.basicConfig(stream=sys.stderr, level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
app.logger.setLevel(logging.INFO) # Ensure Flask's logger also uses INFO level


conn = None # Global connection object

# Function to establish DB connection
def connect_db():
    """Establishes or re-establishes the global DB connection."""
    global conn
    try:
        # Close existing broken connection if it exists and isn't already closed
        # Check _closed safely using getattr, default to True if None or attr missing
        if conn and not getattr(conn, '_closed', True):
             try:
                 conn.close()
                 app.logger.info("Closed previous potentially stale DB connection.")
             except mariadb.Error as close_e:
                 app.logger.warning(f"Error closing stale connection: {close_e}")

        app.logger.info("Attempting to connect to MariaDB...")
        conn = mariadb.connect(**DB_CONFIG)
        conn.autocommit = False # Important: Keep autocommit off for transactions
        app.logger.info("Successfully connected to MariaDB.")
    except mariadb.Error as e:
        app.logger.error(f"FATAL: Could not connect/reconnect to MariaDB: {e}", exc_info=True)
        conn = None # Ensure conn is None if connection fails

# Establish initial connection on startup
connect_db()

# --- Database Helper Functions ---
def get_db_cursor():
    """ Returns a new cursor for the existing connection. Handles reconnects using _closed. """
    global conn
    MAX_RETRIES = 2
    for attempt in range(MAX_RETRIES):
        try:
            # Check connection status using _closed attribute
            is_closed = getattr(conn, '_closed', True) # Assume closed if attr missing or conn is None
            if conn is None or is_closed:
                app.logger.warning(f"DB connection is None or closed (Attempt {attempt + 1}/{MAX_RETRIES}). Reconnecting.")
                connect_db() # Try to reconnect
                if conn is None: # Check if reconnection failed
                     # Use a specific exception if available
                     raise mariadb.OperationalError("Database reconnection failed.")

            # Ping to ensure connection is active (optional but recommended)
            # ping() itself might try reconnecting depending on library version/settings
            conn.ping()
            app.logger.debug("DB connection ping successful.")
            return conn.cursor() # Return cursor on success

        except (mariadb.Error, mariadb.InterfaceError, mariadb.OperationalError) as e:
            app.logger.error(f"DB Error in get_db_cursor (Attempt {attempt + 1}/{MAX_RETRIES}): {e}", exc_info=False) # Log less verbosely on retry
            if attempt < MAX_RETRIES - 1:
                 app.logger.warning("Retrying DB connection...")
                 # Force a reconnect attempt before the next loop iteration
                 conn = None # Force connect_db to establish new connection
                 connect_db()
            else:
                app.logger.error("Max DB connection retries reached. Raising error.")
                raise # Re-raise the last exception after max retries

        except AttributeError as ae:
             app.logger.error(f"AttributeError getting DB cursor (likely conn object state check): {ae}", exc_info=True)
             raise mariadb.InterfaceError("Internal error checking DB connection state.") from ae
        except Exception as e:
             app.logger.error(f"Unexpected error in get_db_cursor: {e}", exc_info=True)
             raise # Re-raise other unexpected errors

    # Should not be reached if logic above is correct
    raise mariadb.OperationalError("Failed to get DB cursor after multiple retries.")


def dict_rows(cur):
    """ Converts cursor results into list of dicts. Handles potential None description. """
    if not cur.description:
        # Log or handle cases where the cursor didn't produce results (e.g., failed query, non-SELECT query)
        # app.logger.debug("dict_rows called with cursor having no description.")
        return []
    try:
        cols = [d[0] for d in cur.description]
        results = []
        # Use fetchall which returns an empty list if no rows match
        for row in cur.fetchall():
            # Ensure row has same number of elements as cols for zip
            if len(row) == len(cols):
                 results.append(dict(zip(cols, row)))
            else:
                 app.logger.warning(f"Row length mismatch in dict_rows. Cols: {len(cols)}, Row: {len(row)}. Skipping row.")
        return results
    except Exception as e:
        # Log error during processing
        app.logger.error(f"Error processing cursor results in dict_rows: {e}", exc_info=True)
        return [] # Return empty list on error


def insert_gapfill_row(cur, meta):
    """Inserts a new row into gapfill_models, expects dict with potentially None values."""
    # Ensure this SQL matches your ACTUAL current table structure and column order
    sql = """
    INSERT INTO gapfill_models
      (growth_media, gapfill_algorithm, annotation_tool, file_name, file_link,
       growth_data, growth_file, biomass_file_5mM, biomass_file_20mM, Biomass_RCH1)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """
    # Prepare tuple of values in the correct order, using .get(key, None) for safety
    # Ensure the order here matches the SQL columns exactly
    values_tuple = (
        meta.get("growth_media"), meta.get("gapfill_algorithm"), meta.get("annotation_tool"),
        meta.get("file_name"), meta.get("file_link"), # file_link should be relative path now
        meta.get("growth_data"),
        meta.get("growth_file"), meta.get("biomass_file_5mM"), meta.get("biomass_file_20mM"),
        meta.get("Biomass_RCH1"), # Should be None based on current form/logic
    )
    app.logger.debug(f"Executing SQL: {sql} with values: {values_tuple}")
    try:
        cur.execute(sql, values_tuple)
        app.logger.info(f"Insert successful, last row ID: {cur.lastrowid}")
        return cur.lastrowid
    except mariadb.Error as e:
         # Log the specific data that caused the error might be too verbose, log keys instead
         current_app.logger.error(f"Error inserting data: {e} with meta keys: {list(meta.keys())}", exc_info=True)
         # Raise specific, more informative errors if possible
         if e.errno == 1048: # Column cannot be null
              try: column_name = str(e).split("'")[1] # Attempt to parse column name
              except IndexError: column_name = "a required field"
              # Raise IntegrityError which might map to 400 Bad Request later
              raise mariadb.IntegrityError(f"Database Constraint Error: '{column_name}' cannot be empty.")
         elif e.errno == 1062: # Duplicate entry
              raise mariadb.IntegrityError(f"Database Constraint Error: Duplicate entry detected.")
         else:
              raise # Re-raise other database errors

# --- Teardown Function ---
@app.teardown_appcontext
def close_db_connection(exception=None):
    # Runs after request context. Global conn kept open. Cursors closed in routes.
    if exception:
        # Log exceptions passed during teardown
        app.logger.error(f"App teardown triggered with exception: {exception}", exc_info=True)
    pass

# --- Health check ---
@app.route("/ping")
def ping():
    # Optional: Add DB check
    # try:
    #     with get_db_cursor() as cur: cur.execute("SELECT 1")
    #     return "pong (DB OK)", 200
    # except Exception: return "pong (DB Error)", 503
    return "pong", 200

# --- Web UI Routes ---
@app.route("/")
def index():
    """ Renders the main page, showing the latest 5 models. """
    models = []
    error_message = None
    cur = None
    try:
        app.logger.info(f"Request received for index route '/'")
        cur = get_db_cursor()
        # Fetch limited number of models for initial display
        cur.execute("SELECT * FROM gapfill_models ORDER BY id DESC LIMIT 5")
        models = dict_rows(cur)
        app.logger.info(f"Retrieved {len(models)} models for index display.")
    except (mariadb.Error, mariadb.InterfaceError, mariadb.OperationalError) as db_e:
        app.logger.error(f"Database error retrieving models for index: {db_e}", exc_info=True)
        error_message = "Database connection or query error retrieving models. Please try again later."
    except Exception as e:
        app.logger.error(f"Unexpected error in index(): {e}", exc_info=True)
        error_message = "An unexpected server error occurred while retrieving models."
    finally:
        if cur:
            try: cur.close()
            except mariadb.Error as e: app.logger.error(f"Error closing cursor in index(): {e}", exc_info=True)

    return render_template(
        "index.html",
        search_results=models,
        media_search=None, # Indicate no search was performed
        current_year=datetime.now().year,
        error_message=error_message
    )

@app.route("/search", methods=["POST"])
def search():
    """ Handles searching models by growth media and renders the results. """
    models = []
    term = request.form.get("media_search", "").strip()
    error_message = None
    cur = None
    app.logger.info(f"Handling search request for term: '{term}'")
    try:
        cur = get_db_cursor()
        # Fetch all matching models for search
        cur.execute(
            "SELECT * FROM gapfill_models WHERE growth_media LIKE ? ORDER BY id DESC",
            (f"%{term}%",)
        )
        models = dict_rows(cur)
        app.logger.info(f"Found {len(models)} models matching search term '{term}'.")
    except (mariadb.Error, mariadb.InterfaceError, mariadb.OperationalError) as db_e:
        app.logger.error(f"Database error during search for '{term}': {db_e}", exc_info=True)
        error_message = f"Database error during search for '{term}'. Please try again later."
    except Exception as e:
        app.logger.error(f"Unexpected error in search(): {e}", exc_info=True)
        error_message = f"Search failed for '{term}' due to a server error."
    finally:
        if cur:
             try: cur.close()
             except mariadb.Error as e: app.logger.error(f"Error closing cursor in search(): {e}", exc_info=True)

    return render_template(
        "index.html",
        search_results=models,
        media_search=term, # Pass the search term back to display it
        current_year=datetime.now().year,
        error_message=error_message
    )

# --- File Download Route ---
if not UPLOAD_FOLDER.exists():
    app.logger.warning(f"UPLOAD_FOLDER '{UPLOAD_FOLDER}' does not exist at startup.")
elif not os.access(str(UPLOAD_FOLDER), os.R_OK | os.X_OK): # Check read/execute for listing/serving
     app.logger.warning(f"UPLOAD_FOLDER '{UPLOAD_FOLDER}' may lack Read/Execute permissions for the server process.")

@app.route("/download/<path:filepath>")
def download(filepath):
    """ Serves files from UPLOAD_FOLDER, handling subdirectories securely. """
    app.logger.info(f"Download request received for path: '{filepath}'")
    # Basic security check: prevent directory traversal ('..') and absolute paths
    normalized_path = os.path.normpath(filepath)
    if '..' in normalized_path.split(os.sep) or normalized_path.startswith((os.sep, '/')):
         app.logger.warning(f"Download rejected for potentially unsafe path: {filepath} (normalized: {normalized_path})")
         abort(400, "Invalid file path.") # Bad Request

    app.logger.info(f"Attempting download via send_from_directory for path: '{filepath}' relative to '{UPLOAD_FOLDER}'")
    try:
        # send_from_directory handles security checks (path within directory)
        return send_from_directory(
            directory=str(UPLOAD_FOLDER),
            path=filepath,  # Pass the relative path including potential subdirs
            as_attachment=True # Force download dialog
        )
    except (FileNotFoundError, NotFound) as e: # Catch specific not found errors
         app.logger.warning(f"File not found via send_from_directory for path: '{filepath}' within {UPLOAD_FOLDER}. Error: {e}")
         abort(404, "File not found.") # Not Found
    except BadRequest as e:
        app.logger.error(f"Bad Request during file download attempt for '{filepath}': {e}", exc_info=True)
        abort(400, "Invalid request.")
    except Exception as e:
         app.logger.error(f"Error sending file for path '{filepath}': {e}", exc_info=True)
         if isinstance(e, PermissionError):
              error_msg = "Could not send file due to server permission error."
              http_status = 500
         elif isinstance(e, IsADirectoryError):
              error_msg = "The requested path points to a directory, not a downloadable file."
              http_status = 400 # Bad request
         else:
              error_msg = "Could not send file due to an internal server error."
              http_status = 500
         abort(http_status, description=error_msg)


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
    except (mariadb.Error, mariadb.InterfaceError, mariadb.OperationalError) as db_e:
        app.logger.error(f"API DB error in api_list_models(): {db_e}", exc_info=True)
        return jsonify(error=f"Database error: Failed to retrieve models."), 500
    except Exception as e:
        app.logger.error(f"API Exception in api_list_models(): {e}", exc_info=True)
        return jsonify(error="Internal server error listing models"), 500
    finally:
        if cur:
            try: cur.close()
            except mariadb.Error as e: app.logger.error(f"Error closing cursor in api_list_models(): {e}", exc_info=True)


@app.route("/api/models", methods=["POST"])
def api_create_model():
    """ API endpoint to upload model file (XML/TSV) and optional associated TSV files. """
    saved_files_paths = [] # Track Path objects of saved files for cleanup
    main_file_dest = None

    try:
        # --- 1. Handle Main Model File ---
        if "modelUpload" not in request.files:
            return jsonify(error="No main model file part ('modelUpload') provided."), 400
        main_file = request.files["modelUpload"]
        if not main_file or not main_file.filename:
            return jsonify(error="No main model file selected or filename missing."), 400

        main_filename = secure_filename(main_file.filename)
        if not main_filename:
             return jsonify(error="Invalid main model filename (became empty after securing)."), 400

        main_ext = Path(main_filename).suffix.lower()
        if main_ext not in ALLOWED_EXTENSIONS:
            return jsonify(error=f"Invalid main file type '{main_ext}'. Only {', '.join(ALLOWED_EXTENSIONS)} allowed."), 400

        # Determine save location and relative path for main file
        # Example: Save main files directly in uploads root
        main_file_relative_path = main_filename # Path relative to UPLOAD_FOLDER
        main_file_dest = UPLOAD_FOLDER / main_filename # Full path for saving

        # Example: Save main files in a subdirectory like 'main_models'
        # main_file_subdir = 'main_models'
        # main_file_relative_path = str(Path(main_file_subdir) / main_filename).replace('\\', '/') # Use forward slash
        # main_file_dest = UPLOAD_FOLDER / main_file_subdir / main_filename
        # Path(UPLOAD_FOLDER / main_file_subdir).mkdir(parents=True, exist_ok=True) # Ensure subdir exists

        app.logger.info(f"Main file destination: {main_file_dest}, Relative path for DB: {main_file_relative_path}")

        if main_file_dest.exists():
            return jsonify(error=f"Main file '{main_filename}' already exists. Upload cancelled."), 409

        # Save main file
        try:
            main_file.save(str(main_file_dest)) # save expects a string path
            saved_files_paths.append(main_file_dest) # Track saved file Path object
            app.logger.info(f"Main file '{main_filename}' saved successfully to {main_file_dest}")
        except Exception as save_e:
            app.logger.error(f"Error saving main file {main_filename}: {save_e}", exc_info=True)
            # Raise a specific error to be caught by the outer handler for cleanup
            raise IOError(f"Failed to save main model file: {save_e}")

        # --- 2. Handle Optional TSV Files ---
        optional_files_config = {
            # form_input_name: { subdir_name: str, db_column_key: str }
            'growth_file_upload': {'subdir': 'growth_file', 'db_column': 'growth_file'},
            'biomass_5mM_upload': {'subdir': '5mM', 'db_column': 'biomass_file_5mM'},
            'biomass_20mM_upload':{'subdir': '20mM', 'db_column': 'biomass_file_20mM'}
        }
        # Dictionary to hold the relative paths for DB insertion (defaults to None)
        optional_file_paths_for_db = { v['db_column']: None for v in optional_files_config.values() }
        app.logger.debug(f"Processing optional files. Initial paths: {optional_file_paths_for_db}")

        for input_name, config in optional_files_config.items():
            # Check if the file input exists in the request and has a non-empty filename
            if input_name in request.files and request.files[input_name].filename:
                opt_file = request.files[input_name]
                opt_filename = secure_filename(opt_file.filename)
                app.logger.debug(f"Found optional file for {input_name}: {opt_filename}")

                if not opt_filename:
                    app.logger.warning(f"Optional file upload skipped for {input_name} due to invalid filename after securing.")
                    continue # Skip if filename becomes empty after securing

                # Enforce TSV extension for optional files
                if not opt_filename.lower().endswith('.tsv'):
                    app.logger.warning(f"Optional file upload '{opt_filename}' for {input_name} is not a .tsv file. Skipping.")
                    continue # Skip non-TSV files

                subdir_name = config['subdir']
                subdir_path = UPLOAD_FOLDER / subdir_name    # Use pathlib
                opt_dest = subdir_path / opt_filename       # Use pathlib

                # Check for existence *before* creating directory
                if opt_dest.exists():
                    app.logger.warning(f"Optional file '{opt_filename}' already exists in {subdir_name}. Skipping save, but using existing path for DB.")
                    # Store the path relative to UPLOAD_FOLDER using forward slashes
                    relative_path = (Path(subdir_name) / opt_filename).as_posix()
                    optional_file_paths_for_db[config['db_column']] = relative_path
                    app.logger.debug(f"Set DB path for {config['db_column']} to existing: {relative_path}")
                    continue # Don't try to save, move to next optional file

                # Save the optional file
                try:
                    subdir_path.mkdir(parents=True, exist_ok=True) # Create subdir if needed
                    opt_file.save(str(opt_dest)) # Save expects string path
                    saved_files_paths.append(opt_dest) # Track saved file Path object
                    app.logger.info(f"Optional file '{opt_filename}' saved to {opt_dest}")
                    # Store the relative path string (using forward slashes) for DB/URL
                    relative_path = (Path(subdir_name) / opt_filename).as_posix()
                    optional_file_paths_for_db[config['db_column']] = relative_path
                    app.logger.debug(f"Set DB path for {config['db_column']} to new: {relative_path}")

                except Exception as save_e:
                    app.logger.error(f"Error saving optional file {opt_filename} to {opt_dest}: {save_e}", exc_info=True)
                    # Decide: Continue or fail whole upload? Let's continue for now. Path will remain None.
                    # If failing is desired, uncomment below:
                    # raise IOError(f"Failed to save optional file '{opt_filename}': {save_e}")
            else:
                 app.logger.debug(f"No file provided or empty filename for optional input: {input_name}")

        # --- 3. Collect Other Form Data ---
        # Ensure these keys match the 'name' attributes in your HTML form
        form_field_keys = [
            "growth_media", "gapfill_algorithm", "annotation_tool", "growth_data"
        ]
        # .get() defaults to None if key is missing from form submission
        form_data = {fld: request.form.get(fld) for fld in form_field_keys}

        # --- 4. Prepare Metadata for DB ---
        # Note: file_link now stores the relative path of the main file, not a generated URL
        meta = {
            "growth_media":      form_data.get("growth_media"),
            "gapfill_algorithm": form_data.get("gapfill_algorithm"),
            "annotation_tool":   form_data.get("annotation_tool"),
            "file_name":         main_filename,            # Original name of main uploaded file
            "file_link":         main_file_relative_path,  # RELATIVE PATH of main file
            "growth_data":       form_data.get("growth_data"),
            # Add paths for optional files (will be None if not uploaded/skipped/failed)
            "growth_file":       optional_file_paths_for_db.get('growth_file'),
            "biomass_file_5mM":  optional_file_paths_for_db.get('biomass_file_5mM'),
            "biomass_file_20mM": optional_file_paths_for_db.get('biomass_file_20mM'),
            "Biomass_RCH1":      None, # This column seems unused now
        }
        app.logger.debug(f"Meta dictionary prepared for DB insert: {meta}")

        # --- 5. Insert into DB ---
        cur = None
        conn_local = None
        try:
            cur = get_db_cursor()
            conn_local = conn # Use the global connection obtained by cursor function
            new_id = insert_gapfill_row(cur, meta)
            conn_local.commit()
            app.logger.info(f"Successfully inserted DB record ID {new_id} referencing file '{main_filename}'.")

            # Prepare response JSON (don't necessarily need to include all internal paths)
            response_meta = {
                 "id": new_id,
                 "file_name": meta.get("file_name"),
                 # Optionally include the relative path if client needs it:
                 # "file_link_path": meta.get("file_link"),
                 "message": "Upload successful."
             }
            return jsonify(response_meta), 201 # 201 Created

        except (mariadb.Error, mariadb.IntegrityError) as db_e:
            # Log error before rollback attempt
            app.logger.error(f"DB error on insert/commit: {db_e}", exc_info=True)
            # Attempt rollback
            if conn_local:
                 try: conn_local.rollback()
                 except mariadb.Error as rb_e: app.logger.error(f"Rollback failed: {rb_e}")
            # Re-raise the specific DB error to be caught by outer handler for cleanup
            raise db_e

    # --- Outer Exception Handler ---
    except Exception as e:
        # This catches DB errors re-raised from inner block, file save IOErrors, etc.
        app.logger.error(f"Error processing upload request: {e}", exc_info=True)
        # Attempt DB rollback again just in case
        if conn:
            try:
                # Check connection state before rollback
                if not getattr(conn, '_closed', True): conn.rollback()
            except mariadb.Error as rb_e: app.logger.error(f"Outer rollback failed: {rb_e}")

        # --- Cleanup ALL files potentially saved during this failed request ---
        app.logger.warning(f"Initiating file cleanup due to error: {e}")
        app.logger.debug(f"Files to potentially clean up: {saved_files_paths}")
        for file_path_obj in saved_files_paths:
            try:
                # Ensure it's a Path object and check existence/type
                file_path = Path(file_path_obj) # Ensure it's a Path object
                if file_path.is_file(): # Only delete if it's a file
                    file_path.unlink()
                    app.logger.info(f"Cleaned up file: {file_path}")
                elif file_path.exists():
                     app.logger.warning(f"Cleanup skipped: Path exists but is not a file: {file_path}")
            except Exception as del_e: # Catch broader errors during cleanup
                 app.logger.error(f"Could not delete file '{file_path_obj}' during cleanup: {del_e}", exc_info=True)

        # Determine appropriate error response message and status code
        status_code = 500
        error_message = "An unexpected internal server error occurred during upload."
        # Provide more specific error messages based on caught exception type
        if isinstance(e, mariadb.IntegrityError):
            error_message = str(e) # Use specific IntegrityError message from insert_gapfill_row
            status_code = 400 # Constraint violations are often client-fixable (Bad Request)
        elif isinstance(e, mariadb.Error):
            error_message = f"Database operation failed: {e}"
        elif isinstance(e, IOError): # If we raised IOError on file save fail
             error_message = str(e)
        elif isinstance(e, FileExistsError): # Should be caught earlier by explicit check
             error_message = str(e)
             status_code = 409 # Conflict
        # Add specific check for our OperationalError from get_db_cursor
        elif isinstance(e, mariadb.OperationalError):
             error_message = f"Database connection error: {e}"
             status_code = 503 # Service Unavailable

        return jsonify(error=error_message), status_code
    finally:
        # Ensure cursor is closed if it was opened (safely check if 'cur' exists and use getattr)
        if 'cur' in locals() and cur and not getattr(cur, 'closed', True):
             try: cur.close()
             except mariadb.Error as e: app.logger.error(f"Error closing cursor: {e}", exc_info=True)


# --- Run the App ---
if __name__ == "__main__":
    # Example: python app2.py
    # The host='0.0.0.0' makes it accessible on your network
    # Set debug=False for production environments
    app.run(host="0.0.0.0", port=5001, debug=True)