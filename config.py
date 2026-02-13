"""
Configuration module for CoDude application.
Handles all configuration file operations, ConfigWindow dialog, and config-related methods.
"""

import os
import sys
import json
import logging
from collections import deque
from urllib.parse import urlparse

from PyQt5.QtWidgets import (QDialog, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit, QPushButton, QComboBox, QCheckBox, 
                             QSpacerItem, QMessageBox, QSlider, QSizePolicy, QTextEdit)
from PyQt5.QtGui import QIntValidator
from PyQt5.QtCore import Qt, QTimer
import requests


# --- Base Path Detection ---
def get_base_path():
    """Get the base path for file operations, works for both dev and PyInstaller bundles."""
    if getattr(sys, 'frozen', False):
        base_path = os.path.dirname(sys.executable)
    else:
        base_path = os.path.dirname(os.path.abspath(__file__))
    base_path = os.path.normpath(base_path)
    return base_path


# --- Constants ---
BASE_PATH = get_base_path()
CONFIG_FILE = os.path.join(BASE_PATH, "config.json")
ABOUT_FILE = os.path.join(BASE_PATH, "Readme.md")
BACKUP_DIR = os.path.join(BASE_PATH, "backups")
APP_VERSION = "0.1.4"


# --- ConfigWindow Dialog ---
class ConfigWindow(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("CoDude Configuration")
        self.setMinimumWidth(450)
        self.main_app_ref = parent
        self.layout = QVBoxLayout(self)
        self.layout.setSpacing(5)
        self.layout.setContentsMargins(10, 10, 10, 10)
        
        def create_row_layout(*widgets_to_add):
            row = QHBoxLayout()
            row.setSpacing(5)
            for w in widgets_to_add:
                if isinstance(w, (QPushButton, QLineEdit, QComboBox, QCheckBox, QLabel)):
                    w.setFixedHeight(22)
                if isinstance(w, QSpacerItem):
                    row.addSpacerItem(w)
                else:
                    row.addWidget(w)
            return row
            
        def create_label(text):
            lbl = QLabel(text, self)
            lbl.setFixedHeight(22)
            return lbl

        # LLM Provider selection
        self.llm_provider_combo = QComboBox(self)
        self.llm_provider_combo.addItems(["Local OpenAI-Compatible", "OpenAI API", "LM Studio Native API"])
        self.llm_provider_combo.currentTextChanged.connect(self.update_llm_fields_visibility)
        self.llm_provider_combo.currentTextChanged.connect(lambda: QTimer.singleShot(100, self.fetch_available_models))
        self.layout.addLayout(create_row_layout(create_label("LLM Provider:"), self.llm_provider_combo))
        
        # Local LLM URL
        self.llm_url_label = create_label("LLM URL (Local):")
        self.llm_url_input = QLineEdit(self)
        self.llm_url_input.setPlaceholderText("e.g., http://localhost:1234")
        self.llm_url_input.textChanged.connect(lambda: QTimer.singleShot(500, self.fetch_available_models))
        self.llm_url_row = create_row_layout(self.llm_url_label, self.llm_url_input)
        self.layout.addLayout(self.llm_url_row)
        
        # Local API Token
        self.local_api_token_label = create_label("API Token (Optional):")
        self.local_api_token_input = QLineEdit(self)
        self.local_api_token_input.setEchoMode(QLineEdit.Password)
        self.local_api_token_input.setToolTip("Optional: Only needed if your local LLM server requires authentication")
        self.local_api_token_row = create_row_layout(self.local_api_token_label, self.local_api_token_input)
        self.layout.addLayout(self.local_api_token_row)
        
        # OpenAI API Key
        self.openai_api_key_label = create_label("OpenAI API Key:")
        self.openai_api_key_input = QLineEdit(self)
        self.openai_api_key_input.setEchoMode(QLineEdit.Password)
        self.openai_api_key_input.textChanged.connect(lambda: QTimer.singleShot(500, self.fetch_available_models))
        self.openai_key_row = create_row_layout(self.openai_api_key_label, self.openai_api_key_input)
        self.layout.addLayout(self.openai_key_row)
        
        # LM Studio URL
        self.lmstudio_url_label = create_label("LM Studio URL:")
        self.lmstudio_url_input = QLineEdit(self)
        self.lmstudio_url_input.setPlaceholderText("e.g., http://localhost:1234")
        self.lmstudio_url_input.textChanged.connect(lambda: QTimer.singleShot(500, self.fetch_available_models))
        self.lmstudio_url_row = create_row_layout(self.lmstudio_url_label, self.lmstudio_url_input)
        self.layout.addLayout(self.lmstudio_url_row)
        
        # LM Studio API Key
        self.lmstudio_api_key_label = create_label("LM Studio API Token:")
        self.lmstudio_api_key_input = QLineEdit(self)
        self.lmstudio_api_key_input.setEchoMode(QLineEdit.Password)
        self.lmstudio_api_key_input.setToolTip("Optional: Only needed if you have enabled API authentication in LM Studio settings")
        self.lmstudio_api_key_input.textChanged.connect(lambda: QTimer.singleShot(500, self.fetch_available_models))
        self.lmstudio_api_key_row = create_row_layout(self.lmstudio_api_key_label, self.lmstudio_api_key_input)
        self.layout.addLayout(self.lmstudio_api_key_row)
        
        # MCP Plugin IDs
        self.mcp_plugin_ids_label = create_label("MCP Plugin IDs:")
        self.mcp_plugin_ids_input = QLineEdit(self)
        self.mcp_plugin_ids_input.setPlaceholderText("e.g., web-search, filesystem")
        self.mcp_plugin_ids_input.setToolTip("Enter comma-separated MCP server IDs (not individual tool names). Example: web-search, filesystem")
        self.mcp_plugin_ids_row = create_row_layout(self.mcp_plugin_ids_label, self.mcp_plugin_ids_input)
        self.layout.addLayout(self.mcp_plugin_ids_row)
        
        # Require USETOOLS checkbox
        self.require_usetools_checkbox = QCheckBox("Require USETOOLS keyword for tools", self)
        self.require_usetools_checkbox.setToolTip("When enabled, tools will only be used for recipes that start with USETOOLS:")
        self.layout.addWidget(self.require_usetools_checkbox)
        
        # Model name
        self.model_name_combo = QComboBox(self)
        self.model_name_combo.setEditable(True)
        self.model_name_combo.setToolTip("Select or type a model name. The dropdown shows available models when the provider is accessible.")
        self.layout.addLayout(create_row_layout(create_label("LLM Model:"), self.model_name_combo))
        
        self.layout.addSpacerItem(QSpacerItem(20, 10, QSizePolicy.Minimum, QSizePolicy.Fixed))
        
        # Max recents and favorites
        self.max_recents_input = QLineEdit(self)
        self.max_recents_input.setValidator(QIntValidator(0, 100, self))
        self.layout.addLayout(create_row_layout(create_label("Max Recent Recipes:"), self.max_recents_input))
        
        self.max_favorites_input = QLineEdit(self)
        self.max_favorites_input.setValidator(QIntValidator(0, 100, self))
        self.layout.addLayout(create_row_layout(create_label("Max Favorite Recipes:"), self.max_favorites_input))
        
        self.layout.addSpacerItem(QSpacerItem(20, 10, QSizePolicy.Minimum, QSizePolicy.Fixed))
        
        # Hotkey Configuration
        self.layout.addWidget(create_label("Hotkey Configuration:"))
        self.ctrl_checkbox = QCheckBox("Ctrl", self)
        self.shift_checkbox = QCheckBox("Shift", self)
        self.alt_checkbox = QCheckBox("Alt", self)
        modifier_layout = create_row_layout(self.ctrl_checkbox, self.shift_checkbox, self.alt_checkbox, 
                                            QSpacerItem(40, 20, QSizePolicy.Expanding, QSizePolicy.Minimum))
        self.layout.addLayout(modifier_layout)
        
        self.main_key_input = QLineEdit(self)
        self.main_key_input.setMaxLength(1)
        self.layout.addLayout(create_row_layout(create_label("Main Hotkey Key:"), self.main_key_input))
        
        self.layout.addSpacerItem(QSpacerItem(20, 10, QSizePolicy.Minimum, QSizePolicy.Fixed))
        
        # Theme
        self.theme_combo = QComboBox(self)
        self.theme_combo.addItems(['Light', 'Dark'])
        self.layout.addLayout(create_row_layout(create_label("Theme:"), self.theme_combo))
        
        # Results Display
        self.results_display_combo = QComboBox(self)
        self.results_display_combo.addItems(['Separate Windows', 'In-App Textarea'])
        self.layout.addLayout(create_row_layout(create_label("Results Display:"), self.results_display_combo))
        
        # Font Size
        self.font_size_slider = QSlider(Qt.Horizontal, self)
        self.font_size_slider.setMinimum(8)
        self.font_size_slider.setMaximum(18)
        self.font_size_slider.setTickInterval(1)
        self.font_size_slider.setValue(10)
        self.font_size_label = QLabel("Font Size: 10pt")
        self.font_size_slider.valueChanged.connect(lambda v: self.font_size_label.setText(f"Font Size: {v}pt"))
        self.layout.addLayout(create_row_layout(self.font_size_label, self.font_size_slider))
        
        # Permanent Memory
        self.permanent_memory_checkbox = QCheckBox("Enable Permanent Memory", self)
        self.layout.addWidget(self.permanent_memory_checkbox)
        
        # Memory Directory
        self.memory_dir_input = QLineEdit(self)
        self.memory_dir_input.setPlaceholderText(f"e.g., {os.path.join(BASE_PATH, 'memory')}")
        self.layout.addLayout(create_row_layout(create_label("Memory Directory:"), self.memory_dir_input))
        
        # LLM Timeout
        self.timeout_input = QLineEdit(self)
        self.timeout_input.setValidator(QIntValidator(5, 300, self))
        self.layout.addLayout(create_row_layout(create_label("LLM Timeout (seconds):"), self.timeout_input))
        
        # Logging Level
        self.logging_combo = QComboBox(self)
        self.logging_combo.addItems(['Minimal', 'Normal', 'Debug'])
        self.layout.addLayout(create_row_layout(create_label("Logging Level:"), self.logging_combo))
        
        # Logging Output
        self.logging_output_combo = QComboBox(self)
        self.logging_output_combo.addItems(['Console', 'File', 'Both'])
        self.layout.addLayout(create_row_layout(create_label("Logging Output:"), self.logging_output_combo))
        
        # Close Behavior
        self.close_behavior_combo = QComboBox(self)
        self.close_behavior_combo.addItems(['Exit', 'Minimize to Tray'])
        self.layout.addLayout(create_row_layout(create_label("Close Behavior:"), self.close_behavior_combo))
        
        # Recipes File
        self.recipes_file_input = QLineEdit(self)
        self.recipes_file_input.setPlaceholderText(f"e.g., {os.path.join(BASE_PATH, 'recipes.md')}")
        self.layout.addLayout(create_row_layout(create_label("Recipes File:"), self.recipes_file_input))
        
        self.layout.addSpacerItem(QSpacerItem(20, 10, QSizePolicy.Minimum, QSizePolicy.Expanding))
        
        # Buttons
        button_layout = QHBoxLayout()
        self.ok_button = QPushButton("OK", self)
        self.ok_button.clicked.connect(self.save_config_values)
        button_layout.addWidget(self.ok_button)
        
        self.cancel_button = QPushButton("Cancel", self)
        self.cancel_button.clicked.connect(self.reject)
        button_layout.addWidget(self.cancel_button)
        
        self.layout.addLayout(button_layout)
        
        # Version label at the bottom
        version_label = QLabel(f"CoDude v{APP_VERSION}", self)
        version_font = version_label.font()
        version_font.setPointSize(version_font.pointSize() - 2)
        version_label.setFont(version_font)
        version_label.setStyleSheet("color: gray;")
        version_label.setAlignment(Qt.AlignCenter)
        self.layout.addWidget(version_label)
        
        # Load config values into dialog
        self.load_config_values()
    
    def update_llm_fields_visibility(self):
        """Show/hide LLM-specific fields based on provider selection."""
        provider = self.llm_provider_combo.currentText()
        is_local = provider == "Local OpenAI-Compatible"
        is_openai = provider == "OpenAI API"
        is_lmstudio = provider == "LM Studio Native API"
        
        self.llm_url_label.setVisible(is_local)
        self.llm_url_input.setVisible(is_local)
        self.local_api_token_label.setVisible(is_local)
        self.local_api_token_input.setVisible(is_local)
        
        self.openai_api_key_label.setVisible(is_openai)
        self.openai_api_key_input.setVisible(is_openai)
        
        self.lmstudio_url_label.setVisible(is_lmstudio)
        self.lmstudio_url_input.setVisible(is_lmstudio)
        self.lmstudio_api_key_label.setVisible(is_lmstudio)
        self.lmstudio_api_key_input.setVisible(is_lmstudio)
    
    def fetch_available_models(self):
        """Fetch available models from configured LLM provider."""
        try:
            provider = self.llm_provider_combo.currentText()
            current_model = self.model_name_combo.currentText()
            models = []
            headers = {"User-Agent": "CoDude"}
            
            logging.debug(f"fetch_available_models called for provider: {provider}")
            
            if provider == "OpenAI API":
                api_key = self.openai_api_key_input.text().strip()
                if not api_key:
                    logging.debug("OpenAI API key empty, skipping fetch")
                    return
                headers["Authorization"] = f"Bearer {api_key}"
                try:
                    logging.debug("Fetching models from OpenAI API...")
                    response = requests.get("https://api.openai.com/v1/models", headers=headers, timeout=5)
                    if response.status_code == 200:
                        data = response.json()
                        models = [m['id'] for m in data.get('data', []) if m.get('id')]
                        models = [m for m in models if 'gpt' in m.lower() or 'chat' in m.lower()]
                        logging.debug(f"Fetched {len(models)} OpenAI models: {models[:3]}...")
                    else:
                        logging.warning(f"OpenAI API returned status {response.status_code}")
                except Exception as e:
                    logging.warning(f"Failed to fetch OpenAI models: {e}")
                    
            elif provider == "Local OpenAI-Compatible":
                url = self.llm_url_input.text().strip()
                if not url:
                    logging.debug("Local LLM URL empty, skipping fetch")
                    return
                parsed_url = urlparse(url)
                base_url = f"{parsed_url.scheme}://{parsed_url.netloc}" if parsed_url.netloc else url.rstrip('/')
                try:
                    logging.debug(f"Fetching models from local LLM at {base_url}/v1/models...")
                    response = requests.get(f"{base_url}/v1/models", headers=headers, timeout=5)
                    if response.status_code == 200:
                        data = response.json()
                        models = [m['id'] for m in data.get('data', []) if m.get('id')]
                        logging.debug(f"Fetched {len(models)} local models: {models}")
                    else:
                        logging.warning(f"Local LLM API returned status {response.status_code}")
                except requests.exceptions.ConnectionError as e:
                    logging.warning(f"Could not connect to local LLM at {base_url}: {e}")
                except Exception as e:
                    logging.warning(f"Failed to fetch local models: {e}")
                    
            elif provider == "LM Studio Native API":
                url = self.lmstudio_url_input.text().strip()
                if not url:
                    logging.debug("LM Studio URL empty, skipping fetch")
                    return
                
                # Normalize URL - remove trailing slash and /api/v1 if present
                url = url.rstrip('/')
                if url.endswith('/api/v1'):
                    url = url[:-7]
                
                api_key = self.lmstudio_api_key_input.text().strip()
                if api_key:
                    headers["Authorization"] = f"Bearer {api_key}"
                
                # Try multiple possible endpoints for LM Studio
                endpoints_to_try = [
                    f"{url}/api/v1/models",       # LM Studio correct endpoint
                    f"{url}/v1/models",           # Fallback: Standard OpenAI-compatible
                    f"{url}/api/models",          # Fallback: Alternative
                ]
                
                loaded_model = None  # Track the currently loaded model
                
                for endpoint in endpoints_to_try:
                    try:
                        logging.debug(f"Trying LM Studio endpoint: {endpoint}")
                        response = requests.get(endpoint, headers=headers, timeout=5)
                        logging.debug(f"Response status: {response.status_code}, content length: {len(response.text)}")
                        
                        if response.status_code == 200:
                            data = response.json()
                            logging.debug(f"Response data keys: {list(data.keys()) if isinstance(data, dict) else 'list'}")
                            
                            # Handle different response formats
                            if isinstance(data, dict):
                                if 'data' in data:
                                    # Check for loaded model info
                                    if isinstance(data.get('data'), list) and len(data['data']) > 0:
                                        first_item = data['data'][0]
                                        if isinstance(first_item, dict):
                                            loaded_model = first_item.get('id') or first_item.get('model')
                                    models = [m['id'] if isinstance(m, dict) else m for m in data.get('data', []) if m]
                                elif 'models' in data:
                                    # Check for loaded/active model in list
                                    models_list = data.get('models', [])
                                    if models_list and isinstance(models_list[0], dict) and models_list[0].get('loaded'):
                                        loaded_model = models_list[0]['id']
                                    models = [m['id'] if isinstance(m, dict) else m for m in models_list if m]
                                else:
                                    models = []
                            elif isinstance(data, list):
                                models = [m['id'] if isinstance(m, dict) else m for m in data if m]
                            else:
                                models = []
                            
                            if models:
                                logging.debug(f"Successfully fetched {len(models)} LM Studio models from {endpoint}: {models}")
                                if loaded_model:
                                    logging.debug(f"Loaded model detected: {loaded_model}")
                                break
                            else:
                                logging.debug(f"No models found in response from {endpoint}")
                    except requests.exceptions.ConnectionError as e:
                        logging.debug(f"Connection failed for {endpoint}: {e}")
                    except requests.exceptions.Timeout:
                        logging.debug(f"Timeout for {endpoint}")
                    except Exception as e:
                        logging.debug(f"Error with {endpoint}: {e}")
                
                if not models:
                    logging.warning(f"Could not fetch models from any LM Studio endpoint")
                elif loaded_model:
                    # If we found a loaded model, use it as the current_model
                    current_model = loaded_model
            
            if models:
                self.model_name_combo.blockSignals(True)
                self.model_name_combo.clear()
                if current_model and current_model in models:
                    models.remove(current_model)
                    self.model_name_combo.addItem(current_model)
                self.model_name_combo.addItems(sorted(models))
                if current_model and self.model_name_combo.findText(current_model) == -1:
                    self.model_name_combo.setCurrentText(current_model)
                self.model_name_combo.blockSignals(False)
                logging.debug(f"Updated model combo with {len(models)} models")
            else:
                logging.debug("No models fetched")
        except Exception as e:
            logging.error(f"Error in fetch_available_models: {e}", exc_info=True)
    
    def load_config_values(self):
        """Load configuration values from config.json into the dialog."""
        try:
            config = {}
            if os.path.exists(CONFIG_FILE):
                with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
                    config = json.load(f)
            
            self.llm_provider_combo.setCurrentText(config.get("llm_provider", "Local OpenAI-Compatible"))
            self.llm_url_input.setText(config.get("llm_url", "http://127.0.0.1:1234"))
            self.local_api_token_input.setText(config.get("local_api_token", ""))
            self.openai_api_key_input.setText(config.get("openai_api_key", ""))
            self.lmstudio_url_input.setText(config.get("lmstudio_url", "http://127.0.0.1:1234"))
            self.lmstudio_api_key_input.setText(config.get("lmstudio_api_key", ""))
            self.mcp_plugin_ids_input.setText(config.get("mcp_plugin_ids", ""))
            self.require_usetools_checkbox.setChecked(config.get("require_usetools_for_tools", False))
            
            saved_model = config.get("llm_model_name", "gpt-3.5-turbo")
            self.model_name_combo.setCurrentText(saved_model)
            QTimer.singleShot(100, self.fetch_available_models)
            
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
            
            self.update_llm_fields_visibility()
            logging.debug("Config loaded successfully in ConfigWindow")
        except Exception as e:
            logging.error(f"Error loading config file in ConfigWindow: {e}")
            QMessageBox.warning(self, "Config Load Error", f"Could not load configuration: {e}")
    
    def save_config_values(self):
        """Save configuration values from dialog to config.json."""
        try:
            llm_provider_val = self.llm_provider_combo.currentText()
            llm_url_val = self.llm_url_input.text().strip()
            lmstudio_url_val = self.lmstudio_url_input.text().strip()
            
            if llm_provider_val == "Local OpenAI-Compatible" and not llm_url_val:
                reply = QMessageBox.question(self, "LLM URL Not Set", "Use default 'http://127.0.0.1:1234'?",
                                            QMessageBox.Yes | QMessageBox.No | QMessageBox.Cancel)
                if reply == QMessageBox.Yes:
                    llm_url_val = "http://127.0.0.1:1234"
                elif reply == QMessageBox.Cancel:
                    return
            
            if llm_provider_val == "LM Studio Native API" and not lmstudio_url_val:
                reply = QMessageBox.question(self, "LM Studio URL Not Set", "Use default 'http://127.0.0.1:1234'?",
                                            QMessageBox.Yes | QMessageBox.No | QMessageBox.Cancel)
                if reply == QMessageBox.Yes:
                    lmstudio_url_val = "http://127.0.0.1:1234"
                elif reply == QMessageBox.Cancel:
                    return
            
            permanent_memory_checked = self.permanent_memory_checkbox.isChecked()
            memory_dir_val = self.memory_dir_input.text().strip()
            if permanent_memory_checked and not memory_dir_val:
                default_mem_dir = os.path.join(BASE_PATH, "memory")
                reply = QMessageBox.question(self, "Memory Directory", f"Use default '{default_mem_dir}'?",
                                            QMessageBox.Yes | QMessageBox.No | QMessageBox.Cancel)
                if reply == QMessageBox.Yes:
                    memory_dir_val = default_mem_dir
                    os.makedirs(memory_dir_val, exist_ok=True)
                    self.memory_dir_input.setText(memory_dir_val)
                elif reply == QMessageBox.Cancel:
                    return
            
            mcp_value = self.mcp_plugin_ids_input.text().strip()
            local_token = self.local_api_token_input.text().strip()
            logging.debug(f"Saving mcp_plugin_ids from input field: '{mcp_value}'")
            logging.debug(f"Saving local_api_token: '{local_token}'")
            
            config_data = {
                "llm_provider": llm_provider_val,
                "llm_url": llm_url_val,
                "openai_api_key": self.openai_api_key_input.text(),
                "local_api_token": local_token,
                "lmstudio_url": lmstudio_url_val,
                "lmstudio_api_key": self.lmstudio_api_key_input.text(),
                "mcp_plugin_ids": mcp_value,
                "require_usetools_for_tools": self.require_usetools_checkbox.isChecked(),
                "llm_model_name": self.model_name_combo.currentText().strip() or "gpt-3.5-turbo",
                "max_recents": int(self.max_recents_input.text() or 5),
                "max_favorites": int(self.max_favorites_input.text() or 5),
                "recipes_file": self.recipes_file_input.text().strip(),
                "hotkey": {
                    "ctrl": self.ctrl_checkbox.isChecked(),
                    "shift": self.shift_checkbox.isChecked(),
                    "alt": self.alt_checkbox.isChecked(),
                    "main_key": self.main_key_input.text().strip().lower() or "c"
                },
                "logging_level": self.logging_combo.currentText(),
                "logging_output": self.logging_output_combo.currentText(),
                "theme": self.theme_combo.currentText(),
                "results_display": self.results_display_combo.currentText(),
                "font_size": self.font_size_slider.value(),
                "permanent_memory": permanent_memory_checked,
                "memory_dir": memory_dir_val,
                "llm_timeout": int(self.timeout_input.text() or 60),
                "close_behavior": self.close_behavior_combo.currentText(),
                "group_states": getattr(self.main_app_ref, "_group_states", {}),
                "append_mode": getattr(self.main_app_ref, "append_mode", False),
                "textarea_font_sizes": getattr(self.main_app_ref, "textarea_font_sizes", {}),
                "splitter_sizes": getattr(self.main_app_ref, "splitter_sizes", [250, 350, 300]),
                "recently_used_recipes": list(getattr(self.main_app_ref, "recently_used_recipes", deque())),
                "favorite_recipes": getattr(self.main_app_ref, "favorite_recipes", [])
            }
            
            with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
                json.dump(config_data, f, indent=4)
                f.flush()
                os.fsync(f.fileno())
            
            QMessageBox.information(self, "Config Saved", "Configuration saved successfully.")
            logging.debug("Config saved successfully")
            self.accept()
        except ValueError as ve:
            logging.error(f"Invalid input: {ve}")
            QMessageBox.critical(self, "Input Error", f"Invalid numeric value: {ve}")
        except Exception as e:
            logging.error(f"Could not save config: {e}")
            QMessageBox.critical(self, "Save Error", f"Could not save config: {e}")