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
CONFIG_FILE = os.path.join(BASE_PATH, "config.json")
ABOUT_FILE = os.path.join(BASE_PATH, "Readme.md") 
LOG_FILE = os.path.join(BASE_PATH, "codude.log")
BACKUP_DIR = os.path.join(BASE_PATH, "backups")

# --- Whitespace normalization function ---
def normalize_whitespace_for_comparison(s):
    if s is None: return ""
    return ' '.join(str(s).split()).strip()

# Initialize logging
def setup_logging(level='Normal', output='Both'):
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
            import keyboard
            logging.debug("Hotkey listener thread started")
            while True:
                keyboard.wait(self.hotkey_string)
                logging.info(f"Hotkey {self.hotkey_string} activated!")
                keyboard.press_and_release('ctrl+c') 
                time.sleep(0.15) 
                try:
                    clipboard_text = QApplication.clipboard().text()
                    if clipboard_text is None: clipboard_text = ""; logging.warning("Clipboard returned None")
                except Exception as e: clipboard_text = ""; logging.error(f"Failed to access clipboard: {e}")
                logging.debug(f"Captured text: {clipboard_text[:50]}")
                self.text_captured.emit(clipboard_text)
                self.show_window.emit()
        except ImportError: logging.error("`keyboard` library not installed. Hotkey functionality disabled (or might require sudo on Linux).")
        except Exception as e: logging.error(f"Hotkey listener error: {e}")

# Thread for sending request to LLM
class LLMRequestThread(QThread):
    response_received = pyqtSignal(str)
    error_occurred = pyqtSignal(str)
    def __init__(self, llm_config, prompt, text, timeout=60):
        QThread.__init__(self)
        self.llm_config = llm_config; self.prompt = prompt
        self.text = text; self.timeout = timeout
    def run(self):
        raw_response = "N/A" 
        try:
            provider = self.llm_config.get("provider", "Local OpenAI-Compatible")
            llm_url = self.llm_config.get("url", "")
            api_key = self.llm_config.get("api_key", "")
            model_name = self.llm_config.get("model_name", "gpt-3.5-turbo") 
            user_content = f"{self.prompt}\n\nText: {self.text}" if self.text.strip() else self.prompt
            messages = [{"role": "system", "content": "You are a helpful assistant."}, {"role": "user", "content": user_content}]
            payload = {"model": model_name, "messages": messages}
            headers = {"Content-Type": "application/json"}; request_url = ""
            
            if provider == "Local OpenAI-Compatible":
                if not llm_url: self.error_occurred.emit("LLM URL for Local provider not configured."); return
                parsed_url = urlparse(llm_url); path = parsed_url.path.rstrip('/')
                if not path or path == '/': base_url = llm_url.rstrip('/'); request_url = urljoin(f"{base_url}/", 'v1/chat/completions'); logging.info(f"Appended '/v1/chat/completions'. Using: {request_url}")
                elif path.endswith('/v1/chat/completions'): request_url = llm_url
                else: request_url = llm_url; logging.warning(f"Using provided local URL as is: {request_url}. Ensure it's the correct chat completion endpoint.")
            elif provider == "OpenAI API":
                if not api_key: self.error_occurred.emit("OpenAI API Key not configured."); return
                request_url = "https://api.openai.com/v1/chat/completions"; headers["Authorization"] = f"Bearer {api_key}"
            else: self.error_occurred.emit(f"Unsupported LLM provider: {provider}"); return

            logging.debug(f"Sending LLM request to {request_url} for provider {provider} with model {model_name}")
            response = requests.post(request_url, json=payload, headers=headers, timeout=self.timeout)
            raw_response = response.text
            
            if response.status_code != 200:
                logging.error(f"LLM request failed with status {response.status_code}. Response: {raw_response[:500]}...")
                error_msg = f"LLM request failed (Status: {response.status_code})."
                try:
                    error_data = response.json()
                    if isinstance(error_data, dict) and 'error' in error_data:
                         if isinstance(error_data['error'], dict) and 'message' in error_data['error']: error_msg += f" Message: {error_data['error']['message']}"
                         elif isinstance(error_data['error'], str): error_msg += f" Message: {error_data['error']}"
                except json.JSONDecodeError: error_msg += f" Raw Response: {raw_response[:200]}"
                except Exception as parse_err: logging.error(f"Failed to parse LLM error response: {parse_err}"); error_msg += f" Raw Response: {raw_response[:200]}"
                self.error_occurred.emit(error_msg); return

            logging.debug(f"Raw LLM success response: {raw_response[:500]}...")
            result = response.json()
            if not result: raise ValueError("Empty success response from LLM")
            if not isinstance(result, dict): raise ValueError(f"Invalid success response format. Expected dict, got {type(result)}")
            content = None; choices = result.get('choices')
            if isinstance(choices, list) and choices:
                first_choice = choices[0]
                if isinstance(first_choice, dict):
                    message = first_choice.get('message')
                    if isinstance(message, dict): content = message.get('content')
            if content is None: 
                if 'text' in result and isinstance(result['text'], str): content = result['text']; logging.debug("Extracted content using fallback 'text' field.")
                elif 'response' in result and isinstance(result['response'], str): content = result['response']; logging.debug("Extracted content using fallback 'response' field.")
                else: raise ValueError("No valid content found in LLM success response.")
            if not isinstance(content, str): raise ValueError(f"Invalid content type found: {type(content)}. Expected string.")
            self.response_received.emit(content)
        except requests.exceptions.Timeout: self.error_occurred.emit(f"LLM request timed out after {self.timeout} seconds.")
        except requests.exceptions.RequestException as e: self.error_occurred.emit(f"Error communicating with LLM: {e}")
        except json.JSONDecodeError as e: self.error_occurred.emit(f"Failed to decode LLM JSON response: {e}\nRaw response glimpse: {raw_response[:200]}")
        except ValueError as e: self.error_occurred.emit(f"Invalid LLM response data: {e}\nRaw response glimpse: {raw_response[:200]}")
        except Exception as e: logging.error("Unexpected error in LLMRequestThread.run", exc_info=True); self.error_occurred.emit(f"An unexpected error occurred: {e}")

# Window to display LLM results
class ResultWindow(QMainWindow):
    def __init__(self, response_text, parent_app, memory_index=None):
        super().__init__(parent_app); self.parent_app = parent_app; self.memory_index = memory_index
        current_theme = self.parent_app._theme if self.parent_app else "Light"; full_html = ""
        if parent_app and hasattr(parent_app, '_memory') and memory_index is not None and 0 <= memory_index < len(parent_app._memory):
            captured_text, prompt, _, _ = parent_app._memory[memory_index] 
            command_name_match = re.search(r'\*\*(.*?)\*\*', prompt); command_name = command_name_match.group(1) if command_name_match else prompt.split(':')[0].split('\n')[0]
            self.setWindowTitle(f"CoDude: {html.escape(command_name[:50])}")
            formatted_response_html = self.parent_app.format_markdown_for_display(response_text)
            escaped_captured_text = self.parent_app.escape_html_for_manual_construct(captured_text); escaped_command_name_display = html.escape(command_name) 
            full_html = f"<p><b>Command:</b><br/>{escaped_command_name_display}</p><p><b>Text:</b><br/>{escaped_captured_text}</p><p><b>LLM Reply:</b></p>{formatted_response_html}"
        else:
            self.setWindowTitle("CoDude: LLM Result"); formatted_response_html = self.parent_app.format_markdown_for_display(response_text) if self.parent_app else response_text
            full_html = f"<p><b>LLM Reply:</b></p>{formatted_response_html}"
        self.setGeometry(200, 200, 700, 500); central_widget = QWidget(); self.setCentralWidget(central_widget)
        layout = QVBoxLayout(central_widget); self.response_textedit = QTextEdit(self)
        self.response_textedit.setReadOnly(False); doc_style = self.parent_app.get_themed_document_stylesheet()
        self.response_textedit.document().setDefaultStyleSheet(doc_style); self.response_textedit.setHtml(full_html)
        self.response_textedit.textChanged.connect(self.on_text_changed_by_user_in_window); layout.addWidget(self.response_textedit)
        button_layout = QHBoxLayout(); self.export_button = QPushButton("Export to Markdown", self)
        self.export_button.clicked.connect(self.export_to_markdown); button_layout.addWidget(self.export_button)
        self.copy_button = QPushButton("Copy HTML to Clipboard", self); self.copy_button.clicked.connect(self.copy_to_clipboard)
        button_layout.addWidget(self.copy_button); layout.addLayout(button_layout)
        self.setStyleSheet(self.parent_app.dark_stylesheet_base if current_theme == 'Dark' else self.parent_app.light_stylesheet_base)
    def on_text_changed_by_user_in_window(self): pass 
    def focusOutEvent(self, event):
        if self.memory_index is not None and self.parent_app: self.parent_app.save_memory_content_change(self.memory_index, self.response_textedit.toHtml())
        super().focusOutEvent(event)
    def closeEvent(self, event):
        if self.memory_index is not None and self.parent_app: self.parent_app.save_memory_content_change(self.memory_index, self.response_textedit.toHtml())
        if self.parent_app and hasattr(self.parent_app, 'result_windows') and self in self.parent_app.result_windows:
            try: self.parent_app.result_windows.remove(self)
            except ValueError: pass 
        super().closeEvent(event)
    def export_to_markdown(self):
        text_to_export = self.response_textedit.toPlainText() 
        if self.parent_app and self.memory_index is not None and 0 <= self.memory_index < len(self.parent_app._memory): _, _, text_to_export, _ = self.parent_app._memory[self.memory_index] 
        options = QFileDialog.Options(); file_path, _ = QFileDialog.getSaveFileName(self, "Save LLM Response", "", "Markdown Files (*.md);;Text Files (*.txt);;All Files (*)", options=options)
        if file_path:
            try:
                with open(file_path, 'w', encoding='utf-8') as f: f.write(text_to_export)
                QMessageBox.information(self, "Export Successful", f"Response saved to {file_path}")
            except Exception as e: QMessageBox.critical(self, "Export Error", f"Could not save file: {e}")
    def copy_to_clipboard(self): QApplication.clipboard().setText(self.response_textedit.toHtml()); QMessageBox.information(self, "Copy Successful", "HTML content copied to clipboard.")

# Custom widget for Memory entries
class MemoryEntryWidget(QWidget):
    def __init__(self, text, filename=None, parent=None):
        super().__init__(parent); self.filename = filename
        self.layout = QHBoxLayout(self); self.layout.setContentsMargins(5,5,5,5); self.layout.setSpacing(5)
        short_text = ' '.join(text.split()[:15]); short_text += '...' if len(text.split()) > 15 else ''
        self.label = QLabel(short_text, self); self.label.setWordWrap(True); self.label.setMinimumHeight(30)
        self.layout.addWidget(self.label, 1)
        self.delete_button = QPushButton("Del", self); self.delete_button.setFixedWidth(40); self.delete_button.setVisible(False) 
        self.layout.addWidget(self.delete_button); self.setMouseTracking(True)
    def enterEvent(self, event): self.delete_button.setVisible(True); super().enterEvent(event)
    def leaveEvent(self, event): self.delete_button.setVisible(False); super().leaveEvent(event)

# Dialog for editing a recipe
class EditRecipeDialog(QDialog):
    def __init__(self, recipe_name, recipe_prompt, parent=None):
        super().__init__(parent); self.setWindowTitle("Edit Recipe"); self.setMinimumWidth(450)
        layout = QVBoxLayout(self); name_label = QLabel("Recipe Name (bold part):"); layout.addWidget(name_label)
        self.name_input = QLineEdit(recipe_name); layout.addWidget(self.name_input)
        prompt_label = QLabel("Recipe Command/Prompt:"); layout.addWidget(prompt_label)
        self.prompt_input = QTextEdit(recipe_prompt); self.prompt_input.setAcceptRichText(False) 
        self.prompt_input.setMinimumHeight(120); layout.addWidget(self.prompt_input)
        button_layout = QHBoxLayout(); self.ok_button = QPushButton("OK"); self.ok_button.clicked.connect(self.accept)
        button_layout.addWidget(self.ok_button); self.cancel_button = QPushButton("Cancel"); self.cancel_button.clicked.connect(self.reject)
        button_layout.addWidget(self.cancel_button); layout.addLayout(button_layout)
    def get_data(self): return self.name_input.text().strip(), self.prompt_input.toPlainText().strip()

# Configuration Window
class ConfigWindow(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent); self.setWindowTitle("CoDude Configuration"); self.setMinimumWidth(450)
        self.main_app_ref = parent; self.layout = QVBoxLayout(self)
        self.layout.setSpacing(5); self.layout.setContentsMargins(10,10,10,10)
        
        def create_row_layout(*widgets_to_add):
            row = QHBoxLayout(); row.setSpacing(5)
            for w in widgets_to_add:
                if isinstance(w, (QPushButton, QLineEdit, QComboBox, QCheckBox, QLabel)): w.setFixedHeight(22) 
                if isinstance(w, QSpacerItem): row.addSpacerItem(w) 
                else: row.addWidget(w) 
            return row
            
        def create_label(text): lbl = QLabel(text, self); lbl.setFixedHeight(22); return lbl 

        self.llm_provider_combo = QComboBox(self); self.llm_provider_combo.addItems(["Local OpenAI-Compatible", "OpenAI API"])
        self.llm_provider_combo.currentTextChanged.connect(self.update_llm_fields_visibility)
        self.layout.addLayout(create_row_layout(create_label("LLM Provider:"), self.llm_provider_combo))
        self.llm_url_label = create_label("LLM URL (Local):") 
        self.llm_url_input = QLineEdit(self); self.llm_url_input.setPlaceholderText("e.g., http://localhost:1234") 
        self.llm_url_row = create_row_layout(self.llm_url_label, self.llm_url_input)
        self.layout.addLayout(self.llm_url_row)
        self.openai_api_key_label = create_label("OpenAI API Key:")
        self.openai_api_key_input = QLineEdit(self); self.openai_api_key_input.setEchoMode(QLineEdit.Password)
        self.openai_key_row = create_row_layout(self.openai_api_key_label, self.openai_api_key_input)
        self.layout.addLayout(self.openai_key_row)
        self.model_name_input = QLineEdit(self)
        self.layout.addLayout(create_row_layout(create_label("LLM Model Name:"), self.model_name_input))
        self.layout.addSpacerItem(QSpacerItem(20, 10, QSizePolicy.Minimum, QSizePolicy.Fixed))
        self.max_recents_input = QLineEdit(self); self.max_recents_input.setValidator(QIntValidator(0, 100, self))
        self.layout.addLayout(create_row_layout(create_label("Max Recent Recipes:"), self.max_recents_input))
        self.max_favorites_input = QLineEdit(self); self.max_favorites_input.setValidator(QIntValidator(0, 100, self))
        self.layout.addLayout(create_row_layout(create_label("Max Favorite Recipes:"), self.max_favorites_input))
        self.layout.addSpacerItem(QSpacerItem(20, 10, QSizePolicy.Minimum, QSizePolicy.Fixed))
        self.layout.addWidget(create_label("Hotkey Configuration:")) 
        self.ctrl_checkbox = QCheckBox("Ctrl", self); self.shift_checkbox = QCheckBox("Shift", self); self.alt_checkbox = QCheckBox("Alt", self)
        modifier_layout = create_row_layout(self.ctrl_checkbox, self.shift_checkbox, self.alt_checkbox, QSpacerItem(40, 20, QSizePolicy.Expanding, QSizePolicy.Minimum))
        self.layout.addLayout(modifier_layout)
        self.main_key_input = QLineEdit(self); self.main_key_input.setMaxLength(1)
        self.layout.addLayout(create_row_layout(create_label("Main Hotkey Key:"), self.main_key_input))
        self.layout.addSpacerItem(QSpacerItem(20, 10, QSizePolicy.Minimum, QSizePolicy.Fixed))
        self.theme_combo = QComboBox(self); self.theme_combo.addItems(['Light', 'Dark'])
        self.layout.addLayout(create_row_layout(create_label("Theme:"), self.theme_combo))
        self.results_display_combo = QComboBox(self); self.results_display_combo.addItems(['Separate Windows', 'In-App Textarea'])
        self.layout.addLayout(create_row_layout(create_label("Results Display:"), self.results_display_combo))
        self.font_size_slider = QSlider(Qt.Horizontal, self); self.font_size_slider.setMinimum(8); self.font_size_slider.setMaximum(18); self.font_size_slider.setTickInterval(1); self.font_size_slider.setValue(10)
        self.layout.addLayout(create_row_layout(create_label("Global Font Size:"), self.font_size_slider))
        self.layout.addSpacerItem(QSpacerItem(20, 10, QSizePolicy.Minimum, QSizePolicy.Fixed))
        self.recipes_file_input = QLineEdit(self); self.recipes_file_input.setReadOnly(True)
        browse_recipes_button = QPushButton("Browse", self); browse_recipes_button.clicked.connect(self.browse_recipes_file)
        self.layout.addLayout(create_row_layout(create_label("Recipes File:"), self.recipes_file_input, browse_recipes_button))
        self.permanent_memory_checkbox = QCheckBox("Permanent Memory", self); self.layout.addWidget(self.permanent_memory_checkbox)
        self.memory_dir_input = QLineEdit(self); self.memory_dir_input.setReadOnly(True)
        browse_memory_button = QPushButton("Browse", self); browse_memory_button.clicked.connect(self.browse_memory_dir)
        self.layout.addLayout(create_row_layout(create_label("Memory Directory:"), self.memory_dir_input, browse_memory_button))
        self.layout.addSpacerItem(QSpacerItem(20, 10, QSizePolicy.Minimum, QSizePolicy.Fixed))
        self.timeout_input = QLineEdit(self); self.timeout_input.setValidator(QIntValidator(5, 600, self)) 
        self.layout.addLayout(create_row_layout(create_label("LLM Timeout (sec):"), self.timeout_input))
        self.logging_combo = QComboBox(self); self.logging_combo.addItems(['None', 'Minimal', 'Normal', 'Extended', 'Everything'])
        self.layout.addLayout(create_row_layout(create_label("Logging Level:"), self.logging_combo))
        self.logging_output_combo = QComboBox(self); self.logging_output_combo.addItems(['Terminal', 'File', 'Both'])
        self.layout.addLayout(create_row_layout(create_label("Logging Output:"), self.logging_output_combo))
        self.close_behavior_combo = QComboBox(self); self.close_behavior_combo.addItems(['Exit', 'Minimize to Tray'])
        self.layout.addLayout(create_row_layout(create_label("Close Behavior:"), self.close_behavior_combo))
        self.layout.addSpacerItem(QSpacerItem(20, 10, QSizePolicy.Minimum, QSizePolicy.Expanding)) 
        button_layout_bottom = QHBoxLayout(); save_button = QPushButton("Save", self); save_button.clicked.connect(self.save_config_values)
        button_layout_bottom.addWidget(save_button); cancel_button = QPushButton("Cancel", self); cancel_button.clicked.connect(self.reject)
        button_layout_bottom.addWidget(cancel_button); self.layout.addLayout(button_layout_bottom)
        self.load_config_values(); self.update_llm_fields_visibility(); self.adjustSize()
        
    def update_llm_fields_visibility(self):
        provider = self.llm_provider_combo.currentText(); is_local = provider == "Local OpenAI-Compatible"; is_openai_api = provider == "OpenAI API"
        for w in [self.llm_url_label, self.llm_url_input]: w.setVisible(is_local)
        for w in [self.openai_api_key_label, self.openai_api_key_input]: w.setVisible(is_openai_api)
        self.adjustSize()
        
    def browse_recipes_file(self):
        options = QFileDialog.Options(); options |= QFileDialog.DontUseNativeDialog
        fp, _ = QFileDialog.getOpenFileName(self, "Select Recipes File", "", "Markdown Files (*.md);;All Files (*)", options=options)
        if fp: self.recipes_file_input.setText(fp)
        
    def browse_memory_dir(self):
        options = QFileDialog.Options(); options |= QFileDialog.DontUseNativeDialog
        d = QFileDialog.getExistingDirectory(self, "Select Memory Directory", options=options)
        if d: self.memory_dir_input.setText(d)
        
    def load_config_values(self):
        try:
            config = {}; 
            if os.path.exists(CONFIG_FILE):
                with open(CONFIG_FILE, 'r', encoding='utf-8') as f: config = json.load(f)
            self.llm_provider_combo.setCurrentText(config.get("llm_provider", "Local OpenAI-Compatible"))
            self.llm_url_input.setText(config.get("llm_url", "http://127.0.0.1:1234")) # Default Base URL
            self.openai_api_key_input.setText(config.get("openai_api_key", ""))
            self.model_name_input.setText(config.get("llm_model_name", "gpt-3.5-turbo")) # Use LLM model name field
            self.max_recents_input.setText(str(config.get("max_recents", 5))); self.max_favorites_input.setText(str(config.get("max_favorites", 5)))
            self.recipes_file_input.setText(config.get("recipes_file", os.path.join(BASE_PATH, "recipes.md")))
            hotkey = config.get("hotkey", {"ctrl": True, "alt": True, "main_key": "c"})
            self.ctrl_checkbox.setChecked(hotkey.get("ctrl", True)); self.shift_checkbox.setChecked(hotkey.get("shift", False))
            self.alt_checkbox.setChecked(hotkey.get("alt", True)); self.main_key_input.setText(hotkey.get("main_key", "c"))
            self.theme_combo.setCurrentText(config.get("theme", "Light"))
            self.results_display_combo.setCurrentText(config.get("results_display", "Separate Windows"))
            self.font_size_slider.setValue(config.get("font_size", 10))
            self.permanent_memory_checkbox.setChecked(config.get("permanent_memory", False))
            self.memory_dir_input.setText(config.get("memory_dir", os.path.join(BASE_PATH, "memory")))
            self.timeout_input.setText(str(config.get("llm_timeout", 60)))
            self.logging_combo.setCurrentText(config.get("logging_level", "Normal"))
            self.logging_output_combo.setCurrentText(config.get("logging_output", "Both"))
            self.close_behavior_combo.setCurrentText(config.get("close_behavior", "Exit"))
            self.update_llm_fields_visibility(); logging.debug("Config loaded successfully in ConfigWindow")
        except Exception as e: logging.error(f"Error loading config file in ConfigWindow: {e}"); QMessageBox.warning(self, "Config Load Error", f"Could not load configuration: {e}")
        
    def save_config_values(self):
        try:
            llm_provider_val = self.llm_provider_combo.currentText(); llm_url_val = self.llm_url_input.text().strip()
            if llm_provider_val == "Local OpenAI-Compatible" and not llm_url_val:
                reply = QMessageBox.question(self, "LLM URL Not Set", "Use default 'http://127.0.0.1:1234'?", QMessageBox.Yes | QMessageBox.No | QMessageBox.Cancel)
                if reply == QMessageBox.Yes: llm_url_val = "http://127.0.0.1:1234"
                elif reply == QMessageBox.Cancel: return
            permanent_memory_checked = self.permanent_memory_checkbox.isChecked(); memory_dir_val = self.memory_dir_input.text().strip()
            if permanent_memory_checked and not memory_dir_val:
                default_mem_dir = os.path.join(BASE_PATH, "memory")
                reply = QMessageBox.question(self, "Memory Directory", f"Use default '{default_mem_dir}'?", QMessageBox.Yes | QMessageBox.No | QMessageBox.Cancel)
                if reply == QMessageBox.Yes: memory_dir_val = default_mem_dir; os.makedirs(memory_dir_val, exist_ok=True); self.memory_dir_input.setText(memory_dir_val)
                elif reply == QMessageBox.Cancel: return
            config_data = {
                "llm_provider": llm_provider_val, "llm_url": llm_url_val, "openai_api_key": self.openai_api_key_input.text(),
                "llm_model_name": self.model_name_input.text().strip() or "gpt-3.5-turbo",
                "max_recents": int(self.max_recents_input.text() or 5), "max_favorites": int(self.max_favorites_input.text() or 5),
                "recipes_file": self.recipes_file_input.text().strip(),
                "hotkey": {"ctrl": self.ctrl_checkbox.isChecked(), "shift": self.shift_checkbox.isChecked(), "alt": self.alt_checkbox.isChecked(), "main_key": self.main_key_input.text().strip().lower() or "c"},
                "logging_level": self.logging_combo.currentText(), "logging_output": self.logging_output_combo.currentText(),
                "theme": self.theme_combo.currentText(), "results_display": self.results_display_combo.currentText(),
                "font_size": self.font_size_slider.value(), "permanent_memory": permanent_memory_checked, "memory_dir": memory_dir_val,
                "llm_timeout": int(self.timeout_input.text() or 60), "close_behavior": self.close_behavior_combo.currentText(),
                "group_states": getattr(self.main_app_ref, "_group_states", {}), "append_mode": getattr(self.main_app_ref, "append_mode", False), 
                "textarea_font_sizes": getattr(self.main_app_ref, "textarea_font_sizes", {}), "splitter_sizes": getattr(self.main_app_ref, "splitter_sizes", [250,350,300]),
                "recently_used_recipes": list(getattr(self.main_app_ref, "recently_used_recipes", deque())), "favorite_recipes": getattr(self.main_app_ref, "favorite_recipes", [])
            }
            with open(CONFIG_FILE, 'w', encoding='utf-8') as f: json.dump(config_data, f, indent=4)
            QMessageBox.information(self, "Config Saved", "Configuration saved successfully."); logging.debug("Config saved successfully"); self.accept()
        except ValueError as ve: logging.error(f"Invalid input: {ve}"); QMessageBox.critical(self, "Input Error", f"Invalid numeric value: {ve}")
        except Exception as e: logging.error(f"Could not save config: {e}"); QMessageBox.critical(self, "Save Error", f"Could not save config: {e}")

class CoDudeApp(QMainWindow):
    def __init__(self):
        super().__init__(); self._minimized_by_shortcut = False; logging.info("Starting CoDudeApp initialization")
        self.setWindowTitle("CoDude"); self.setGeometry(100, 100, 900, 800); self.setWindowFlags(Qt.Window | Qt.WindowStaysOnTopHint)
        self._group_states = {}; self._memory = []; self._all_recipes_data = [] 
        self.result_windows = []; self.textarea_font_sizes = {}; self.results_in_app = False; self.append_mode = False; self.font_size = 10 
        self.permanent_memory = False; self.memory_dir = ""; self.llm_provider = "Local OpenAI-Compatible"; self.llm_url = "http://127.0.0.1:1234" 
        self.openai_api_key = ""; self.llm_model_name = "gpt-3.5-turbo"; self.recipes_file = ""; self._theme = "Light" 
        self.active_memory_index = None; self._deleting_memory = False; self.splitter_sizes = [250, 350, 300] 
        self.max_recents = 5; self.max_favorites = 5; self.recently_used_recipes = deque(maxlen=self.max_recents); self.favorite_recipes = [] 
        self.dark_stylesheet_base = ""; self.light_stylesheet_base = ""
        central_widget = QWidget(); self.setCentralWidget(central_widget); main_layout = QVBoxLayout(central_widget)
        menubar = QMenuBar(self); self.setMenuBar(menubar); codude_menu = menubar.addMenu("CoDude")
        configure_action = QAction("Configure", self); configure_action.triggered.connect(self.open_config_window); codude_menu.addAction(configure_action)
        open_recipes_action = QAction("Open Recipes.md", self); open_recipes_action.triggered.connect(self.open_recipes_file_externally); codude_menu.addAction(open_recipes_action)
        about_action = QAction("About", self); about_action.triggered.connect(self.show_about); codude_menu.addAction(about_action)
        quit_action = QAction("Quit", self); quit_action.triggered.connect(QApplication.instance().quit); codude_menu.addAction(quit_action)
        self.splitter = QSplitter(Qt.Horizontal); self.splitter.setHandleWidth(5); main_layout.addWidget(self.splitter, 1)
        self.validate_and_load_config()
        left_widget = QWidget(); self.left_layout = QVBoxLayout(left_widget); self.left_layout.setContentsMargins(5,5,5,5); self.left_layout.setSpacing(3)
        search_layout = QHBoxLayout(); search_layout.setSpacing(3); search_layout.addWidget(QLabel("Search:", self))
        self.search_input = QLineEdit(self); self.search_input.setPlaceholderText("Filter recipes..."); self.search_input.setFixedHeight(22)
        self.search_input.textChanged.connect(self.filter_recipes_display); search_layout.addWidget(self.search_input); self.left_layout.addLayout(search_layout)
        self.recipes_scroll_area = QScrollArea(); self.recipes_scroll_area.setWidgetResizable(True)
        self.recipes_scroll_widget = QWidget(); self.recipe_buttons_layout = QVBoxLayout(self.recipes_scroll_widget)
        self.recipe_buttons_layout.setAlignment(Qt.AlignTop); self.recipe_buttons_layout.setContentsMargins(0,0,0,0); self.recipe_buttons_layout.setSpacing(1)
        self.recipes_scroll_area.setWidget(self.recipes_scroll_widget); self.left_layout.addWidget(self.recipes_scroll_area)
        self.input_mode_combo = QComboBox(self); self.input_mode_combo.addItems(["Custom Input:", "Chat Mode:"]); self.input_mode_combo.setFixedHeight(24)
        self.input_mode_combo.currentTextChanged.connect(self.on_input_mode_changed); self.left_layout.addWidget(self.input_mode_combo)
        self.custom_input_textedit = QTextEdit(self); self.custom_input_textedit.setToolTip("Enter custom instructions or chat message here (Ctrl+Enter to send).")
        self.custom_input_textedit.setMaximumHeight(100); self.left_layout.addWidget(self.custom_input_textedit)
        custom_controls_layout = QHBoxLayout(); custom_controls_layout.setSpacing(3); send_custom_button = QPushButton("Send", self); send_custom_button.setFixedHeight(24)
        send_custom_button.clicked.connect(self.send_custom_or_chat_command); custom_controls_layout.addWidget(send_custom_button, 1)
        custom_font_up = QPushButton("↑", self); custom_font_up.setFixedSize(24, 24); custom_font_up.clicked.connect(lambda: self.adjust_textarea_font(self.custom_input_textedit, 1))
        custom_controls_layout.addWidget(custom_font_up); custom_font_down = QPushButton("↓", self); custom_font_down.setFixedSize(24, 24)
        custom_font_down.clicked.connect(lambda: self.adjust_textarea_font(self.custom_input_textedit, -1)); custom_controls_layout.addWidget(custom_font_down)
        self.left_layout.addLayout(custom_controls_layout); self.splitter.addWidget(left_widget)
        tabs_widget = QWidget(); tabs_layout = QVBoxLayout(tabs_widget); tabs_layout.setContentsMargins(0,0,0,0); right_tabs = QTabWidget(self) 
        captured_widget = QWidget(); captured_layout = QVBoxLayout(captured_widget); captured_layout.addWidget(QLabel("Captured Text:", self))
        self.captured_text_edit = QTextEdit(self); captured_layout.addWidget(self.captured_text_edit, 1)
        captured_font_layout = QHBoxLayout(); captured_font_layout.addStretch()
        cap_font_up = QPushButton("↑",self); cap_font_up.setFixedSize(24,24); cap_font_up.clicked.connect(lambda: self.adjust_textarea_font(self.captured_text_edit,1)); captured_font_layout.addWidget(cap_font_up)
        cap_font_down = QPushButton("↓",self); cap_font_down.setFixedSize(24,24); cap_font_down.clicked.connect(lambda: self.adjust_textarea_font(self.captured_text_edit,-1)); captured_font_layout.addWidget(cap_font_down)
        captured_layout.addLayout(captured_font_layout); right_tabs.addTab(captured_widget, "Captured Text")
        memory_widget = QWidget(); memory_layout = QVBoxLayout(memory_widget); memory_layout.addWidget(QLabel("CoDude's Memory:", self))
        self.memory_list = QListWidget(self); self.memory_list.itemDoubleClicked.connect(self.show_memory_entry_from_list_item)
        memory_layout.addWidget(self.memory_list, 1); right_tabs.addTab(memory_widget, "Memory")
        tabs_layout.addWidget(right_tabs, 1); self.splitter.addWidget(tabs_widget)
        self.results_container = QWidget(); results_layout = QVBoxLayout(self.results_container); results_layout.setContentsMargins(5,5,5,5); results_layout.setSpacing(3)
        results_layout.addWidget(QLabel("LLM Results:", self)); self.results_textedit = QTextEdit(self); self.results_textedit.setReadOnly(False) 
        self.results_textedit.textChanged.connect(self.on_results_text_changed_by_user); results_layout.addWidget(self.results_textedit, 1)
        results_controls_layout = QHBoxLayout(); results_controls_layout.setSpacing(3)
        self.append_mode_checkbox = QCheckBox("Append Mode", self); self.append_mode_checkbox.setFixedHeight(22); self.append_mode_checkbox.stateChanged.connect(self.save_append_mode_state)
        results_controls_layout.addWidget(self.append_mode_checkbox); export_results_button = QPushButton("Export", self); export_results_button.setFixedHeight(24); export_results_button.clicked.connect(self.export_results_to_markdown)
        results_controls_layout.addWidget(export_results_button); copy_results_button = QPushButton("Copy HTML", self); copy_results_button.setFixedHeight(24); copy_results_button.clicked.connect(self.copy_results_to_clipboard)
        results_controls_layout.addWidget(copy_results_button); results_controls_layout.addStretch() 
        res_font_up = QPushButton("↑", self); res_font_up.setFixedSize(24,24); res_font_up.clicked.connect(lambda: self.adjust_textarea_font(self.results_textedit,1))
        results_controls_layout.addWidget(res_font_up); res_font_down = QPushButton("↓", self); res_font_down.setFixedSize(24,24); res_font_down.clicked.connect(lambda: self.adjust_textarea_font(self.results_textedit,-1))
        results_controls_layout.addWidget(res_font_down); results_layout.addLayout(results_controls_layout); self.splitter.addWidget(self.results_container)
        self.results_container.setVisible(self.results_in_app)
        if not self.results_in_app and len(self.splitter_sizes) == 3: self.splitter.setSizes([self.splitter_sizes[0], self.splitter_sizes[1] + self.splitter_sizes[2], 0])
        else: self.splitter.setSizes(self.splitter_sizes)
        self.splitter.splitterMoved.connect(self.save_splitter_sizes)
        self.status_bar = self.statusBar(); self.progress_bar = QProgressBar(self); self.progress_bar.setMaximumWidth(200); self.progress_bar.setVisible(False)
        self.status_bar.addPermanentWidget(self.progress_bar)
        self.tray_icon = QSystemTrayIcon(self); icon_path = os.path.join(BASE_PATH, 'text-analytics.png') 
        if os.path.exists(icon_path): self.tray_icon.setIcon(QIcon(icon_path))
        else: self.tray_icon.setIcon(self.style().standardIcon(QStyle.SP_ComputerIcon))
        self.tray_icon.setToolTip("CoDude"); tray_menu = QMenu()
        show_action = QAction("Show/Hide", self); show_action.triggered.connect(self.show_hide_window); tray_menu.addAction(show_action); tray_menu.addSeparator()
        exit_action = QAction("Exit", self); exit_action.triggered.connect(QApplication.instance().quit); tray_menu.addAction(exit_action)
        self.tray_icon.setContextMenu(tray_menu); self.tray_icon.activated.connect(self.on_tray_icon_activated); self.tray_icon.show()
        self.custom_command_shortcut = QShortcut(QKeySequence("Ctrl+Return"), self); self.custom_command_shortcut.activated.connect(self.send_custom_or_chat_command)
        self.load_recipes_and_populate_list(); self.apply_theme(); self.append_mode_checkbox.setChecked(self.append_mode) 
        self.on_input_mode_changed(self.input_mode_combo.currentText()) 
        if self.permanent_memory and self.memory_dir and os.path.exists(self.memory_dir): self.load_permanent_memory_entries() 
        QTimer.singleShot(1000, self.start_hotkey_thread); logging.info("CoDudeApp initialization complete")

    def get_themed_document_stylesheet(self):
        font_family = self.font().family(); current_doc_font_size = self.font_size 
        base_css = f""" body {{ font-family: "{font_family}"; font-size: {current_doc_font_size}pt; margin: 5px; line-height: 1.4; }} p {{ margin: 0.5em 0; }} h1, h2, h3, h4, h5, h6 {{ margin-top: 1em; margin-bottom: 0.5em; font-weight: bold; line-height: 1.2; }} h1 {{ font-size: {current_doc_font_size + 6}pt; }} h2 {{ font-size: {current_doc_font_size + 4}pt; }} h3 {{ font-size: {current_doc_font_size + 2}pt; }} h4 {{ font-size: {current_doc_font_size + 1}pt;}} ul, ol {{ margin-left: 1.5em; padding-left: 0.5em; }} li {{ margin-bottom: 0.3em; }} pre {{ padding: 0.8em; border-radius: 5px; overflow-x: auto; white-space: pre-wrap; word-wrap: break-word; font-family: Consolas, Monaco, 'Andale Mono', 'Ubuntu Mono', monospace; font-size: {max(8, current_doc_font_size -1)}pt; }} code {{ padding: 0.1em 0.3em; border-radius: 3px; font-family: Consolas, Monaco, 'Andale Mono', 'Ubuntu Mono', monospace; font-size: {max(8, current_doc_font_size -1)}pt;}} pre code {{ padding: 0; background-color: transparent; border: none; font-size: inherit; }} blockquote {{ border-left: 3px solid; padding-left: 1em; margin: 0.8em 0; font-style: italic;}} table {{ border-collapse: collapse; width: auto; max-width: 98%; margin: 1em auto; box-shadow: 0 0 3px rgba(0,0,0,0.1); }} th, td {{ border: 1px solid; padding: 0.5em; text-align: left; }} hr {{ border: 0; border-top: 1px solid; margin: 1em 0; }} .think-block {{ border: 1px dashed; border-radius: 5px; padding: 0.8em; margin: 0.8em 0; font-style: italic; opacity: 0.8; }} """
        if self._theme == 'Dark': return base_css + f""" body {{ background-color: #3c3f41; color: #e0e0e0; }} h1, h2, h3, h4, h5, h6 {{ color: #79a6dc; border-bottom: 1px solid #4a4a4f; padding-bottom: 0.2em;}} pre {{ background-color: #2a2a2e; color: #d0d0d0; border: 1px solid #4a4a4f; }} code {{ background-color: #2a2a2e; color: #d0d0d0; }} blockquote {{ border-left-color: #557799; color: #b0b0b0; background-color: #404048;}} th, td {{ border-color: #555555; }} th {{ background-color: #45454a; }} hr {{ border-top-color: #555555; }} a {{ color: #82b1ff; }} .think-block {{ background-color: #404048; border-color: #557799; color: #b0b0b0; }} """
        else: return base_css + f""" body {{ background-color: #ffffff; color: #1e1e1e; }} h1, h2, h3, h4, h5, h6 {{ color: #003366; border-bottom: 1px solid #e0e0e0; padding-bottom: 0.2em; }} pre {{ background-color: #f0f0f0; color: #2e2e2e; border: 1px solid #cccccc; }} code {{ background-color: #f0f0f0; color: #2e2e2e; }} blockquote {{ border-left-color: #cccccc; color: #444444; background-color: #f8f8f8;}} th, td {{ border-color: #cccccc; }} th {{ background-color: #e8e8e8; }} hr {{ border-top-color: #cccccc; }} a {{ color: #007acc; }} .think-block {{ background-color: #f8f8f8; border-color: #ccc; color: #444; }} """

    def format_markdown_for_display(self, markdown_text):
        if markdown_text is None: markdown_text = ""
        text_for_md = markdown_text.replace('<think>', '<div class="think-block">').replace('</think>', '</div>')
        return md_to_html(text_for_md, extensions=['fenced_code', 'tables', 'sane_lists', 'nl2br', 'attr_list'])

    def escape_html_for_manual_construct(self, text):
        if text is None: return ""
        return html.escape(str(text)).replace("\n", "<br/>")

    def on_input_mode_changed(self, mode_text):
        is_chat_mode = (mode_text == "Chat Mode:")
        self.append_mode_checkbox.setChecked(is_chat_mode); self.append_mode_checkbox.setEnabled(not is_chat_mode)
        self.custom_input_textedit.setPlaceholderText("Enter chat message (Ctrl+Enter)" if is_chat_mode else "Enter custom instructions (Ctrl+Enter)")
        if is_chat_mode and self.results_in_app and not self.results_textedit.toPlainText().strip() :
             self.results_textedit.setHtml("<p style='color: grey; font-style: italic;'>Chat mode started. Type your message below.</p>")

    def _save_partial_config(self, updates_dict):
        try:
            config = {}; 
            if os.path.exists(CONFIG_FILE):
                with open(CONFIG_FILE, 'r', encoding='utf-8') as f: 
                    try: config = json.load(f)
                    except json.JSONDecodeError as e: logging.error(f"Error decoding config file {CONFIG_FILE}: {e}. Config saving aborted."); return
            config.update(updates_dict)
            with open(CONFIG_FILE, 'w', encoding='utf-8') as f: json.dump(config, f, indent=4)
        except Exception as e: logging.error(f"Error saving partial config: {e}")

    def save_splitter_sizes(self, pos, index):
        try:
            current_sizes = self.splitter.sizes(); min_width = 50
            if self.results_container.isVisible() and len(current_sizes) == 3: self.splitter_sizes = [max(min_width, s) for s in current_sizes]
            elif not self.results_container.isVisible() and len(current_sizes) >= 2: self.splitter_sizes = [max(min_width, current_sizes[0]), max(min_width, current_sizes[1]), 0]
            else: logging.warning(f"Splitter unexpected widget count: {len(current_sizes)}. Sizes not saved."); return
            self._save_partial_config({'splitter_sizes': self.splitter_sizes}); logging.debug(f"Splitter sizes saved: {self.splitter_sizes}")
        except Exception as e: logging.error(f"Error saving splitter sizes: {e}")

    def start_hotkey_thread(self):
        try:
            hotkey_string = self.load_hotkey_config_string()
            if not hotkey_string: logging.warning("Hotkey string empty/invalid. Listener not started."); return
            if hasattr(self, 'hotkey_thread') and self.hotkey_thread and self.hotkey_thread.isRunning():
                logging.info("Terminating existing hotkey thread..."); self.hotkey_thread.terminate(); self.hotkey_thread.wait(500) 
            self.hotkey_thread = HotkeySignal(hotkey_string)
            self.hotkey_thread.text_captured.connect(self.update_captured_text_area)
            self.hotkey_thread.show_window.connect(self.show_hide_window); self.hotkey_thread.start()
            logging.info(f"Hotkey thread started with {hotkey_string}")
        except Exception as e:
            logging.error(f"Error starting hotkey thread: {e}")
            if "keyboard" not in str(e).lower(): QMessageBox.critical(self, "Hotkey Error", f"Could not start hotkey listener: {e}")

    def load_hotkey_config_string(self):
        default_hotkey = 'ctrl+alt+c'; 
        try:
            config = {}; 
            if os.path.exists(CONFIG_FILE):
                with open(CONFIG_FILE, 'r', encoding='utf-8') as f: config = json.load(f)
            hotkey_cfg = config.get("hotkey", {"ctrl": True, "alt": True, "main_key": "c"})
            ctrl = hotkey_cfg.get("ctrl", False); shift = hotkey_cfg.get("shift", False); alt = hotkey_cfg.get("alt", False); main_k = hotkey_cfg.get("main_key", "c").lower().strip()
            modifiers = []; valid_chars = "abcdefghijklmnopqrstuvwxyz0123456789`-=[]\\;',./"
            if ctrl: modifiers.append("ctrl"); 
            if shift: modifiers.append("shift"); 
            if alt: modifiers.append("alt")
            if not main_k or len(main_k) != 1 or main_k not in valid_chars: logging.warning(f"Invalid main key '{main_k}', using default {default_hotkey}"); return default_hotkey
            hotkey_str = '+'.join(modifiers + [main_k]) if modifiers else main_k; 
            if not hotkey_str: return default_hotkey
            logging.debug(f"Loaded hotkey string: {hotkey_str}"); return hotkey_str
        except Exception as e: logging.error(f"Error loading hotkey config string: {e}"); return default_hotkey

    def validate_and_load_config(self):
        default_recipes_path = os.path.join(BASE_PATH, "recipes.md"); default_memory_path = os.path.join(BASE_PATH, "memory")
        default_config = { "llm_provider": "Local OpenAI-Compatible", "llm_url": "http://127.0.0.1:1234", "openai_api_key": "", "llm_model_name": "gpt-3.5-turbo", "recipes_file": default_recipes_path, "hotkey": {"ctrl": True, "shift": False, "alt": True, "main_key": "c"}, "logging_level": "Normal", "logging_output": "Both", "theme": "Light", "group_states": {}, "results_display": "Separate Windows", "font_size": 10, "permanent_memory": False, "memory_dir": default_memory_path, "append_mode": False, "textarea_font_sizes": {}, "splitter_sizes": self.splitter_sizes, "llm_timeout": 60, "close_behavior": "Exit", "max_recents": 5, "max_favorites": 5, "recently_used_recipes": [], "favorite_recipes": [] }
        try:
            logging.debug(f"Validating and loading config from {CONFIG_FILE}"); os.makedirs(os.path.dirname(CONFIG_FILE), exist_ok=True)
            config_to_load = default_config.copy()
            if os.path.exists(CONFIG_FILE):
                with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
                    loaded_config = json.load(f); 
                    for key in config_to_load: 
                        if key in loaded_config: config_to_load[key] = loaded_config[key]
            else: 
                 with open(CONFIG_FILE, 'w', encoding='utf-8') as f: json.dump(default_config, f, indent=4); logging.info(f"Default config file created at {CONFIG_FILE}")
            self.llm_provider = config_to_load['llm_provider']; self.llm_url = config_to_load['llm_url']; self.openai_api_key = config_to_load['openai_api_key']; self.llm_model_name = config_to_load['llm_model_name']
            self.recipes_file = config_to_load['recipes_file']
            if self.recipes_file and not os.path.isabs(self.recipes_file): self.recipes_file = os.path.join(BASE_PATH, self.recipes_file)
            self.hotkey_config = config_to_load['hotkey']; setup_logging(config_to_load['logging_level'], config_to_load['logging_output'])
            self._theme = config_to_load['theme']; self._group_states = config_to_load.get('group_states', {}); self.results_in_app = config_to_load['results_display'] == 'In-App Textarea'; self.font_size = config_to_load.get('font_size', 10)
            self.permanent_memory = config_to_load.get('permanent_memory', False); self.memory_dir = config_to_load.get('memory_dir', default_memory_path)
            if self.memory_dir and not os.path.isabs(self.memory_dir): self.memory_dir = os.path.join(BASE_PATH, self.memory_dir)
            if self.permanent_memory and self.memory_dir: os.makedirs(self.memory_dir, exist_ok=True)
            self.append_mode = config_to_load.get('append_mode', False); self.textarea_font_sizes = config_to_load.get('textarea_font_sizes', {})
            loaded_splitter_sizes = config_to_load.get('splitter_sizes', self.splitter_sizes)
            if isinstance(loaded_splitter_sizes, list) and len(loaded_splitter_sizes) == 3 and all(isinstance(s, int) and s >= 0 for s in loaded_splitter_sizes): self.splitter_sizes = loaded_splitter_sizes
            else: logging.warning(f"Invalid splitter_sizes: {loaded_splitter_sizes}. Using default."); self.splitter_sizes = default_config['splitter_sizes']
            self.llm_timeout = config_to_load.get('llm_timeout', 60); self.close_behavior = config_to_load.get('close_behavior', "Exit")
            self.max_recents = config_to_load.get('max_recents', 5); self.max_favorites = config_to_load.get('max_favorites', 5)
            self.recently_used_recipes = deque([tuple(item) for item in config_to_load.get('recently_used_recipes', []) if isinstance(item, list) and len(item) == 2], maxlen=self.max_recents if self.max_recents > 0 else None)
            self.favorite_recipes = [tuple(item) for item in config_to_load.get('favorite_recipes', []) if isinstance(item, list) and len(item) == 2]
            logging.debug("Config loaded successfully.")
        except json.JSONDecodeError as json_err:
             logging.error(f"Config file {CONFIG_FILE} is invalid JSON: {json_err}. Using defaults.", exc_info=True); QMessageBox.critical(self, "Config Error", f"Config file corrupted (invalid JSON).\nPlease fix or delete '{CONFIG_FILE}'.\nUsing default settings.")
             for key, value in default_config.items():
                 try: setattr(self, key, value); 
                 except: logging.error(f"Failed to set default for {key}")
             if not os.path.isabs(self.recipes_file): self.recipes_file = os.path.join(BASE_PATH, self.recipes_file)
        except Exception as e:
            logging.error(f"Config validation/loading failed: {e}. Using defaults.", exc_info=True); QMessageBox.warning(self, "Config Error", f"Invalid config file. Using defaults.\nDetails: {e}")
            for key, value in default_config.items():
                try: setattr(self, key, value); 
                except: logging.error(f"Failed to set default for {key}")
            if not os.path.isabs(self.recipes_file): self.recipes_file = os.path.join(BASE_PATH, self.recipes_file)
            try:
                with open(CONFIG_FILE, 'w', encoding='utf-8') as f: json.dump(default_config, f, indent=4)
            except Exception as save_e: logging.error(f"Failed to write default config after error: {save_e}")

    def apply_theme(self):
        try:
            logging.debug(f"Applying theme: {self._theme} with font size {self.font_size}pt"); app = QApplication.instance(); base_font = QFont(self.font().family(), self.font_size); app.setFont(base_font)
            self.light_stylesheet_base = f""" QMainWindow, QWidget {{ background-color: #f0f0f0; color: #000000; }} QTextEdit, QLineEdit {{ background-color: #ffffff; color: #000000; border: 1px solid #cccccc; }} QPushButton {{ background-color: #e0e0e0; color: #000000; border: 1px solid #bbbbbb; padding: 3px 6px; text-align: left; }} QPushButton:hover {{ background-color: #d0d0d0; }} QPushButton#groupButton {{ background-color: #d8d8d8; font-weight: bold; text-align: left; border: 1px solid #b0b0b0; }} QComboBox {{ background-color: #ffffff; color: #000000; border: 1px solid #cccccc; padding: 1px; min-height: 20px; }} QTabWidget::pane {{ border: 1px solid #cccccc; background: #f0f0f0; }} QTabBar::tab {{ background: #e0e0e0; color: #000000; padding: 4px; border: 1px solid #cccccc; border-bottom: none; }} QTabBar::tab:selected {{ background: #f0f0f0; }} QScrollArea {{ background-color: #f0f0f0; border: none; }} QScrollBar:vertical {{ background: #e0e0e0; width: 12px; margin: 0px; }} QScrollBar::handle:vertical {{ background: #c0c0c0; min-height: 20px; border-radius: 6px;}} QScrollBar:horizontal {{ background: #e0e0e0; height: 12px; margin: 0px; }} QScrollBar::handle:horizontal {{ background: #c0c0c0; min-width: 20px; border-radius: 6px;}} QMenuBar {{ background-color: #e0e0e0; color: #000000; }} QMenu {{ background-color: #ffffff; color: #000000; border: 1px solid #cccccc; }} QMenu::item:selected {{ background-color: #0078d7; color: #ffffff; }} QLabel, QCheckBox {{ color: #000000; }} QSplitter::handle {{ background: #cccccc; }} QSplitter::handle:hover {{ background: #bbbbbb; }} QDialog {{ background-color: #f0f0f0; }} """
            self.dark_stylesheet_base = f""" QMainWindow, QWidget {{ background-color: #2b2b2b; color: #e0e0e0; }} QTextEdit, QLineEdit {{ background-color: #3c3f41; color: #e0e0e0; border: 1px solid #555555; }} QPushButton {{ background-color: #4a4a4a; color: #e0e0e0; border: 1px solid #5f5f5f; padding: 3px 6px; text-align: left; }} QPushButton:hover {{ background-color: #5a5a5a; }} QPushButton#groupButton {{ background-color: #525252; font-weight: bold; text-align: left; border: 1px solid #666666; }} QComboBox {{ background-color: #3c3f41; color: #e0e0e0; border: 1px solid #555555; selection-background-color: #5a5a5a; padding: 1px; min-height: 20px; }} QComboBox QAbstractItemView {{ background-color: #3c3f41; color: #e0e0e0; selection-background-color: #5a5a5a; border: 1px solid #555555;}} QTabWidget::pane {{ border: 1px solid #555555; background: #2b2b2b; }} QTabBar::tab {{ background: #3c3f41; color: #e0e0e0; padding: 4px; border: 1px solid #555555; border-bottom: none; }} QTabBar::tab:selected {{ background: #2b2b2b; }} QScrollArea {{ background-color: #2b2b2b; border: none; }} QScrollBar:vertical {{ background: #3c3f41; width: 12px; margin: 0px; }} QScrollBar::handle:vertical {{ background: #5a5a5a; min-height: 20px; border-radius: 6px; }} QScrollBar:horizontal {{ background: #3c3f41; height: 12px; margin: 0px; }} QScrollBar::handle:horizontal {{ background: #5a5a5a; min-width: 20px; border-radius: 6px; }} QMenuBar {{ background-color: #3c3f41; color: #e0e0e0; }} QMenu {{ background-color: #3c3f41; color: #e0e0e0; border: 1px solid #555555; }} QMenu::item:selected {{ background-color: #0078d7; color: #ffffff; }} QLabel, QCheckBox {{ color: #e0e0e0; }} QSplitter::handle {{ background: #555555; }} QSplitter::handle:hover {{ background: #666666; }} QDialog {{ background-color: #2b2b2b; }} """
            chosen_stylesheet = self.dark_stylesheet_base if self._theme == 'Dark' else self.light_stylesheet_base; chosen_stylesheet += f" * {{ font-size: {self.font_size}pt; }}" 
            app.setStyleSheet(chosen_stylesheet); doc_style = self.get_themed_document_stylesheet()
            text_areas_to_style = [(self.custom_input_textedit, False), (self.captured_text_edit, False), (self.results_textedit, True)]
            for textarea, is_markdown_view in text_areas_to_style:
                textarea_id = str(id(textarea)); size_pt = self.textarea_font_sizes.get(textarea_id, self.font_size)
                font = textarea.font(); font.setPointSize(size_pt); textarea.setFont(font)
                if is_markdown_view:
                    textarea.document().setDefaultStyleSheet(doc_style); 
                    if textarea.toPlainText(): current_html = textarea.toHtml(); textarea.setHtml(current_html) 
            self.update(); self.repaint(); QApplication.processEvents()
        except Exception as e: logging.error(f"Error applying theme: {e}", exc_info=True)

    def _clear_layout(self, layout):
        if layout is not None:
            while layout.count():
                item = layout.takeAt(0); widget = item.widget()
                if widget is not None: widget.deleteLater()
                else:
                    sub_layout = item.layout()
                    if sub_layout is not None: self._clear_layout(sub_layout);

    def _parse_recipes_file_to_structure(self):
        structured_recipes = []; current_group_title = None
        if not self.recipes_file or not os.path.exists(self.recipes_file): logging.warning(f"Recipes file missing: {self.recipes_file}"); return structured_recipes
        try:
            with open(self.recipes_file, 'r', encoding='utf-8') as f: lines = f.readlines()
        except Exception as e: logging.error(f"Error reading recipes file {self.recipes_file}: {e}"); return structured_recipes
        for line_num, line_content in enumerate(lines):
            line = line_content.strip()
            if not line: continue
            if line.startswith('#'): current_group_title = line.lstrip('#').strip(); structured_recipes.append({'type': 'group', 'title': current_group_title, 'line_num': line_num})
            elif line.startswith('**') and ':' in line:
                try:
                    name_part, prompt_part = line.split(':', 1); name = name_part.strip().strip('**').strip(); prompt_from_file = prompt_part.strip()
                    if name and prompt_from_file: structured_recipes.append({'type': 'recipe', 'name': name, 'prompt': prompt_from_file, 'group_title': current_group_title, 'line_num': line_num, 'id': (name, prompt_from_file)})
                    else: logging.warning(f"Skipping malformed recipe (line {line_num+1}): {line}")
                except ValueError: logging.warning(f"Skipping malformed recipe line (line {line_num+1}): {line}")
        return structured_recipes

    def load_recipes_and_populate_list(self):
        logging.info(f"Loading recipes from: {self.recipes_file}"); self._clear_layout(self.recipe_buttons_layout)
        self._all_recipes_data = self._parse_recipes_file_to_structure()
        if not self._all_recipes_data and (not self.recipes_file or not os.path.exists(self.recipes_file)):
            if not self.recipes_file or not os.path.exists(self.recipes_file):
                reply = QMessageBox.question(self, "Recipes File Missing", f"Recipes file ({self.recipes_file or 'Not Set'}) missing. Download default?", QMessageBox.Yes | QMessageBox.No)
                if reply == QMessageBox.Yes: pass # TODO: Download
                else: self.recipe_buttons_layout.addWidget(QLabel("Recipes file missing. Set in Configure."))
            else: self.recipe_buttons_layout.addWidget(QLabel("No valid recipes found in file."))
            self.recipe_buttons_layout.addStretch(); return
        if self.recently_used_recipes.maxlen != self.max_recents: self.recently_used_recipes = deque(list(self.recently_used_recipes), maxlen=self.max_recents if self.max_recents > 0 else None)
        self._add_virtual_group_to_layout("Recently Used", self.recently_used_recipes)
        self._add_virtual_group_to_layout("Favorites", self.favorite_recipes, is_favorites_group=True)
        last_group_items_layout = None 
        for item_data in self._all_recipes_data:
            if item_data['type'] == 'group':
                group_title = item_data['title']; group_button, group_widget_container, group_items_layout = self._create_collapsible_group(group_title)
                self.recipe_buttons_layout.addWidget(group_button); self.recipe_buttons_layout.addWidget(group_widget_container)
                last_group_items_layout = group_items_layout 
            elif item_data['type'] == 'recipe':
                name, prompt = item_data['name'], item_data['prompt']; is_fav = (name, prompt) in self.favorite_recipes
                recipe_button = self._create_recipe_button(name, prompt, is_fav)
                if last_group_items_layout is not None: last_group_items_layout.addWidget(recipe_button) 
                else: self.recipe_buttons_layout.addWidget(recipe_button); logging.warning(f"Recipe '{name}' added outside group. Check recipes.md.")
        self.recipe_buttons_layout.addStretch(); self.recipes_scroll_widget.setLayout(self.recipe_buttons_layout) 
        self.recipes_scroll_widget.adjustSize(); self.recipes_scroll_area.updateGeometry()

    def _add_virtual_group_to_layout(self, group_name, recipe_id_list, is_favorites_group=False):
        effective_list = list(recipe_id_list); 
        if group_name == "Recently Used": effective_list.reverse()
        if not effective_list and group_name != "Favorites": return
        group_button, group_widget_container, group_items_layout = self._create_collapsible_group(group_name)
        self.recipe_buttons_layout.addWidget(group_button); self.recipe_buttons_layout.addWidget(group_widget_container)
        for recipe_name, recipe_prompt_from_file in effective_list:
            is_fav = (recipe_name, recipe_prompt_from_file) in self.favorite_recipes
            recipe_button = self._create_recipe_button(recipe_name, recipe_prompt_from_file, is_fav)
            group_items_layout.addWidget(recipe_button)
        if not effective_list: group_items_layout.addStretch()

    def _create_collapsible_group(self, title):
        group_button = QPushButton(); group_button.setObjectName("groupButton"); group_button.setCheckable(True)
        group_button.setStyleSheet("text-align: left; font-weight: bold;") 
        is_expanded = self._group_states.get(title, True); group_button.setChecked(is_expanded)
        group_button.setText(f"{title} {'▼' if is_expanded else '▶'}"); group_button.setFixedHeight(22)
        group_button.setContextMenuPolicy(Qt.CustomContextMenu)
        group_button.customContextMenuRequested.connect(partial(self.show_group_context_menu, title))
        group_widget_container = QWidget(); sp = QSizePolicy(QSizePolicy.MinimumExpanding, QSizePolicy.Fixed) 
        group_widget_container.setSizePolicy(sp)
        group_items_layout = QVBoxLayout(group_widget_container) 
        group_items_layout.setContentsMargins(15, 2, 0, 2); group_items_layout.setSpacing(1) 
        group_widget_container.setVisible(is_expanded); 
        group_button.toggled.connect(lambda checked, gc=group_widget_container, gb=group_button, t=title: self.toggle_group_visibility(checked, gc, gb, t))
        return group_button, group_widget_container, group_items_layout

    def _create_recipe_button(self, name, prompt_from_file, is_favorite):
        button_text = f"[★] {name}" if is_favorite else name; button = QPushButton(button_text); button.setFixedHeight(20)
        button.setToolTip(f"Prompt: {prompt_from_file[:100]}{'...' if len(prompt_from_file)>100 else ''}")
        button.clicked.connect(partial(self.execute_recipe_command, prompt_from_file, name, button))
        button.setContextMenuPolicy(Qt.CustomContextMenu); button.customContextMenuRequested.connect(partial(self.show_recipe_context_menu, name, prompt_from_file, button))
        return button

    def toggle_group_visibility(self, is_checked, group_container, group_button, title):
        group_container.setVisible(is_checked)
        sp = group_container.sizePolicy(); sp.setVerticalPolicy(QSizePolicy.Preferred if is_checked else QSizePolicy.Fixed); group_container.setSizePolicy(sp)
        if group_container.layout(): group_container.layout().invalidate(); group_container.layout().activate()   
        group_container.adjustSize(); group_container.updateGeometry()
        self._group_states[title] = is_checked; group_button.setText(f"{title} {'▼' if is_checked else '▶'}"); self._save_partial_config({'group_states': self._group_states})
        self.recipes_scroll_widget.adjustSize(); self.recipes_scroll_widget.updateGeometry()
        self.recipes_scroll_area.updateGeometry(); QApplication.processEvents()

    def filter_recipes_display(self, query): 
        query = query.lower(); any_match_found = False
        for i in range(self.recipe_buttons_layout.count()):
            top_item = self.recipe_buttons_layout.itemAt(i); 
            if not top_item: continue
            widget = top_item.widget()
            if not widget: continue
            is_group_container = False
            group_button_ref = None
            group_title = None
            if i > 0:
                prev_item = self.recipe_buttons_layout.itemAt(i-1)
                if prev_item and prev_item.widget() and isinstance(prev_item.widget(), QPushButton) and prev_item.widget().objectName() == "groupButton" and isinstance(widget, QWidget) and widget.layout() is not None:
                    is_group_container = True; group_button_ref = prev_item.widget(); group_title = group_button_ref.text().rsplit(' ',1)[0]
            if is_group_container:
                group_layout = widget.layout(); group_has_visible_recipe = False
                for j in range(group_layout.count()):
                    recipe_item = group_layout.itemAt(j)
                    if recipe_item and recipe_item.widget() and isinstance(recipe_item.widget(), QPushButton):
                        recipe_button = recipe_item.widget(); 
                        if recipe_button.objectName() == "groupButton": continue
                        recipe_name = recipe_button.text().lower().replace("[★]", "").strip(); recipe_prompt_tooltip = recipe_button.toolTip().lower().replace("prompt:","").strip()
                        matches = query in recipe_name or query in recipe_prompt_tooltip; recipe_button.setVisible(matches)
                        if matches: group_has_visible_recipe = True; any_match_found = True
                is_expanded = self._group_states.get(group_title, True); widget.setVisible(group_has_visible_recipe and is_expanded); group_button_ref.setVisible(group_has_visible_recipe or not query)
        if not query: self.load_recipes_and_populate_list(); return
        self.recipes_scroll_widget.adjustSize(); self.recipes_scroll_area.updateGeometry(); QApplication.processEvents()

    def show_recipe_context_menu(self, recipe_name, recipe_prompt_from_file, recipe_button, point):
        menu = QMenu(self); recipe_id = (recipe_name, recipe_prompt_from_file) 
        is_starred = recipe_id in self.favorite_recipes; star_action = menu.addAction("⭐ Unstar Recipe" if is_starred else "⭐ Star Recipe")
        star_action.triggered.connect(partial(self.toggle_favorite_status, recipe_id)); menu.addSeparator()
        edit_action = menu.addAction("✏️ Edit Recipe..."); edit_action.triggered.connect(partial(self.edit_recipe_from_context_menu, recipe_id))
        delete_action = menu.addAction("🗑️ Delete Recipe"); delete_action.triggered.connect(partial(self.delete_recipe_from_context_menu, recipe_id))
        menu.exec_(recipe_button.mapToGlobal(point))

    def show_group_context_menu(self, group_title, point):
        menu = QMenu(self)
        edit_action = menu.addAction("✏️ Edit Group Title...")
        edit_action.triggered.connect(partial(self.edit_group_title, group_title))
        menu.addSeparator()
        new_group_action = menu.addAction("➕ New Group Below")
        new_group_action.triggered.connect(partial(self.create_new_group, group_title))
        new_command_action = menu.addAction("➕ New Command in Group")
        new_command_action.triggered.connect(partial(self.create_new_command_in_group, group_title))
        menu.addSeparator()
        delete_action = menu.addAction("🗑️ Delete Group")
        delete_action.triggered.connect(partial(self.delete_group, group_title))
        menu.exec_(self.sender().mapToGlobal(point))

    def show_recipes_area_context_menu(self, point):
        menu = QMenu(self)
        
        # Get the item under cursor if any
        clicked_item = None
        for i in range(self.recipe_buttons_layout.count()):
            item = self.recipe_buttons_layout.itemAt(i)
            if item and item.widget() and item.widget().underMouse():
                clicked_item = item.widget()
                break
                
        # Determine context
        is_on_group = clicked_item and clicked_item.objectName() == "groupButton"
        is_on_recipe = clicked_item and not is_on_group
        is_empty_space = not clicked_item
        
        # Add appropriate menu items
        if is_on_group:
            group_title = clicked_item.text().split(' ')[0]
            edit_action = menu.addAction(f"✏️ Edit '{group_title}'")
            edit_action.triggered.connect(partial(self.edit_group_title, group_title))
            
            new_group_action = menu.addAction("➕ New Group Below")
            new_group_action.triggered.connect(partial(self.create_new_group, group_title))
            
            new_command_action = menu.addAction("➕ New Command in Group")
            new_command_action.triggered.connect(partial(self.create_new_command_in_group, group_title))
            
            menu.addSeparator()
            
            delete_action = menu.addAction(f"🗑️ Delete '{group_title}'")
            delete_action.triggered.connect(partial(self.delete_group, group_title))
            
        elif is_on_recipe:
            # Find which group this recipe belongs to
            recipe_name = clicked_item.text().replace("[★]", "").strip()
            group_title = None
            for item in self._all_recipes_data:
                if item['type'] == 'recipe' and item['name'] == recipe_name:
                    group_title = item['group_title']
                    break
            
            if group_title:
                new_group_action = menu.addAction("➕ New Group Below")
                new_group_action.triggered.connect(partial(self.create_new_group, group_title))
                
                new_command_action = menu.addAction("➕ New Command Here")
                new_command_action.triggered.connect(partial(self.create_new_command_at_position, group_title, recipe_name))
                
        else:  # Empty space
            new_group_action = menu.addAction("➕ New Group")
            new_group_action.triggered.connect(partial(self.create_new_group, None))
            
            new_command_action = menu.addAction("➕ New Command")
            new_command_action.triggered.connect(partial(self.create_new_command_in_group, "Basic Recipes"))
            
        menu.exec_(self.mapToGlobal(point))

    def toggle_favorite_status(self, recipe_id):
        if recipe_id in self.favorite_recipes: self.favorite_recipes.remove(recipe_id)
        else:
            if len(self.favorite_recipes) < self.max_favorites or self.max_favorites <= 0: self.favorite_recipes.append(recipe_id)
            else: QMessageBox.information(self, "Favorites Full", f"Max {self.max_favorites} favorites allowed."); return
        self._save_partial_config({'favorite_recipes': self.favorite_recipes}); self.load_recipes_and_populate_list()

    def edit_recipe_from_context_menu(self, recipe_id_to_edit):
        old_name, old_prompt_from_file = recipe_id_to_edit; dialog = EditRecipeDialog(old_name, old_prompt_from_file, self)
        if dialog.exec_() == QDialog.Accepted:
            new_name, new_prompt_from_file = dialog.get_data()
            if not new_name or not new_prompt_from_file: QMessageBox.warning(self, "Input Error", "Recipe name/prompt cannot be empty."); return
            if self._update_recipe_in_file(old_name, old_prompt_from_file, new_name, new_prompt_from_file):
                new_id = (new_name, new_prompt_from_file)
                if recipe_id_to_edit in self.recently_used_recipes:
                    temp_list = list(self.recently_used_recipes); 
                    try: temp_list[temp_list.index(recipe_id_to_edit)] = new_id; self.recently_used_recipes = deque(temp_list, maxlen=self.recently_used_recipes.maxlen)
                    except ValueError: pass 
                    self._save_partial_config({'recently_used_recipes': list(self.recently_used_recipes)})
                if recipe_id_to_edit in self.favorite_recipes:
                    try: self.favorite_recipes[self.favorite_recipes.index(recipe_id_to_edit)] = new_id
                    except ValueError: pass
                    self._save_partial_config({'favorite_recipes': self.favorite_recipes})
                self.load_recipes_and_populate_list(); logging.info(f"Recipe '{old_name}' edited to '{new_name}'.")
            else: QMessageBox.critical(self, "Edit Error", f"Failed to update recipe in {self.recipes_file}.")

    def _update_recipe_in_file(self, old_name, old_prompt_from_file, new_name, new_prompt_from_file):
        if not self.recipes_file or not os.path.exists(self.recipes_file): return False
        self._backup_recipes_file("before_edit"); 
        try:
            with open(self.recipes_file, 'r', encoding='utf-8') as f: lines = f.readlines()
            found_and_updated = False; norm_old_name = normalize_whitespace_for_comparison(old_name)
            norm_old_prompt = normalize_whitespace_for_comparison(old_prompt_from_file); updated_lines = []
            for line_num, line_content in enumerate(lines):
                stripped_line = line_content.strip()
                if stripped_line.startswith('**') and ':' in stripped_line:
                    try:
                        name_part, prompt_part = stripped_line.split(':', 1); current_line_name = name_part.strip().strip('**').strip(); current_line_prompt = prompt_part.strip()
                        if normalize_whitespace_for_comparison(current_line_name) == norm_old_name and normalize_whitespace_for_comparison(current_line_prompt) == norm_old_prompt:
                            newline_char = line_content[len(stripped_line):]; updated_lines.append(f"**{new_name}**: {new_prompt_from_file}{newline_char}"); found_and_updated = True; logging.info(f"Found and replaced recipe on line {line_num+1}"); continue
                    except Exception as parse_ex: logging.warning(f"Could not parse line {line_num+1} for update check: {stripped_line} - {parse_ex}")
                updated_lines.append(line_content)
            if found_and_updated:
                with open(self.recipes_file, 'w', encoding='utf-8') as f: f.writelines(updated_lines); return True
            else: logging.warning(f"Recipe to edit not found: Name='{old_name}', Prompt='{old_prompt_from_file[:50]}...'"); return False
        except Exception as e: logging.error(f"Error updating recipes file: {e}", exc_info=True); return False

    def delete_recipe_from_context_menu(self, recipe_id_to_delete):
        name, prompt_from_file = recipe_id_to_delete; reply = QMessageBox.question(self, "Confirm Deletion", f"Delete recipe '{name}'?", QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if reply != QMessageBox.Yes: return
        if self._remove_recipe_from_file(name, prompt_from_file):
            if recipe_id_to_delete in self.recently_used_recipes: self.recently_used_recipes.remove(recipe_id_to_delete); self._save_partial_config({'recently_used_recipes': list(self.recently_used_recipes)})
            if recipe_id_to_delete in self.favorite_recipes: self.favorite_recipes.remove(recipe_id_to_delete); self._save_partial_config({'favorite_recipes': self.favorite_recipes})
            self.load_recipes_and_populate_list(); logging.info(f"Recipe '{name}' deleted.")
        else: QMessageBox.critical(self, "Delete Error", f"Failed to delete recipe from {self.recipes_file}.")

    def _backup_recipes_file(self, suffix="backup"):
        if not self.recipes_file or not os.path.exists(self.recipes_file): return
        try:
            os.makedirs(BACKUP_DIR, exist_ok=True); timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            base_name = os.path.basename(self.recipes_file); backup_filename = f"{os.path.splitext(base_name)[0]}_{timestamp}_{suffix}.md"
            backup_path = os.path.join(BACKUP_DIR, backup_filename); shutil.copy2(self.recipes_file, backup_path)
            logging.info(f"Recipes file backed up to {backup_path}")
        except Exception as e: logging.error(f"Failed to backup recipes file: {e}")

    def _remove_recipe_from_file(self, name_to_delete, prompt_to_delete):
        if not self.recipes_file or not os.path.exists(self.recipes_file): return False
        self._backup_recipes_file("before_delete"); 
        try:
            with open(self.recipes_file, 'r', encoding='utf-8') as f: lines = f.readlines()
            found_and_removed = False; updated_lines = []; norm_name_del = normalize_whitespace_for_comparison(name_to_delete); norm_prompt_del = normalize_whitespace_for_comparison(prompt_to_delete)
            for line_num, line_content in enumerate(lines):
                stripped_line = line_content.strip()
                if stripped_line.startswith('**') and ':' in stripped_line:
                    try:
                        name_part, prompt_part = stripped_line.split(':', 1); current_line_name = name_part.strip().strip('**').strip(); current_line_prompt = prompt_part.strip()
                        if normalize_whitespace_for_comparison(current_line_name) == norm_name_del and normalize_whitespace_for_comparison(current_line_prompt) == norm_prompt_del: found_and_removed = True; logging.info(f"Found and removed recipe on line {line_num+1}"); continue 
                    except: pass 
                updated_lines.append(line_content)
            if found_and_removed:
                with open(self.recipes_file, 'w', encoding='utf-8') as f: f.writelines(updated_lines); return True
            else: logging.warning(f"Recipe to delete not found: {name_to_delete}"); return False
        except Exception as e: logging.error(f"Error removing recipe from file: {e}", exc_info=True); return False

    def send_custom_or_chat_command(self):
        command_text = self.custom_input_textedit.toPlainText().strip()
        if not command_text: QMessageBox.information(self, "No Input", "Please enter command or chat message."); return
        is_chat = (self.input_mode_combo.currentText() == "Chat Mode:"); captured_text_val = self.captured_text_edit.toPlainText()
        if is_chat and self.results_in_app:
            user_html = f"<div style='margin: 5px 0;'><p style='margin-bottom:0.1em; font-weight: bold; color: {self._theme_color('chat_user_label')};'>User:</p><div style='margin-left:10px; padding:5px 8px; border-radius:8px; background-color:{self._theme_color('chat_user_bg')}; display: inline-block; max-width: 85%;'><p style=\"margin:0;\">{self.escape_html_for_manual_construct(command_text)}</p></div></div>"
            if not self.results_textedit.toPlainText().strip().endswith("Chat mode started. Type your message below.") and self.results_textedit.toPlainText().strip() : self.results_textedit.append("<br>")
            self.results_textedit.append(user_html); self.results_textedit.moveCursor(QTextCursor.End)
        self.execute_recipe_command(command_text, "Custom Command/Chat", None, is_chat_mode=is_chat, text_override=captured_text_val); self.custom_input_textedit.clear()

    def _theme_color(self, key):
        if self._theme == 'Dark': colors = {'chat_user_bg': '#303848', 'chat_llm_bg': '#384030', 'general_text_edit_bg': '#3c3f41', 'chat_user_label': '#87CEFA', 'chat_llm_label': '#98FB98'}; return colors.get(key, '#e0e0e0')
        else: colors = {'chat_user_bg': '#e8f0fe', 'chat_llm_bg': '#f0f8e8', 'general_text_edit_bg': '#ffffff', 'chat_user_label': '#00008B', 'chat_llm_label': '#006400'}; return colors.get(key, '#1e1e1e')

    def execute_recipe_command(self, prompt_from_file_or_custom, recipe_name="Recipe", button_ref=None, is_chat_mode=False, text_override=None):
        captured_text = text_override if text_override is not None else self.captured_text_edit.toPlainText()
        if not is_chat_mode and not captured_text.strip(): QMessageBox.information(self, "No Text", "Please capture text for non-chat recipes."); return
        llm_api_config = { "provider": self.llm_provider, "url": self.llm_url, "api_key": self.openai_api_key, "model_name": self.llm_model_name }
        if not llm_api_config.get("url") and llm_api_config["provider"] == "Local OpenAI-Compatible": QMessageBox.warning(self, "LLM URL Missing", "LLM URL not configured."); return
        if not llm_api_config.get("api_key") and llm_api_config["provider"] == "OpenAI API": QMessageBox.warning(self, "API Key Missing", f"{llm_api_config['provider']} API Key not configured."); return
        logging.info(f"Executing: '{prompt_from_file_or_custom[:50]}...' (Chat: {is_chat_mode}) with text: '{captured_text[:50]}...'")
        if button_ref and isinstance(button_ref, QPushButton):
            original_style = button_ref.styleSheet(); highlight_style = "background-color: #90EE90; color: black; text-align: left;"
            button_ref.setStyleSheet(highlight_style); QTimer.singleShot(700, lambda b=button_ref, s=original_style: b.setStyleSheet(s))
        self.progress_bar.setVisible(True); self.progress_bar.setRange(0, 0)
        if recipe_name != "Custom Command/Chat" and not is_chat_mode:
            cleaned_name = recipe_name.replace("[★] ", "").strip(); recipe_id = (cleaned_name, prompt_from_file_or_custom) 
            if recipe_id in self.recently_used_recipes: self.recently_used_recipes.remove(recipe_id)
            self.recently_used_recipes.appendleft(recipe_id)
            if self.recently_used_recipes.maxlen != self.max_recents: self.recently_used_recipes = deque(list(self.recently_used_recipes), maxlen=self.max_recents if self.max_recents > 0 else None)
            self._save_partial_config({'recently_used_recipes': list(self.recently_used_recipes)})
        self.llm_thread = LLMRequestThread(llm_api_config, prompt_from_file_or_custom, captured_text, self.llm_timeout)
        self.llm_thread.response_received.connect(partial(self.handle_llm_response, captured_text=captured_text, prompt=prompt_from_file_or_custom, is_chat_mode=is_chat_mode))
        self.llm_thread.error_occurred.connect(self.handle_llm_error); self.llm_thread.start()

    def handle_llm_response(self, response_text, captured_text, prompt, is_chat_mode=False):
        logging.info("LLM Response Received"); self.progress_bar.setVisible(False); filename = None
        if self.permanent_memory and self.memory_dir:
            try:
                os.makedirs(self.memory_dir, exist_ok=True); safe_prompt_tag = "".join(c for c in prompt[:25] if c.isalnum() or c in " -_").strip() or "entry"
                timestamp = datetime.now().strftime("%Y%m%d_%H%M%S"); filename = f"{safe_prompt_tag}_{timestamp}.md"; file_path = os.path.join(self.memory_dir, filename)
                memory_content = f"Captured Text:\n{captured_text}\n\nPrompt:\n{prompt}\n\nLLM Response:\n{response_text}"; 
                with open(file_path, 'w', encoding='utf-8') as f: f.write(memory_content); logging.debug(f"Saved memory entry to {file_path}")
            except Exception as e: logging.error(f"Error saving permanent memory file: {e}"); filename = None 
        self._memory.append((captured_text, prompt, response_text, filename)); current_memory_idx = len(self._memory) - 1
        if self.results_in_app:
            formatted_llm_html_content = self.format_markdown_for_display(response_text)
            if is_chat_mode:
                llm_html = f"<div style='margin: 5px 0;'><p style='margin-bottom:0.1em; font-weight: bold; color: {self._theme_color('chat_llm_label')};'>LLM:</p><div style='margin-left:10px; padding:5px 8px; border-radius:8px; background-color:{self._theme_color('chat_llm_bg')}; display: inline-block; max-width: 85%;'><p style=\"margin:0;\">{formatted_llm_html_content}</p></div></div>"
                if self.results_textedit.toPlainText().strip(): self.results_textedit.append("<br>")
                self.results_textedit.append(llm_html)
            else:
                if self.append_mode_checkbox.isChecked() and self.results_textedit.toPlainText().strip(): self.results_textedit.append("<hr/>" + formatted_llm_html_content)
                else: self.results_textedit.setHtml(formatted_llm_html_content)
            self.results_textedit.moveCursor(QTextCursor.End); self.active_memory_index = current_memory_idx
        else: result_window = ResultWindow(response_text, self, current_memory_idx); result_window.show(); self.result_windows.append(result_window)
        item_text_summary = f"Prompt: {prompt[:25]}... Text: {captured_text[:25]}..."; entry_widget = MemoryEntryWidget(item_text_summary, filename); list_item = QListWidgetItem(self.memory_list); list_item.setSizeHint(entry_widget.sizeHint())
        entry_widget.delete_button.clicked.connect(partial(self.delete_memory_entry_from_button, list_item)); self.memory_list.setItemWidget(list_item, entry_widget); self.memory_list.scrollToBottom()

    def handle_llm_error(self, error_message):
        logging.error(f"LLM Error: {error_message}"); self.progress_bar.setVisible(False); QMessageBox.critical(self, "LLM Error", error_message)
        if self.results_in_app:
            error_html = f"<p style='color: red;'><b>LLM Error:</b><br/>{self.escape_html_for_manual_construct(error_message)}</p>"
            if self.input_mode_combo.currentText() == "Chat Mode:" or self.append_mode_checkbox.isChecked(): self.results_textedit.append("<hr style='border-color: red;'/>" + error_html)
            else: self.results_textedit.setHtml(error_html)

    def show_memory_entry_from_list_item(self, list_widget_item):
        index = self.memory_list.row(list_widget_item)
        if not (0 <= index < len(self._memory)): logging.error(f"Invalid memory index from list item: {index}"); return
        captured_text, prompt, response_content, filename = self._memory[index]; logging.debug(f"Showing memory entry {index}: Prompt '{prompt[:30]}...'")
        if self.results_in_app:
            if self.active_memory_index is not None and self.active_memory_index != index: self.save_memory_content_change(self.active_memory_index, self.results_textedit.toHtml())
            if response_content.strip().startswith('<'): response_display = response_content # Already HTML
            else: response_display = self.format_markdown_for_display(response_content) # Render MD
            full_entry_html = f"""<p><b>Original Captured Text:</b><br/>{self.escape_html_for_manual_construct(captured_text)}</p><p><b>Original Prompt:</b><br/>{self.escape_html_for_manual_construct(prompt)}</p><hr/><p><b>LLM Reply:</b></p>{response_display}"""; self.results_textedit.setHtml(full_entry_html); self.active_memory_index = index; self.results_textedit.moveCursor(QTextCursor.Start)
        else:
            existing_window = next((win for win in self.result_windows if win.memory_index == index), None)
            if existing_window: existing_window.showNormal(); existing_window.activateWindow()
            else: result_window = ResultWindow(response_content, self, index); result_window.show(); self.result_windows.append(result_window)

    def delete_memory_entry_from_button(self, item_from_list_widget):
        if self._deleting_memory: return 
        self._deleting_memory = True
        try:
            index_to_delete = self.memory_list.row(item_from_list_widget)
            if not (0 <= index_to_delete < len(self._memory)): logging.error(f"Delete: Invalid memory index {index_to_delete}"); self._deleting_memory = False; return
            reply = QMessageBox.question(self, "Confirm Deletion", "Delete this memory entry?", QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
            if reply != QMessageBox.Yes: self._deleting_memory = False; return
            _, _, _, filename_to_delete = self._memory[index_to_delete]; widget = self.memory_list.itemWidget(item_from_list_widget)
            if widget and hasattr(widget, 'delete_button'):
                try: widget.delete_button.clicked.disconnect() 
                except: pass
            self.memory_list.takeItem(index_to_delete); self._memory.pop(index_to_delete)
            if self.permanent_memory and self.memory_dir and filename_to_delete:
                file_path = os.path.join(self.memory_dir, filename_to_delete)
                if os.path.exists(file_path):
                    try: os.remove(file_path); logging.debug(f"Deleted permanent memory file: {file_path}")
                    except OSError as e: logging.error(f"Error deleting file {file_path}: {e}")
            if self.active_memory_index is not None:
                if self.active_memory_index == index_to_delete: self.active_memory_index = None; 
                if self.results_in_app: self.results_textedit.clear()
                elif self.active_memory_index > index_to_delete: self.active_memory_index -= 1
            logging.debug(f"Memory entry at index {index_to_delete} deleted.")
        except Exception as e: logging.error(f"Error deleting memory entry: {e}", exc_info=True); QMessageBox.critical(self, "Error", f"Failed to delete memory entry: {e}")
        finally: self._deleting_memory = False

    def on_results_text_changed_by_user(self): pass 
    
    def focusOutEvent(self, event): 
        if self.results_in_app and self.active_memory_index is not None:
            active_app_window = QApplication.activeWindow(); is_child_dialog = isinstance(active_app_window, QDialog) and active_app_window.parent() == self
            if active_app_window is None or (active_app_window != self and not is_child_dialog and active_app_window not in self.result_windows):
                logging.debug("Main window focus possibly lost. Saving active memory."); self.save_memory_content_change(self.active_memory_index, self.results_textedit.toHtml())
        super().focusOutEvent(event)

    def edit_group_title(self, current_title):
        new_title, ok = QInputDialog.getText(self, "Edit Group Title", "New title:", text=current_title)
        if ok and new_title and new_title != current_title:
            if self._update_group_title_in_file(current_title, new_title):
                self.load_recipes_and_populate_list()
            else:
                QMessageBox.critical(self, "Error", "Failed to update group title in file.")

    def _update_group_title_in_file(self, old_title, new_title):
        if not self.recipes_file or not os.path.exists(self.recipes_file): return False
        self._backup_recipes_file("before_group_edit")
        try:
            with open(self.recipes_file, 'r', encoding='utf-8') as f:
                lines = f.readlines()
            updated_lines = []
            for line in lines:
                stripped = line.strip()
                if stripped.startswith('#') and stripped[1:].strip() == old_title:
                    updated_lines.append(f"# {new_title}\n")
                else:
                    updated_lines.append(line)
            with open(self.recipes_file, 'w', encoding='utf-8') as f:
                f.writelines(updated_lines)
            return True
        except Exception as e:
            logging.error(f"Error updating group title: {e}")
            return False

    def create_new_group(self, current_group_title):
        new_title, ok = QInputDialog.getText(self, "New Group", "Enter new group name:")
        if not ok or not new_title.strip():
            return
            
        if not self.recipes_file:
            reply = QMessageBox.question(self, "No Recipes File", 
                "No recipes file exists. Create new one with this group?",
                QMessageBox.Yes | QMessageBox.No)
            if reply == QMessageBox.Yes:
                self.recipes_file = os.path.join(BASE_PATH, "recipes.md")
                with open(self.recipes_file, 'w', encoding='utf-8') as f:
                    f.write(f"# {new_title}\n")
                self.load_recipes_and_populate_list()
                return
            else:
                return

        self._backup_recipes_file("before_new_group")
        try:
            with open(self.recipes_file, 'r', encoding='utf-8') as f:
                lines = f.readlines()
            
            # Find insertion point after current group
            insert_at = len(lines)
            found_current = False
            for i, line in enumerate(lines):
                stripped = line.strip()
                if stripped.startswith('#') and stripped[1:].strip() == current_group_title:
                    found_current = True
                elif found_current and stripped.startswith('#'):
                    insert_at = i
                    break
            
            # Insert new group
            lines.insert(insert_at, f"# {new_title}\n\n")
            
            with open(self.recipes_file, 'w', encoding='utf-8') as f:
                f.writelines(lines)
                
            self.load_recipes_and_populate_list()
            return True
            
        except Exception as e:
            logging.error(f"Error creating new group: {e}")
            QMessageBox.critical(self, "Error", f"Failed to create new group: {e}")
            return False

    def create_new_command_in_group(self, group_title):
        return self.create_new_command_at_position(group_title, None)

    def create_new_command_at_position(self, group_title, after_recipe_name=None):
        dialog = EditRecipeDialog("New Command", "", self)
        if dialog.exec_() == QDialog.Accepted:
            name, prompt = dialog.get_data()
            if not name or not prompt:
                QMessageBox.warning(self, "Input Error", "Command name and prompt cannot be empty.")
                return False
                
            if not self.recipes_file:
                reply = QMessageBox.question(self, "No Recipes File", 
                    "No recipes file exists. Create new one with this command?",
                    QMessageBox.Yes | QMessageBox.No)
                if reply == QMessageBox.Yes:
                    self.recipes_file = os.path.join(BASE_PATH, "recipes.md")
                    with open(self.recipes_file, 'w', encoding='utf-8') as f:
                        f.write(f"# {group_title}\n**{name}**: {prompt}\n")
                    self.load_recipes_and_populate_list()
                    return True
                else:
                    return False

            self._backup_recipes_file("before_new_command")
            try:
                with open(self.recipes_file, 'r', encoding='utf-8') as f:
                    lines = f.readlines()
                
                # Find insertion point
                insert_at = len(lines)
                in_target_group = False
                found_after_recipe = after_recipe_name is None
                
                for i, line in enumerate(lines):
                    stripped = line.strip()
                    if stripped.startswith('#') and stripped[1:].strip() == group_title:
                        in_target_group = True
                    elif in_target_group and stripped.startswith('#'):
                        insert_at = i
                        break
                    elif in_target_group and stripped.startswith('**') and ':' in stripped:
                        if not found_after_recipe and after_recipe_name:
                            current_name = stripped.split(':', 1)[0].strip().strip('**').strip()
                            if current_name == after_recipe_name:
                                found_after_recipe = True
                                insert_at = i + 1  # Insert after this recipe
                        elif found_after_recipe:
                            insert_at = i  # Insert before next recipe
                            break
                        else:
                            insert_at = i + 1  # Default: insert after last recipe
                
                # Insert new command
                lines.insert(insert_at, f"**{name}**: {prompt}\n")
                
                with open(self.recipes_file, 'w', encoding='utf-8') as f:
                    f.writelines(lines)
                    
                self.load_recipes_and_populate_list()
                return True
                
            except Exception as e:
                logging.error(f"Error creating new command: {e}")
                QMessageBox.critical(self, "Error", f"Failed to create new command: {e}")
                return False

    def delete_group(self, group_title):
        # Get confirmation with warning about merging
        msg_box = QMessageBox(self)
        msg_box.setWindowTitle("Confirm Group Deletion")
        msg_box.setText(f"Delete group '{group_title}'?")
        msg_box.setInformativeText("All recipes in this group will be merged into the next group (or Basic Recipes if this is the last group).")
        msg_box.setStandardButtons(QMessageBox.Yes | QMessageBox.No)
        msg_box.setDefaultButton(QMessageBox.No)
        
        # Add checkbox for keeping group but moving recipes
        merge_only_checkbox = QCheckBox("Keep empty group (only move recipes)")
        msg_box.setCheckBox(merge_only_checkbox)
        
        if msg_box.exec_() != QMessageBox.Yes:
            return False

        if not self.recipes_file or not os.path.exists(self.recipes_file):
            QMessageBox.critical(self, "Error", "Recipes file not found.")
            return False

        self._backup_recipes_file("before_group_delete")
        try:
            with open(self.recipes_file, 'r', encoding='utf-8') as f:
                lines = f.readlines()

            # Find group and its recipes
            group_start = -1
            group_end = len(lines)
            next_group_start = len(lines)
            in_target_group = False
            recipes_in_group = []

            for i, line in enumerate(lines):
                stripped = line.strip()
                if stripped.startswith('#'):
                    if stripped[1:].strip() == group_title:
                        group_start = i
                        in_target_group = True
                    elif in_target_group:
                        next_group_start = i
                        break
                elif in_target_group and stripped.startswith('**') and ':' in stripped:
                    recipes_in_group.append(line)

            if group_start == -1:
                QMessageBox.critical(self, "Error", f"Group '{group_title}' not found.")
                return False

            # Remove group header unless checkbox is checked
            updated_lines = []
            for i, line in enumerate(lines):
                if i == group_start and not merge_only_checkbox.isChecked():
                    continue  # Skip the group header
                elif i > group_start and i < next_group_start and line.strip() and not line.strip().startswith('**'):
                    continue  # Skip empty lines between header and recipes
                elif i == next_group_start and recipes_in_group:
                    # Insert recipes before next group
                    updated_lines.extend(recipes_in_group)
                    updated_lines.append('\n')  # Add separator
                    updated_lines.append(line)
                else:
                    updated_lines.append(line)

            # If no next group, append to Basic Recipes group
            if next_group_start == len(lines) and recipes_in_group:
                # Find Basic Recipes group
                basic_recipes_pos = -1
                for i, line in enumerate(lines):
                    if line.strip().startswith('#') and line.strip()[1:].strip() == "Basic Recipes":
                        basic_recipes_pos = i
                        break
                
                if basic_recipes_pos != -1:
                    # Insert after Basic Recipes header
                    updated_lines.insert(basic_recipes_pos + 1, '\n'.join(recipes_in_group) + '\n')
                else:
                    # Just append to end
                    updated_lines.extend(recipes_in_group)

            with open(self.recipes_file, 'w', encoding='utf-8') as f:
                f.writelines(updated_lines)

            self.load_recipes_and_populate_list()
            QMessageBox.information(self, "Success", 
                f"Group '{group_title}' {'emptied' if merge_only_checkbox.isChecked() else 'deleted'}. Recipes merged successfully.")
            return True

        except Exception as e:
            logging.error(f"Error deleting group: {e}")
            QMessageBox.critical(self, "Error", f"Failed to delete group: {e}")
            return False

    def save_memory_content_change(self, memory_idx_to_save, new_html_content):
        if not (0 <= memory_idx_to_save < len(self._memory)): logging.warning(f"Invalid memory index for saving: {memory_idx_to_save}"); return
        captured_text, prompt, old_response_content, filename = self._memory[memory_idx_to_save]
        if new_html_content != old_response_content: 
            self._memory[memory_idx_to_save] = (captured_text, prompt, new_html_content, filename) # Store HTML if edited
            logging.debug(f"Memory entry {memory_idx_to_save} content updated with new HTML.")
            if self.permanent_memory and self.memory_dir and filename:
                file_path = os.path.join(self.memory_dir, filename)
                try:
                    disk_content = f"Captured Text:\n{captured_text}\n\nPrompt:\n{prompt}\n\nLLM Response:\n{new_html_content}"; 
                    with open(file_path, 'w', encoding='utf-8') as f: f.write(disk_content); logging.debug(f"Updated permanent memory file: {file_path} with new HTML.")
                except Exception as e: logging.error(f"Error saving updated memory to file {file_path}: {e}")

    def open_config_window(self):
        try:
            config_dialog = ConfigWindow(self) 
            if config_dialog.exec_(): 
                self.validate_and_load_config(); self.apply_theme(); self.load_recipes_and_populate_list() 
                self.results_container.setVisible(self.results_in_app)
                if not self.results_in_app and len(self.splitter_sizes) == 3: self.splitter.setSizes([self.splitter_sizes[0], self.splitter_sizes[1] + self.splitter_sizes[2], 0])
                else: self.splitter.setSizes(self.splitter_sizes) 
                self.append_mode_checkbox.setChecked(self.append_mode); self.on_input_mode_changed(self.input_mode_combo.currentText())
                self.start_hotkey_thread() # Restart hotkey thread
                logging.debug("Configuration applied after dialog save.")
            else: logging.debug("Config dialog cancelled.")
        except Exception as e: logging.error(f"Error in open_config_window or applying changes: {e}", exc_info=True); QMessageBox.critical(self, "Configuration Error", f"Failed to apply config changes:\n{e}")

    def open_recipes_file_externally(self): 
        try:
            recipes_path = self.recipes_file
            if not recipes_path or not os.path.exists(recipes_path): QMessageBox.warning(self, "File Not Found", f"Recipes file '{recipes_path or 'Not Set'}' not configured or does not exist."); return
            QDesktopServices.openUrl(QUrl.fromLocalFile(recipes_path)); logging.debug(f"Attempted to open recipes file: {recipes_path}")
        except Exception as e: logging.error(f"Could not open recipes file: {e}"); QMessageBox.critical(self, "Error", f"Could not open recipes file: {e}")

    def show_about(self):
        about_path_abs = os.path.join(BASE_PATH, ABOUT_FILE) if not os.path.isabs(ABOUT_FILE) else ABOUT_FILE
        if not os.path.exists(about_path_abs): QMessageBox.warning(self, "File Not Found", f"{ABOUT_FILE} not found at {about_path_abs}."); return
        try: QDesktopServices.openUrl(QUrl.fromLocalFile(about_path_abs))
        except Exception as e: QMessageBox.critical(self, "Error", f"Could not open {ABOUT_FILE}: {e}")

    def closeEvent(self, event):
        try:
            if self.results_in_app and self.active_memory_index is not None: self.save_memory_content_change(self.active_memory_index, self.results_textedit.toHtml())
            for window in self.result_windows[:]: window.close() 
            if self.close_behavior == "Minimize to Tray":
                event.ignore(); self.hide()
                for window in self.result_windows[:]:
                    if window and window.isVisible(): window.hide()
                self.tray_icon.showMessage("CoDude", "CoDude is running in the background.", QSystemTrayIcon.Information, 2000)
            else: QApplication.instance().quit()
        except Exception as e: logging.error(f"Error in closeEvent: {e}"); event.accept() 

    def changeEvent(self, event): 
        try:
            if event.type() == QEvent.WindowStateChange and self.windowState() & Qt.WindowMinimized:
                if self.close_behavior == "Minimize to Tray": 
                    event.ignore(); self.hide()
                    for window in self.result_windows[:]:
                         if window and window.isVisible(): window.hide()
                    if not self._minimized_by_shortcut: self.tray_icon.showMessage("CoDude", "CoDude minimized to tray.", QSystemTrayIcon.Information, 1500)
                    self._minimized_by_shortcut = False; return 
            super().changeEvent(event)
        except Exception as e: logging.error(f"Error in changeEvent: {e}")

    def on_tray_icon_activated(self, reason):
        if reason == QSystemTrayIcon.Trigger: self.show_hide_window()

    def show_hide_window(self): 
        try:
            if self.isHidden():
                self.showNormal(); self.activateWindow(); self.raise_()
                for window in self.result_windows[:]:
                    if window and not window.isVisible() and not window.isMinimized(): window.showNormal(); window.activateWindow()
                self._minimized_by_shortcut = False
            else: 
                if self.results_in_app and self.active_memory_index is not None: self.save_memory_content_change(self.active_memory_index, self.results_textedit.toHtml())
                self.hide()
                for window in self.result_windows[:]:
                     if window and window.isVisible(): window.hide()
                self._minimized_by_shortcut = True 
            logging.debug("Window visibility toggled.")
        except Exception as e: logging.error(f"Error in show_hide_window: {e}")

    def update_captured_text_area(self, text): self.captured_text_edit.setText(text if text is not None else ""); logging.debug("Captured text updated in text area.")

    def export_results_to_markdown(self):
        if not self.results_in_app: QMessageBox.information(self, "Not Applicable", "Export from here is for In-App results."); return
        text_to_export = "";
        if self.active_memory_index is not None and 0 <= self.active_memory_index < len(self._memory): _, _, raw_llm_response, _ = self._memory[self.active_memory_index]; text_to_export = raw_llm_response
        else: text_to_export = self.results_textedit.toPlainText() 
        if not text_to_export.strip(): QMessageBox.information(self, "Nothing to Export", "Results area is empty."); return
        options = QFileDialog.Options(); file_path, _ = QFileDialog.getSaveFileName(self, "Save LLM Response", "", "Markdown Files (*.md);;Text Files (*.txt);;All Files (*)", options=options)
        if file_path:
            try:
                with open(file_path, 'w', encoding='utf-8') as f: f.write(text_to_export)
                QMessageBox.information(self, "Export Successful", f"Response saved to {file_path}")
            except Exception as e: QMessageBox.critical(self, "Export Error", f"Could not save file: {e}")

    def copy_results_to_clipboard(self):
        if not self.results_in_app: return
        QApplication.clipboard().setText(self.results_textedit.toHtml())
        QMessageBox.information(self, "Copy Successful", "HTML content from results area copied.")

    def save_append_mode_state(self): 
        if self.input_mode_combo.currentText() != "Chat Mode:":
            self.append_mode = self.append_mode_checkbox.isChecked(); self._save_partial_config({'append_mode': self.append_mode}); logging.debug(f"Append mode state saved: {self.append_mode}")

    def adjust_textarea_font(self, textarea_widget, delta):
        textarea_id = str(id(textarea_widget)); current_size_pt = self.textarea_font_sizes.get(textarea_id, self.font_size)
        new_size_pt = max(8, min(24, current_size_pt + delta))
        font = textarea_widget.font(); font.setPointSize(new_size_pt); textarea_widget.setFont(font)
        self.textarea_font_sizes[textarea_id] = new_size_pt; self._save_partial_config({'textarea_font_sizes': self.textarea_font_sizes})
        if textarea_widget == self.results_textedit and textarea_widget.toPlainText(): 
             current_html = textarea_widget.toHtml(); textarea_widget.setHtml(current_html)
        logging.debug(f"Adjusted font for textarea {textarea_id} to {new_size_pt}pt.")

    def load_permanent_memory_entries(self): 
        if not (self.permanent_memory and self.memory_dir and os.path.exists(self.memory_dir)): return
        logging.debug(f"Loading permanent memory from {self.memory_dir}"); self._memory.clear(); self.memory_list.clear()
        try:
            memory_files = sorted([os.path.join(self.memory_dir, f) for f in os.listdir(self.memory_dir) if f.endswith(".md")], key=os.path.getmtime )
            for file_path in memory_files:
                filename = os.path.basename(file_path)
                try:
                    with open(file_path, 'r', encoding='utf-8') as f: content = f.read()
                    cap_text_m = re.search(r"Captured Text:\n(.*?)\n\nPrompt:", content, re.DOTALL); prompt_m = re.search(r"Prompt:\n(.*?)\n\nLLM Response:", content, re.DOTALL); response_m = re.search(r"LLM Response:\n(.*)", content, re.DOTALL)
                    if cap_text_m and prompt_m and response_m:
                        cap_text, prompt, resp = cap_text_m.group(1).strip(), prompt_m.group(1).strip(), response_m.group(1).strip()
                        self._memory.append((cap_text, prompt, resp, filename)); item_txt = f"Prompt: {prompt[:25]}... Text: {cap_text[:25]}..."
                        entry_w = MemoryEntryWidget(item_txt, filename); list_i = QListWidgetItem(self.memory_list); list_i.setSizeHint(entry_w.sizeHint())
                        entry_w.delete_button.clicked.connect(partial(self.delete_memory_entry_from_button, list_i)); self.memory_list.setItemWidget(list_i, entry_w)
                    else: logging.warning(f"Could not parse memory file: {filename}. Skipping.")
                except Exception as e_file: logging.error(f"Error processing memory file {filename}: {e_file}")
            self.memory_list.scrollToBottom(); logging.debug(f"Loaded {len(self._memory)} entries from permanent memory.")
        except Exception as e: logging.error(f"General error loading permanent memory: {e}", exc_info=True)

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
