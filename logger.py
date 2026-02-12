"""
Logger module for CoDude.
Handles all logging configuration and setup.
"""

import logging
import os
import sys

# --- Corrected Base Path Detection ---
def get_base_path():
    """Get the base path for file operations, works for both dev and PyInstaller bundles."""
    if getattr(sys, 'frozen', False):
        # If the application is run as a bundle (compiled), the base path
        # is the directory containing the executable file.
        base_path = os.path.dirname(sys.executable)
    else:
        # If running in a normal Python environment (e.g., from source), 
        # the base path is the script's directory.
        base_path = os.path.dirname(os.path.abspath(__file__))
    # Ensure the path uses correct directory separators for the OS
    base_path = os.path.normpath(base_path)
    return base_path


BASE_PATH = get_base_path()
LOG_FILE = os.path.join(BASE_PATH, "codude.log")


def setup_logging(level='Normal', output='Both'):
    """Initialize logging with specified level and output destination."""
    levels = {
        'None': logging.NOTSET, 'Minimal': logging.ERROR, 'Normal': logging.WARNING, 
        'Extended': logging.INFO, 'Everything': logging.DEBUG
    }
    try:
        logging.getLogger().handlers = []
        logger = logging.getLogger()
        logger.setLevel(levels.get(level, logging.WARNING))
        logger.handlers = []
        if output in ['File', 'Both'] and level != 'None':
            log_dir = os.path.dirname(LOG_FILE)
            if log_dir and not os.path.exists(log_dir):
                 try: os.makedirs(log_dir)
                 except OSError as e: print(f"Warning: Could not create log directory {log_dir}: {e}")
            file_handler = logging.FileHandler(filename=LOG_FILE, mode='a', encoding='utf-8')
            file_handler.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - %(message)s'))
            logger.addHandler(file_handler)
        if output in ['Terminal', 'Both'] and level != 'None':
            console_handler = logging.StreamHandler()
            console_handler.setFormatter(logging.Formatter('%(levelname)s - %(message)s'))
            logger.addHandler(console_handler)
        if not os.path.exists(LOG_FILE) and level != 'None' and output in ['File', 'Both']:
            try:
                with open(LOG_FILE, 'a', encoding='utf-8') as f: f.write("")
                if sys.platform != 'win32':
                     try: os.chmod(LOG_FILE, 0o666) 
                     except OSError as e: logging.warning(f"Could not chmod log file: {e}")
            except OSError as e:
                logging.warning(f"Could not create or set permissions for log file {LOG_FILE}: {e}")
        logging.debug(f"Logging initialized with level: {level}, output: {output}")
    except Exception as e: print(f"Error setting up logging: {e}")
