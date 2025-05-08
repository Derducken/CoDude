import sys
import time
import os
import requests
import json
import logging
import subprocess
import glob
from datetime import datetime
from PyQt5.QtWidgets import (QApplication, QMainWindow, QVBoxLayout, QHBoxLayout, QWidget, QPushButton, QTextEdit, QLabel, 
                             QSystemTrayIcon, QMenu, QAction, QFileDialog, QMessageBox, QLineEdit, QDialog, QCheckBox, 
                             QScrollArea, QMenuBar, QProgressBar, QTabWidget, QListWidget, QListWidgetItem, QComboBox, 
                             QShortcut, QSlider, QSizePolicy, QSpacerItem, QSplitter, QInputDialog, QStyle)
from PyQt5.QtGui import QIcon, QKeySequence, QFont, QIntValidator, QTextCursor, QDesktopServices
from PyQt5.QtCore import Qt, QThread, pyqtSignal, QTimer, QEvent, QUrl

# New imports
from markdown import markdown as md_to_html
import shutil # For file backups
from functools import partial # For connecting signals
import re # For parsing recipes
from collections import deque # For recently used
import html # For escaping HTML in chat
from urllib.parse import urlparse, urljoin # For smarter URL handling

def get_base_path():
    """Get the base path for file operations, works for both dev and PyInstaller"""
    if getattr(sys, 'frozen', False):
        # Running as a PyInstaller bundle
        exe_dir = os.path.dirname(sys.executable)
        # Check if running from a temp dir (_MEIPASS) or directly
        # The check for _MEIPASS might be fragile depending on PyInstaller version/config
        base_path = sys._MEIPASS if hasattr(sys, '_MEIPASS') else exe_dir
    else:
        # Running in a normal Python environment
        base_path = os.path.dirname(os.path.abspath(__file__))
    return base_path

BASE_PATH = get_base_path()
CONFIG_FILE = os.path.join(BASE_PATH, "config.json")
ABOUT_FILE = os.path.join(BASE_PATH, "Readme.md") 
LOG_FILE = os.path.join(BASE_PATH, "codude.log")
BACKUP_DIR = os.path.join(BASE_PATH, "backups")

# --- Whitespace normalization function ---
def normalize_whitespace_for_comparison(s):
    if s is None: return ""
    # Replace all whitespace sequences with a single space and strip ends
    return ' '.join(str(s).split()).strip()

# Initialize logging
def setup_logging(level='Normal', output='Both'):
    levels = {
        'None': logging.NOTSET, 'Minimal': logging.ERROR, 'Normal': logging.WARNING, 
        'Extended': logging.INFO, 'Everything': logging.DEBUG
    }
    try:
        # Clear any existing handlers to avoid duplicates if called again
        logging.getLogger().handlers = []
        
        logger = logging.getLogger()
        logger.setLevel(levels.get(level, logging.WARNING))
        
        logger.handlers = [] # Ensure handlers list is clear
        
        if output in ['File', 'Both'] and level != 'None':
            # Ensure log directory exists
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
        
        # Create log file if it doesn't exist, only if logging to file is enabled
        if not os.path.exists(LOG_FILE) and level != 'None' and output in ['File', 'Both']:
            try:
                with open(LOG_FILE, 'a', encoding='utf-8') as f: f.write("")
                if sys.platform != 'win32':
                     try: os.chmod(LOG_FILE, 0o666) 
                     except OSError as e: logging.warning(f"Could not chmod log file: {e}")
            except OSError as e:
                logging.warning(f"Could not create or set permissions for log file {LOG_FILE}: {e}")
        
        logging.debug(f"Logging initialized with level: {level}, output: {output}")
    except Exception as e:
        # Fallback to print if logging setup fails critically
        print(f"CRITICAL ERROR setting up logging: {e}")

# Signal for updating the GUI from the hotkey listener thread
class HotkeySignal(QThread):
    text_captured = pyqtSignal(str)
    show_window = pyqtSignal()

    def __init__(self, hotkey_string):
        QThread.__init__(self)
        self.hotkey_string = hotkey_string
        logging.debug(f"HotkeySignal thread initialized with hotkey: {self.hotkey_string}")

    def run(self):
        try:
            import keyboard # This import might fail if not installed or permissions are wrong
            logging.debug("Hotkey listener thread started")
            while True:
                keyboard.wait(self.hotkey_string)
                logging.info(f"Hotkey {self.hotkey_string} activated!")
                # Use platform-specific copy command if needed, Ctrl+C is common
                keyboard.press_and_release('ctrl+c') 
                time.sleep(0.15) # Allow time for clipboard operation
                try:
                    # Use QApplication.clipboard() which works within Qt event loop context
                    clipboard_text = QApplication.clipboard().text()
                    if clipboard_text is None:
                        clipboard_text = ""
                        logging.warning("Clipboard returned None, setting empty text")
                except Exception as e:
                    clipboard_text = ""
                    logging.error(f"Failed to access clipboard: {e}")
                logging.debug(f"Captured text: {clipboard_text[:50]}")
                self.text_captured.emit(clipboard_text)
                self.show_window.emit()
        except ImportError:
            logging.error("`keyboard` library not installed. Hotkey functionality disabled (or might require sudo on Linux).")
            # Optionally, emit a signal to show a non-modal warning in the UI
        except Exception as e:
            # Avoid crashing the whole app if the listener fails
            logging.error(f"Hotkey listener error: {e}")

# Thread for sending request to LLM
class LLMRequestThread(QThread):
    response_received = pyqtSignal(str)
    error_occurred = pyqtSignal(str)

    def __init__(self, llm_config, prompt, text, timeout=60): # Increased default timeout
        QThread.__init__(self)
        self.llm_config = llm_config
        self.prompt = prompt 
        self.text = text     
        self.timeout = timeout

    def run(self):
        raw_response = "N/A" # Initialize for error reporting context
        try:
            provider = self.llm_config.get("provider", "Local OpenAI-Compatible")
            llm_url = self.llm_config.get("url", "")
            api_key = self.llm_config.get("api_key", "")
            model_name = self.llm_config.get("model_name", "gpt-3.5-turbo") 

            # Construct the user content for the LLM request
            user_content = f"{self.prompt}\n\nText: {self.text}" if self.text.strip() else self.prompt

            messages = [
                {"role": "system", "content": "You are a helpful assistant."},
                {"role": "user", "content": user_content}
            ]
            
            payload = {
                "model": model_name, 
                "messages": messages
                # Add other parameters like temperature, max_tokens if needed, maybe from config
                # "temperature": 0.7 
            }
            headers = {"Content-Type": "application/json"}
            request_url = ""

            # --- Determine Request URL and Headers ---
            if provider == "Local OpenAI-Compatible":
                if not llm_url:
                    self.error_occurred.emit("LLM URL for Local provider not configured."); return
                # Ensure URL ends with /v1/chat/completions for local servers expecting it
                parsed_url = urlparse(llm_url)
                path = parsed_url.path.rstrip('/') # Remove trailing slash if present
                # Check if ONLY the base URL was provided (e.g., http://localhost:1234)
                if not path or path == '/':
                    base_url = llm_url.rstrip('/')
                    # Use urljoin which is safer for constructing URLs
                    request_url = urljoin(f"{base_url}/", 'v1/chat/completions') 
                    logging.info(f"Assuming standard endpoint. Appending '/v1/chat/completions' to base URL. Using: {request_url}")
                elif path.endswith('/v1/chat/completions'):
                     request_url = llm_url # URL already seems complete
                else:
                    # If path exists but isn't the standard one, use it as is but log warning
                    request_url = llm_url
                    logging.warning(f"Using provided local URL as is: {request_url}. Ensure it's the correct chat completion endpoint.")

            elif provider == "OpenAI API":
                if not api_key: self.error_occurred.emit("OpenAI API Key not configured."); return
                request_url = "https://api.openai.com/v1/chat/completions"
                headers["Authorization"] = f"Bearer {api_key}"
            # Add elif for other providers (Gemini, Claude, etc.) here if needed
            else:
                self.error_occurred.emit(f"Unsupported LLM provider: {provider}"); return

            # --- Make Request ---
            logging.debug(f"Sending LLM request to {request_url} for provider {provider} with model {model_name}")
            
            response = requests.post(request_url, json=payload, headers=headers, timeout=self.timeout)
            raw_response = response.text # Get raw text regardless of status code for debugging
            
            # --- Improved Error Handling based on Status Code ---
            if response.status_code != 200:
                logging.error(f"LLM request failed with status {response.status_code}. Response: {raw_response[:500]}...")
                error_msg = f"LLM request failed (Status: {response.status_code})."
                try:
                    # Try to get specific error message from JSON if available
                    error_data = response.json()
                    if isinstance(error_data, dict) and 'error' in error_data:
                         # OpenAI format error: {"error": {"message": "...", "type": ...}}
                         if isinstance(error_data['error'], dict) and 'message' in error_data['error']:
                              error_msg += f" Message: {error_data['error']['message']}"
                         # Simpler format: {"error": "message string"}
                         elif isinstance(error_data['error'], str): 
                             error_msg += f" Message: {error_data['error']}"
                except json.JSONDecodeError: # Response wasn't valid JSON
                     error_msg += f" Raw Response: {raw_response[:200]}" 
                except Exception as parse_err: # Catch other potential errors during error parsing
                     logging.error(f"Failed to parse LLM error response: {parse_err}")
                     error_msg += f" Raw Response: {raw_response[:200]}"
                self.error_occurred.emit(error_msg)
                return # Stop processing on non-200 status

            # --- Process successful (200 OK) response ---
            logging.debug(f"Raw LLM success response: {raw_response[:500]}...")
            result = response.json() # Should succeed now based on status code check

            if not result: raise ValueError("Empty success response from LLM")
            if not isinstance(result, dict): raise ValueError(f"Invalid success response format. Expected dict, got {type(result)}")
            
            # Safely extract content, checking structure at each step
            content = None
            choices = result.get('choices')
            if isinstance(choices, list) and choices:
                first_choice = choices[0]
                if isinstance(first_choice, dict):
                    message = first_choice.get('message')
                    if isinstance(message, dict):
                        content = message.get('content')
            
            if content is None: 
                # Check for alternative common structures if standard one fails
                if 'text' in result and isinstance(result['text'], str):
                     content = result['text']
                     logging.debug("Extracted content using fallback 'text' field.")
                elif 'response' in result and isinstance(result['response'], str):
                     content = result['response']
                     logging.debug("Extracted content using fallback 'response' field.")
                else:
                     raise ValueError("No valid content found in LLM success response (checked choices[0].message.content and fallbacks).")

            if not isinstance(content, str): 
                raise ValueError(f"Invalid content type found: {type(content)}. Expected string.")

            self.response_received.emit(content)

        except requests.exceptions.Timeout:
            self.error_occurred.emit(f"LLM request timed out after {self.timeout} seconds.")
        except requests.exceptions.RequestException as e:
            # Network errors, DNS errors, connection errors etc.
            self.error_occurred.emit(f"Error communicating with LLM: {e}")
        except json.JSONDecodeError as e: 
             # Error decoding the JSON response (even if status was 200)
             self.error_occurred.emit(f"Failed to decode LLM JSON response: {e}\nRaw response glimpse: {raw_response[:200]}")
        except ValueError as e: 
             # Our custom ValueErrors for format issues
             self.error_occurred.emit(f"Invalid LLM response data: {e}\nRaw response glimpse: {raw_response[:200]}")
        except Exception as e: 
            # Catch any other unexpected errors during processing
            logging.error("Unexpected error in LLMRequestThread.run", exc_info=True)
            self.error_occurred.emit(f"An unexpected error occurred during LLM request processing: {e}")


# Window to display LLM results
class ResultWindow(QMainWindow):
    def __init__(self, response_text, parent_app, memory_index=None):
        super().__init__(parent_app)
        self.parent_app = parent_app
        self.memory_index = memory_index
        
        current_theme = self.parent_app._theme if self.parent_app else "Light"
        full_html = ""

        if parent_app and hasattr(parent_app, '_memory') and memory_index is not None and 0 <= memory_index < len(parent_app._memory):
            captured_text, prompt, _, _ = parent_app._memory[memory_index] # Original response passed as arg
            
            command_name_match = re.search(r'\*\*(.*?)\*\*', prompt) 
            command_name = command_name_match.group(1) if command_name_match else prompt.split(':')[0].split('\n')[0]

            self.setWindowTitle(f"CoDude: {html.escape(command_name[:50])}")
            
            formatted_response_html = self.parent_app.format_markdown_for_display(response_text)
            escaped_captured_text = self.parent_app.escape_html_for_manual_construct(captured_text)
            escaped_command_name_display = html.escape(command_name) 

            full_html = f"""
                <p><b>Command:</b><br/>{escaped_command_name_display}</p>
                <p><b>Text:</b><br/>{escaped_captured_text}</p>
                <p><b>LLM Reply:</b></p>
                {formatted_response_html}
            """
        else:
            # Handle case where memory entry might not be available (e.g., direct custom command result?)
            self.setWindowTitle("CoDude: LLM Result")
            formatted_response_html = self.parent_app.format_markdown_for_display(response_text) if self.parent_app else response_text
            full_html = f"<p><b>LLM Reply:</b></p>{formatted_response_html}"
            
        self.setGeometry(200, 200, 700, 500)
        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        layout = QVBoxLayout(central_widget)

        self.response_textedit = QTextEdit(self)
        self.response_textedit.setReadOnly(False) # Allow editing
        
        # Apply themed stylesheet to the document for Markdown rendering
        doc_style = self.parent_app.get_themed_document_stylesheet()
        self.response_textedit.document().setDefaultStyleSheet(doc_style)
        self.response_textedit.setHtml(full_html)
        
        self.response_textedit.textChanged.connect(self.on_text_changed_by_user_in_window)
        layout.addWidget(self.response_textedit)

        button_layout = QHBoxLayout()
        self.export_button = QPushButton("Export to Markdown", self)
        self.export_button.clicked.connect(self.export_to_markdown)
        button_layout.addWidget(self.export_button)
        self.copy_button = QPushButton("Copy HTML to Clipboard", self)
        self.copy_button.clicked.connect(self.copy_to_clipboard)
        button_layout.addWidget(self.copy_button)
        layout.addLayout(button_layout)

        # Apply base window stylesheet
        self.setStyleSheet(self.parent_app.dark_stylesheet_base if current_theme == 'Dark' else self.parent_app.light_stylesheet_base)

    def on_text_changed_by_user_in_window(self):
        # Called when user types. Save on focus loss instead for performance.
        pass 

    def focusOutEvent(self, event):
        # Save changes when the window loses focus
        if self.memory_index is not None and self.parent_app:
             # Only save if the content actually differs from what's stored?
             # For simplicity, save current HTML state. Parent handles comparison.
             self.parent_app.save_memory_content_change(self.memory_index, self.response_textedit.toHtml())
        super().focusOutEvent(event)

    def closeEvent(self, event):
        # Ensure final changes are saved before closing
        if self.memory_index is not None and self.parent_app: 
             self.parent_app.save_memory_content_change(self.memory_index, self.response_textedit.toHtml())
        # Remove self from parent's list of open windows
        if self.parent_app and hasattr(self.parent_app, 'result_windows') and self in self.parent_app.result_windows:
            try:
                self.parent_app.result_windows.remove(self)
            except ValueError:
                pass # Already removed, ignore
        super().closeEvent(event)

    def export_to_markdown(self):
        text_to_export = self.response_textedit.toPlainText() # Default to plain text
        # Try to get the original raw response (likely Markdown) from parent memory
        if self.parent_app and self.memory_index is not None and 0 <= self.memory_index < len(self.parent_app._memory):
            _, _, raw_response, _ = self.parent_app._memory[self.memory_index] 
            text_to_export = raw_response # Use the raw response for export
        
        options = QFileDialog.Options()
        file_path, _ = QFileDialog.getSaveFileName(self, "Save LLM Response", "", "Markdown Files (*.md);;Text Files (*.txt);;All Files (*)", options=options)
        if file_path:
            try:
                with open(file_path, 'w', encoding='utf-8') as f:
                    f.write(text_to_export)
                QMessageBox.information(self, "Export Successful", f"Response saved to {file_path}")
            except Exception as e:
                QMessageBox.critical(self, "Export Error", f"Could not save file: {e}")

    def copy_to_clipboard(self):
        # Copy the rendered HTML content
        QApplication.clipboard().setText(self.response_textedit.toHtml())
        QMessageBox.information(self, "Copy Successful", "HTML content copied to clipboard.")

# Custom widget for Memory entries
class MemoryEntryWidget(QWidget):
    def __init__(self, text, filename=None, parent=None):
        super().__init__(parent)
        self.filename = filename
        self.layout = QHBoxLayout(self)
        self.layout.setContentsMargins(5, 5, 5, 5); self.layout.setSpacing(5)
        
        short_text = ' '.join(text.split()[:15]) # Limit words for display
        short_text += '...' if len(text.split()) > 15 else ''
            
        self.label = QLabel(short_text, self)
        self.label.setWordWrap(True); self.label.setMinimumHeight(30) # Allow for ~2 lines
        self.layout.addWidget(self.label, 1)
        
        self.delete_button = QPushButton("Del", self) # Shorter button text
        self.delete_button.setFixedWidth(40); self.delete_button.setVisible(False) 
        self.layout.addWidget(self.delete_button)
        
        self.setMouseTracking(True) # Enable mouse tracking for hover effects

    def enterEvent(self, event): # Show delete button on hover
        self.delete_button.setVisible(True); super().enterEvent(event)
        
    def leaveEvent(self, event): # Hide delete button when mouse leaves
        self.delete_button.setVisible(False); super().leaveEvent(event)

# Dialog for editing a recipe
class EditRecipeDialog(QDialog):
    def __init__(self, recipe_name, recipe_prompt, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Edit Recipe")
        self.setMinimumWidth(450) 
        layout = QVBoxLayout(self)

        name_label = QLabel("Recipe Name (bold part):"); layout.addWidget(name_label)
        self.name_input = QLineEdit(recipe_name); layout.addWidget(self.name_input)

        prompt_label = QLabel("Recipe Command/Prompt:"); layout.addWidget(prompt_label)
        self.prompt_input = QTextEdit(recipe_prompt)
        self.prompt_input.setAcceptRichText(False) # Ensure plain text input/output
        self.prompt_input.setMinimumHeight(120); layout.addWidget(self.prompt_input)

        button_layout = QHBoxLayout()
        self.ok_button = QPushButton("OK"); self.ok_button.clicked.connect(self.accept)
        button_layout.addWidget(self.ok_button)
        self.cancel_button = QPushButton("Cancel"); self.cancel_button.clicked.connect(self.reject)
        button_layout.addWidget(self.cancel_button)
        layout.addLayout(button_layout)

    def get_data(self): # Return the edited data
        return self.name_input.text().strip(), self.prompt_input.toPlainText().strip()


# Configuration Window
class ConfigWindow(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("CoDude Configuration")
        self.setMinimumWidth(450) # Ensure reasonable width
        
        self.main_app_ref = parent # Store reference to CoDudeApp instance
        self.layout = QVBoxLayout(self)
        self.layout.setSpacing(5) 
        self.layout.setContentsMargins(10,10,10,10)

        # Helper function to create consistent rows
        def create_row_layout(*widgets_to_add):
            row = QHBoxLayout()
            row.setSpacing(5)
            for w in widgets_to_add:
                # Set fixed height for interactive elements for alignment
                if isinstance(w, (QPushButton, QLineEdit, QComboBox, QCheckBox, QLabel)):
                    w.setFixedHeight(22) 
                
                if isinstance(w, QSpacerItem):
                    row.addSpacerItem(w) # Use specific method for spacers
                else:
                    row.addWidget(w) # Add widgets normally
            return row
        
        # Helper to create labels with consistent height
        def create_label(text):
            lbl = QLabel(text, self)
            lbl.setFixedHeight(22) # Match other controls in the row
            return lbl

        # --- LLM Configuration ---
        self.llm_provider_combo = QComboBox(self)
        self.llm_provider_combo.addItems(["Local OpenAI-Compatible", "OpenAI API"])
        self.llm_provider_combo.currentTextChanged.connect(self.update_llm_fields_visibility)
        self.layout.addLayout(create_row_layout(create_label("LLM Provider:"), self.llm_provider_combo))

        self.llm_url_label = create_label("LLM URL (Local):") 
        self.llm_url_input = QLineEdit(self)
        self.llm_url_input.setPlaceholderText("e.g., http://localhost:1234") # Example base URL
        self.llm_url_row = create_row_layout(self.llm_url_label, self.llm_url_input)
        self.layout.addLayout(self.llm_url_row)

        self.openai_api_key_label = create_label("OpenAI API Key:")
        self.openai_api_key_input = QLineEdit(self)
        self.openai_api_key_input.setEchoMode(QLineEdit.Password)
        self.openai_key_row = create_row_layout(self.openai_api_key_label, self.openai_api_key_input)
        self.layout.addLayout(self.openai_key_row)

        self.model_name_input = QLineEdit(self)
        self.layout.addLayout(create_row_layout(create_label("LLM Model Name:"), self.model_name_input))
        
        self.layout.addSpacerItem(QSpacerItem(20, 10, QSizePolicy.Minimum, QSizePolicy.Fixed))

        # --- Recipe List Configuration ---
        self.max_recents_input = QLineEdit(self)
        self.max_recents_input.setValidator(QIntValidator(0, 100, self))
        self.layout.addLayout(create_row_layout(create_label("Max Recent Recipes:"), self.max_recents_input))
        
        self.max_favorites_input = QLineEdit(self)
        self.max_favorites_input.setValidator(QIntValidator(0, 100, self))
        self.layout.addLayout(create_row_layout(create_label("Max Favorite Recipes:"), self.max_favorites_input))

        self.layout.addSpacerItem(QSpacerItem(20, 10, QSizePolicy.Minimum, QSizePolicy.Fixed))

        # --- Hotkey Configuration ---
        # Add the label for the hotkey section directly
        self.layout.addWidget(create_label("Hotkey Configuration:")) 
        
        self.ctrl_checkbox = QCheckBox("Ctrl", self)
        self.shift_checkbox = QCheckBox("Shift", self)
        self.alt_checkbox = QCheckBox("Alt", self)
        # Pass QSpacerItem correctly to the helper function
        modifier_layout = create_row_layout(self.ctrl_checkbox, self.shift_checkbox, self.alt_checkbox, 
                                            QSpacerItem(40, 20, QSizePolicy.Expanding, QSizePolicy.Minimum))
        self.layout.addLayout(modifier_layout)
        
        self.main_key_input = QLineEdit(self)
        self.main_key_input.setMaxLength(1)
        self.layout.addLayout(create_row_layout(create_label("Main Hotkey Key:"), self.main_key_input))

        self.layout.addSpacerItem(QSpacerItem(20, 10, QSizePolicy.Minimum, QSizePolicy.Fixed))

        # --- UI / Behavior Configuration ---
        self.theme_combo = QComboBox(self); self.theme_combo.addItems(['Light', 'Dark'])
        self.layout.addLayout(create_row_layout(create_label("Theme:"), self.theme_combo))
        
        self.results_display_combo = QComboBox(self); self.results_display_combo.addItems(['Separate Windows', 'In-App Textarea'])
        self.layout.addLayout(create_row_layout(create_label("Results Display:"), self.results_display_combo))
        
        self.font_size_slider = QSlider(Qt.Horizontal, self); self.font_size_slider.setMinimum(8); self.font_size_slider.setMaximum(18)
        self.font_size_slider.setTickInterval(1); self.font_size_slider.setValue(10)
        self.layout.addLayout(create_row_layout(create_label("Global Font Size:"), self.font_size_slider))

        self.layout.addSpacerItem(QSpacerItem(20, 10, QSizePolicy.Minimum, QSizePolicy.Fixed))
        
        # --- File/Path Configuration ---
        self.recipes_file_input = QLineEdit(self); self.recipes_file_input.setReadOnly(True)
        browse_recipes_button = QPushButton("Browse", self); browse_recipes_button.clicked.connect(self.browse_recipes_file)
        self.layout.addLayout(create_row_layout(create_label("Recipes File:"), self.recipes_file_input, browse_recipes_button))
        
        self.permanent_memory_checkbox = QCheckBox("Permanent Memory", self); self.layout.addWidget(self.permanent_memory_checkbox) # Checkbox on its own line for clarity
        self.memory_dir_input = QLineEdit(self); self.memory_dir_input.setReadOnly(True)
        browse_memory_button = QPushButton("Browse", self); browse_memory_button.clicked.connect(self.browse_memory_dir)
        self.layout.addLayout(create_row_layout(create_label("Memory Directory:"), self.memory_dir_input, browse_memory_button))

        self.layout.addSpacerItem(QSpacerItem(20, 10, QSizePolicy.Minimum, QSizePolicy.Fixed))

        # --- Other Settings ---
        self.timeout_input = QLineEdit(self); self.timeout_input.setValidator(QIntValidator(5, 600, self)) 
        self.layout.addLayout(create_row_layout(create_label("LLM Timeout (sec):"), self.timeout_input))
        
        self.logging_combo = QComboBox(self); self.logging_combo.addItems(['None', 'Minimal', 'Normal', 'Extended', 'Everything'])
        self.layout.addLayout(create_row_layout(create_label("Logging Level:"), self.logging_combo))
        
        self.logging_output_combo = QComboBox(self); self.logging_output_combo.addItems(['Terminal', 'File', 'Both'])
        self.layout.addLayout(create_row_layout(create_label("Logging Output:"), self.logging_output_combo))
        
        self.close_behavior_combo = QComboBox(self); self.close_behavior_combo.addItems(['Exit', 'Minimize to Tray'])
        self.layout.addLayout(create_row_layout(create_label("Close Behavior:"), self.close_behavior_combo))

        self.layout.addSpacerItem(QSpacerItem(20, 10, QSizePolicy.Minimum, QSizePolicy.Expanding)) # Push buttons to bottom

        # --- Dialog Buttons ---
        button_layout_bottom = QHBoxLayout()
        save_button = QPushButton("Save", self); save_button.clicked.connect(self.save_config_values); save_button.setFixedHeight(24) # Standard button height
        button_layout_bottom.addWidget(save_button)
        cancel_button = QPushButton("Cancel", self); cancel_button.clicked.connect(self.reject); cancel_button.setFixedHeight(24)
        button_layout_bottom.addWidget(cancel_button)
        self.layout.addLayout(button_layout_bottom)

        self.load_config_values() # Load current settings into fields
        self.update_llm_fields_visibility() # Set initial visibility based on loaded provider
        self.adjustSize() # Adjust dialog size to content

    def update_llm_fields_visibility(self):
        provider = self.llm_provider_combo.currentText()
        is_local = provider == "Local OpenAI-Compatible"
        is_openai_api = provider == "OpenAI API"

        # Show/hide the relevant rows by showing/hiding their widgets
        for w in [self.llm_url_label, self.llm_url_input]: w.setVisible(is_local)
        for w in [self.openai_api_key_label, self.openai_api_key_input]: w.setVisible(is_openai_api)
        # Add similar logic here for future providers (Gemini, Claude etc.)
        
        self.adjustSize() # Resize dialog to fit visible fields

    def browse_recipes_file(self):
        options = QFileDialog.Options()
        options |= QFileDialog.DontUseNativeDialog # Optional: Use Qt's dialog
        fp, _ = QFileDialog.getOpenFileName(self, "Select Recipes File", "", "Markdown Files (*.md);;All Files (*)", options=options)
        if fp: self.recipes_file_input.setText(fp)

    def browse_memory_dir(self):
        options = QFileDialog.Options()
        options |= QFileDialog.DontUseNativeDialog
        d = QFileDialog.getExistingDirectory(self, "Select Memory Directory", options=options)
        if d: self.memory_dir_input.setText(d)

    def load_config_values(self):
        try:
            config = {}
            if os.path.exists(CONFIG_FILE):
                with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
                    config = json.load(f)
            
            self.llm_provider_combo.setCurrentText(config.get("llm_provider", "Local OpenAI-Compatible"))
            self.llm_url_input.setText(config.get("llm_url", "http://127.0.0.1:1234")) # Default Base URL
            self.openai_api_key_input.setText(config.get("openai_api_key", ""))
            self.model_name_input.setText(config.get("llm_model_name", "gpt-3.5-turbo")) # Use LLM model name field
            
            self.max_recents_input.setText(str(config.get("max_recents", 5)))
            self.max_favorites_input.setText(str(config.get("max_favorites", 5)))

            self.recipes_file_input.setText(config.get("recipes_file", os.path.join(BASE_PATH, "recipes.md")))
            hotkey = config.get("hotkey", {"ctrl": True, "alt": True, "main_key": "c"})
            self.ctrl_checkbox.setChecked(hotkey.get("ctrl", True))
            self.shift_checkbox.setChecked(hotkey.get("shift", False))
            self.alt_checkbox.setChecked(hotkey.get("alt", True))
            self.main_key_input.setText(hotkey.get("main_key", "c"))
            
            self.theme_combo.setCurrentText(config.get("theme", "Light"))
            self.results_display_combo.setCurrentText(config.get("results_display", "Separate Windows"))
            self.font_size_slider.setValue(config.get("font_size", 10))
            self.permanent_memory_checkbox.setChecked(config.get("permanent_memory", False))
            self.memory_dir_input.setText(config.get("memory_dir", os.path.join(BASE_PATH, "memory")))
            self.timeout_input.setText(str(config.get("llm_timeout", 60)))
            self.logging_combo.setCurrentText(config.get("logging_level", "Normal"))
            self.logging_output_combo.setCurrentText(config.get("logging_output", "Both"))
            self.close_behavior_combo.setCurrentText(config.get("close_behavior", "Exit"))
            
            self.update_llm_fields_visibility() # Update visibility after loading
            logging.debug("Config loaded successfully in ConfigWindow")
        except Exception as e:
            logging.error(f"Error loading config file in ConfigWindow: {e}")
            QMessageBox.warning(self, "Config Load Error", f"Could not load configuration: {e}")

    def save_config_values(self): # Renamed for clarity
        try:
            # Validate LLM URL for local provider if selected
            llm_provider_val = self.llm_provider_combo.currentText()
            llm_url_val = self.llm_url_input.text().strip()
            if llm_provider_val == "Local OpenAI-Compatible" and not llm_url_val:
                reply = QMessageBox.question(self, "LLM URL Not Set",
                                             "LLM URL for Local provider is empty. Use default 'http://127.0.0.1:1234'?",
                                             QMessageBox.Yes | QMessageBox.No | QMessageBox.Cancel)
                if reply == QMessageBox.Yes: llm_url_val = "http://127.0.0.1:1234"
                elif reply == QMessageBox.Cancel: return
                # If No, it will be saved as empty, potentially causing errors later.
            
            # Validate Memory Directory if Permanent Memory is checked
            permanent_memory_checked = self.permanent_memory_checkbox.isChecked()
            memory_dir_val = self.memory_dir_input.text().strip()
            if permanent_memory_checked and not memory_dir_val:
                default_mem_dir = os.path.join(BASE_PATH, "memory")
                reply = QMessageBox.question(self, "Memory Directory",
                                             f"Permanent Memory is enabled but no directory is selected. Create/use default '{default_mem_dir}'?",
                                             QMessageBox.Yes | QMessageBox.No | QMessageBox.Cancel)
                if reply == QMessageBox.Yes:
                    memory_dir_val = default_mem_dir
                    os.makedirs(memory_dir_val, exist_ok=True)
                    self.memory_dir_input.setText(memory_dir_val) # Update display in dialog
                elif reply == QMessageBox.Cancel: return
                # If No, save as empty.
            
            # Prepare config dictionary
            config_data = {
                "llm_provider": llm_provider_val,
                "llm_url": llm_url_val,
                "openai_api_key": self.openai_api_key_input.text(), # Don't strip API keys
                "llm_model_name": self.model_name_input.text().strip() or "gpt-3.5-turbo", # Default model if empty
                
                "max_recents": int(self.max_recents_input.text() or 5), # Default if empty
                "max_favorites": int(self.max_favorites_input.text() or 5), # Default if empty

                "recipes_file": self.recipes_file_input.text().strip(),
                "hotkey": {
                    "ctrl": self.ctrl_checkbox.isChecked(), "shift": self.shift_checkbox.isChecked(),
                    "alt": self.alt_checkbox.isChecked(), "main_key": self.main_key_input.text().strip().lower() or "c" # Default key
                },
                "logging_level": self.logging_combo.currentText(), 
                "logging_output": self.logging_output_combo.currentText(),
                "theme": self.theme_combo.currentText(), 
                "results_display": self.results_display_combo.currentText(),
                "font_size": self.font_size_slider.value(), 
                "permanent_memory": permanent_memory_checked, 
                "memory_dir": memory_dir_val,
                "llm_timeout": int(self.timeout_input.text() or 60), # Default timeout
                "close_behavior": self.close_behavior_combo.currentText(),
                
                # Fetch dynamic states from parent (CoDudeApp) if available
                "group_states": getattr(self.main_app_ref, "_group_states", {}), 
                "append_mode": getattr(self.main_app_ref, "append_mode", False), 
                "textarea_font_sizes": getattr(self.main_app_ref, "textarea_font_sizes", {}), 
                "splitter_sizes": getattr(self.main_app_ref, "splitter_sizes", [250,350,300]), # Use parent's current or default
                "recently_used_recipes": list(getattr(self.main_app_ref, "recently_used_recipes", deque())), 
                "favorite_recipes": getattr(self.main_app_ref, "favorite_recipes", [])
            }
            
            # Save to file
            with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
                json.dump(config_data, f, indent=4)
            
            QMessageBox.information(self, "Config Saved", "Configuration saved successfully.")
            logging.debug("Config saved successfully")
            self.accept() # Closes dialog with QDialog.Accepted result
            
        except ValueError as ve: # Catch errors from int() conversion
            logging.error(f"Invalid input for a numeric field: {ve}")
            QMessageBox.critical(self, "Input Error", f"Invalid numeric value entered: {ve}")
        except Exception as e:
            logging.error(f"Could not save config file: {e}", exc_info=True)
            QMessageBox.critical(self, "Save Error", f"Could not save config file: {e}")


class CoDudeApp(QMainWindow):
    def __init__(self):
        super().__init__()
        self._minimized_by_shortcut = False
        logging.info("Starting CoDudeApp initialization")
        self.setWindowTitle("CoDude")
        self.setGeometry(100, 100, 900, 800) # Default size
        self.setWindowFlags(Qt.Window | Qt.WindowStaysOnTopHint) # Keep on top

        # Initialize attributes with defaults or empty values
        self._group_states = {}
        self._memory = [] # Stores (captured_text, prompt, response_content, filename)
        self._all_recipes_data = [] # Parsed from recipes.md: list of dicts
        
        self.result_windows = []
        self.textarea_font_sizes = {}
        self.results_in_app = False # Controlled by config
        self.append_mode = False # Controlled by config and checkbox
        self.font_size = 10 # Default, from config
        
        self.permanent_memory = False
        self.memory_dir = ""
        
        self.llm_provider = "Local OpenAI-Compatible"
        self.llm_url = "http://127.0.0.1:1234" # Base URL default
        self.openai_api_key = ""
        self.llm_model_name = "gpt-3.5-turbo" # Default, can be overridden by config

        self.recipes_file = ""
        self._theme = "Light" # Default, from config
        self.active_memory_index = None # Index in self._memory for current display in results_textedit
        self._deleting_memory = False # Semaphore for memory deletion
        self.splitter_sizes = [250, 350, 300] # Default, recipes, captured/memory, results

        self.max_recents = 5
        self.max_favorites = 5
        self.recently_used_recipes = deque(maxlen=self.max_recents) 
        self.favorite_recipes = [] # Stores (name, prompt) tuples
        
        self.dark_stylesheet_base = "" # Populated by apply_theme
        self.light_stylesheet_base = "" # Populated by apply_theme

        # --- Setup UI ---
        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        main_layout = QVBoxLayout(central_widget)

        # --- Menu Bar ---
        menubar = QMenuBar(self); self.setMenuBar(menubar)
        codude_menu = menubar.addMenu("CoDude")
        configure_action = QAction("Configure", self); configure_action.triggered.connect(self.open_config_window)
        codude_menu.addAction(configure_action)
        open_recipes_action = QAction("Open Recipes.md", self); open_recipes_action.triggered.connect(self.open_recipes_file_externally)
        codude_menu.addAction(open_recipes_action)
        about_action = QAction("About", self); about_action.triggered.connect(self.show_about)
        codude_menu.addAction(about_action)
        quit_action = QAction("Quit", self); quit_action.triggered.connect(QApplication.instance().quit)
        codude_menu.addAction(quit_action)

        # --- Main Splitter Layout ---
        self.splitter = QSplitter(Qt.Horizontal); self.splitter.setHandleWidth(5)
        main_layout.addWidget(self.splitter, 1) # Give splitter stretch factor

        # --- Load Configuration Early ---
        self.validate_and_load_config() # Load settings before creating widgets that depend on them

        # --- Left Column (Recipes) ---
        left_widget = QWidget()
        self.left_layout = QVBoxLayout(left_widget); self.left_layout.setContentsMargins(5,5,5,5); self.left_layout.setSpacing(3)
        
        search_layout = QHBoxLayout(); search_layout.setSpacing(3)
        search_layout.addWidget(QLabel("Search:", self)) 
        self.search_input = QLineEdit(self); self.search_input.setPlaceholderText("Filter recipes...")
        self.search_input.setFixedHeight(22) # Consistent height
        self.search_input.textChanged.connect(self.filter_recipes_display)
        search_layout.addWidget(self.search_input); self.left_layout.addLayout(search_layout)

        self.recipes_scroll_area = QScrollArea()
        self.recipes_scroll_area.setWidgetResizable(True)
        self.recipes_scroll_widget = QWidget() 
        self.recipe_buttons_layout = QVBoxLayout(self.recipes_scroll_widget)
        self.recipe_buttons_layout.setAlignment(Qt.AlignTop)
        self.recipe_buttons_layout.setContentsMargins(0,0,0,0) 
        self.recipe_buttons_layout.setSpacing(1) # Minimal spacing between items
        self.recipes_scroll_area.setWidget(self.recipes_scroll_widget)
        self.left_layout.addWidget(self.recipes_scroll_area)

        self.input_mode_combo = QComboBox(self)
        self.input_mode_combo.addItems(["Custom Input:", "Chat Mode:"])
        self.input_mode_combo.setFixedHeight(24) # Slightly taller for combo box
        self.input_mode_combo.currentTextChanged.connect(self.on_input_mode_changed)
        self.left_layout.addWidget(self.input_mode_combo)
        
        self.custom_input_textedit = QTextEdit(self)
        self.custom_input_textedit.setToolTip("Enter custom instructions or chat message here (Ctrl+Enter to send).")
        self.custom_input_textedit.setMaximumHeight(100) 
        self.left_layout.addWidget(self.custom_input_textedit)

        custom_controls_layout = QHBoxLayout(); custom_controls_layout.setSpacing(3)
        send_custom_button = QPushButton("Send", self); send_custom_button.setFixedHeight(24)
        send_custom_button.clicked.connect(self.send_custom_or_chat_command)
        custom_controls_layout.addWidget(send_custom_button, 1) # Give stretch factor
        custom_font_up = QPushButton("↑", self); custom_font_up.setFixedSize(24, 24)
        custom_font_up.clicked.connect(lambda: self.adjust_textarea_font(self.custom_input_textedit, 1))
        custom_controls_layout.addWidget(custom_font_up)
        custom_font_down = QPushButton("↓", self); custom_font_down.setFixedSize(24, 24)
        custom_font_down.clicked.connect(lambda: self.adjust_textarea_font(self.custom_input_textedit, -1))
        custom_controls_layout.addWidget(custom_font_down)
        self.left_layout.addLayout(custom_controls_layout)
        self.splitter.addWidget(left_widget)

        # --- Middle Column (Tabs: Captured Text / Memory) ---
        tabs_widget = QWidget()
        tabs_layout = QVBoxLayout(tabs_widget); tabs_layout.setContentsMargins(0,0,0,0) 
        right_tabs = QTabWidget(self) # Name kept for less diff, but it's middle pane
        
        # Captured Text Tab
        captured_widget = QWidget(); captured_layout = QVBoxLayout(captured_widget)
        captured_layout.addWidget(QLabel("Captured Text:", self))
        self.captured_text_edit = QTextEdit(self)
        captured_layout.addWidget(self.captured_text_edit, 1)
        captured_font_layout = QHBoxLayout(); captured_font_layout.addStretch()
        cap_font_up = QPushButton("↑",self); cap_font_up.setFixedSize(24,24); cap_font_up.clicked.connect(lambda: self.adjust_textarea_font(self.captured_text_edit,1)); captured_font_layout.addWidget(cap_font_up)
        cap_font_down = QPushButton("↓",self); cap_font_down.setFixedSize(24,24); cap_font_down.clicked.connect(lambda: self.adjust_textarea_font(self.captured_text_edit,-1)); captured_font_layout.addWidget(cap_font_down)
        captured_layout.addLayout(captured_font_layout)
        right_tabs.addTab(captured_widget, "Captured Text")
        
        # Memory Tab
        memory_widget = QWidget(); memory_layout = QVBoxLayout(memory_widget)
        memory_layout.addWidget(QLabel("CoDude's Memory:", self))
        self.memory_list = QListWidget(self)
        self.memory_list.itemDoubleClicked.connect(self.show_memory_entry_from_list_item)
        memory_layout.addWidget(self.memory_list, 1)
        right_tabs.addTab(memory_widget, "Memory")
        
        tabs_layout.addWidget(right_tabs, 1)
        self.splitter.addWidget(tabs_widget)

        # --- Right Column (LLM Results / Chat) ---
        self.results_container = QWidget()
        results_layout = QVBoxLayout(self.results_container); results_layout.setContentsMargins(5,5,5,5); results_layout.setSpacing(3)
        results_layout.addWidget(QLabel("LLM Results:", self))
        self.results_textedit = QTextEdit(self)
        self.results_textedit.setReadOnly(False) # Keep editable for now
        self.results_textedit.textChanged.connect(self.on_results_text_changed_by_user)
        results_layout.addWidget(self.results_textedit, 1)
        
        results_controls_layout = QHBoxLayout(); results_controls_layout.setSpacing(3)
        self.append_mode_checkbox = QCheckBox("Append Mode", self); self.append_mode_checkbox.setFixedHeight(22)
        self.append_mode_checkbox.stateChanged.connect(self.save_append_mode_state)
        results_controls_layout.addWidget(self.append_mode_checkbox)
        export_results_button = QPushButton("Export", self); export_results_button.setFixedHeight(24); export_results_button.clicked.connect(self.export_results_to_markdown)
        results_controls_layout.addWidget(export_results_button)
        copy_results_button = QPushButton("Copy HTML", self); copy_results_button.setFixedHeight(24); copy_results_button.clicked.connect(self.copy_results_to_clipboard)
        results_controls_layout.addWidget(copy_results_button)
        results_controls_layout.addStretch() # Push font buttons to right
        res_font_up = QPushButton("↑", self); res_font_up.setFixedSize(24,24); res_font_up.clicked.connect(lambda: self.adjust_textarea_font(self.results_textedit,1))
        results_controls_layout.addWidget(res_font_up)
        res_font_down = QPushButton("↓", self); res_font_down.setFixedSize(24,24); res_font_down.clicked.connect(lambda: self.adjust_textarea_font(self.results_textedit,-1))
        results_controls_layout.addWidget(res_font_down)
        results_layout.addLayout(results_controls_layout)
        self.splitter.addWidget(self.results_container)
        
        # --- Initial UI State Setup ---
        self.results_container.setVisible(self.results_in_app)
        if not self.results_in_app and len(self.splitter_sizes) == 3:
             self.splitter.setSizes([self.splitter_sizes[0], self.splitter_sizes[1] + self.splitter_sizes[2], 0]) # Hide 3rd pane
        else:
            self.splitter.setSizes(self.splitter_sizes) # Apply loaded/default sizes
        self.splitter.splitterMoved.connect(self.save_splitter_sizes)

        # --- Status Bar ---
        self.status_bar = self.statusBar()
        self.progress_bar = QProgressBar(self); self.progress_bar.setMaximumWidth(200); self.progress_bar.setVisible(False)
        self.status_bar.addPermanentWidget(self.progress_bar)

        # --- System Tray Icon ---
        self.tray_icon = QSystemTrayIcon(self)
        icon_path = os.path.join(BASE_PATH, 'text-analytics.png') # Ensure icon is found relative to base path
        if os.path.exists(icon_path):
            self.tray_icon.setIcon(QIcon(icon_path))
        else:
            logging.warning(f"Icon file not found: {icon_path}. Using default.")
            self.tray_icon.setIcon(self.style().standardIcon(QStyle.SP_ComputerIcon)) # Use a standard system icon as fallback
        self.tray_icon.setToolTip("CoDude")
        tray_menu = QMenu()
        show_action = QAction("Show/Hide", self); show_action.triggered.connect(self.show_hide_window)
        tray_menu.addAction(show_action); tray_menu.addSeparator()
        exit_action = QAction("Exit", self); exit_action.triggered.connect(QApplication.instance().quit)
        tray_menu.addAction(exit_action)
        self.tray_icon.setContextMenu(tray_menu)
        self.tray_icon.activated.connect(self.on_tray_icon_activated)
        self.tray_icon.show()
        
        # --- Shortcuts ---
        self.custom_command_shortcut = QShortcut(QKeySequence("Ctrl+Return"), self)
        self.custom_command_shortcut.activated.connect(self.send_custom_or_chat_command) # Connect to unified handler

        # --- Final Initialization Steps ---
        self.load_recipes_and_populate_list() # Populate recipes list
        self.apply_theme() # Applies styles and font sizes based on config
        self.append_mode_checkbox.setChecked(self.append_mode) # Set checkbox from loaded config
        self.on_input_mode_changed(self.input_mode_combo.currentText()) # Initialize chat/custom mode UI state

        # Load permanent memory if enabled and directory exists
        if self.permanent_memory and self.memory_dir and os.path.exists(self.memory_dir):
            self.load_permanent_memory_entries() 

        # Start hotkey listener after a short delay
        QTimer.singleShot(1000, self.start_hotkey_thread) 
        logging.info("CoDudeApp initialization complete")

    # --- Theming and Markdown ---
    def get_themed_document_stylesheet(self):
        font_family = self.font().family() # Use app's base font family
        current_doc_font_size = self.font_size # Use global font size for base document size
        
        # Define base CSS common to light/dark themes
        base_css = f"""
            body {{ font-family: "{font_family}"; font-size: {current_doc_font_size}pt; margin: 5px; line-height: 1.4; }}
            p {{ margin: 0.5em 0; }}
            h1, h2, h3, h4, h5, h6 {{ margin-top: 1em; margin-bottom: 0.5em; font-weight: bold; line-height: 1.2; }}
            /* Relative font sizes for headers based on base size */
            h1 {{ font-size: {current_doc_font_size + 6}pt; }} h2 {{ font-size: {current_doc_font_size + 4}pt; }} 
            h3 {{ font-size: {current_doc_font_size + 2}pt; }} h4 {{ font-size: {current_doc_font_size + 1}pt;}}
            ul, ol {{ margin-left: 1.5em; padding-left: 0.5em; }} li {{ margin-bottom: 0.3em; }}
            /* Code blocks styling */
            pre {{ padding: 0.8em; border-radius: 5px; overflow-x: auto; white-space: pre-wrap; word-wrap: break-word; font-family: Consolas, Monaco, 'Andale Mono', 'Ubuntu Mono', monospace; font-size: {max(8, current_doc_font_size -1)}pt; }}
            code {{ padding: 0.1em 0.3em; border-radius: 3px; font-family: Consolas, Monaco, 'Andale Mono', 'Ubuntu Mono', monospace; font-size: {max(8, current_doc_font_size -1)}pt;}}
            /* Reset code style inside pre */
            pre code {{ padding: 0; background-color: transparent; border: none; font-size: inherit; }} 
            blockquote {{ border-left: 3px solid; padding-left: 1em; margin: 0.8em 0; font-style: italic;}}
            table {{ border-collapse: collapse; width: auto; max-width: 98%; margin: 1em auto; box-shadow: 0 0 3px rgba(0,0,0,0.1); }}
            th, td {{ border: 1px solid; padding: 0.5em; text-align: left; }}
            hr {{ border: 0; border-top: 1px solid; margin: 1em 0; }}
            /* Special think block style */
            .think-block {{ border: 1px dashed; border-radius: 5px; padding: 0.8em; margin: 0.8em 0; font-style: italic; opacity: 0.8; }}
        """
        # Append theme-specific colors and styles
        if self._theme == 'Dark':
            return base_css + f"""
                body {{ background-color: #3c3f41; color: #e0e0e0; }}
                h1, h2, h3, h4, h5, h6 {{ color: #79a6dc; border-bottom: 1px solid #4a4a4f; padding-bottom: 0.2em;}}
                pre {{ background-color: #2a2a2e; color: #d0d0d0; border: 1px solid #4a4a4f; }}
                code {{ background-color: #2a2a2e; color: #d0d0d0; }}
                blockquote {{ border-left-color: #557799; color: #b0b0b0; background-color: #404048;}}
                th, td {{ border-color: #555555; }} th {{ background-color: #45454a; }}
                hr {{ border-top-color: #555555; }} a {{ color: #82b1ff; }}
                .think-block {{ background-color: #404048; border-color: #557799; color: #b0b0b0; }}
            """
        else: # Light Theme
            return base_css + f"""
                body {{ background-color: #ffffff; color: #1e1e1e; }}
                h1, h2, h3, h4, h5, h6 {{ color: #003366; border-bottom: 1px solid #e0e0e0; padding-bottom: 0.2em; }}
                pre {{ background-color: #f0f0f0; color: #2e2e2e; border: 1px solid #cccccc; }}
                code {{ background-color: #f0f0f0; color: #2e2e2e; }}
                blockquote {{ border-left-color: #cccccc; color: #444444; background-color: #f8f8f8;}}
                th, td {{ border-color: #cccccc; }} th {{ background-color: #e8e8e8; }}
                hr {{ border-top-color: #cccccc; }} a {{ color: #007acc; }}
                .think-block {{ background-color: #f8f8f8; border-color: #ccc; color: #444; }}
            """

    def format_markdown_for_display(self, markdown_text):
        if markdown_text is None: markdown_text = ""
        # Pre-process special tags like <think> before Markdown conversion
        text_for_md = markdown_text.replace('<think>', '<div class="think-block">')
        text_for_md = text_for_md.replace('</think>', '</div>')
        # Convert Markdown to HTML using python-markdown library
        # Added extensions for common features like fenced code blocks, tables, better lists, line breaks, attribute lists
        html_output = md_to_html(text_for_md, extensions=['fenced_code', 'tables', 'sane_lists', 'nl2br', 'attr_list'])
        return html_output 

    def escape_html_for_manual_construct(self, text):
        """ Escapes text to be safely included in manually constructed HTML """
        if text is None: return ""
        return html.escape(str(text)).replace("\n", "<br/>")

    # --- UI Event Handlers ---
    def on_input_mode_changed(self, mode_text):
        is_chat_mode = (mode_text == "Chat Mode:")
        self.append_mode_checkbox.setChecked(is_chat_mode)
        self.append_mode_checkbox.setEnabled(not is_chat_mode) # Disable append toggle in chat mode
        self.custom_input_textedit.setPlaceholderText(
            "Enter chat message (Ctrl+Enter)" if is_chat_mode 
            else "Enter custom instructions (Ctrl+Enter)"
        )
        # Optionally clear results pane or add a "Chat started" message when switching to Chat Mode
        if is_chat_mode and self.results_in_app and not self.results_textedit.toPlainText().strip() :
             # Avoid clearing if there's already chat history
             self.results_textedit.setHtml("<p style='color: grey; font-style: italic;'>Chat mode started. Type your message below.</p>")

    def _save_partial_config(self, updates_dict):
        """ Safely updates specific keys in the config file """
        try:
            config = {}
            if os.path.exists(CONFIG_FILE):
                with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
                    try:
                        config = json.load(f)
                    except json.JSONDecodeError as e:
                        logging.error(f"Error decoding config file {CONFIG_FILE}: {e}. Config saving aborted.")
                        return # Avoid overwriting corrupted config
            config.update(updates_dict)
            with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
                json.dump(config, f, indent=4)
        except Exception as e:
            logging.error(f"Error saving partial config: {e}")

    def save_splitter_sizes(self, pos, index): # pos, index are arguments from QSplitter.splitterMoved
        """ Saves the current splitter sizes to config """
        try:
            current_sizes = self.splitter.sizes()
            # Check if results_container is visible to decide how to save sizes for 3rd pane
            if self.results_container.isVisible() and len(current_sizes) == 3:
                # Ensure minimum sizes aren't violated (e.g., 50 pixels)
                min_width = 50
                self.splitter_sizes = [max(min_width, s) for s in current_sizes]
            elif not self.results_container.isVisible() and len(current_sizes) >= 2:
                # Save first two, third is effectively 0 or very small
                min_width = 50
                self.splitter_sizes = [max(min_width, current_sizes[0]), max(min_width, current_sizes[1]), 0] # Store 0 for hidden
            else: # Fallback or unexpected number of widgets in splitter
                logging.warning(f"Splitter has unexpected widget count: {len(current_sizes)}. Sizes not saved robustly.")
                return # Avoid saving potentially wrong sizes

            self._save_partial_config({'splitter_sizes': self.splitter_sizes})
            logging.debug(f"Splitter sizes saved: {self.splitter_sizes}")
        except Exception as e:
            logging.error(f"Error saving splitter sizes: {e}")


    # --- Hotkey Handling ---
    def start_hotkey_thread(self):
        try:
            hotkey_string = self.load_hotkey_config_string()
            if not hotkey_string:
                logging.warning("Hotkey string is empty or invalid. Hotkey listener not started.")
                return # Don't show pop-up on startup generally

            # Terminate existing thread if running (e.g., after config change)
            if hasattr(self, 'hotkey_thread') and self.hotkey_thread and self.hotkey_thread.isRunning():
                logging.info("Terminating existing hotkey thread...")
                self.hotkey_thread.terminate()
                self.hotkey_thread.wait(500) # Wait briefly for termination

            self.hotkey_thread = HotkeySignal(hotkey_string)
            self.hotkey_thread.text_captured.connect(self.update_captured_text_area)
            self.hotkey_thread.show_window.connect(self.show_hide_window)
            self.hotkey_thread.start()
            logging.info(f"Hotkey thread started with {hotkey_string}")
        except Exception as e:
            logging.error(f"Error starting hotkey thread: {e}")
            if "keyboard" not in str(e).lower(): # Show pop-up for non-library errors
                QMessageBox.critical(self, "Hotkey Error", f"Could not start hotkey listener: {e}")

    def load_hotkey_config_string(self):
        """ Constructs the hotkey string (e.g., 'ctrl+alt+c') from config """
        default_hotkey = 'ctrl+alt+c'
        try:
            config = {}
            if os.path.exists(CONFIG_FILE):
                with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
                    config = json.load(f)
            
            hotkey_cfg = config.get("hotkey", {"ctrl": True, "alt": True, "main_key": "c"})
            ctrl = hotkey_cfg.get("ctrl", False)
            shift = hotkey_cfg.get("shift", False)
            alt = hotkey_cfg.get("alt", False)
            main_k = hotkey_cfg.get("main_key", "c").lower().strip()

            modifiers = []
            if ctrl: modifiers.append("ctrl")
            if shift: modifiers.append("shift")
            if alt: modifiers.append("alt")

            # Define valid main keys (alphanumeric + common symbols)
            valid_chars = "abcdefghijklmnopqrstuvwxyz0123456789`-=[]\\;',./" 
            if not main_k or len(main_k) != 1 or main_k not in valid_chars:
                logging.warning(f"Invalid main key '{main_k}', using default hotkey {default_hotkey}")
                return default_hotkey
            
            hotkey_str = '+'.join(modifiers + [main_k]) if modifiers else main_k
            if not hotkey_str: return default_hotkey # Should not happen if main_key is valid
            logging.debug(f"Loaded hotkey string: {hotkey_str}")
            return hotkey_str
        except Exception as e:
            logging.error(f"Error loading hotkey config string: {e}")
            return default_hotkey

    # --- Configuration Loading ---
    def validate_and_load_config(self):
        # Define default values first
        default_recipes_path = os.path.join(BASE_PATH, "recipes.md")
        default_memory_path = os.path.join(BASE_PATH, "memory")
        default_config = {
            "llm_provider": "Local OpenAI-Compatible",
            "llm_url": "http://127.0.0.1:1234", # Default Base URL
            "openai_api_key": "",
            "llm_model_name": "gpt-3.5-turbo", # Default model
            "recipes_file": default_recipes_path,
            "hotkey": {"ctrl": True, "shift": False, "alt": True, "main_key": "c"},
            "logging_level": "Normal", "logging_output": "Both",
            "theme": "Light",
            "group_states": {}, # Stores {group_title: is_expanded}
            "results_display": "Separate Windows", 
            "font_size": 10,
            "permanent_memory": False, "memory_dir": default_memory_path,
            "append_mode": False, # For non-chat mode results pane
            "textarea_font_sizes": {}, # Stores {widget_id: font_size}
            "splitter_sizes": self.splitter_sizes, # Use initial default
            "llm_timeout": 60, # Default timeout in seconds
            "close_behavior": "Exit", # or "Minimize to Tray"
            "max_recents": 5, "max_favorites": 5,
            "recently_used_recipes": [], # List of (name, prompt) tuples
            "favorite_recipes": [] # List of (name, prompt) tuples
        }
        try:
            logging.debug(f"Validating and loading config from {CONFIG_FILE}")
            os.makedirs(os.path.dirname(CONFIG_FILE), exist_ok=True) # Ensure config dir exists
            
            config_to_load = default_config.copy() # Start with defaults
            if os.path.exists(CONFIG_FILE):
                with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
                    loaded_config = json.load(f)
                    # Merge loaded config over defaults to ensure all keys exist and handle new keys
                    for key in config_to_load: 
                        if key in loaded_config:
                            config_to_load[key] = loaded_config[key]
            else: # No config file, create one with defaults
                 with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
                    json.dump(default_config, f, indent=4)
                 logging.info(f"Default config file created at {CONFIG_FILE}")

            # Apply loaded/default values to instance attributes
            self.llm_provider = config_to_load['llm_provider']
            self.llm_url = config_to_load['llm_url']
            self.openai_api_key = config_to_load['openai_api_key']
            self.llm_model_name = config_to_load['llm_model_name']
            self.recipes_file = config_to_load['recipes_file']
            # Ensure paths are absolute or made relative to BASE_PATH
            if self.recipes_file and not os.path.isabs(self.recipes_file):
                self.recipes_file = os.path.join(BASE_PATH, self.recipes_file)
            self.hotkey_config = config_to_load['hotkey'] # Store the dict
            
            # Setup logging based on loaded level AFTER potentially creating log file/dir
            setup_logging(config_to_load['logging_level'], config_to_load['logging_output'])
            
            self._theme = config_to_load['theme']
            self._group_states = config_to_load.get('group_states', {}) # Use get for robustness if key missing
            self.results_in_app = config_to_load['results_display'] == 'In-App Textarea'
            self.font_size = config_to_load.get('font_size', 10)
            self.permanent_memory = config_to_load.get('permanent_memory', False)
            self.memory_dir = config_to_load.get('memory_dir', default_memory_path)
            if self.memory_dir and not os.path.isabs(self.memory_dir):
                self.memory_dir = os.path.join(BASE_PATH, self.memory_dir)
            # Ensure memory directory exists if permanent memory is enabled
            if self.permanent_memory and self.memory_dir:
                os.makedirs(self.memory_dir, exist_ok=True)


            self.append_mode = config_to_load.get('append_mode', False)
            self.textarea_font_sizes = config_to_load.get('textarea_font_sizes', {}) 
            
            # Validate and apply splitter sizes
            loaded_splitter_sizes = config_to_load.get('splitter_sizes', self.splitter_sizes)
            if isinstance(loaded_splitter_sizes, list) and len(loaded_splitter_sizes) == 3 and all(isinstance(s, int) and s >= 0 for s in loaded_splitter_sizes):
                self.splitter_sizes = loaded_splitter_sizes
            else: # Fallback to default if invalid format/values
                logging.warning(f"Invalid splitter_sizes in config: {loaded_splitter_sizes}. Using default: {default_config['splitter_sizes']}")
                self.splitter_sizes = default_config['splitter_sizes']


            self.llm_timeout = config_to_load.get('llm_timeout', 60)
            self.close_behavior = config_to_load.get('close_behavior', "Exit")
            self.max_recents = config_to_load.get('max_recents', 5)
            self.max_favorites = config_to_load.get('max_favorites', 5)
            
            # Load and validate recent/favorite lists (ensure they are lists of pairs)
            self.recently_used_recipes = deque(
                [tuple(item) for item in config_to_load.get('recently_used_recipes', []) if isinstance(item, list) and len(item) == 2], 
                maxlen=self.max_recents if self.max_recents > 0 else None # maxlen=None for unlimited if 0
            )
            self.favorite_recipes = [tuple(item) for item in config_to_load.get('favorite_recipes', []) if isinstance(item, list) and len(item) == 2]
            
            logging.debug("Config loaded successfully.")

        except json.JSONDecodeError as json_err:
             logging.error(f"Config file {CONFIG_FILE} is invalid JSON: {json_err}. Using defaults.", exc_info=True)
             QMessageBox.critical(self, "Config Error", f"Config file is corrupted (invalid JSON).\nPlease fix or delete '{CONFIG_FILE}'.\nUsing default settings for now.")
             # Apply defaults strictly after critical error
             for key, value in default_config.items():
                 try: setattr(self, key, value)
                 except: logging.error(f"Failed to set default for {key}")
             if not os.path.isabs(self.recipes_file): self.recipes_file = os.path.join(BASE_PATH, self.recipes_file)

        except Exception as e:
            logging.error(f"Config validation/loading failed: {e}. Using hardcoded defaults.", exc_info=True)
            QMessageBox.warning(self, "Config Error", f"Invalid or missing config file. Using defaults.\nDetails: {e}")
            # Apply hardcoded defaults directly if full load fails
            for key, value in default_config.items(): # Apply defaults strictly
                try: setattr(self, key, value)
                except: logging.error(f"Failed to set default for {key}")
            if not os.path.isabs(self.recipes_file): self.recipes_file = os.path.join(BASE_PATH, self.recipes_file)
            # Attempt to save a fresh default config if loading failed badly
            try:
                with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
                    json.dump(default_config, f, indent=4)
            except Exception as save_e:
                logging.error(f"Failed to write default config after error: {save_e}")

    # --- Theming and UI Styling ---
    def apply_theme(self):
        try:
            logging.debug(f"Applying theme: {self._theme} with font size {self.font_size}pt")
            app = QApplication.instance()
            base_font = QFont(self.font().family(), self.font_size) # Use app's default family, configured size
            app.setFont(base_font)

            # Define base stylesheets including text-align fix for general buttons
            self.light_stylesheet_base = f"""
                QMainWindow, QWidget {{ background-color: #f0f0f0; color: #000000; }}
                QTextEdit, QLineEdit {{ background-color: #ffffff; color: #000000; border: 1px solid #cccccc; }}
                QPushButton {{ background-color: #e0e0e0; color: #000000; border: 1px solid #bbbbbb; padding: 3px 6px; text-align: left; }} /* Default left align */
                QPushButton:hover {{ background-color: #d0d0d0; }}
                QPushButton#groupButton {{ background-color: #d8d8d8; font-weight: bold; text-align: left; border: 1px solid #b0b0b0; }} /* Group button specific, now left */
                QComboBox {{ background-color: #ffffff; color: #000000; border: 1px solid #cccccc; padding: 1px; min-height: 20px; }}
                QTabWidget::pane {{ border: 1px solid #cccccc; background: #f0f0f0; }}
                QTabBar::tab {{ background: #e0e0e0; color: #000000; padding: 4px; border: 1px solid #cccccc; border-bottom: none; }}
                QTabBar::tab:selected {{ background: #f0f0f0; }}
                QScrollArea {{ background-color: #f0f0f0; border: none; }}
                QScrollBar:vertical {{ background: #e0e0e0; width: 12px; margin: 0px; }} QScrollBar::handle:vertical {{ background: #c0c0c0; min-height: 20px; border-radius: 6px;}}
                QScrollBar:horizontal {{ background: #e0e0e0; height: 12px; margin: 0px; }} QScrollBar::handle:horizontal {{ background: #c0c0c0; min-width: 20px; border-radius: 6px;}}
                QMenuBar {{ background-color: #e0e0e0; color: #000000; }}
                QMenu {{ background-color: #ffffff; color: #000000; border: 1px solid #cccccc; }}
                QMenu::item:selected {{ background-color: #0078d7; color: #ffffff; }}
                QLabel, QCheckBox {{ color: #000000; }}
                QSplitter::handle {{ background: #cccccc; }} QSplitter::handle:hover {{ background: #bbbbbb; }}
                QDialog {{ background-color: #f0f0f0; }} 
            """
            self.dark_stylesheet_base = f"""
                QMainWindow, QWidget {{ background-color: #2b2b2b; color: #e0e0e0; }}
                QTextEdit, QLineEdit {{ background-color: #3c3f41; color: #e0e0e0; border: 1px solid #555555; }}
                QPushButton {{ background-color: #4a4a4a; color: #e0e0e0; border: 1px solid #5f5f5f; padding: 3px 6px; text-align: left; }} /* Default left align */
                QPushButton:hover {{ background-color: #5a5a5a; }}
                QPushButton#groupButton {{ background-color: #525252; font-weight: bold; text-align: left; border: 1px solid #666666; }} /* Group button specific, now left */
                QComboBox {{ background-color: #3c3f41; color: #e0e0e0; border: 1px solid #555555; selection-background-color: #5a5a5a; padding: 1px; min-height: 20px; }}
                QComboBox QAbstractItemView {{ background-color: #3c3f41; color: #e0e0e0; selection-background-color: #5a5a5a; border: 1px solid #555555;}}
                QTabWidget::pane {{ border: 1px solid #555555; background: #2b2b2b; }}
                QTabBar::tab {{ background: #3c3f41; color: #e0e0e0; padding: 4px; border: 1px solid #555555; border-bottom: none; }}
                QTabBar::tab:selected {{ background: #2b2b2b; }}
                QScrollArea {{ background-color: #2b2b2b; border: none; }}
                QScrollBar:vertical {{ background: #3c3f41; width: 12px; margin: 0px; }} QScrollBar::handle:vertical {{ background: #5a5a5a; min-height: 20px; border-radius: 6px; }}
                QScrollBar:horizontal {{ background: #3c3f41; height: 12px; margin: 0px; }} QScrollBar::handle:horizontal {{ background: #5a5a5a; min-width: 20px; border-radius: 6px; }}
                QMenuBar {{ background-color: #3c3f41; color: #e0e0e0; }}
                QMenu {{ background-color: #3c3f41; color: #e0e0e0; border: 1px solid #555555; }}
                QMenu::item:selected {{ background-color: #0078d7; color: #ffffff; }}
                QLabel, QCheckBox {{ color: #e0e0e0; }}
                QSplitter::handle {{ background: #555555; }} QSplitter::handle:hover {{ background: #666666; }}
                QDialog {{ background-color: #2b2b2b; }} 
            """
            
            chosen_stylesheet = self.dark_stylesheet_base if self._theme == 'Dark' else self.light_stylesheet_base
            chosen_stylesheet += f" * {{ font-size: {self.font_size}pt; }}" # Apply global font size as base
            app.setStyleSheet(chosen_stylesheet)

            # Apply specific font sizes and document styles to text areas
            doc_style = self.get_themed_document_stylesheet()
            text_areas_to_style = [
                (self.custom_input_textedit, False), 
                (self.captured_text_edit, False),
                (self.results_textedit, True) # True means apply doc_style for markdown
            ]
            for textarea, is_markdown_view in text_areas_to_style:
                textarea_id = str(id(textarea))
                size_pt = self.textarea_font_sizes.get(textarea_id, self.font_size) # Get specific or global size
                font = textarea.font() # Get current font object
                font.setPointSize(size_pt) # Set its point size
                textarea.setFont(font) # Apply the modified font object
                if is_markdown_view:
                    textarea.document().setDefaultStyleSheet(doc_style)
                    # Force re-render if content exists to apply new styles
                    if textarea.toPlainText(): 
                        current_html = textarea.toHtml()
                        textarea.setHtml(current_html) 

            self.update() # Update main window appearance
            self.repaint() # Repaint it
            QApplication.processEvents() # Process events to ensure UI updates
        except Exception as e:
            logging.error(f"Error applying theme: {e}", exc_info=True)

    # --- Recipe List Management ---
    def _clear_layout(self, layout):
        """ Recursively removes all widgets and layouts from a given layout. """
        if layout is not None:
            while layout.count():
                item = layout.takeAt(0)
                widget = item.widget()
                if widget is not None:
                    widget.setParent(None) 
                    widget.deleteLater() 
                else:
                    sub_layout = item.layout()
                    if sub_layout is not None:
                        self._clear_layout(sub_layout) 
                        # Let Qt handle deletion of the layout itself after its items are gone
                        # sub_layout.deleteLater() # Avoid this unless necessary

    def _parse_recipes_file_to_structure(self):
        """ Parses the recipes.md file into a list of dictionaries representing groups and recipes. """
        structured_recipes = []
        current_group_title = None
        if not self.recipes_file or not os.path.exists(self.recipes_file):
            logging.warning(f"Recipes file not found or not specified: {self.recipes_file}")
            return structured_recipes # Return empty list

        try:
            with open(self.recipes_file, 'r', encoding='utf-8') as f:
                lines = f.readlines()
        except Exception as e:
            logging.error(f"Error reading recipes file {self.recipes_file}: {e}")
            return structured_recipes # Return empty list on error

        for line_num, line_content in enumerate(lines):
            line = line_content.strip()
            if not line: continue # Skip empty lines

            if line.startswith('#'): # Found a group heading
                current_group_title = line.lstrip('#').strip()
                structured_recipes.append({'type': 'group', 'title': current_group_title, 'line_num': line_num})
            elif line.startswith('**') and ':' in line: # Found a potential recipe
                try:
                    name_part, prompt_part = line.split(':', 1)
                    name = name_part.strip().strip('**').strip()
                    prompt_from_file = prompt_part.strip() # This is the key prompt used for ID
                    if name and prompt_from_file: # Ensure both parts are non-empty
                        structured_recipes.append({
                            'type': 'recipe', 'name': name, 'prompt': prompt_from_file,
                            'group_title': current_group_title, # Associate with current group
                            'line_num': line_num,
                            'id': (name, prompt_from_file) # Unique ID tuple
                        })
                    else:
                        logging.warning(f"Skipping malformed recipe (empty name or prompt on line {line_num+1}): {line}")
                except ValueError: # Handle lines with ':' but wrong format (e.g., multiple colons)
                    logging.warning(f"Skipping malformed recipe line (format error on line {line_num+1}): {line}")
            # Else: Ignore lines that are not groups or recipes (comments, etc.)
        return structured_recipes

    def load_recipes_and_populate_list(self):
        """ Clears and rebuilds the entire recipe list UI from scratch. """
        logging.info(f"Loading recipes from: {self.recipes_file}")
        self._clear_layout(self.recipe_buttons_layout) # Clear existing UI elements
        
        self._all_recipes_data = self._parse_recipes_file_to_structure() # Parse the file content
        
        # Handle case where recipes file is missing or empty
        if not self._all_recipes_data and (not self.recipes_file or not os.path.exists(self.recipes_file)):
            if not self.recipes_file or not os.path.exists(self.recipes_file):
                reply = QMessageBox.question(self, "Recipes File Missing",
                                             f"The recipes file ({self.recipes_file or 'Not Set'}) is missing or unconfigured.\nWould you like to try and download a default recipes.md from GitHub?",
                                             QMessageBox.Yes | QMessageBox.No)
                if reply == QMessageBox.Yes:
                    # TODO: Implement download logic (requires requests library, URL, error handling)
                    QMessageBox.information(self, "Download", "Download functionality not yet implemented. Please configure the recipes file manually.")
                    self.recipe_buttons_layout.addWidget(QLabel("Recipes file missing. Set in Configure."))
                else:
                    self.recipe_buttons_layout.addWidget(QLabel("Recipes file missing. Set in Configure."))
            else: # File exists but is empty or unparsable
                 self.recipe_buttons_layout.addWidget(QLabel("No valid recipes found in file."))
            self.recipe_buttons_layout.addStretch() # Push message to top
            return # Stop processing if no recipes data

        # Ensure deque has correct maxlen based on current config
        if self.recently_used_recipes.maxlen != self.max_recents:
            self.recently_used_recipes = deque(list(self.recently_used_recipes), 
                                               maxlen=self.max_recents if self.max_recents > 0 else None)

        # Add virtual groups first
        self._add_virtual_group_to_layout("Recently Used", self.recently_used_recipes)
        self._add_virtual_group_to_layout("Favorites", self.favorite_recipes, is_favorites_group=True)

        # Add groups and recipes from the parsed file data
        last_group_items_layout = None # **** Track the layout for the CURRENT group ****
        for item_data in self._all_recipes_data:
            if item_data['type'] == 'group':
                group_title = item_data['title']
                # Create the UI elements for this group
                group_button, group_widget_container, group_items_layout = self._create_collapsible_group(group_title)
                # Add the group button and its container to the main recipe layout
                self.recipe_buttons_layout.addWidget(group_button)
                self.recipe_buttons_layout.addWidget(group_widget_container)
                # **** CRITICAL: Update the reference to the layout WHERE recipes should be added ****
                last_group_items_layout = group_items_layout 
            elif item_data['type'] == 'recipe':
                name, prompt = item_data['name'], item_data['prompt'] # prompt is from file
                is_fav = (name,prompt) in self.favorite_recipes
                recipe_button = self._create_recipe_button(name, prompt, is_fav)
                # **** CRITICAL: Add the recipe button to the *correct* group layout ****
                if last_group_items_layout is not None: 
                    last_group_items_layout.addWidget(recipe_button) 
                else: # Fallback if recipes appear before the first # heading
                    self.recipe_buttons_layout.addWidget(recipe_button) 
                    logging.warning(f"Recipe '{name}' added outside of any defined group. Check recipes.md format.")
        
        self.recipe_buttons_layout.addStretch() # Add stretch at the end
        self.recipes_scroll_widget.setLayout(self.recipe_buttons_layout) # Re-set layout on widget
        self.recipes_scroll_widget.adjustSize() # Adjust size hint of scroll content
        self.recipes_scroll_area.updateGeometry() # Ensure scroll area updates

    def _add_virtual_group_to_layout(self, group_name, recipe_id_list, is_favorites_group=False):
        """ Adds a collapsible group for virtual lists (Recents, Favorites) to the UI. """
        effective_list = list(recipe_id_list) # Convert deque/list to list for iteration
        if group_name == "Recently Used":
            effective_list.reverse() # Show newest on top for recents

        # Don't add empty virtual groups unless it's Favorites (which can be shown as empty)
        if not effective_list and group_name != "Favorites":
            return

        # Create the group UI elements
        group_button, group_widget_container, group_items_layout = self._create_collapsible_group(group_name)
        self.recipe_buttons_layout.addWidget(group_button)
        self.recipe_buttons_layout.addWidget(group_widget_container)

        # Add recipe buttons to this group's layout
        for recipe_name, recipe_prompt_from_file in effective_list: 
            is_fav = (recipe_name, recipe_prompt_from_file) in self.favorite_recipes
            recipe_button = self._create_recipe_button(recipe_name, recipe_prompt_from_file, is_fav)
            group_items_layout.addWidget(recipe_button)
        
        # Add stretch if the group is empty (only happens for Favorites)
        if not effective_list:
            group_items_layout.addStretch()

    def _create_collapsible_group(self, title):
        """ Creates the QPushButton (header) and QWidget (container) for a collapsible group. """
        group_button = QPushButton() 
        group_button.setObjectName("groupButton") # For styling
        group_button.setCheckable(True)
        # Apply specific style for group button (bold, left-aligned)
        group_button.setStyleSheet("text-align: left; font-weight: bold;") 
        is_expanded = self._group_states.get(title, True) # Default to expanded
        group_button.setChecked(is_expanded)
        group_button.setText(f"{title} {'▼' if is_expanded else '▶'}")
        group_button.setFixedHeight(22) # Consistent height

        group_widget_container = QWidget()
        # Size policy: Expanding horizontally, Fixed vertically initially, changing when shown/hidden
        sp = QSizePolicy(QSizePolicy.MinimumExpanding, QSizePolicy.Fixed) 
        group_widget_container.setSizePolicy(sp)
        
        group_items_layout = QVBoxLayout(group_widget_container) # Layout *inside* the container
        group_items_layout.setContentsMargins(15, 2, 0, 2) # Indent items, add small top/bottom margin
        group_items_layout.setSpacing(1) # Minimal spacing between recipe buttons
        group_widget_container.setVisible(is_expanded)
        # No need to adjustSize here, let the layout manage it

        # Connect the button's toggled signal to the visibility handler
        group_button.toggled.connect(
            lambda checked, gc=group_widget_container, gb=group_button, t=title: self.toggle_group_visibility(checked, gc, gb, t)
        )
        return group_button, group_widget_container, group_items_layout

    def _create_recipe_button(self, name, prompt_from_file, is_favorite):
        """ Creates a QPushButton for a single recipe. """
        button_text = f"[★] {name}" if is_favorite else name
        button = QPushButton(button_text)
        button.setFixedHeight(20) # Consistent height
        # Let the main stylesheet handle text-align: left for QPushButton
        button.setToolTip(f"Prompt: {prompt_from_file[:100]}{'...' if len(prompt_from_file)>100 else ''}")
        # Connect click to execute the recipe
        button.clicked.connect(partial(self.execute_recipe_command, prompt_from_file, name, button))
        
        # Enable context menu for starring/editing/deleting
        button.setContextMenuPolicy(Qt.CustomContextMenu)
        button.customContextMenuRequested.connect(
            partial(self.show_recipe_context_menu, name, prompt_from_file, button) # Pass ID components
        )
        return button

    def toggle_group_visibility(self, is_checked, group_container, group_button, title):
        """ Handles showing/hiding the recipe container when a group header is clicked. """
        group_container.setVisible(is_checked)
        
        # Adjust container's vertical size policy based on visibility
        sp = group_container.sizePolicy()
        sp.setVerticalPolicy(QSizePolicy.Preferred if is_checked else QSizePolicy.Fixed) # Allow expansion when visible
        group_container.setSizePolicy(sp)

        # Invalidate layout and recalculate sizes forcefully
        if group_container.layout(): 
            group_container.layout().invalidate() # Mark as needing relayout
            group_container.layout().activate()   # Force relayout
        group_container.adjustSize() # Ensure container recalculates its minimum/sizehint
        group_container.updateGeometry() # Request geometry update based on new size hint

        # Update button text (arrow indicator) and save state
        self._group_states[title] = is_checked
        group_button.setText(f"{title} {'▼' if is_checked else '▶'}")
        self._save_partial_config({'group_states': self._group_states})
        
        # Critical: Force the parent scroll widget and scroll area to update THEIR layouts
        self.recipes_scroll_widget.adjustSize() # Adjust size based on children's new visibility/size
        self.recipes_scroll_widget.updateGeometry() # Request geometry update for scroll content
        self.recipes_scroll_area.updateGeometry()   # Request scroll area update (might adjust scrollbars)
        QApplication.processEvents() # Process events to ensure UI updates immediately

    def filter_recipes_display(self, query): 
        """ Filters the displayed recipes based on the search query. """
        query = query.lower()
        any_match_found = False 

        # Iterate through all top-level items in the main recipe layout
        for i in range(self.recipe_buttons_layout.count()):
            top_item = self.recipe_buttons_layout.itemAt(i)
            if not top_item: continue

            widget = top_item.widget()
            if not widget: continue

            # Identify group containers (they follow a group button)
            is_group_container = False
            group_button_ref = None
            group_title = None
            if i > 0:
                prev_item = self.recipe_buttons_layout.itemAt(i-1)
                if prev_item and prev_item.widget() and isinstance(prev_item.widget(), QPushButton) and \
                   prev_item.widget().objectName() == "groupButton" and \
                   isinstance(widget, QWidget) and widget.layout() is not None:
                    is_group_container = True
                    group_button_ref = prev_item.widget()
                    group_title = group_button_ref.text().rsplit(' ',1)[0] # Extract title

            if is_group_container:
                group_layout = widget.layout() 
                group_has_visible_recipe = False
                # Iterate through recipes within this group
                for j in range(group_layout.count()):
                    recipe_item = group_layout.itemAt(j)
                    if recipe_item and recipe_item.widget() and isinstance(recipe_item.widget(), QPushButton):
                        recipe_button = recipe_item.widget()
                        if recipe_button.objectName() == "groupButton": continue # Skip nested group buttons

                        recipe_name = recipe_button.text().lower().replace("[★]", "").strip()
                        recipe_prompt_tooltip = recipe_button.toolTip().lower().replace("prompt:","").strip()
                        
                        matches = query in recipe_name or query in recipe_prompt_tooltip
                        recipe_button.setVisible(matches)
                        if matches:
                            group_has_visible_recipe = True
                            any_match_found = True 
                
                # Update visibility of the group container and its button
                is_expanded = self._group_states.get(group_title, True) # Check if user wants it expanded
                widget.setVisible(group_has_visible_recipe and is_expanded) # Show container only if match AND expanded
                group_button_ref.setVisible(group_has_visible_recipe or not query) # Hide button if no match in group AND query exists


        # If query is cleared, restore default visibility based on saved expanded states
        if not query:
            self.load_recipes_and_populate_list() # Easiest way to reset all visibilities
            return

        # Force UI update after filtering
        self.recipes_scroll_widget.adjustSize()
        self.recipes_scroll_area.updateGeometry()
        QApplication.processEvents()


    # --- Recipe Context Menu and Actions ---
    def show_recipe_context_menu(self, recipe_name, recipe_prompt_from_file, recipe_button, point):
        """ Shows the right-click context menu for a recipe button. """
        menu = QMenu(self)
        recipe_id = (recipe_name, recipe_prompt_from_file) # Use the tuple ID

        # Star/Unstar Action
        is_starred = recipe_id in self.favorite_recipes
        star_action = menu.addAction("⭐ Unstar Recipe" if is_starred else "⭐ Star Recipe")
        star_action.triggered.connect(partial(self.toggle_favorite_status, recipe_id))
        menu.addSeparator()
        # Edit Action
        edit_action = menu.addAction("✏️ Edit Recipe...")
        edit_action.triggered.connect(partial(self.edit_recipe_from_context_menu, recipe_id))
        # Delete Action
        delete_action = menu.addAction("🗑️ Delete Recipe")
        delete_action.triggered.connect(partial(self.delete_recipe_from_context_menu, recipe_id))
        
        menu.exec_(recipe_button.mapToGlobal(point)) # Show menu at cursor position

    def toggle_favorite_status(self, recipe_id): 
        """ Adds or removes a recipe from the favorites list. """
        if recipe_id in self.favorite_recipes:
            self.favorite_recipes.remove(recipe_id)
            logging.info(f"Unstarred recipe: {recipe_id[0]}")
        else:
            # Add to favorites if limit allows (<= 0 means unlimited)
            if len(self.favorite_recipes) < self.max_favorites or self.max_favorites <= 0:
                self.favorite_recipes.append(recipe_id)
                logging.info(f"Starred recipe: {recipe_id[0]}")
            else:
                QMessageBox.information(self, "Favorites Full", f"Maximum {self.max_favorites} favorite recipes allowed."); return
        
        self._save_partial_config({'favorite_recipes': self.favorite_recipes}) # Save updated list
        self.load_recipes_and_populate_list() # Refresh UI

    def edit_recipe_from_context_menu(self, recipe_id_to_edit): 
        """ Opens the edit dialog and handles updating the recipe. """
        old_name, old_prompt_from_file = recipe_id_to_edit
        dialog = EditRecipeDialog(old_name, old_prompt_from_file, self)
        if dialog.exec_() == QDialog.Accepted: # User clicked OK
            new_name, new_prompt_from_file = dialog.get_data()
            if not new_name or not new_prompt_from_file: # Basic validation
                QMessageBox.warning(self, "Input Error", "Recipe name and prompt cannot be empty."); return

            # Update the recipe in the recipes.md file
            if self._update_recipe_in_file(old_name, old_prompt_from_file, new_name, new_prompt_from_file):
                new_id = (new_name, new_prompt_from_file)
                # Update the ID in dynamic lists (Recents, Favorites) if it existed there
                if recipe_id_to_edit in self.recently_used_recipes:
                    # Deques don't support direct item assignment, convert, modify, reconvert
                    temp_list = list(self.recently_used_recipes)
                    try:
                        idx = temp_list.index(recipe_id_to_edit)
                        temp_list[idx] = new_id
                        self.recently_used_recipes = deque(temp_list, maxlen=self.recently_used_recipes.maxlen)
                    except ValueError: pass # Item wasn't in list, ignore
                    self._save_partial_config({'recently_used_recipes': list(self.recently_used_recipes)})
                
                if recipe_id_to_edit in self.favorite_recipes:
                    try:
                        idx = self.favorite_recipes.index(recipe_id_to_edit)
                        self.favorite_recipes[idx] = new_id
                    except ValueError: pass # Item wasn't in list, ignore
                    self._save_partial_config({'favorite_recipes': self.favorite_recipes})
                
                self.load_recipes_and_populate_list() # Refresh UI with changes
                logging.info(f"Recipe '{old_name}' edited to '{new_name}'.")
            else:
                # Error message if file update failed
                QMessageBox.critical(self, "Edit Error", f"Failed to find or update recipe in {self.recipes_file}.\nCheck logs for details.")

    def _update_recipe_in_file(self, old_name, old_prompt_from_file, new_name, new_prompt_from_file):
        """ Finds and replaces a recipe line in recipes.md using normalized comparison. """
        if not self.recipes_file or not os.path.exists(self.recipes_file): return False
        self._backup_recipes_file("before_edit") # Backup before modifying
        try:
            with open(self.recipes_file, 'r', encoding='utf-8') as f:
                lines = f.readlines()
            
            found_and_updated = False
            # Normalize search keys for robust comparison
            norm_old_name = normalize_whitespace_for_comparison(old_name)
            norm_old_prompt = normalize_whitespace_for_comparison(old_prompt_from_file)
            updated_lines = []

            for line_num, line_content in enumerate(lines):
                stripped_line = line_content.strip()
                # Check if line looks like a recipe before trying to parse
                if stripped_line.startswith('**') and ':' in stripped_line:
                    try:
                        name_part, prompt_part = stripped_line.split(':', 1)
                        current_line_name = name_part.strip().strip('**').strip()
                        current_line_prompt = prompt_part.strip()
                        
                        # Compare normalized versions
                        if normalize_whitespace_for_comparison(current_line_name) == norm_old_name and \
                           normalize_whitespace_for_comparison(current_line_prompt) == norm_old_prompt:
                            
                            # Found the line, replace it with new data
                            newline_char = line_content[len(stripped_line):] # Preserve original line ending
                            updated_lines.append(f"**{new_name}**: {new_prompt_from_file}{newline_char}")
                            found_and_updated = True
                            logging.info(f"Found and replaced recipe on line {line_num+1}")
                            continue # Move to next line
                    except Exception as parse_ex:
                        # Log if a line looked like a recipe but couldn't be parsed
                        logging.warning(f"Could not parse potential recipe line {line_num+1} for update check: {stripped_line} - {parse_ex}")
                
                # If not the line to update, keep the original line
                updated_lines.append(line_content)

            if found_and_updated:
                # Write the modified content back to the file
                with open(self.recipes_file, 'w', encoding='utf-8') as f:
                    f.writelines(updated_lines)
                return True
            else:
                # Log detailed info if not found
                logging.warning(f"Recipe to edit not found in file using normalized comparison.")
                logging.debug(f"Search Details: Name='{old_name}', NormName='{norm_old_name}', Prompt='{old_prompt_from_file[:50]}...', NormPrompt='{norm_old_prompt[:50]}...'")
                return False
        except Exception as e:
            logging.error(f"Error updating recipes file: {e}", exc_info=True)
            return False

    def delete_recipe_from_context_menu(self, recipe_id_to_delete): 
        """ Handles deleting a recipe after confirmation. """
        name, prompt_from_file = recipe_id_to_delete
        reply = QMessageBox.question(self, "Confirm Deletion", 
                                     f"Are you sure you want to delete the recipe:\n'{name}'?",
                                     QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if reply != QMessageBox.Yes: return

        if self._remove_recipe_from_file(name, prompt_from_file):
            # Remove from dynamic lists if present
            if recipe_id_to_delete in self.recently_used_recipes:
                self.recently_used_recipes.remove(recipe_id_to_delete)
                self._save_partial_config({'recently_used_recipes': list(self.recently_used_recipes)})
            if recipe_id_to_delete in self.favorite_recipes:
                self.favorite_recipes.remove(recipe_id_to_delete)
                self._save_partial_config({'favorite_recipes': self.favorite_recipes})
            
            self.load_recipes_and_populate_list() # Refresh UI
            logging.info(f"Recipe '{name}' deleted.")
        else:
            QMessageBox.critical(self, "Delete Error", f"Failed to find or delete recipe from {self.recipes_file}.\nCheck logs for details.")

    def _backup_recipes_file(self, suffix="backup"):
        """ Creates a timestamped backup of the recipes file. """
        if not self.recipes_file or not os.path.exists(self.recipes_file): return
        try:
            os.makedirs(BACKUP_DIR, exist_ok=True)
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            base_name = os.path.basename(self.recipes_file)
            backup_filename = f"{os.path.splitext(base_name)[0]}_{timestamp}_{suffix}.md"
            backup_path = os.path.join(BACKUP_DIR, backup_filename)
            shutil.copy2(self.recipes_file, backup_path) # copy2 preserves metadata
            logging.info(f"Recipes file backed up to {backup_path}")
        except Exception as e:
            logging.error(f"Failed to backup recipes file: {e}")

    def _remove_recipe_from_file(self, name_to_delete, prompt_to_delete):
        """ Finds and removes a recipe line from recipes.md using normalized comparison. """
        if not self.recipes_file or not os.path.exists(self.recipes_file): return False
        self._backup_recipes_file("before_delete") # Backup first
        try:
            with open(self.recipes_file, 'r', encoding='utf-8') as f:
                lines = f.readlines()
            
            found_and_removed = False
            updated_lines = []
            norm_name_del = normalize_whitespace_for_comparison(name_to_delete)
            norm_prompt_del = normalize_whitespace_for_comparison(prompt_to_delete)

            for line_num, line_content in enumerate(lines):
                stripped_line = line_content.strip()
                # Check if line looks like a recipe
                if stripped_line.startswith('**') and ':' in stripped_line:
                    try:
                        name_part, prompt_part = stripped_line.split(':', 1)
                        current_line_name = name_part.strip().strip('**').strip()
                        current_line_prompt = prompt_part.strip()
                        # Compare normalized versions to find the match
                        if normalize_whitespace_for_comparison(current_line_name) == norm_name_del and \
                           normalize_whitespace_for_comparison(current_line_prompt) == norm_prompt_del:
                            found_and_removed = True
                            logging.info(f"Found and removed recipe on line {line_num+1}")
                            continue # Skip adding this line to updated_lines
                    except Exception: 
                        pass # Ignore lines that fail parsing
                
                # Keep lines that are not the one to be deleted
                updated_lines.append(line_content)

            if found_and_removed:
                # Write the filtered content back to the file
                with open(self.recipes_file, 'w', encoding='utf-8') as f:
                    f.writelines(updated_lines)
                return True
            else:
                # Log details if not found
                logging.warning(f"Recipe to delete not found using normalized comparison.")
                logging.debug(f"Search Details: Name='{name_to_delete}', NormName='{norm_name_del}', Prompt='{prompt_to_delete[:50]}...', NormPrompt='{norm_prompt_del[:50]}...'")
                return False
        except Exception as e:
            logging.error(f"Error removing recipe from file: {e}", exc_info=True)
            return False

    # --- Core Action Execution ---
    def send_custom_or_chat_command(self):
        """ Handles sending input from the text edit, either as custom command or chat message. """
        command_text = self.custom_input_textedit.toPlainText().strip()
        if not command_text:
            QMessageBox.information(self, "No Input", "Please enter a command or chat message.")
            return

        is_chat = (self.input_mode_combo.currentText() == "Chat Mode:")
        captured_text_val = self.captured_text_edit.toPlainText() # Always get current captured text

        # If chat mode and using in-app display, add user message to results pane immediately
        if is_chat and self.results_in_app:
            user_html = f"""
                <div style="margin: 5px 0;"> 
                  <p style='margin-bottom:0.1em; font-weight: bold; color: {self._theme_color('chat_user_label')};'>User:</p>
                  <div style='margin-left:10px; padding:5px 8px; border-radius:8px; background-color:{self._theme_color('chat_user_bg')}; display: inline-block; max-width: 85%;'>
                    <p style="margin:0;">{self.escape_html_for_manual_construct(command_text)}</p>
                  </div>
                </div>"""
            # Add space if results pane is not empty or just showing the initial message
            if not self.results_textedit.toPlainText().strip().endswith("Chat mode started. Type your message below.") and self.results_textedit.toPlainText().strip() :
                 self.results_textedit.append("<br>") 
            self.results_textedit.append(user_html)
            self.results_textedit.moveCursor(QTextCursor.End) # Scroll to bottom
        
        # Decide prompt and text for LLM based on mode
        prompt_for_llm = command_text 
        # Pass captured text to LLM thread; it will handle if it's empty
        self.execute_recipe_command(prompt_for_llm, "Custom Command/Chat", None, 
                                     is_chat_mode=is_chat, text_override=captured_text_val)
        self.custom_input_textedit.clear() # Clear input area after sending

    def _theme_color(self, key):
        """ Helper to get theme-specific colors for dynamic HTML styling. """
        if self._theme == 'Dark':
            # Define dark theme colors
            colors = {'chat_user_bg': '#303848', 'chat_llm_bg': '#384030', 
                      'general_text_edit_bg': '#3c3f41', 'chat_user_label': '#87CEFA', 'chat_llm_label': '#98FB98'}
            return colors.get(key, '#e0e0e0') # Default dark text color
        else:
            # Define light theme colors
            colors = {'chat_user_bg': '#e8f0fe', 'chat_llm_bg': '#f0f8e8', 
                      'general_text_edit_bg': '#ffffff', 'chat_user_label': '#00008B', 'chat_llm_label': '#006400'}
            return colors.get(key, '#1e1e1e') # Default light text color

    def execute_recipe_command(self, prompt_from_file_or_custom, recipe_name="Recipe", button_ref=None, 
                              is_chat_mode=False, text_override=None):
        """ Initiates the LLM request thread with the appropriate prompt and text. """
        
        # Determine the text to process (captured text or override)
        captured_text = text_override if text_override is not None else self.captured_text_edit.toPlainText()
        
        # For non-chat recipes, ensure captured text is provided
        if not is_chat_mode and not captured_text.strip(): 
            QMessageBox.information(self, "No Text", "Please capture text (or type in 'Captured Text' pane) to use with non-chat recipes.")
            return

        # Gather LLM API configuration from instance variables
        llm_api_config = {
            "provider": self.llm_provider, "url": self.llm_url, 
            "api_key": self.openai_api_key, "model_name": self.llm_model_name
        }
        
        # Basic validation before starting thread
        if not llm_api_config.get("url") and llm_api_config["provider"] == "Local OpenAI-Compatible":
             QMessageBox.warning(self, "LLM URL Missing", "LLM URL for Local provider not configured in Settings."); return
        if not llm_api_config.get("api_key") and llm_api_config["provider"] == "OpenAI API":
             QMessageBox.warning(self, "API Key Missing", f"{llm_api_config['provider']} API Key is not configured in Settings."); return

        # Log execution details
        logging.info(f"Executing: '{prompt_from_file_or_custom[:50]}...' (Chat: {is_chat_mode}) with text: '{captured_text[:50]}...'")
        
        # Provide visual feedback if a recipe button was clicked
        if button_ref and isinstance(button_ref, QPushButton):
            original_style = button_ref.styleSheet()
            # Apply temporary style (green background, ensure text alignment is kept)
            highlight_style = "background-color: #90EE90; color: black; text-align: left;" # Ensure left align during highlight
            # if "text-align: left;" in original_style: highlight_style += " text-align: left;" # Less reliable way
            button_ref.setStyleSheet(highlight_style) 
            # Restore original style after a delay
            QTimer.singleShot(700, lambda b=button_ref, s=original_style: b.setStyleSheet(s))
        
        # Show progress bar
        self.progress_bar.setVisible(True); self.progress_bar.setRange(0, 0) # Indeterminate

        # Update 'Recently Used' list for actual recipes (not custom/chat)
        if recipe_name != "Custom Command/Chat" and not is_chat_mode:
            cleaned_name = recipe_name.replace("[★] ", "").strip() # Remove star prefix if present
            recipe_id = (cleaned_name, prompt_from_file_or_custom) # ID tuple
            # Remove if already exists to move it to the front
            if recipe_id in self.recently_used_recipes:
                self.recently_used_recipes.remove(recipe_id)
            # Add to the left (most recent)
            self.recently_used_recipes.appendleft(recipe_id)
            # Ensure maxlen is respected if changed
            if self.recently_used_recipes.maxlen != self.max_recents:
                new_deque = deque(list(self.recently_used_recipes), maxlen=self.max_recents if self.max_recents > 0 else None)
                self.recently_used_recipes = new_deque
            # Save the updated list to config
            self._save_partial_config({'recently_used_recipes': list(self.recently_used_recipes)})
            # TODO: Consider a targeted refresh for the "Recently Used" group UI instead of full reload

        # Start the LLM request thread
        # Pass the actual prompt and the captured text to the thread
        self.llm_thread = LLMRequestThread(llm_api_config, prompt_from_file_or_custom, captured_text, self.llm_timeout)
        # Connect signals for response and error handling
        self.llm_thread.response_received.connect(
            partial(self.handle_llm_response, captured_text=captured_text, prompt=prompt_from_file_or_custom, is_chat_mode=is_chat_mode)
        )
        self.llm_thread.error_occurred.connect(self.handle_llm_error)
        self.llm_thread.start() # Run the thread

    # --- LLM Response/Error Handling ---
    def handle_llm_response(self, response_text, captured_text, prompt, is_chat_mode=False):
        """ Processes the successful response from the LLM thread. """
        logging.info("LLM Response Received")
        self.progress_bar.setVisible(False) # Hide progress bar
        filename = None # Filename for permanent memory, if enabled

        # Save to permanent memory file if enabled
        if self.permanent_memory and self.memory_dir:
            try:
                os.makedirs(self.memory_dir, exist_ok=True) # Ensure directory exists
                # Create a safe filename based on prompt and timestamp
                safe_prompt_tag = "".join(c for c in prompt[:25] if c.isalnum() or c in " -_").strip() or "entry"
                timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                filename = f"{safe_prompt_tag}_{timestamp}.md"
                file_path = os.path.join(self.memory_dir, filename)
                # Store structured content in the file
                memory_content = f"Captured Text:\n{captured_text}\n\nPrompt:\n{prompt}\n\nLLM Response:\n{response_text}"
                with open(file_path, 'w', encoding='utf-8') as f:
                    f.write(memory_content)
                logging.debug(f"Saved memory entry to {file_path}")
            except Exception as e:
                logging.error(f"Error saving permanent memory file: {e}")
                filename = None # Reset filename if saving failed

        # Add entry to in-memory list: (captured_text, prompt, raw_response, filename)
        self._memory.append((captured_text, prompt, response_text, filename))
        current_memory_idx = len(self._memory) - 1 # Get index of the newly added item

        # Display results based on configuration (in-app or separate window)
        if self.results_in_app:
            formatted_llm_html_content = self.format_markdown_for_display(response_text)
            if is_chat_mode:
                # Append LLM response bubble to chat view
                llm_html = f"""
                    <div style="margin: 5px 0;">
                      <p style='margin-bottom:0.1em; font-weight: bold; color: {self._theme_color('chat_llm_label')};'>LLM:</p>
                      <div style='margin-left:10px; padding:5px 8px; border-radius:8px; background-color:{self._theme_color('chat_llm_bg')}; display: inline-block; max-width: 85%;'>
                        <p style="margin:0;">{formatted_llm_html_content}</p> 
                      </div>
                    </div>"""
                if self.results_textedit.toPlainText().strip(): # Add space if not first message
                    self.results_textedit.append("<br>")
                self.results_textedit.append(llm_html)
            else: # Custom Input mode
                if self.append_mode_checkbox.isChecked() and self.results_textedit.toPlainText().strip():
                    self.results_textedit.append("<hr/>" + formatted_llm_html_content) # Append with separator
                else: # Overwrite
                    self.results_textedit.setHtml(formatted_llm_html_content)
            
            self.results_textedit.moveCursor(QTextCursor.End) # Scroll to bottom
            self.active_memory_index = current_memory_idx # Link this display to the memory entry
        
        else: # Open results in a separate window
            result_window = ResultWindow(response_text, self, current_memory_idx)
            result_window.show()
            self.result_windows.append(result_window) # Keep track of open windows

        # Add entry to the Memory QListWidget
        item_text_summary = f"Prompt: {prompt[:25]}... Text: {captured_text[:25]}..."
        entry_widget = MemoryEntryWidget(item_text_summary, filename) # Create custom widget for the item
        list_item = QListWidgetItem(self.memory_list) # Create list item
        list_item.setSizeHint(entry_widget.sizeHint()) # Set size hint for proper display
        
        # Connect the delete button within the custom widget
        entry_widget.delete_button.clicked.connect(partial(self.delete_memory_entry_from_button, list_item))
        self.memory_list.setItemWidget(list_item, entry_widget) # Assign widget to item
        self.memory_list.scrollToBottom() # Ensure new item is visible

    def handle_llm_error(self, error_message):
        """ Handles errors reported by the LLM thread. """
        logging.error(f"LLM Error: {error_message}")
        self.progress_bar.setVisible(False) # Hide progress bar
        QMessageBox.critical(self, "LLM Error", error_message) # Show error dialog
        
        # Optionally display error in results pane too
        if self.results_in_app:
            error_html = f"<p style='color: red;'><b>LLM Error:</b><br/>{self.escape_html_for_manual_construct(error_message)}</p>"
            # Append if chat/append mode, otherwise overwrite
            if self.input_mode_combo.currentText() == "Chat Mode:" or self.append_mode_checkbox.isChecked():
                self.results_textedit.append("<hr style='border-color: red;'/>" + error_html)
            else:
                self.results_textedit.setHtml(error_html)


    # --- Memory List Handling ---
    def show_memory_entry_from_list_item(self, list_widget_item):
        """ Displays the content of a selected memory item. """
        index = self.memory_list.row(list_widget_item) # Get index from the clicked item
        if not (0 <= index < len(self._memory)): # Validate index
            logging.error(f"Invalid memory index from list item: {index}")
            return

        # Retrieve data from the internal memory list
        captured_text, prompt, response_content, filename = self._memory[index]
        logging.debug(f"Showing memory entry {index}: Prompt '{prompt[:30]}...'")

        if self.results_in_app:
            # Save any pending changes to the previously active entry before switching
            if self.active_memory_index is not None and self.active_memory_index != index:
                 self.save_memory_content_change(self.active_memory_index, self.results_textedit.toHtml())
            
            # Construct HTML to display the full Q&A context for this memory item
            # Check if response_content looks like HTML already (might have been edited)
            if response_content.strip().startswith('<'): 
                response_display = response_content # Assume it's already HTML
            else:
                response_display = self.format_markdown_for_display(response_content) # Render Markdown

            full_entry_html = f"""
                <p><b>Original Captured Text:</b><br/>{self.escape_html_for_manual_construct(captured_text)}</p>
                <p><b>Original Prompt:</b><br/>{self.escape_html_for_manual_construct(prompt)}</p>
                <hr/>
                <p><b>LLM Reply:</b></p>
                {response_display} 
            """
            # Display this HTML, overwriting current content (double-click implies specific view)
            self.results_textedit.setHtml(full_entry_html)
            self.active_memory_index = index # Update the active index
            self.results_textedit.moveCursor(QTextCursor.Start) # Scroll to top
        else:
            # Check if a window for this index already exists
            existing_window = next((win for win in self.result_windows if win.memory_index == index), None)
            if existing_window:
                existing_window.showNormal() # Bring existing window to front
                existing_window.activateWindow()
            else: # Create and show a new window
                result_window = ResultWindow(response_content, self, index)
                result_window.show()
                self.result_windows.append(result_window) # Track the new window

    def delete_memory_entry_from_button(self, item_from_list_widget):
        """ Handles deleting a memory entry when its 'Del' button is clicked. """
        if self._deleting_memory: return # Prevent concurrent deletions
        self._deleting_memory = True
        try:
            index_to_delete = self.memory_list.row(item_from_list_widget)
            if not (0 <= index_to_delete < len(self._memory)): # Validate index
                logging.error(f"Delete: Invalid memory index {index_to_delete}")
                self._deleting_memory = False; return

            # Confirm deletion with user
            reply = QMessageBox.question(self, "Confirm Deletion", 
                                         "Are you sure you want to delete this memory entry?",
                                         QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
            if reply != QMessageBox.Yes:
                self._deleting_memory = False; return

            # Get data needed for file deletion before removing from lists
            _, _, _, filename_to_delete = self._memory[index_to_delete]
            
            # Disconnect the button's signal to prevent issues during removal
            widget = self.memory_list.itemWidget(item_from_list_widget)
            if widget and hasattr(widget, 'delete_button'):
                try: widget.delete_button.clicked.disconnect() 
                except TypeError: pass # Ignore if no connections

            # Remove from UI list and internal memory list
            self.memory_list.takeItem(index_to_delete) # Removes item AND its widget
            self._memory.pop(index_to_delete)

            # Delete corresponding file if permanent memory is enabled
            if self.permanent_memory and self.memory_dir and filename_to_delete:
                file_path = os.path.join(self.memory_dir, filename_to_delete)
                if os.path.exists(file_path):
                    try:
                        os.remove(file_path)
                        logging.debug(f"Deleted permanent memory file: {file_path}")
                    except OSError as e:
                        logging.error(f"Error deleting file {file_path}: {e}")
            
            # Adjust active_memory_index if the deleted item was active or before active
            if self.active_memory_index is not None:
                if self.active_memory_index == index_to_delete:
                    self.active_memory_index = None # No active item now
                    if self.results_in_app: self.results_textedit.clear() # Clear display
                elif self.active_memory_index > index_to_delete:
                    self.active_memory_index -= 1 # Shift index back
            
            logging.debug(f"Memory entry at index {index_to_delete} deleted.")

        except Exception as e:
            logging.error(f"Error deleting memory entry: {e}", exc_info=True)
            QMessageBox.critical(self, "Error", f"Failed to delete memory entry: {e}")
        finally:
            self._deleting_memory = False # Release semaphore


    # --- Handling User Edits in Text Areas ---
    def on_results_text_changed_by_user(self): # Connected to results_textedit.textChanged
        # This signal fires for every character change. Saving here is too intensive.
        # Rely on focusOutEvent or closeEvent to save changes made by the user.
        pass 

    def focusOutEvent(self, event): 
        # Save content if focus moves outside the main window or its known children
        if self.results_in_app and self.active_memory_index is not None:
            active_app_window = QApplication.activeWindow()
            # Check if focus moved to something outside the app's main window, its result windows, or its dialogs
            is_child_dialog = isinstance(active_app_window, QDialog) and active_app_window.parent() == self
            if active_app_window is None or (active_app_window != self and not is_child_dialog and active_app_window not in self.result_windows):
                logging.debug("Main window focus possibly lost. Saving active memory if applicable.")
                self.save_memory_content_change(self.active_memory_index, self.results_textedit.toHtml())
        super().focusOutEvent(event)

    def save_memory_content_change(self, memory_idx_to_save, new_html_content):
        """ Updates the stored response content for a memory entry if it differs. """
        if not (0 <= memory_idx_to_save < len(self._memory)):
            logging.warning(f"Invalid memory index for saving changes: {memory_idx_to_save}")
            return

        captured_text, prompt, old_response_content, filename = self._memory[memory_idx_to_save]
        
        # Only update if the content has actually changed
        # Comparing HTML can be tricky. A simple string comparison might suffice for now.
        # This saves the potentially edited HTML content, overwriting original Markdown.
        # A future improvement could involve trying to convert HTML back to Markdown before saving.
        if new_html_content != old_response_content: 
            self._memory[memory_idx_to_save] = (captured_text, prompt, new_html_content, filename)
            logging.debug(f"Memory entry {memory_idx_to_save} content updated with new HTML.")

            # If permanent memory is enabled, update the corresponding file
            if self.permanent_memory and self.memory_dir and filename:
                file_path = os.path.join(self.memory_dir, filename)
                try:
                    # Write the full structure back, using the new (HTML) response content
                    disk_content = f"Captured Text:\n{captured_text}\n\nPrompt:\n{prompt}\n\nLLM Response:\n{new_html_content}" 
                    with open(file_path, 'w', encoding='utf-8') as f:
                        f.write(disk_content)
                    logging.debug(f"Updated permanent memory file: {file_path} with new HTML content.")
                except Exception as e:
                    logging.error(f"Error saving updated memory content to file {file_path}: {e}")


    # --- Configuration Window Handling ---
    def open_config_window(self):
        """ Opens the configuration dialog and applies changes if saved. """
        try:
            config_dialog = ConfigWindow(self) # Pass self as parent reference
            if config_dialog.exec_(): # Blocks until dialog is closed, returns True if Accepted
                # Config was saved by ConfigWindow.save_config_values(). Reload and apply changes.
                self.validate_and_load_config() # Reloads all config values into self attributes
                self.apply_theme() # Re-applies theme, font sizes
                self.load_recipes_and_populate_list() # Re-populates recipes based on new file/settings
                
                # Update UI elements based on potentially changed config
                self.results_container.setVisible(self.results_in_app)
                # Reset splitter sizes based on visibility and loaded config
                if not self.results_in_app and len(self.splitter_sizes) == 3:
                     self.splitter.setSizes([self.splitter_sizes[0], self.splitter_sizes[1] + self.splitter_sizes[2], 0])
                else:
                    self.splitter.setSizes(self.splitter_sizes) 
                
                self.append_mode_checkbox.setChecked(self.append_mode)
                self.on_input_mode_changed(self.input_mode_combo.currentText()) # Re-evaluate chat mode UI state

                # Restart hotkey listener to apply potential hotkey changes
                self.start_hotkey_thread() # Will terminate existing thread first
                
                logging.debug("Configuration applied after dialog save.")
            else: # Dialog was cancelled
                logging.debug("Config dialog cancelled.")
        except Exception as e:
            logging.error(f"Error in open_config_window or applying changes: {e}", exc_info=True)
            QMessageBox.critical(self, "Configuration Error", f"Failed to open or apply configuration changes:\n{e}")

    # --- External File / About Handling ---
    def open_recipes_file_externally(self): 
        """ Opens the configured recipes.md file in the default system editor. """
        try:
            recipes_path = self.recipes_file
            if not recipes_path or not os.path.exists(recipes_path):
                QMessageBox.warning(self, "File Not Found", f"Recipes file '{recipes_path or 'Not Set'}' not configured or does not exist."); return
            # Use QDesktopServices for platform-independent opening
            QDesktopServices.openUrl(QUrl.fromLocalFile(recipes_path))
            logging.debug(f"Attempted to open recipes file: {recipes_path}")
        except Exception as e:
            logging.error(f"Could not open recipes file: {e}")
            QMessageBox.critical(self, "Error", f"Could not open recipes file: {e}")

    def show_about(self):
        """ Opens the About.md/Readme.md file in the default system editor. """
        # Ensure path is absolute relative to BASE_PATH if not already absolute
        about_path_abs = os.path.join(BASE_PATH, ABOUT_FILE) if not os.path.isabs(ABOUT_FILE) else ABOUT_FILE
        if not os.path.exists(about_path_abs):
            QMessageBox.warning(self, "File Not Found", f"{ABOUT_FILE} not found at {about_path_abs}."); return
        try:
            QDesktopServices.openUrl(QUrl.fromLocalFile(about_path_abs))
        except Exception as e:
             QMessageBox.critical(self, "Error", f"Could not open {ABOUT_FILE}: {e}")

    # --- Window Closing / State Changes ---
    def closeEvent(self, event):
        """ Handles the main window close event (e.g., clicking the 'X'). """
        try:
            # Save any pending changes from in-app results textedit
            if self.results_in_app and self.active_memory_index is not None:
                self.save_memory_content_change(self.active_memory_index, self.results_textedit.toHtml())
            
            # Close any open separate result windows
            for window in self.result_windows[:]: # Iterate copy as list might change
                window.close() # This should trigger their own close logic
            
            # Decide whether to exit or minimize based on config
            if self.close_behavior == "Minimize to Tray":
                event.ignore() # Prevent the window from actually closing
                self.hide() # Hide the main window
                # Also hide child windows if main window is minimized to tray by X button
                for window in self.result_windows[:]: # Use copy again
                    if window and window.isVisible(): window.hide()
                # Show tray notification
                self.tray_icon.showMessage("CoDude", "CoDude is running in the background.", QSystemTrayIcon.Information, 2000)
            else: # Default: Exit the application
                QApplication.instance().quit() # Tell the application to quit
        except Exception as e:
            logging.error(f"Error in closeEvent: {e}")
            event.accept() # Ensure app can close even if there's an error during cleanup

    def changeEvent(self, event): 
        """ Handles window state changes (minimize, maximize, etc.). """
        try:
            if event.type() == QEvent.WindowStateChange:
                # Check if the window was minimized (e.g., by clicking the minimize button)
                if self.windowState() & Qt.WindowMinimized:
                    # If configured to minimize to tray, intercept the minimize event
                    if self.close_behavior == "Minimize to Tray": 
                        event.ignore() # Prevent actual minimization to taskbar
                        self.hide() # Hide the window instead
                        # Also hide any open child windows
                        for window in self.result_windows[:]:
                             if window and window.isVisible(): window.hide()
                        # Show tray message only if not hidden by shortcut
                        if not self._minimized_by_shortcut: 
                            self.tray_icon.showMessage("CoDude", "CoDude minimized to tray.", QSystemTrayIcon.Information, 1500)
                        self._minimized_by_shortcut = False # Reset flag after handling minimize
                        return # Important: return after handling minimize event
            # Call the base class implementation for other state changes
            super().changeEvent(event)
        except Exception as e:
            logging.error(f"Error in changeEvent: {e}")


    # --- System Tray Icon Interaction ---
    def on_tray_icon_activated(self, reason):
        """ Handles clicks on the system tray icon. """
        # Show/hide window on single left click (Trigger)
        if reason == QSystemTrayIcon.Trigger:
            self.show_hide_window()
        # Right-click (Context) is handled automatically by setContextMenu

    def show_hide_window(self): 
        """ Toggles the visibility of the main window and associated result windows. """
        try:
            if self.isHidden(): # If currently hidden, show it
                self.showNormal() # Restore from minimized/hidden state
                self.activateWindow() # Bring to foreground
                self.raise_() # Ensure it's on top
                # Also show result windows that were hidden along with the main window
                for window in self.result_windows[:]:
                    if window and not window.isVisible() and not window.isMinimized(): 
                        window.showNormal()
                        window.activateWindow()
                self._minimized_by_shortcut = False # Reset flag
            else: # If currently visible, hide it
                # Save current state if needed before hiding
                if self.results_in_app and self.active_memory_index is not None:
                    self.save_memory_content_change(self.active_memory_index, self.results_textedit.toHtml())
                self.hide()
                # Also hide child windows when main window is hidden by hotkey/tray click
                for window in self.result_windows[:]:
                     if window and window.isVisible(): window.hide()
                self._minimized_by_shortcut = True # Flag that it was hidden intentionally
            logging.debug("Window visibility toggled.")
        except Exception as e:
            logging.error(f"Error in show_hide_window: {e}")

    # --- UI Update Callbacks ---
    def update_captured_text_area(self, text): 
        """ Updates the 'Captured Text' text edit. """
        self.captured_text_edit.setText(text if text is not None else "") # Handle None case
        logging.debug("Captured text updated in text area.")

    # --- Export/Copy Actions ---
    def export_results_to_markdown(self):
        """ Exports the content of the results pane (ideally raw Markdown) to a file. """
        if not self.results_in_app:
            QMessageBox.information(self, "Not Applicable", "Export from here is for In-App results. Use the individual window's export button."); return
        
        text_to_export = ""
        # Try to get the original raw response from memory if an item is active
        if self.active_memory_index is not None and 0 <= self.active_memory_index < len(self._memory):
            _, _, raw_llm_response, _ = self._memory[self.active_memory_index]
            text_to_export = raw_llm_response # Export the raw response (likely Markdown)
        else: # Fallback: current content as plain text (loses formatting)
            text_to_export = self.results_textedit.toPlainText() 

        if not text_to_export.strip():
            QMessageBox.information(self, "Nothing to Export", "Results area is empty or no active memory entry."); return

        options = QFileDialog.Options()
        file_path, _ = QFileDialog.getSaveFileName(self, "Save LLM Response", "", "Markdown Files (*.md);;Text Files (*.txt);;All Files (*)", options=options)
        if file_path:
            try:
                with open(file_path, 'w', encoding='utf-8') as f:
                    f.write(text_to_export)
                QMessageBox.information(self, "Export Successful", f"Response saved to {file_path}")
            except Exception as e:
                QMessageBox.critical(self, "Export Error", f"Could not save file: {e}")

    def copy_results_to_clipboard(self):
        """ Copies the HTML content of the results pane to the clipboard. """
        if not self.results_in_app: return
        QApplication.clipboard().setText(self.results_textedit.toHtml())
        QMessageBox.information(self, "Copy Successful", "HTML content from results area copied to clipboard.")

    # --- State Saving ---
    def save_append_mode_state(self): 
        """ Saves the state of the 'Append Mode' checkbox to config (only when not in Chat Mode). """
        if self.input_mode_combo.currentText() != "Chat Mode:":
            self.append_mode = self.append_mode_checkbox.isChecked()
            self._save_partial_config({'append_mode': self.append_mode})
            logging.debug(f"Append mode state saved: {self.append_mode}")

    # --- Font Size Adjustment ---
    def adjust_textarea_font(self, textarea_widget, delta):
        """ Adjusts the font size for a specific QTextEdit widget. """
        textarea_id = str(id(textarea_widget))
        current_size_pt = self.textarea_font_sizes.get(textarea_id, self.font_size) # Use specific or global
        new_size_pt = max(8, min(24, current_size_pt + delta)) # Clamp between 8 and 24pt
        
        font = textarea_widget.font() # Get the current font object
        font.setPointSize(new_size_pt) # Set the new point size
        textarea_widget.setFont(font) # Apply the updated font object
        
        # Save the new size specific to this text area
        self.textarea_font_sizes[textarea_id] = new_size_pt
        self._save_partial_config({'textarea_font_sizes': self.textarea_font_sizes})
        
        # If this is the results_textedit (markdown view), force re-render to apply style changes
        if textarea_widget == self.results_textedit and textarea_widget.toPlainText(): 
             current_html = textarea_widget.toHtml() # Get current HTML content
             textarea_widget.setHtml(current_html) # Re-set HTML to force re-render with new base font


        logging.debug(f"Adjusted font for textarea {textarea_id} to {new_size_pt}pt.")

    # --- Permanent Memory Loading ---
    def load_permanent_memory_entries(self): 
        """ Loads memory entries from files in the configured memory directory. """
        if not (self.permanent_memory and self.memory_dir and os.path.exists(self.memory_dir)):
            return # Don't proceed if not enabled or dir doesn't exist
            
        logging.debug(f"Loading permanent memory from {self.memory_dir}")
        self._memory.clear() # Clear existing session memory before loading
        self.memory_list.clear() # Clear the UI list

        try:
            # Get all .md files, sort by modification time (oldest first)
            memory_files = sorted(
                [os.path.join(self.memory_dir, f) for f in os.listdir(self.memory_dir) if f.endswith(".md")],
                key=os.path.getmtime 
            )
            
            for file_path in memory_files:
                filename = os.path.basename(file_path)
                try:
                    with open(file_path, 'r', encoding='utf-8') as f:
                        content = f.read()
                    
                    # Parse the structured content from the file
                    cap_text_m = re.search(r"Captured Text:\n(.*?)\n\nPrompt:", content, re.DOTALL)
                    prompt_m = re.search(r"Prompt:\n(.*?)\n\nLLM Response:", content, re.DOTALL)
                    response_m = re.search(r"LLM Response:\n(.*)", content, re.DOTALL)

                    if cap_text_m and prompt_m and response_m: # If all parts found
                        cap_text = cap_text_m.group(1).strip()
                        prompt = prompt_m.group(1).strip()
                        resp = response_m.group(1).strip() # This might be raw Markdown or HTML if edited
                        
                        # Add to internal memory list
                        self._memory.append((cap_text, prompt, resp, filename))
                        
                        # Create and add UI list item
                        item_txt = f"Prompt: {prompt[:25]}... Text: {cap_text[:25]}..."
                        entry_w = MemoryEntryWidget(item_txt, filename)
                        list_i = QListWidgetItem(self.memory_list)
                        list_i.setSizeHint(entry_w.sizeHint()) # Important for custom widget height
                        
                        # Connect delete button for this item
                        entry_w.delete_button.clicked.connect(
                            partial(self.delete_memory_entry_from_button, list_i)
                        )
                        self.memory_list.setItemWidget(list_i, entry_w) # Set the custom widget for the item
                    else:
                        logging.warning(f"Could not parse memory file structure: {filename}. Skipping.")
                except Exception as e_file:
                    # Log errors reading or processing individual files
                    logging.error(f"Error processing memory file {filename}: {e_file}")
            
            self.memory_list.scrollToBottom() # Show the most recent entries if list is long
            logging.debug(f"Loaded {len(self._memory)} entries from permanent memory.")
        except Exception as e:
            # Log general errors during directory listing or sorting
            logging.error(f"General error loading permanent memory: {e}", exc_info=True)

# --- Application Entry Point ---
def main():
    # Basic logging config before app starts, level might be overridden later by config
    logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s') 
    
    logging.info("Starting CoDude application")
    try:
        app = QApplication(sys.argv)
        # Don't quit when the main window is closed if minimizing to tray is possible
        app.setQuitOnLastWindowClosed(False) 
        
        # Set application name (useful for macOS menu bar, etc.)
        app.setApplicationName("CoDude")
        
        # Create and show the main window
        window = CoDudeApp() # Config loading happens inside here
        window.show() 
        
        # Start the Qt event loop
        sys.exit(app.exec_())
        
    except Exception as e:
        # Log critical errors that prevent the app from starting
        logging.critical("Unhandled exception at main level: %s", e, exc_info=True)
        # Show a simple message box for critical startup errors
        error_box = QMessageBox()
        error_box.setIcon(QMessageBox.Critical)
        error_box.setWindowTitle("CoDude Critical Error")
        error_box.setText(f"A critical error occurred during startup:\n{e}\n\nApplication will now exit. Check logs for details.")
        error_box.setStandardButtons(QMessageBox.Ok)
        error_box.exec_()
        sys.exit(1) # Exit with error code

if __name__ == "__main__":
    # Ensure the BASE_PATH is correctly set for resource loading relative to the script/executable
    logging.info(f"Base path detected: {BASE_PATH}") 
    main()