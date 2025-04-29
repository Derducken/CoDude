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
                             QShortcut, QSlider, QSizePolicy, QSpacerItem, QSplitter)
from PyQt5.QtGui import QIcon, QKeySequence, QFont
from PyQt5.QtCore import Qt, QThread, pyqtSignal, QTimer, QEvent

CONFIG_FILE = "config.json"
ABOUT_FILE = "About.md"
LOG_FILE = "codude.log"

# Initialize logging
def setup_logging(level='Normal'):
    levels = {
        'Minimal': logging.ERROR,
        'Normal': logging.WARNING,
        'Extended': logging.INFO,
        'Everything': logging.DEBUG
    }
    try:
        logging.basicConfig(
            filename=LOG_FILE,
            level=levels.get(level, logging.WARNING),
            format='%(asctime)s - %(levelname)s - %(message)s',
            filemode='a',
            encoding='utf-8',
            force=True
        )
        # Create log file if it doesn't exist
        if not os.path.exists(LOG_FILE):
            with open(LOG_FILE, 'a', encoding='utf-8') as f:
                f.write("")
        os.chmod(LOG_FILE, 0o666)
        logging.debug("Logging initialized with level: %s", level)
    except Exception as e:
        print(f"Error setting up logging: {e}")

# Signal for updating the GUI from the hotkey listener thread
class HotkeySignal(QThread):
    text_captured = pyqtSignal(str)
    show_window = pyqtSignal()

    def __init__(self, hotkey_string):
        QThread.__init__(self)
        self.hotkey_string = hotkey_string
        logging.debug("HotkeySignal thread initialized with hotkey: %s", hotkey_string)

    def run(self):
        try:
            import keyboard
            logging.debug("Hotkey listener thread started")
            while True:
                keyboard.wait(self.hotkey_string)
                logging.info("Hotkey %s activated!", self.hotkey_string)
                keyboard.press_and_release('ctrl+c')
                time.sleep(0.1)
                try:
                    clipboard_text = QApplication.clipboard().text()
                    if clipboard_text is None:
                        clipboard_text = ""
                        logging.warning("Clipboard returned None, setting empty text")
                except Exception as e:
                    clipboard_text = ""
                    logging.error("Failed to access clipboard: %s", e)
                logging.debug("Captured text: %s", clipboard_text[:50])
                self.text_captured.emit(clipboard_text)
                self.show_window.emit()
        except Exception as e:
            logging.error("Hotkey listener error: %s", e)

# Thread for sending request to LLM
class LLMRequestThread(QThread):
    response_received = pyqtSignal(str)
    error_occurred = pyqtSignal(str)

    def __init__(self, llm_url, prompt, text):
        QThread.__init__(self)
        self.llm_url = llm_url
        self.prompt = prompt
        self.text = text

    def run(self):
        try:
            messages = [
                {"role": "system", "content": "You are a helpful assistant."},
                {"role": "user", "content": f"{self.prompt}\n\nText: {self.text}"}
            ]
            payload = {
                "model": "gpt-3.5-turbo",
                "messages": messages
            }
            headers = {
                "Content-Type": "application/json"
            }

            if not self.llm_url:
                self.error_occurred.emit("LLM URL is not configured.")
                return

            response = requests.post(f"{self.llm_url}/v1/chat/completions", json=payload, headers=headers)
            response.raise_for_status()

            result = response.json()
            if result and result.get('choices'):
                llm_reply = result['choices'][0]['message']['content']
                self.response_received.emit(llm_reply)
            else:
                self.error_occurred.emit("Invalid response from LLM.")
        except requests.exceptions.RequestException as e:
            self.error_occurred.emit(f"Error communicating with LLM: {e}")
        except Exception as e:
            self.error_occurred.emit(f"An unexpected error occurred: {e}")

# Window to display LLM results
class ResultWindow(QMainWindow):
    def __init__(self, response_text, parent=None, memory_index=None):
        super().__init__(parent)
        self.setWindowTitle("CoDude Result")
        self.setGeometry(200, 200, 600, 400)
        self.parent = parent
        self.memory_index = memory_index

        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        layout = QVBoxLayout(central_widget)

        self.response_textedit = QTextEdit(self)
        self.response_textedit.setText(response_text)
        self.response_textedit.textChanged.connect(self.on_text_changed)
        layout.addWidget(self.response_textedit)

        button_layout = QHBoxLayout()

        self.export_button = QPushButton("Export to Markdown", self)
        self.export_button.setToolTip("Save the LLM response to a markdown file.")
        self.export_button.clicked.connect(self.export_to_markdown)
        button_layout.addWidget(self.export_button)

        self.copy_button = QPushButton("Copy to Clipboard", self)
        self.copy_button.setToolTip("Copy the LLM response to the clipboard.")
        self.copy_button.clicked.connect(self.copy_to_clipboard)
        button_layout.addWidget(self.copy_button)

        layout.addLayout(button_layout)

    def on_text_changed(self):
        if self.memory_index is not None and self.parent:
            self.parent.save_textarea_changes(self.memory_index, self.response_textedit.toPlainText())

    def focusOutEvent(self, event):
        self.on_text_changed()
        super().focusOutEvent(event)

    def closeEvent(self, event):
        self.on_text_changed()
        super().closeEvent(event)

    def export_to_markdown(self):
        options = QFileDialog.Options()
        file_path, _ = QFileDialog.getSaveFileName(self, "Save LLM Response", "", "Markdown Files (*.md);;All Files (*)", options=options)
        if file_path:
            try:
                with open(file_path, 'w', encoding='utf-8') as f:
                    f.write(self.response_textedit.toPlainText())
                QMessageBox.information(self, "Export Successful", f"Response saved to {file_path}")
            except Exception as e:
                QMessageBox.critical(self, "Export Error", f"Could not save file: {e}")

    def copy_to_clipboard(self):
        clipboard = QApplication.clipboard()
        clipboard.setText(self.response_textedit.toPlainText())
        QMessageBox.information(self, "Copy Successful", "Response copied to clipboard.")

# Custom widget for Memory entries with hover-activated Delete button
class MemoryEntryWidget(QWidget):
    def __init__(self, text, filename=None, parent=None):
        super().__init__(parent)
        self.filename = filename
        self.layout = QHBoxLayout(self)
        self.layout.setContentsMargins(0, 0, 0, 0)
        self.layout.setSpacing(5)
        
        self.label = QLabel(text, self)
        self.label.setWordWrap(True)
        self.layout.addWidget(self.label, 1)
        
        self.delete_button = QPushButton("Delete", self)
        self.delete_button.setFixedWidth(60)
        self.delete_button.setVisible(False)
        self.layout.addWidget(self.delete_button)
        
        self.setMouseTracking(True)

    def enterEvent(self, event):
        self.delete_button.setVisible(True)
        super().enterEvent(event)

    def leaveEvent(self, event):
        self.delete_button.setVisible(False)
        super().leaveEvent(event)

# Configuration Window
class ConfigWindow(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("CoDude Configuration")
        self.setGeometry(300, 300, 400, 400)

        # Main layout
        self.layout = QVBoxLayout(self)
        self.layout.setSpacing(3)
        self.layout.setContentsMargins(5, 5, 5, 5)

        # 1. LLM URL
        llm_url_layout = QHBoxLayout()
        llm_url_label = QLabel("LLM URL:", self)
        llm_url_label.setFixedHeight(20)
        llm_url_layout.addWidget(llm_url_label)
        self.llm_url_input = QLineEdit(self)
        self.llm_url_input.setFixedHeight(20)
        self.llm_url_input.setPlaceholderText("Enter LLM API URL (e.g., http://localhost:8000)")
        llm_url_layout.addWidget(self.llm_url_input)
        self.standardize_layout(llm_url_layout)
        self.layout.addLayout(llm_url_layout)

        # 2. Spacer
        self.layout.addSpacerItem(QSpacerItem(20, 3, QSizePolicy.Minimum, QSizePolicy.Fixed))

        # 3. Hotkey Configuration
        hotkey_label = QLabel("Hotkey:", self)
        hotkey_label.setFixedHeight(20)
        self.layout.addWidget(hotkey_label)

        # Modifier Keys
        modifier_layout = QHBoxLayout()
        self.ctrl_checkbox = QCheckBox("Ctrl", self)
        self.ctrl_checkbox.setFixedHeight(20)
        modifier_layout.addWidget(self.ctrl_checkbox)
        self.shift_checkbox = QCheckBox("Shift", self)
        self.shift_checkbox.setFixedHeight(20)
        modifier_layout.addWidget(self.shift_checkbox)
        self.alt_checkbox = QCheckBox("Alt", self)
        self.alt_checkbox.setFixedHeight(20)
        modifier_layout.addWidget(self.alt_checkbox)
        self.standardize_layout(modifier_layout)
        self.layout.addLayout(modifier_layout)

        # Main Key
        main_key_layout = QHBoxLayout()
        main_key_label = QLabel("Main Key:", self)
        main_key_label.setFixedHeight(20)
        main_key_layout.addWidget(main_key_label)
        self.main_key_input = QLineEdit(self)
        self.main_key_input.setMaxLength(1)
        self.main_key_input.setFixedHeight(20)
        self.main_key_input.setMaximumHeight(20)
        main_key_layout.addWidget(self.main_key_input)
        self.standardize_layout(main_key_layout)
        self.layout.addLayout(main_key_layout)

        # 4. Thematic Spacer (10px before Theme)
        self.layout.addSpacerItem(QSpacerItem(20, 10, QSizePolicy.Minimum, QSizePolicy.Fixed))

        # 5. Theme Selection
        theme_layout = QHBoxLayout()
        theme_label = QLabel("Theme:", self)
        theme_label.setFixedHeight(20)
        theme_layout.addWidget(theme_label)
        self.theme_combo = QComboBox(self)
        self.theme_combo.setFixedHeight(20)
        self.theme_combo.addItems(['Light', 'Dark'])
        theme_layout.addWidget(self.theme_combo)
        self.standardize_layout(theme_layout)
        self.layout.addLayout(theme_layout)

        # 6. Results Display Mode
        results_display_layout = QHBoxLayout()
        results_display_label = QLabel("Results Display:", self)
        results_display_label.setFixedHeight(20)
        results_display_layout.addWidget(results_display_label)
        self.results_display_combo = QComboBox(self)
        self.results_display_combo.setFixedHeight(20)
        self.results_display_combo.addItems(['Separate Windows', 'In-App Textarea'])
        results_display_layout.addWidget(self.results_display_combo)
        self.standardize_layout(results_display_layout)
        self.layout.addLayout(results_display_layout)

        # 7. Font Size Slider
        font_size_layout = QHBoxLayout()
        font_size_label = QLabel("Global Font Size:", self)
        font_size_label.setFixedHeight(20)
        font_size_layout.addWidget(font_size_label)
        self.font_size_slider = QSlider(Qt.Horizontal, self)
        self.font_size_slider.setFixedHeight(20)
        self.font_size_slider.setMinimum(8)
        self.font_size_slider.setMaximum(18)
        self.font_size_slider.setTickInterval(1)
        self.font_size_slider.setValue(10)
        font_size_layout.addWidget(self.font_size_slider)
        self.standardize_layout(font_size_layout)
        self.layout.addLayout(font_size_layout)

        # 8. Thematic Spacer (10px before Recipes File)
        self.layout.addSpacerItem(QSpacerItem(20, 10, QSizePolicy.Minimum, QSizePolicy.Fixed))

        # 9. Recipes File Path
        recipes_file_layout = QHBoxLayout()
        recipes_file_label = QLabel("Recipes File:", self)
        recipes_file_label.setFixedHeight(20)
        recipes_file_layout.addWidget(recipes_file_label)
        self.recipes_file_input = QLineEdit(self)
        self.recipes_file_input.setFixedHeight(20)
        self.recipes_file_input.setReadOnly(True)
        recipes_file_layout.addWidget(self.recipes_file_input)
        browse_button = QPushButton("Browse", self)
        browse_button.setFixedHeight(20)
        browse_button.clicked.connect(self.browse_recipes_file)
        recipes_file_layout.addWidget(browse_button)
        self.standardize_layout(recipes_file_layout)
        self.layout.addLayout(recipes_file_layout)

        # 10. Permanent Memory Toggle
        self.permanent_memory_checkbox = QCheckBox("Permanent Memory", self)
        self.permanent_memory_checkbox.setFixedHeight(20)
        self.layout.addWidget(self.permanent_memory_checkbox)

        # 11. Memory Directory
        memory_dir_layout = QHBoxLayout()
        memory_dir_label = QLabel("Memory Directory:", self)
        memory_dir_label.setFixedHeight(20)
        memory_dir_layout.addWidget(memory_dir_label)
        self.memory_dir_input = QLineEdit(self)
        self.memory_dir_input.setFixedHeight(20)
        self.memory_dir_input.setReadOnly(True)
        memory_dir_layout.addWidget(self.memory_dir_input)
        browse_memory_button = QPushButton("Browse", self)
        browse_memory_button.setFixedHeight(20)
        browse_memory_button.clicked.connect(self.browse_memory_dir)
        memory_dir_layout.addWidget(browse_memory_button)
        self.standardize_layout(memory_dir_layout)
        self.layout.addLayout(memory_dir_layout)

        # 12. Thematic Spacer (10px before Logging Level)
        self.layout.addSpacerItem(QSpacerItem(20, 10, QSizePolicy.Minimum, QSizePolicy.Fixed))

        # 13. Logging Level
        logging_layout = QHBoxLayout()
        logging_label = QLabel("Logging Level:", self)
        logging_label.setFixedHeight(20)
        logging_layout.addWidget(logging_label)
        self.logging_combo = QComboBox(self)
        self.logging_combo.setFixedHeight(20)
        self.logging_combo.addItems(['Minimal', 'Normal', 'Extended', 'Everything'])
        logging_layout.addWidget(self.logging_combo)
        self.standardize_layout(logging_layout)
        self.layout.addLayout(logging_layout)

        # 14. Thematic Spacer (10px before Buttons)
        self.layout.addSpacerItem(QSpacerItem(20, 10, QSizePolicy.Minimum, QSizePolicy.Fixed))

        # 15. Buttons
        button_layout = QHBoxLayout()
        save_button = QPushButton("Save", self)
        save_button.setFixedHeight(20)
        save_button.clicked.connect(self.save_config)
        button_layout.addWidget(save_button)
        cancel_button = QPushButton("Cancel", self)
        cancel_button.setFixedHeight(20)
        cancel_button.clicked.connect(self.reject)
        button_layout.addWidget(cancel_button)
        self.standardize_layout(button_layout)
        self.layout.addLayout(button_layout)

        self.load_config()

        # Debug spacing
        logging.debug("Main Key label geometry: %s", main_key_label.geometry().getRect())
        logging.debug("Main Key input geometry: %s", self.main_key_input.geometry().getRect())
        logging.debug("Hotkey label geometry: %s", hotkey_label.geometry().getRect())

    def standardize_layout(self, layout):
        """Apply consistent spacing and margins to layouts and their widgets."""
        layout.setSpacing(3)
        layout.setContentsMargins(0, 0, 0, 0)
        for i in range(layout.count()):
            item = layout.itemAt(i)
            if item.widget():
                widget = item.widget()
                widget.setStyleSheet("margin: 0; padding: 0;")
                if isinstance(widget, (QLabel, QLineEdit, QComboBox, QCheckBox, QPushButton, QSlider)):
                    widget.setFixedHeight(20)
            elif item.layout():
                self.standardize_layout(item.layout())

    def browse_recipes_file(self):
        options = QFileDialog.Options()
        file_path, _ = QFileDialog.getOpenFileName(self, "Select Recipes File", "", "Markdown Files (*.md);;All Files (*)", options=options)
        if file_path:
            self.recipes_file_input.setText(file_path)

    def browse_memory_dir(self):
        options = QFileDialog.Options()
        directory = QFileDialog.getExistingDirectory(self, "Select Memory Directory", options=options)
        if directory:
            self.memory_dir_input.setText(directory)

    def load_config(self):
        try:
            if os.path.exists(CONFIG_FILE):
                with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
                    config = json.load(f)
                    self.llm_url_input.setText(config.get("llm_url", ""))
                    self.recipes_file_input.setText(config.get("recipes_file", ""))
                    hotkey = config.get("hotkey", {})
                    self.ctrl_checkbox.setChecked(hotkey.get("ctrl", False))
                    self.shift_checkbox.setChecked(hotkey.get("shift", False))
                    self.alt_checkbox.setChecked(hotkey.get("alt", False))
                    self.main_key_input.setText(hotkey.get("main_key", ""))
                    self.logging_combo.setCurrentText(config.get("logging_level", "Normal"))
                    self.theme_combo.setCurrentText(config.get("theme", "Light"))
                    self.results_display_combo.setCurrentText(config.get("results_display", "Separate Windows"))
                    self.font_size_slider.setValue(config.get("font_size", 10))
                    self.permanent_memory_checkbox.setChecked(config.get("permanent_memory", False))
                    self.memory_dir_input.setText(config.get("memory_dir", ""))
            logging.debug("Config loaded successfully in ConfigWindow")
        except Exception as e:
            logging.error("Error loading config file in ConfigWindow: %s", e)

    def save_config(self):
        try:
            config = {
                "llm_url": self.llm_url_input.text(),
                "recipes_file": self.recipes_file_input.text(),
                "hotkey": {
                    "ctrl": self.ctrl_checkbox.isChecked(),
                    "shift": self.shift_checkbox.isChecked(),
                    "alt": self.alt_checkbox.isChecked(),
                    "main_key": self.main_key_input.text()
                },
                "logging_level": self.logging_combo.currentText(),
                "theme": self.theme_combo.currentText(),
                "group_states": getattr(self.parent(), "_group_states", {}),
                "results_display": self.results_display_combo.currentText(),
                "font_size": self.font_size_slider.value(),
                "permanent_memory": self.permanent_memory_checkbox.isChecked(),
                "memory_dir": self.memory_dir_input.text(),
                "append_mode": getattr(self.parent(), "append_mode", False),
                "textarea_font_sizes": getattr(self.parent(), "textarea_font_sizes", {})
            }
            with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
                json.dump(config, f, indent=4)
            QMessageBox.information(self, "Config Saved", "Configuration saved successfully.")
            logging.debug("Config saved successfully")
            self.accept()
        except Exception as e:
            logging.error("Could not save config file: %s", e)
            QMessageBox.critical(self, "Save Error", f"Could not save config file: {e}")

class CoDudeApp(QMainWindow):
    def __init__(self):
        super().__init__()
        logging.info("Starting CoDudeApp initialization")
        self.setWindowTitle("CoDude")
        self.setGeometry(100, 100, 800, 800)
        self.setMaximumHeight(1000)
        self.setWindowFlags(Qt.Window | Qt.WindowStaysOnTopHint)

        # Initialize attributes with defaults
        self._group_states = {}
        self._memory = []
        self._group_buttons = {}
        self._recipe_buttons = []
        self.result_windows = []
        self.textarea_font_sizes = {}
        self.results_in_app = False
        self.append_mode = False
        self.font_size = 10
        self.permanent_memory = False
        self.memory_dir = ""
        self.llm_url = ""
        self.recipes_file = ""
        self._theme = "Light"
        self.active_memory_index = None
        self._deleting_memory = False
        logging.debug("Initialized group states, memory, group buttons, result windows, and config defaults")

        # Load configuration early
        self.validate_and_load_config()
        logging.debug("Configuration loaded")

        # Central widget and main layout
        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        main_layout = QVBoxLayout(central_widget)
        logging.debug("Created central widget and main layout")

        # Menu bar
        menubar = QMenuBar(self)
        self.setMenuBar(menubar)
        codude_menu = menubar.addMenu("CoDude")
        
        configure_action = QAction("Configure", self)
        configure_action.triggered.connect(self.open_config_window)
        codude_menu.addAction(configure_action)
        
        open_recipes_action = QAction("Open Recipes.md", self)
        open_recipes_action.triggered.connect(self.open_recipes_file)
        codude_menu.addAction(open_recipes_action)
        
        about_action = QAction("About", self)
        about_action.triggered.connect(self.show_about)
        codude_menu.addAction(about_action)
        logging.debug("Menu bar setup complete")

        # Content layout with QSplitter
        self.splitter = QSplitter(Qt.Horizontal)
        main_layout.addWidget(self.splitter)

        # Left column: Recipes
        left_widget = QWidget()
        self.left_layout = QVBoxLayout(left_widget)
        self.left_layout.setContentsMargins(0, 0, 0, 0)
        
        # Search bar
        search_layout = QHBoxLayout()
        search_label = QLabel("Search Recipes:", self)
        search_layout.addWidget(search_label)
        self.search_input = QLineEdit(self)
        self.search_input.setPlaceholderText("Enter recipe name or prompt")
        self.search_input.textChanged.connect(self.filter_recipes)
        search_layout.addWidget(self.search_input)
        self.left_layout.addLayout(search_layout)

        # Scroll area for recipe buttons
        scroll_area = QScrollArea()
        scroll_area.setWidgetResizable(True)
        self.scroll_widget = QWidget()
        self.recipe_buttons_layout = QVBoxLayout(self.scroll_widget)
        self.recipe_buttons_layout.setAlignment(Qt.AlignTop)
        self.recipe_buttons_layout.setContentsMargins(0, 0, 0, 0)
        scroll_area.setWidget(self.scroll_widget)
        self.left_layout.addWidget(scroll_area)

        # Custom Input Area
        custom_input_label = QLabel("Custom Input:", self)
        self.left_layout.addWidget(custom_input_label)
        self.custom_input_textedit = QTextEdit(self)
        self.custom_input_textedit.setToolTip("Enter custom instructions here (press Ctrl+Enter to send).")
        self.custom_input_textedit.setMaximumHeight(100)
        self.left_layout.addWidget(self.custom_input_textedit)

        # Font size controls for custom input
        custom_font_layout = QHBoxLayout()
        custom_font_layout.addStretch()
        custom_font_up = QPushButton("↑", self)
        custom_font_up.setFixedSize(30, 30)
        custom_font_up.clicked.connect(lambda: self.adjust_textarea_font(self.custom_input_textedit, 1))
        custom_font_layout.addWidget(custom_font_up)
        custom_font_down = QPushButton("↓", self)
        custom_font_down.setFixedSize(30, 30)
        custom_font_down.clicked.connect(lambda: self.adjust_textarea_font(self.custom_input_textedit, -1))
        custom_font_layout.addWidget(custom_font_down)
        self.left_layout.addLayout(custom_font_layout)

        # Send Custom Command Button
        send_custom_button = QPushButton("Send Custom Command", self)
        send_custom_button.setToolTip("Send the custom command to the LLM.")
        send_custom_button.clicked.connect(self.send_custom_command)
        self.left_layout.addWidget(send_custom_button)

        # Debug Font Sizes Button
        debug_font_button = QPushButton("Debug Font Sizes", self)
        debug_font_button.clicked.connect(self.debug_font_sizes)
        self.left_layout.addWidget(debug_font_button)

        self.splitter.addWidget(left_widget)
        logging.debug("Left column (recipes and custom input) setup complete")

        # Middle column: Captured Text and Memory
        tabs_widget = QWidget()
        tabs_layout = QVBoxLayout(tabs_widget)
        right_tabs = QTabWidget(self)

        # Captured Text Tab
        captured_widget = QWidget()
        captured_layout = QVBoxLayout(captured_widget)
        captured_text_label = QLabel("Captured Text:", self)
        captured_layout.addWidget(captured_text_label)
        self.captured_text_edit = QTextEdit(self)
        self.captured_text_edit.setToolTip("This area shows the text captured by the hotkey. You can edit it before processing.")
        captured_layout.addWidget(self.captured_text_edit, 1)
        # Font size controls for captured text
        captured_font_layout = QHBoxLayout()
        captured_font_layout.addStretch()
        captured_font_up = QPushButton("↑", self)
        captured_font_up.setFixedSize(30, 30)
        captured_font_up.clicked.connect(lambda: self.adjust_textarea_font(self.captured_text_edit, 1))
        captured_font_layout.addWidget(captured_font_up)
        captured_font_down = QPushButton("↓", self)
        captured_font_down.setFixedSize(30, 30)
        captured_font_down.clicked.connect(lambda: self.adjust_textarea_font(self.captured_text_edit, -1))
        captured_font_layout.addWidget(captured_font_down)
        captured_layout.addLayout(captured_font_layout)
        right_tabs.addTab(captured_widget, "Captured Text")

        # Memory Tab
        memory_widget = QWidget()
        memory_layout = QVBoxLayout(memory_widget)
        memory_label = QLabel("CoDude's Memory:", self)
        memory_layout.addWidget(memory_label)
        self.memory_list = QListWidget(self)
        self.memory_list.setToolTip("Double-click an entry to view the full response. Hover to delete.")
        self.memory_list.itemDoubleClicked.connect(self.show_memory_entry)
        memory_layout.addWidget(self.memory_list, 1)
        right_tabs.addTab(memory_widget, "Memory")

        tabs_layout.addWidget(right_tabs, 1)
        self.splitter.addWidget(tabs_widget)
        logging.debug("Middle column (captured text/memory) setup complete")

        # Right column: Results
        self.results_container = QWidget()
        results_layout = QVBoxLayout(self.results_container)
        results_label = QLabel("LLM Results:", self)
        results_layout.addWidget(results_label)
        self.results_textedit = QTextEdit(self)
        self.results_textedit.setToolTip("LLM responses are displayed here when in-app results are enabled.")
        self.results_textedit.textChanged.connect(self.on_results_text_changed)
        results_layout.addWidget(self.results_textedit, 1)
        # Font size controls for results
        results_font_layout = QHBoxLayout()
        results_font_layout.addStretch()
        results_font_up = QPushButton("↑", self)
        results_font_up.setFixedSize(30, 30)
        results_font_up.clicked.connect(lambda: self.adjust_textarea_font(self.results_textedit, 1))
        results_font_layout.addWidget(results_font_up)
        results_font_down = QPushButton("↓", self)
        results_font_down.setFixedSize(30, 30)
        results_font_down.clicked.connect(lambda: self.adjust_textarea_font(self.results_textedit, -1))
        results_font_layout.addWidget(results_font_down)
        results_layout.addLayout(results_font_layout)
        # Append Mode Toggle and Buttons
        results_controls_layout = QHBoxLayout()
        self.append_mode_checkbox = QCheckBox("Append Mode", self)
        self.append_mode_checkbox.stateChanged.connect(self.save_append_mode)
        results_controls_layout.addWidget(self.append_mode_checkbox)
        export_results_button = QPushButton("Export to Markdown", self)
        export_results_button.clicked.connect(self.export_results_to_markdown)
        results_controls_layout.addWidget(export_results_button)
        copy_results_button = QPushButton("Copy to Clipboard", self)
        copy_results_button.clicked.connect(self.copy_results_to_clipboard)
        results_controls_layout.addWidget(copy_results_button)
        results_layout.addLayout(results_controls_layout)
        self.splitter.addWidget(self.results_container)
        self.results_container.setVisible(self.results_in_app)
        logging.debug("Right column (LLM results) setup complete")

        # Set initial splitter sizes
        self.splitter.setSizes([300, 200, 300])
        self.splitter.splitterMoved.connect(self.log_splitter_sizes)
        logging.debug("Splitter initialized with sizes [300, 200, 300]")

        # Status bar with progress bar
        self.status_bar = self.statusBar()
        self.progress_bar = QProgressBar(self)
        self.progress_bar.setMaximumWidth(200)
        self.progress_bar.setVisible(False)
        self.status_bar.addPermanentWidget(self.progress_bar)
        logging.debug("Status bar and progress bar setup complete")

        # System Tray Icon
        self.tray_icon = QSystemTrayIcon(self)
        self.tray_icon.setIcon(QIcon('text-analytics.png'))
        self.tray_icon.setToolTip("CoDude")

        # Tray Menu
        tray_menu = QMenu()
        show_action = QAction("Show", self)
        show_action.triggered.connect(self.show_window)
        tray_menu.addAction(show_action)

        hide_action = QAction("Hide", self)
        hide_action.triggered.connect(self.hide)
        tray_menu.addAction(hide_action)

        exit_action = QAction("Exit", self)
        exit_action.triggered.connect(QApplication.instance().quit)
        tray_menu.addAction(exit_action)

        self.tray_icon.setContextMenu(tray_menu)
        self.tray_icon.activated.connect(self.on_tray_icon_activated)
        logging.debug("System tray icon setup complete")

        # Ctrl+Enter shortcut
        self.custom_command_shortcut = QShortcut(QKeySequence("Ctrl+Return"), self)
        self.custom_command_shortcut.activated.connect(self.send_custom_command)
        logging.debug("Ctrl+Enter shortcut setup complete")

        # Load recipes and apply theme
        self.load_recipes()
        self.apply_theme()
        self.captured_text_edit.setFont(QFont("Arial", self.font_size))
        self.results_textedit.setFont(QFont("Arial", self.font_size))
        self.custom_input_textedit.setFont(QFont("Arial", self.font_size))
        self.append_mode_checkbox.setChecked(self.append_mode)
        logging.debug("Recipes and theme loaded")

        if self.permanent_memory and self.memory_dir and os.path.exists(self.memory_dir):
            self.load_permanent_memory()

        self.tray_icon.show()
        logging.info("Tray icon shown")

        # Start hotkey thread after UI initialization
        QTimer.singleShot(2000, self.start_hotkey_thread)
        logging.info("CoDudeApp initialization complete")

    def log_splitter_sizes(self, pos, index):
        sizes = self.splitter.sizes()
        logging.debug("Splitter sizes updated: Recipes=%d, Tabs=%d, Results=%d", sizes[0], sizes[1], sizes[2])

    def start_hotkey_thread(self):
        try:
            hotkey_string = self.load_hotkey_config()
            self.hotkey_thread = HotkeySignal(hotkey_string)
            self.hotkey_thread.text_captured.connect(self.update_captured_text)
            self.hotkey_thread.show_window.connect(self.show_window)
            self.hotkey_thread.start()
            logging.info("Hotkey thread started with %s", hotkey_string)
        except Exception as e:
            logging.error("Error starting hotkey thread: %s", e)

    def load_hotkey_config(self):
        default_hotkey = 'ctrl+alt+c'
        try:
            if os.path.exists(CONFIG_FILE):
                with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
                    config = json.load(f)
                    hotkey = config.get("hotkey", {})
                    ctrl = hotkey.get("ctrl", False)
                    shift = hotkey.get("shift", False)
                    alt = hotkey.get("alt", False)
                    main_key = hotkey.get("main_key", "c").lower()

                    modifiers = []
                    if ctrl:
                        modifiers.append("ctrl")
                    if shift:
                        modifiers.append("shift")
                    if alt:
                        modifiers.append("alt")

                    if not main_key or len(main_key) != 1:
                        logging.warning("Invalid main key '%s', using default hotkey", main_key)
                        return default_hotkey

                    valid_keys = set('abcdefghijklmnopqrstuvwxyz0123456789`')
                    if main_key not in valid_keys:
                        logging.warning("Main key '%s' not supported, using default hotkey", main_key)
                        return default_hotkey

                    hotkey_string = '+'.join(modifiers + [main_key])
                    logging.debug("Loaded hotkey: %s", hotkey_string)
                    return hotkey_string
            return default_hotkey
        except Exception as e:
            logging.error("Error loading hotkey config: %s", e)
            return default_hotkey

    def validate_and_load_config(self):
        default_config = {
            "llm_url": "",
            "recipes_file": "",
            "hotkey": {"ctrl": True, "shift": False, "alt": True, "main_key": "c"},
            "logging_level": "Normal",
            "theme": "Light",
            "group_states": {},
            "results_display": "Separate Windows",
            "font_size": 10,
            "permanent_memory": False,
            "memory_dir": "",
            "append_mode": False,
            "textarea_font_sizes": {}
        }
        try:
            logging.debug("Validating and loading config")
            if not os.path.exists(CONFIG_FILE):
                logging.warning("Config file not found, creating default")
                with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
                    json.dump(default_config, f, indent=4)
            with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
                config = json.load(f)
                config['llm_url'] = config.get('llm_url', "")
                config['recipes_file'] = config.get('recipes_file', "")
                config['hotkey'] = config.get('hotkey', default_config['hotkey'])
                if not isinstance(config['hotkey'], dict) or 'main_key' not in config['hotkey']:
                    logging.warning("Invalid hotkey configuration, using default")
                    config['hotkey'] = default_config['hotkey']
                if config['recipes_file'] and not os.path.exists(config['recipes_file']):
                    logging.warning("Recipes file not found: %s", config['recipes_file'])
                    config['recipes_file'] = ""
                config['logging_level'] = config.get('logging_level', 'Normal')
                if config['logging_level'] not in ['Minimal', 'Normal', 'Extended', 'Everything']:
                    config['logging_level'] = 'Normal'
                config['theme'] = config.get('theme', 'Light')
                if config['theme'] not in ['Light', 'Dark']:
                    config['theme'] = 'Light'
                self.llm_url = config['llm_url']
                self.recipes_file = config['recipes_file']
                self._group_states = config.get('group_states', {})
                self.results_in_app = config.get('results_display', 'Separate Windows') == 'In-App Textarea'
                self.font_size = config.get('font_size', 10)
                self.permanent_memory = config.get('permanent_memory', False)
                self.memory_dir = config.get('memory_dir', '')
                self.append_mode = config.get('append_mode', False)
                self.textarea_font_sizes = config.get('textarea_font_sizes', {})
                setup_logging(config['logging_level'])
                self._theme = config['theme']
            logging.debug("Config loaded successfully")
        except Exception as e:
            logging.error("Config validation failed: %s", e)
            QMessageBox.warning(self, "Config Error", "Invalid config file. Using defaults.")
            self.llm_url = ""
            self.recipes_file = ""
            self._theme = "Light"
            self.results_in_app = False
            self.font_size = 10
            self.permanent_memory = False
            self.memory_dir = ""
            self.append_mode = False
            self.textarea_font_sizes = {}
            setup_logging('Normal')
            with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
                json.dump(default_config, f, indent=4)

    def apply_theme(self):
        try:
            logging.debug("Applying theme: %s", self._theme)
            app = QApplication.instance()
            central_widget = self.centralWidget()
            font = QFont("Arial", self.font_size)
            app.setFont(font)
            for textarea in [self.captured_text_edit, self.results_textedit, self.custom_input_textedit]:
                textarea_id = str(id(textarea))
                size = self.textarea_font_sizes.get(textarea_id, self.font_size)
                textarea.setStyleSheet(f"font-size: {size}pt;")
                logging.debug("Initialized textarea %s with font size %d", textarea_id, size)
            if self._theme == 'Dark':
                stylesheet = """
                    QMainWindow, QWidget { background-color: #2b2b2b; color: #ffffff; }
                    QTextEdit { background-color: #3c3f41; color: #ffffff; border: 1px solid #555555; }
                    QLineEdit { background-color: #3c3f41; color: #ffffff; border: 1px solid #555555; max-height: 20px; }
                    QComboBox { background-color: #3c3f41; color: #ffffff; border: 1px solid #555555; max-height: 20px; }
                    QPushButton { background-color: #4a4a4a; color: #ffffff; border: 1px solid #555555; padding: 5px 10px; text-align: left; max-height: 20px; }
                    QPushButton:hover { background-color: #5a5a5a; }
                    QPushButton#groupButton { background-color: #e0e0e0; color: #333333; font-weight: bold; text-align: left; padding: 5px 10px; }
                    QTabWidget::pane { border: 1px solid #555555; background: #2b2b2b; }
                    QTabBar::tab { background: #3c3f41; color: #ffffff; padding: 5px; }
                    QTabBar::tab:selected { background: #4a4a4a; }
                    QScrollArea { background-color: #2b2b2b; border: none; }
                    QScrollBar:vertical { background: #3c3f41; width: 10px; }
                    QScrollBar::handle:vertical { background: #5a5a5a; }
                    QMenuBar { background-color: #2b2b2b; color: #ffffff; }
                    QMenu { background-color: #3c3f41; color: #ffffff; }
                    QMenu::item:selected { background-color: #5a5a5a; }
                    QLabel { color: #ffffff; margin: 0; padding: 0; max-height: 20px; }
                    QCheckBox { color: #ffffff; margin: 0; padding: 0; max-height: 20px; }
                    QProgressBar { background-color: #3c3f41; color: #ffffff; border: 1px solid #555555; }
                    QDialog, QDialog QLabel, QDialog QHBoxLayout, QDialog QVBoxLayout { margin: 0; padding: 0; }
                    QSplitter::handle { background: #555555; width: 5px; }
                    QSplitter::handle:hover { background: #777777; }
                """
                stylesheet += f" * {{ font-size: {self.font_size}pt; }}"
            else:
                stylesheet = f"""
                    QPushButton {{ text-align: left; padding: 5px 10px; max-height: 20px; }}
                    QPushButton#groupButton {{ background-color: #e0e0e0; color: #333333; font-weight: bold; text-align: left; padding: 5px 10px; }}
                    QLabel {{ margin: 0; padding: 0; max-height: 20px; }}
                    QLineEdit {{ margin: 0; padding: 0; max-height: 20px; }}
                    QCheckBox {{ margin: 0; padding: 0; max-height: 20px; }}
                    QComboBox {{ margin: 0; padding: 0; max-height: 20px; }}
                    QDialog, QDialog QLabel, QDialog QHBoxLayout, QDialog QVBoxLayout {{ margin: 0; padding: 0; }}
                    QSplitter::handle {{ background: #cccccc; width: 5px; }}
                    QSplitter::handle:hover {{ background: #aaaaaa; }}
                    * {{ font-size: {self.font_size}pt; }}
                """
            app.setStyleSheet(stylesheet)
            if central_widget:
                central_widget.setStyleSheet(stylesheet)
            self.repaint()
            QApplication.processEvents()
            QTimer.singleShot(100, self.log_widget_sizes)
        except Exception as e:
            logging.error("Error applying theme: %s", e)

    def log_widget_sizes(self):
        try:
            sizes = self.splitter.sizes()
            logging.debug("Column widths: Recipes=%d, Tabs=%d, Results=%d", sizes[0], sizes[1], sizes[2])
            hotkey_label = self.findChild(QLabel, "Hotkey:")
            main_key_label = self.findChild(QLabel, "Main Key:")
            logging.debug("ConfigWindow Hotkey label geometry: %s", hotkey_label.geometry().getRect() if hotkey_label else "N/A")
            logging.debug("ConfigWindow Main Key label geometry: %s", main_key_label.geometry().getRect() if main_key_label else "N/A")
        except Exception as e:
            logging.error("Error logging widget sizes: %s", e)

    def load_recipes(self):
        logging.info("Loading recipes from: %s", self.recipes_file)
        while self.recipe_buttons_layout.count():
            item = self.recipe_buttons_layout.takeAt(0)
            if item.widget():
                widget = item.widget()
                widget.setParent(None)
                widget.deleteLater()
            elif item.layout():
                layout = item.layout()
                while layout.count():
                    sub_item = layout.takeAt(0)
                    if sub_item.widget():
                        sub_widget = sub_item.widget()
                        sub_widget.setParent(None)
                        sub_widget.deleteLater()
                layout.setParent(None)
                layout.deleteLater()
        self._group_buttons.clear()
        self._recipe_buttons = []

        if not self.recipes_file or not os.path.exists(self.recipes_file):
            logging.warning("Recipes file not found or not specified: %s", self.recipes_file)
            error_label = QLabel("No recipes file configured. Please set a valid recipes.md in Configure.")
            error_label.setStyleSheet("color: red;")
            self.recipe_buttons_layout.addWidget(error_label)
            self.recipe_buttons_layout.addStretch()
            return

        try:
            with open(self.recipes_file, 'r', encoding='utf-8') as f:
                lines = f.readlines()
            logging.debug("Recipes file read successfully")
        except Exception as e:
            logging.error("Error reading recipes file: %s", e)
            error_label = QLabel(f"Error reading recipes file: {e}")
            error_label.setStyleSheet("color: red;")
            self.recipe_buttons_layout.addWidget(error_label)
            self.recipe_buttons_layout.addStretch()
            return

        group_layout = None
        group_container = None
        group_title = None

        for line in lines:
            line = line.strip()
            if not line:
                continue

            if line.startswith('#'):
                group_title = line.lstrip('#').strip()
                group_button = QPushButton(f"{group_title} ▼")
                group_button.setObjectName("groupButton")
                self.recipe_buttons_layout.addWidget(group_button)
                group_container = QWidget()
                group_container.setProperty("group_title", group_title)
                group_layout = QVBoxLayout(group_container)
                group_layout.setContentsMargins(10, 0, 0, 0)
                group_layout.setSpacing(2)
                self.recipe_buttons_layout.addWidget(group_container)
                self._group_buttons[group_title] = (group_button, group_container)
                is_expanded = self._group_states.get(group_title, True)
                group_container.setVisible(is_expanded)
                group_button.setText(f"{group_title} {'▼' if is_expanded else '▶'}")
                group_button.clicked.connect(lambda checked, gc=group_container, gt=group_title: self.toggle_group(gc, gt))
                logging.debug("Created group button for %s, expanded: %s", group_title, is_expanded)
            elif line.startswith('**') and ':' in line:
                try:
                    parts = line.split(':', 1)
                    if len(parts) != 2:
                        logging.warning("Skipping malformed recipe: %s", line)
                        continue
                    button_name = parts[0].strip().strip('*').strip()
                    prompt = parts[1].strip()
                    if not button_name or not prompt:
                        logging.warning("Skipping empty recipe: %s", line)
                        continue

                    button = QPushButton(button_name, self)
                    button.setToolTip(prompt)
                    button.clicked.connect(lambda checked, p=prompt, b=button: self.execute_recipe(p, b))
                    self._recipe_buttons.append((button, prompt, group_container))
                    logging.debug("Added recipe button: %s in group: %s", button_name, group_title if group_container else "None")

                    if group_layout:
                        group_layout.addWidget(button)
                        logging.debug("Button %s added to group_container layout", button_name)
                    else:
                        self.recipe_buttons_layout.addWidget(button)
                        logging.debug("Button %s added to main recipe_buttons_layout (no group)", button_name)
                except Exception as e:
                    logging.error("Error processing recipe '%s': %s", line, e)
                    continue

        if self.recipe_buttons_layout.count() == 0:
            logging.warning("No valid recipes found in file")
            no_recipes_label = QLabel("No valid recipes found in recipes.md")
            no_recipes_label.setStyleSheet("color: orange;")
            self.recipe_buttons_layout.addWidget(no_recipes_label)

        self.recipe_buttons_layout.addStretch()
        self.scroll_widget.setLayout(self.recipe_buttons_layout)
        self.scroll_widget.update()
        self.centralWidget().update()
        QApplication.processEvents()
        logging.info("Recipes loaded successfully")
        # Log group hierarchy
        for title, (button, container) in self._group_buttons.items():
            layout = container.layout()
            children = [layout.itemAt(i).widget() for i in range(layout.count()) if layout.itemAt(i).widget()]
            logging.debug("Group %s contains %d widgets: %s", title, len(children), [w.text() for w in children if w])

    def toggle_group(self, container, title):
        try:
            logging.debug("Toggling group: %s, current visibility: %s", title, container.isVisible())
            is_visible = not container.isVisible()
            container.setVisible(is_visible)
            self._group_states[title] = is_visible
            group_button, _ = self._group_buttons.get(title, (None, None))
            if group_button:
                group_button.setText(f"{title} {'▼' if is_visible else '▶'}")
            with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
                config = json.load(f)
            config['group_states'] = self._group_states
            with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
                json.dump(config, f, indent=4)
            container.repaint()
            container.update()
            self.recipe_buttons_layout.update()
            self.scroll_widget.update()
            logging.debug("Group %s set to visible: %s, config updated", title, is_visible)
        except Exception as e:
            logging.error("Error toggling group %s: %s", title, e)

    def filter_recipes(self, query):
        try:
            query = query.lower()
            for button, prompt, group_container in self._recipe_buttons:
                matches = query in button.text().lower() or query in prompt.lower()
                button.setVisible(matches)
                if group_container:
                    group_title = group_container.property("group_title")
                    group_has_visible = any(b.isVisible() for b, _, gc in self._recipe_buttons if gc == group_container)
                    is_expanded = self._group_states.get(group_title, True)
                    group_container.setVisible(group_has_visible and is_expanded)
                    logging.debug("Group %s visibility: %s (has_visible: %s, expanded: %s)", 
                                 group_title, group_container.isVisible(), group_has_visible, is_expanded)
            logging.debug("Recipe filtering complete")
        except Exception as e:
            logging.error("Error in filter_recipes: %s", e)
            QMessageBox.critical(self, "Error", f"Failed to filter recipes: {e}")

    def send_custom_command(self):
        try:
            custom_prompt = self.custom_input_textedit.toPlainText().strip()
            if not custom_prompt:
                logging.info("No custom command entered")
                QMessageBox.information(self, "No Command", "Please enter a custom command in the input area.")
                return
            self.execute_recipe(custom_prompt)
            logging.debug("Custom command sent")
        except Exception as e:
            logging.error("Error in send_custom_command: %s", e)
            QMessageBox.critical(self, "Error", f"Failed to send custom command: {e}")

    def execute_recipe(self, prompt, button=None):
        try:
            captured_text = self.captured_text_edit.toPlainText()
            if not captured_text:
                logging.info("No text captured to process")
                QMessageBox.information(self, "No Text", "Please capture some text first using the hotkey.")
                return

            if not self.llm_url:
                logging.warning("LLM URL is not configured")
                QMessageBox.warning(self, "LLM URL Missing", "LLM URL is not configured. Please go to Configure.")
                return

            logging.info("Executing recipe: %s with text: %s", prompt[:50], captured_text[:50])

            if button:
                button.setStyleSheet("background-color: #90EE90;")
                QTimer.singleShot(500, lambda: button.setStyleSheet(""))
            self.progress_bar.setVisible(True)
            self.progress_bar.setRange(0, 0)

            self.llm_thread = LLMRequestThread(self.llm_url, prompt, captured_text)
            self.llm_thread.response_received.connect(lambda resp: self.handle_llm_response(resp, captured_text, prompt))
            self.llm_thread.error_occurred.connect(self.handle_llm_error)
            self.llm_thread.start()
            logging.debug("LLM thread started")
        except Exception as e:
            logging.error("Error in execute_recipe: %s", e)
            QMessageBox.critical(self, "Error", f"Failed to execute recipe: {e}")

    def handle_llm_response(self, response_text, captured_text, prompt):
        try:
            logging.info("LLM Response Received")
            self.progress_bar.setVisible(False)
            filename = None
            if self.permanent_memory and self.memory_dir and os.path.exists(self.memory_dir):
                safe_prompt = "".join(c for c in prompt[:50] if c.isalnum() or c in " -_").strip()
                if not safe_prompt:
                    safe_prompt = "memory_entry"
                timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                filename = f"{safe_prompt}_{timestamp}.md"
                file_path = os.path.join(self.memory_dir, filename)
                with open(file_path, 'w', encoding='utf-8') as f:
                    f.write(f"{captured_text}\n\n{prompt}\n\n{response_text}")
                logging.debug("Saved memory entry to %s", file_path)
            
            if self.results_in_app:
                if self.append_mode:
                    current_text = self.results_textedit.toPlainText()
                    if current_text:
                        current_text += "\n\n"
                    self.results_textedit.setPlainText(current_text + response_text)
                else:
                    self.results_textedit.setPlainText(response_text)
                self.active_memory_index = len(self._memory)
            else:
                result_window = ResultWindow(response_text, self, len(self._memory))
                result_window.show()
                self.result_windows.append(result_window)
                result_window.destroyed.connect(lambda: self.result_windows.remove(result_window))
            
            self._memory.append((captured_text, prompt, response_text, filename))
            item_text = f"{prompt[:30]}... on {captured_text[:30]}..."
            entry_widget = MemoryEntryWidget(item_text, filename)
            list_item = QListWidgetItem(self.memory_list)
            list_item.setSizeHint(entry_widget.sizeHint())
            self.memory_list.setItemWidget(list_item, entry_widget)
            entry_widget.delete_button.clicked.connect(lambda: self.delete_memory_entry(list_item))
            logging.debug("Added response to memory")
        except Exception as e:
            logging.error("Error in handle_llm_response: %s", e)
            QMessageBox.critical(self, "Error", f"Failed to handle LLM response: {e}")

    def handle_llm_error(self, error_message):
        try:
            logging.error("LLM Error: %s", error_message)
            self.progress_bar.setVisible(False)
            QMessageBox.critical(self, "LLM Error", error_message)
        except Exception as e:
            logging.error("Error in handle_llm_error: %s", e)
            QMessageBox.critical(self, "Error", f"Failed to handle LLM error: {e}")

    def show_memory_entry(self, item):
        try:
            index = self.memory_list.row(item)
            captured_text, prompt, response, filename = self._memory[index]
            if self.results_in_app:
                if self.append_mode:
                    current_text = self.results_textedit.toPlainText()
                    if current_text:
                        current_text += "\n\n"
                    self.results_textedit.setPlainText(current_text + response)
                else:
                    self.results_textedit.setPlainText(response)
                self.active_memory_index = index
            else:
                result_window = ResultWindow(response, self, index)
                result_window.show()
                self.result_windows.append(result_window)
                result_window.destroyed.connect(lambda: self.result_windows.remove(result_window))
            logging.debug("Memory entry shown, index: %d", index)
        except Exception as e:
            logging.error("Error in show_memory_entry: %s", e)
            QMessageBox.critical(self, "Error", f"Failed to show memory entry: {e}")

    def delete_memory_entry(self, item):
        try:
            if self._deleting_memory:
                logging.debug("Skipping deletion, already in progress")
                return
            self._deleting_memory = True

            reply = QMessageBox.question(self, "Confirm Deletion", "Are you sure you want to delete this memory entry?",
                                        QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
            if reply != QMessageBox.Yes:
                self._deleting_memory = False
                return

            index = self.memory_list.row(item)
            if index < 0 or index >= len(self._memory):
                logging.error("Invalid memory index: %d", index)
                self._deleting_memory = False
                return

            logging.debug("Memory list before deletion: %d entries", self.memory_list.count())
            captured_text, prompt, response, filename = self._memory[index]

            entry_widget = self.memory_list.itemWidget(item)
            if entry_widget:
                entry_widget.delete_button.disconnect()

            self.memory_list.takeItem(index)
            self._memory.pop(index)

            if self.permanent_memory and self.memory_dir and filename and os.path.exists(self.memory_dir):
                file_path = os.path.join(self.memory_dir, filename)
                if os.path.exists(file_path):
                    os.remove(file_path)
                    logging.debug("Deleted memory file: %s", file_path)

            if self.active_memory_index == index:
                self.active_memory_index = None
            elif self.active_memory_index and self.active_memory_index > index:
                self.active_memory_index -= 1

            logging.debug("Memory list after deletion: %d entries", self.memory_list.count())
            self._deleting_memory = False
        except Exception as e:
            logging.error("Error deleting memory entry: %s", e)
            QMessageBox.critical(self, "Error", f"Failed to delete memory entry: {e}")
            self._deleting_memory = False

    def on_results_text_changed(self):
        if self.active_memory_index is not None:
            self.save_textarea_changes(self.active_memory_index, self.results_textedit.toPlainText())

    def focusOutEvent(self, event):
        if event.gotFocus():
            return
        if self.active_memory_index is not None:
            self.save_textarea_changes(self.active_memory_index, self.results_textedit.toPlainText())
        super().focusOutEvent(event)

    def save_textarea_changes(self, memory_index, new_text):
        try:
            if memory_index < 0 or memory_index >= len(self._memory):
                logging.warning("Invalid memory index for saving: %d", memory_index)
                return
            captured_text, prompt, old_response, filename = self._memory[memory_index]
            self._memory[memory_index] = (captured_text, prompt, new_text, filename)
            if self.permanent_memory and self.memory_dir and filename and os.path.exists(self.memory_dir):
                file_path = os.path.join(self.memory_dir, filename)
                with open(file_path, 'w', encoding='utf-8') as f:
                    f.write(f"{captured_text}\n\n{prompt}\n\n{new_text}")
                logging.debug("Updated memory file: %s", file_path)
        except Exception as e:
            logging.error("Error saving textarea changes: %s", e)

    def open_config_window(self):
        try:
            config_dialog = ConfigWindow(self)
            if config_dialog.exec_():
                self.validate_and_load_config()
                self.load_recipes()
                self.apply_theme()
                self.results_container.setVisible(self.results_in_app)
                self.append_mode_checkbox.setChecked(self.append_mode)
                self.centralWidget().update()
            logging.debug("Config window closed")
        except Exception as e:
            logging.error("Error in open_config_window: %s", e)
            QMessageBox.critical(self, "Error", f"Failed to open config window: {e}")

    def open_recipes_file(self):
        try:
            if not self.recipes_file or not os.path.exists(self.recipes_file):
                logging.warning("Recipes file not configured or does not exist")
                QMessageBox.warning(self, "File Not Found", "Recipes file not configured or does not exist.")
                return
            if sys.platform.startswith('win'):
                os.startfile(self.recipes_file)
            elif sys.platform.startswith('darwin'):
                subprocess.run(['open', self.recipes_file])
            else:
                subprocess.run(['xdg-open', self.recipes_file])
            logging.debug("Recipes file opened")
        except Exception as e:
            logging.error("Could not open recipes file: %s", e)
            QMessageBox.critical(self, "Error", f"Could not open recipes file: {e}")

    def show_about(self):
        try:
            if not os.path.exists(ABOUT_FILE):
                logging.warning("About.md file not found")
                QMessageBox.warning(self, "File Not Found", "About.md file not found.")
                return
            with open(ABOUT_FILE, 'r', encoding='utf-8') as f:
                about_text = f.read()
            QMessageBox.information(self, "About CoDude", about_text)
            logging.debug("About dialog shown")
        except Exception as e:
            logging.error("Could not read About.md: %s", e)
            QMessageBox.critical(self, "Error", f"Could not read About.md: {e}")

    def closeEvent(self, event):
        try:
            if self.active_memory_index is not None:
                self.save_textarea_changes(self.active_memory_index, self.results_textedit.toPlainText())
            for window in self.result_windows[:]:
                window.close()
            QApplication.instance().quit()
        except Exception as e:
            logging.error("Error in closeEvent: %s", e)

    def changeEvent(self, event):
        try:
            if event.type() == QEvent.Type.WindowStateChange:
                if self.isMinimized():
                    logging.debug("Window minimized, hiding to tray")
                    if self.active_memory_index is not None:
                        self.save_textarea_changes(self.active_memory_index, self.results_textedit.toPlainText())
                    event.ignore()
                    self.hide()
                    for window in self.result_windows:
                        window.hide()
                    self.tray_icon.showMessage(
                        "CoDude",
                        "CoDude is running in the background.",
                        QSystemTrayIcon.Information,
                        2000
                    )
            super().changeEvent(event)
        except Exception as e:
            logging.error("Error in changeEvent: %s", e)

    def on_tray_icon_activated(self, reason):
        try:
            if reason == QSystemTrayIcon.Trigger:
                self.show_window()
        except Exception as e:
            logging.error("Error in on_tray_icon_activated: %s", e)
            QMessageBox.critical(self, "Error", f"Failed to handle tray icon activation: {e}")

    def show_window(self):
        try:
            if self.isHidden():
                self.showNormal()
                self.activateWindow()
                for window in self.result_windows:
                    window.showNormal()
            else:
                if self.active_memory_index is not None:
                    self.save_textarea_changes(self.active_memory_index, self.results_textedit.toPlainText())
                self.hide()
                for window in self.result_windows:
                    window.hide()
            logging.debug("Window visibility toggled")
        except Exception as e:
            logging.error("Error in show_window: %s", e)
            QMessageBox.critical(self, "Error", f"Failed to toggle window visibility: {e}")

    def update_captured_text(self, text):
        try:
            if text is None:
                text = ""
                logging.warning("Received None from clipboard, setting empty text")
            self.captured_text_edit.setText(text)
            logging.debug("Captured text updated")
        except Exception as e:
            logging.error("Error in update_captured_text: %s", e)
            QMessageBox.critical(self, "Error", f"Failed to update captured text: {e}")

    def export_results_to_markdown(self):
        try:
            options = QFileDialog.Options()
            file_path, _ = QFileDialog.getSaveFileName(self, "Save LLM Response", "", "Markdown Files (*.md);;All Files (*)", options=options)
            if file_path:
                with open(file_path, 'w', encoding='utf-8') as f:
                    f.write(self.results_textedit.toPlainText())
                QMessageBox.information(self, "Export Successful", f"Response saved to {file_path}")
        except Exception as e:
            logging.error("Error exporting results: %s", e)
            QMessageBox.critical(self, "Export Error", f"Could not save file: {e}")

    def copy_results_to_clipboard(self):
        try:
            clipboard = QApplication.clipboard()
            clipboard.setText(self.results_textedit.toPlainText())
            QMessageBox.information(self, "Copy Successful", "Response copied to clipboard.")
        except Exception as e:
            logging.error("Error copying results: %s", e)
            QMessageBox.critical(self, "Copy Error", f"Could not copy to clipboard: {e}")

    def save_append_mode(self):
        try:
            self.append_mode = self.append_mode_checkbox.isChecked()
            with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
                config = json.load(f)
            config['append_mode'] = self.append_mode
            with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
                json.dump(config, f, indent=4)
            logging.debug("Append mode saved: %s", self.append_mode)
        except Exception as e:
            logging.error("Error saving append mode: %s", e)

    def adjust_textarea_font(self, textarea, delta):
        try:
            textarea_id = str(id(textarea))
            logging.debug("Adjusting font for textarea %s by %d", textarea_id, delta)
            current_size = self.textarea_font_sizes.get(textarea_id, self.font_size)
            logging.debug("Current font size for %s: %d", textarea_id, current_size)
            new_size = max(8, min(18, current_size + delta))
            textarea.setStyleSheet(f"font-size: {new_size}pt;")
            self.textarea_font_sizes[textarea_id] = new_size
            textarea.repaint()
            textarea.update()
            QApplication.processEvents()
            with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
                config = json.load(f)
            config['textarea_font_sizes'] = self.textarea_font_sizes
            with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
                json.dump(config, f, indent=4)
            logging.debug("Adjusted font size for textarea %s to %d, saved to config", textarea_id, new_size)
        except Exception as e:
            logging.error("Error adjusting textarea font: %s", e)

    def debug_font_sizes(self):
        try:
            logging.info("Debugging textarea font sizes")
            font_info = []
            for textarea, name in [
                (self.captured_text_edit, "Captured Text"),
                (self.custom_input_textedit, "Custom Input"),
                (self.results_textedit, "LLM Results")
            ]:
                textarea_id = str(id(textarea))
                saved_size = self.textarea_font_sizes.get(textarea_id, self.font_size)
                font_info.append(f"{name}: {saved_size}pt")
                logging.info("%s textarea (ID: %s): Saved font size = %d", name, textarea_id, saved_size)
            log_path = os.path.abspath(LOG_FILE)
            message = f"Font sizes:\n" + "\n".join(font_info) + f"\n\nLogged to: {log_path}"
            QMessageBox.information(self, "Font Sizes", message)
        except Exception as e:
            logging.error("Error debugging font sizes: %s", e)

    def load_permanent_memory(self):
        try:
            logging.debug("Loading permanent memory from %s", self.memory_dir)
            memory_files = sorted(glob.glob(os.path.join(self.memory_dir, "*.md")), key=os.path.getmtime)
            for file_path in memory_files:
                with open(file_path, 'r', encoding='utf-8') as f:
                    content = f.read()
                    parts = content.split("\n\n", 2)
                    if len(parts) == 3:
                        captured_text, prompt, response = parts
                        filename = os.path.basename(file_path)
                        self._memory.append((captured_text, prompt, response, filename))
                        item_text = f"{prompt[:30]}... on {captured_text[:30]}..."
                        entry_widget = MemoryEntryWidget(item_text, filename)
                        list_item = QListWidgetItem(self.memory_list)
                        list_item.setSizeHint(entry_widget.sizeHint())
                        self.memory_list.setItemWidget(list_item, entry_widget)
                        entry_widget.delete_button.clicked.connect(lambda: self.delete_memory_entry(list_item))
            logging.debug("Loaded %d memory entries", len(self._memory))
        except Exception as e:
            logging.error("Error loading permanent memory: %s", e)

    def save_memory_entry(self, captured_text, prompt, response):
        try:
            if self.permanent_memory and self.memory_dir and os.path.exists(self.memory_dir):
                safe_prompt = "".join(c for c in prompt[:50] if c.isalnum() or c in " -_").strip()
                if not safe_prompt:
                    safe_prompt = "memory_entry"
                timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                filename = f"{safe_prompt}_{timestamp}.md"
                file_path = os.path.join(self.memory_dir, filename)
                with open(file_path, 'w', encoding='utf-8') as f:
                    f.write(f"{captured_text}\n\n{prompt}\n\n{response}")
                logging.debug("Saved memory entry to %s", file_path)
                return filename
            return None
        except Exception as e:
            logging.error("Error saving memory entry: %s", e)
            return None

def main():
    logging.info("Starting CoDude application")
    try:
        app = QApplication(sys.argv)
        app.setQuitOnLastWindowClosed(False)
        logging.debug("QApplication initialized")
        window = CoDudeApp()
        logging.debug("CoDudeApp instance created")
        sys.exit(app.exec_())
    except Exception as e:
        logging.error("Unexpected error during startup: %s", e)
        print(f"Error during startup: {e}")
        sys.exit(1)

if __name__ == "__main__":
    main()