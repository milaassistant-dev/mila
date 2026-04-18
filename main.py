# main.py - Mila Assistant (aiogram 3.x compatible)
# Features:
# - GUI (customtkinter)
# - Voice control (speech_recognition)
# - Telegram bot (aiogram 3.x)
# - Clipboard sync server (WebSocket) integrated
#
# Requirements (example):
# pip install customtkinter pyautogui psutil pyperclip keyboard SpeechRecognition aiogram==3.0.0b7 pillow googletrans==4.0.0-rc1 websockets pywin32
# For image clipboard support on Windows: pywin32
#
import os
import sys
import json
import time
import threading
import subprocess
import webbrowser
import asyncio
import platform
from io import BytesIO
from typing import List, Optional
import re
import socket
import base64
import struct

import websockets
import requests

# GUI
import customtkinter as ctk
from tkinter import messagebox, filedialog, simpledialog

# Automation
import pyautogui
import psutil
import pyperclip
import keyboard

# Speech recognition (STT)
import speech_recognition as sr

# Telegram (aiogram 3.x)
from aiogram import Bot, Dispatcher, types
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, BufferedInputFile
from aiogram.filters import Command

# Image handling
from PIL import Image, ImageGrab
import hashlib
import tempfile
import shutil

# Windows API
import ctypes

IS_WINDOWS = platform.system() == 'Windows'
IS_LINUX = platform.system() == 'Linux'

# Optional translator
try:
    from googletrans import Translator
    translator = Translator()
except Exception:
    translator = None

# ----------------- Configuration -----------------
def get_app_dir() -> str:
    """Return persistent per-user app directory."""
    if platform.system() == 'Windows':
        base_dir = os.getenv('APPDATA') or os.path.expanduser('~')
        return os.path.join(base_dir, 'MilaAssistant')
    base_dir = os.getenv('XDG_CONFIG_HOME') or os.path.join(os.path.expanduser('~'), '.config')
    return os.path.join(base_dir, 'mila_assistant')

def resource_path(relative_path: str) -> str:
    """Resolve resource path for PyInstaller or source mode."""
    base_dir = getattr(sys, '_MEIPASS', SCRIPT_DIR)
    return os.path.join(base_dir, relative_path)

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
APP_DIR = get_app_dir()
os.makedirs(APP_DIR, exist_ok=True)
SETTINGS_FILE = os.path.join(APP_DIR, 'mila_settings.json')
APPS_FILE = os.path.join(APP_DIR, 'app.txt')
TOKEN_FILE = os.path.join(APP_DIR, 'token.txt')

def _migrate_persistent_files():
    """Optional legacy migration (disabled by default to avoid personal data carry-over)."""
    if os.getenv('MILA_IMPORT_LEGACY_DATA', '').strip() != '1':
        return
    candidates = [
        SCRIPT_DIR,
        os.getcwd(),
        os.path.dirname(APP_DIR),
    ]
    for name in ('app.txt',):
        dst = os.path.join(APP_DIR, name)
        if os.path.exists(dst):
            continue
        for base in candidates:
            src = os.path.join(base, name)
            if os.path.exists(src):
                try:
                    shutil.copy2(src, dst)
                except Exception:
                    pass
                break

def _merge_settings_from_candidates(current: dict) -> bool:
    """Optional merge of legacy OpenAI settings (disabled by default)."""
    if os.getenv('MILA_IMPORT_LEGACY_DATA', '').strip() != '1':
        return False
    if current.get('openai_api_key') and current.get('openai_api_url'):
        return False
    candidates = [
        os.path.join(os.getcwd(), 'mila_settings.json'),
        os.path.join(SCRIPT_DIR, 'mila_settings.json'),
        os.path.join(os.path.dirname(APP_DIR), 'mila_settings.json'),
    ]
    changed = False
    for path in candidates:
        if path == SETTINGS_FILE or not os.path.exists(path):
            continue
        try:
            with open(path, 'r', encoding='utf-8') as f:
                data = json.load(f)
            if not current.get('openai_api_key') and data.get('openai_api_key'):
                current['openai_api_key'] = data.get('openai_api_key')
                changed = True
            if not current.get('openai_api_url') and data.get('openai_api_url'):
                current['openai_api_url'] = data.get('openai_api_url')
                changed = True
            if changed:
                break
        except Exception:
            continue
    return changed

def _resolve_apps_file() -> str:
    candidates = [
        os.path.join(APP_DIR, 'app.txt'),
        os.path.join(SCRIPT_DIR, 'app.txt'),
        os.path.join(os.getcwd(), 'app.txt'),
        os.path.join(os.path.dirname(APP_DIR), 'app.txt'),
    ]
    for p in candidates:
        if os.path.exists(p):
            return p
    return candidates[0]

DEFAULT_SETTINGS = {
    'assistant_name': 'Мила',
    'user_name': 'Пользователь',
    'hotkey': 'f8',
    'autostart': False,
    'telegram_token': '',
    'open_apps': [],
    'history': [],
    'last_chat_id': None,
    # Настройки аккаунта для синхронизации буфера обмена
    'account_username': '',
    'account_password': '',
    'account_token': '',
    'sync_server_url': 'https://sea-lion-app-i3rnh.ondigitalocean.app/',  # Ваш облачный сервер
    'account_logged_in': False,
    'use_local_server': False,  # True = локальный тест, False = облачный сервер
}

_migrate_persistent_files()

if not os.path.exists(SETTINGS_FILE):
    with open(SETTINGS_FILE, 'w', encoding='utf-8') as f:
        json.dump(DEFAULT_SETTINGS, f, ensure_ascii=False, indent=2)

with open(SETTINGS_FILE, 'r', encoding='utf-8') as f:
    settings = json.load(f)

if _merge_settings_from_candidates(settings):
    with open(SETTINGS_FILE, 'w', encoding='utf-8') as f:
        json.dump(settings, f, ensure_ascii=False, indent=2)

_defaults_changed = False
for _key, _value in DEFAULT_SETTINGS.items():
    if _key not in settings:
        settings[_key] = _value
        _defaults_changed = True
if _defaults_changed:
    with open(SETTINGS_FILE, 'w', encoding='utf-8') as f:
        json.dump(settings, f, ensure_ascii=False, indent=2)

# Принудительно исправляем настройки для правильного облачного сервера
if 'localhost' in settings.get('sync_server_url', '') or settings.get('sync_server_url', '').startswith(('ws://', 'wss://')):
    settings['sync_server_url'] = 'https://sea-lion-app-i3rnh.ondigitalocean.app/'
    settings['use_local_server'] = False
    # Сохраняем исправленные настройки
    with open(SETTINGS_FILE, 'w', encoding='utf-8') as f:
        json.dump(settings, f, ensure_ascii=False, indent=2)

# Optional legacy token import (disabled by default for release safety)
if os.getenv('MILA_IMPORT_LEGACY_DATA', '').strip() == '1' and os.path.exists(TOKEN_FILE):
    with open(TOKEN_FILE, 'r', encoding='utf-8') as f:
        tok = f.read().strip()
        if tok:
            settings['telegram_token'] = tok

# ----------------- Globals -----------------
# NOTE: pyttsx3 removed; using SAPI via win32com for TTS (from ozvuchka.py)
user_history: List[str] = settings.get('history', [])

# Telegram globals (set when bot starts)
TG_BOT: Optional[Bot] = None
TG_DISPATCHER: Optional[Dispatcher] = None
TG_LOOP: Optional[asyncio.AbstractEventLoop] = None

# Virtual-key codes for volume (windows)
VK_VOLUME_MUTE = 0xAD
VK_VOLUME_DOWN = 0xAE
VK_VOLUME_UP = 0xAF

# Voice/confirmation globals
ASSISTANT_NAME = settings.get('assistant_name', 'Мила').lower()
pending_confirmation: Optional[str] = None
pending_origin: Optional[str] = None  # 'voice' or 'telegram' and maybe chat_id
GUI_LOG_TEXT = None  # will hold CTkTextbox for logs when GUI initialized
VOICE_THREAD: Optional[threading.Thread] = None
VOICE_STOP_EVENT = threading.Event()
VOICE_ENABLED = False

# pending lock for thread-safe confirmation handling (used by some TTS/voice parts)
pending_lock = threading.Lock()
# OpenAI pending-question globals
pending_openai = False
pending_openai_origin: Optional[str] = None
pending_openai_chat_id: Optional[int] = None
pending_openai_lock = threading.Lock()

# TTS control globals for "мила стоп"
tts_in_progress = False
tts_should_stop = False
tts_thread_lock = threading.Lock()
last_command_was_think = False

# ----------------- Clipboard server integration -----------------
# Uses websockets to sync clipboard between PC and clients (e.g., Android)
# Minimal integration: monitor clipboard and broadcast; accept messages from clients.

# Try to import win32clipboard for image clipboard support on Windows
try:
    import win32clipboard
    WIN32_CLIPBOARD = True
except Exception:
    WIN32_CLIPBOARD = False

# Websocket server globals
CLIPBOARD_CLIENTS = set()
CLIPBOARD_LOOP: Optional[asyncio.AbstractEventLoop] = None
CLIPBOARD_TASK = None
CLIPBOARD_SERVER_TASK = None
CLIPBOARD_RUNNING = False
CLIPBOARD_PORT = 8765

# Account-based sync globals
ACCOUNT_WEBSOCKET = None
ACCOUNT_AUTHENTICATED = False
ACCOUNT_DEVICES = set()  # Set of device IDs connected to the same account

# Remote access (RustDesk launcher) globals
REMOTE_CLIENT_PROCESS = None
REMOTE_CLIENT_RUNNING = False
GUI_REMOTE_LOG = None  # Textbox for remote access log

# Alarms and Timers globals
ALARMS = []  # List of {'id': int, 'time': datetime, 'message': str, 'active': bool}
TIMERS = []  # List of {'id': int, 'end_time': datetime, 'duration': int, 'message': str, 'active': bool}
ALARM_TIMER_THREAD = None
ALARM_COUNTER = 0
TIMER_COUNTER = 0

# Used to avoid double-broadcast when we programmatically set the clipboard
# after creating/sending an image from this process.
# Structure: {'sig': <str>, 'ts': <float>} where sig is md5 hex of image bytes (JPEG/PNG)
CLIPBOARD_IGNORE_IMAGE = None

def get_local_host_addr():
    """Get local host address for local network bind."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        host_addr = s.getsockname()[0]
        s.close()
        return host_addr
    except Exception:
        return "127.0.0.1"

def _open_https_links_from_text(text: str):
    """Open every https:// link found in incoming synced text."""
    if not isinstance(text, str) or not text:
        return
    links = re.findall(r'https://\S+', text, flags=re.IGNORECASE)
    for raw in links:
        url = re.sub(r'[\)\]\}>\.,;:!?"\']+$', '', raw.strip())
        if not url.lower().startswith('https://'):
            continue
        try:
            webbrowser.open(url, new=2)
            append_clipboard_log(f"[{datetime_now_str()}] ✓ Открыта ссылка из синхронизации: {url}")
        except Exception as e:
            append_clipboard_log(f"[{datetime_now_str()}] ✗ Не удалось открыть ссылку {url}: {e}")

# ----------------- Account Authentication System -----------------

async def authenticate_account(username: str, password: str) -> dict:
    """Authenticate user with the sync server"""
    try:
        # Получаем URL сервера и заменяем порт на HTTP API порт
        server_url = settings.get('sync_server_url', 'https://sea-lion-app-i3rnh.ondigitalocean.app/')
        if settings.get('use_local_server', False):
            server_url = 'ws://localhost:8765'
        
        # Для облачного сервера используем тот же порт для HTTP API
        if server_url.startswith('https://') or 'ondigitalocean.app' in server_url or 'herokuapp.com' in server_url:
            if server_url.startswith('https://'):
                auth_url = server_url.rstrip('/') + '/auth'
            else:
                auth_url = server_url.replace('ws://', 'http://').replace('wss://', 'https://').rstrip('/') + '/auth'
        else:
            # Для локального сервера используем порт 8080
            auth_url = server_url.replace('ws://', 'http://').replace('wss://', 'https://').replace(':8765', ':8080') + '/auth'
        
        auth_data = {
            'username': username,
            'password': password,
            'device_id': get_device_id(),
            'device_name': get_device_name()
        }
        
        response = requests.post(auth_url, json=auth_data, timeout=10)
        
        if response.status_code == 200:
            result = response.json()
            if result.get('success'):
                # Save token and additional info
                settings['account_token'] = result.get('token')
                settings['account_username'] = result.get('username', username)
                settings['account_logged_in'] = True
                settings['token_expires_in'] = result.get('expires_in', 86400)
                save_settings()
                append_clipboard_log(f"[{datetime_now_str()}] ✓ Успешная авторизация: {username}")
                append_clipboard_log(f"[{datetime_now_str()}] ℹ Токен действителен {result.get('expires_in', 86400)//3600} часов")
                return {'success': True, 'token': result.get('token'), 'username': result.get('username')}
            else:
                error_msg = result.get('error', 'Неизвестная ошибка')
                append_clipboard_log(f"[{datetime_now_str()}] ✗ Ошибка авторизации: {error_msg}")
                return {'success': False, 'error': error_msg}
        else:
            append_clipboard_log(f"[{datetime_now_str()}] ✗ Ошибка сервера: {response.status_code}")
            return {'success': False, 'error': f'Server error: {response.status_code}'}
    except Exception as e:
        append_clipboard_log(f"[{datetime_now_str()}] ✗ Ошибка подключения к серверу: {e}")
        return {'success': False, 'error': str(e)}

async def register_account(username: str, password: str, email: str = "") -> dict:
    """Register new user account"""
    try:
        # Получаем URL сервера и заменяем порт на HTTP API порт
        server_url = settings.get('sync_server_url', 'https://sea-lion-app-i3rnh.ondigitalocean.app/')
        if settings.get('use_local_server', False):
            server_url = 'ws://localhost:8765'
            
        # Для облачного сервера используем тот же порт для HTTP API
        if server_url.startswith('https://') or 'ondigitalocean.app' in server_url or 'herokuapp.com' in server_url:
            if server_url.startswith('https://'):
                register_url = server_url.rstrip('/') + '/register'
            else:
                register_url = server_url.replace('ws://', 'http://').replace('wss://', 'https://').rstrip('/') + '/register'
        else:
            # Для локального сервера используем порт 8080
            register_url = server_url.replace('ws://', 'http://').replace('wss://', 'https://').replace(':8765', ':8080') + '/register'
        
        register_data = {
            'username': username,
            'password': password,
            'email': email,
            'device_id': get_device_id(),
            'device_name': get_device_name()
        }
        
        response = requests.post(register_url, json=register_data, timeout=10)
        
        if response.status_code == 200:
            result = response.json()
            if result.get('success'):
                append_clipboard_log(f"[{datetime_now_str()}] ✓ Успешная регистрация: {username}")
                append_clipboard_log(f"[{datetime_now_str()}] ℹ Токен действителен {result.get('expires_in', 86400)//3600} часов")
                # Save token info after successful registration
                settings['account_token'] = result.get('token')
                settings['account_username'] = result.get('username', username)
                settings['account_logged_in'] = True
                settings['token_expires_in'] = result.get('expires_in', 86400)
                save_settings()
                return {'success': True, 'token': result.get('token'), 'username': result.get('username')}
            else:
                error_msg = result.get('error', result.get('message', 'Неизвестная ошибка'))
                append_clipboard_log(f"[{datetime_now_str()}] ✗ Ошибка регистрации: {error_msg}")
                return {'success': False, 'error': error_msg}
        else:
            try:
                error_response = response.text
                append_clipboard_log(f"[{datetime_now_str()}] ✗ Ошибка сервера: {response.status_code}")
                append_clipboard_log(f"[{datetime_now_str()}] ℹ Подробности: {error_response[:200]}")
            except:
                append_clipboard_log(f"[{datetime_now_str()}] ✗ Ошибка сервера: {response.status_code}")
            return {'success': False, 'error': f'Server error: {response.status_code}'}
    except Exception as e:
        append_clipboard_log(f"[{datetime_now_str()}] ✗ Ошибка подключения к серверу: {e}")
        return {'success': False, 'error': str(e)}

def get_device_id() -> str:
    """Get unique device identifier"""
    try:
        import platform
        import uuid
        
        # Try to get MAC address
        mac = ':'.join(['{:02x}'.format((uuid.getnode() >> elements) & 0xff) for elements in range(0,2*6,2)][::-1])
        hostname = platform.node()
        system = platform.system()
        
        # Create hash from MAC + hostname + system
        device_info = f"{mac}-{hostname}-{system}"
        device_hash = hashlib.md5(device_info.encode()).hexdigest()[:16]
        return device_hash
    except Exception:
        # Fallback to random device ID (will be different each run)
        import random
        return f"device_{random.randint(100000, 999999)}"

def get_device_name() -> str:
    """Get human-readable device name"""
    try:
        import platform
        return f"{platform.node()} ({platform.system()})"
    except Exception:
        return "Unknown Device"

def logout_account():
    """Logout from current account"""
    global ACCOUNT_AUTHENTICATED
    settings['account_token'] = ''
    settings['account_username'] = ''
    settings['account_logged_in'] = False
    ACCOUNT_AUTHENTICATED = False
    save_settings()
    # Stop clipboard sync if running
    stop_clipboard_server()
    append_clipboard_log(f"[{datetime_now_str()}] Выход из аккаунта")

def is_authenticated() -> bool:
    """Check if user is authenticated"""
    return settings.get('account_logged_in', False) and settings.get('account_token', '') != ''

def _sync_server_http_base() -> str:
    """Return HTTP base URL for sync server."""
    server_url = settings.get('sync_server_url', 'https://sea-lion-app-i3rnh.ondigitalocean.app/')
    if settings.get('use_local_server', False):
        return 'http://localhost:8080'
    if server_url.startswith('wss://'):
        server_url = server_url.replace('wss://', 'https://')
    elif server_url.startswith('ws://'):
        server_url = server_url.replace('ws://', 'http://')
    return server_url.rstrip('/')

def push_command_via_account(command_text: str) -> bool:
    """Send a command to other devices via cloud sync server.
    The companion app must poll /sync and handle type='command'.
    """
    if not is_authenticated():
        return False
    base_url = _sync_server_http_base()
    token = settings.get('account_token', '')
    if not token:
        return False
    try:
        push_data = {
            'token': token,
            'content': command_text,
            'device_id': get_device_id(),
            'type': 'command',
        }
        resp = requests.post(f"{base_url}/push", json=push_data, timeout=10)
        if resp.status_code == 200:
            try:
                return bool(resp.json().get('success'))
            except Exception:
                return True
        return False
    except Exception as e:
        append_clipboard_log(f"[{datetime_now_str()}] ✗ Ошибка отправки команды через аккаунт: {e}")
        return False

def push_content_via_account(content: str, content_type: str = 'text', target_device_id: Optional[str] = None) -> bool:
    """Send text/image payload to other devices via cloud sync server."""
    if not is_authenticated():
        return False
    base_url = _sync_server_http_base()
    token = settings.get('account_token', '')
    if not token:
        return False
    try:
        payload_text = '' if content is None else str(content)
        if content_type == 'text' and not payload_text.strip():
            return False
        push_data = {
            'token': token,
            'content': payload_text,
            'device_id': get_device_id(),
            'type': content_type,
        }
        if target_device_id:
            # Keep several aliases for backend compatibility.
            push_data['target_device_id'] = target_device_id
            push_data['to_device_id'] = target_device_id
            push_data['recipient_device_id'] = target_device_id
        timeout_sec = 30 if content_type == 'image' else 10
        resp = requests.post(f"{base_url}/push", json=push_data, timeout=timeout_sec)
        if resp.status_code == 200:
            try:
                payload = resp.json()
                ok = bool(payload.get('success'))
                if not ok:
                    append_clipboard_log(
                        f"[{datetime_now_str()}] ✗ push отклонён сервером (type={content_type}): {payload.get('error', 'unknown')}"
                    )
                return ok
            except Exception:
                return True
        append_clipboard_log(
            f"[{datetime_now_str()}] ✗ push HTTP {resp.status_code} (type={content_type}): {(resp.text or '')[:200]}"
        )
        return False
    except Exception as e:
        append_clipboard_log(f"[{datetime_now_str()}] ✗ Ошибка отправки данных через аккаунт: {e}")
        return False

def validate_saved_account_token() -> bool:
    """Validate saved token with server to restore session without re-login."""
    token = settings.get('account_token', '')
    if not token:
        return False
    base_url = _sync_server_http_base()
    try:
        resp = requests.get(
            f"{base_url}/sync",
            params={'token': token, 'device_id': get_device_id(), 'last_id': 0},
            timeout=10
        )
        if resp.status_code == 200:
            settings['account_logged_in'] = True
            save_settings()
            return True
        if resp.status_code == 400:
            try:
                data = resp.json()
                err = (data.get('error') or '').lower()
                if 'token' in err:
                    settings['account_logged_in'] = False
                    settings['account_token'] = ''
                    save_settings()
            except Exception:
                pass
    except Exception:
        pass
    return False

def copy_image_to_clipboard(image_path):
    """Copy image to Windows clipboard (DIB format)"""
    if not WIN32_CLIPBOARD:
        return False
    
    try:
        img = Image.open(image_path)
        # Convert to RGB if necessary
        if img.mode not in ("RGB", "RGBA"):
            img = img.convert("RGB")
        elif img.mode == "RGBA":
            # Create white background for RGBA images
            background = Image.new("RGB", img.size, (255, 255, 255))
            background.paste(img, mask=img.split()[3] if len(img.split()) == 4 else None)
            img = background
        
        # Save as BMP
        output = BytesIO()
        img.save(output, "BMP")
        bmp_data = output.getvalue()
        output.close()
        
        # Remove BMP file header (14 bytes) to get DIB data
        dib_data = bmp_data[14:]

        win32clipboard.OpenClipboard()
        win32clipboard.EmptyClipboard()
        win32clipboard.SetClipboardData(win32clipboard.CF_DIB, dib_data)
        win32clipboard.CloseClipboard()
        # Успешно скопировано
        return True
    except Exception as e:
        # Логируем ошибку
        try:
            append_clipboard_log(f"[{datetime_now_str()}] ⚠ Ошибка копирования в буфер: {e}")
        except:
            print(f"⚠ Ошибка копирования изображения в буфер: {e}")
        import traceback
        traceback.print_exc()
        try:
            win32clipboard.CloseClipboard()
        except:
            pass
        return False

def get_image_from_clipboard():
    """Get image from Windows clipboard, return PIL Image or None"""
    if not WIN32_CLIPBOARD:
        return None
    try:
        win32clipboard.OpenClipboard()
        # Try CF_DIB format first (Device Independent Bitmap)
        if win32clipboard.IsClipboardFormatAvailable(win32clipboard.CF_DIB):
            data = win32clipboard.GetClipboardData(win32clipboard.CF_DIB)
            win32clipboard.CloseClipboard()
            
            # Convert DIB to BMP by adding BMP file header
            # DIB data already contains BITMAPINFOHEADER + color table + pixel data
            # We just need to add the 14-byte BMP file header
            
            # Get the size of the bitmap info header (first 4 bytes of DIB)
            dib_header_size = struct.unpack('<I', data[:4])[0]
            
            # Simple approach: assume standard BITMAPINFOHEADER (40 bytes)
            # and calculate the offset to pixel data
            if dib_header_size >= 40:
                # Read bit count to determine if there's a color palette
                bit_count = struct.unpack('<H', data[14:16])[0]
                # Calculate color table size
                if bit_count <= 8:
                    num_colors = struct.unpack('<I', data[32:36])[0]
                    if num_colors == 0:
                        num_colors = 1 << bit_count
                    offset = 14 + dib_header_size + (num_colors * 4)
                else:
                    offset = 14 + dib_header_size
            else:
                offset = 14 + dib_header_size
            
            # Create BMP file header (14 bytes)
            file_size = 14 + len(data)
            bmp_header = struct.pack('<2sIHHI', b'BM', file_size, 0, 0, offset)
            
            # Combine header and DIB data
            bmp_data = bmp_header + data
            img = Image.open(BytesIO(bmp_data))
            return img
        else:
            win32clipboard.CloseClipboard()
    except Exception as e:
        try:
            win32clipboard.CloseClipboard()
        except Exception:
            pass
    return None

def copy_pil_image_to_clipboard(img: Image.Image):
    """Copy a PIL Image to Windows clipboard (via DIB) without writing to disk."""
    if not WIN32_CLIPBOARD:
        return False
    try:
        # Ensure RGB
        if img.mode not in ("RGB", "RGBA"):
            img = img.convert("RGB")
        elif img.mode == "RGBA":
            background = Image.new("RGB", img.size, (255, 255, 255))
            background.paste(img, mask=img.split()[3] if len(img.split()) == 4 else None)
            img = background

        output = BytesIO()
        img.save(output, "BMP")
        bmp_data = output.getvalue()
        output.close()
        dib_data = bmp_data[14:]

        win32clipboard.OpenClipboard()
        win32clipboard.EmptyClipboard()
        win32clipboard.SetClipboardData(win32clipboard.CF_DIB, dib_data)
        win32clipboard.CloseClipboard()
        return True
    except Exception as e:
        try:
            append_clipboard_log(f"[{datetime_now_str()}] ⚠ Ошибка копирования PIL->буфер: {e}")
        except Exception:
            print(f"⚠ Ошибка копирования PIL->буфер: {e}")
        try:
            win32clipboard.CloseClipboard()
        except Exception:
            pass
        return False

async def _broadcast_image_b64(img_b64: str):
    """Async helper to broadcast base64-encoded image to all connected clipboard clients."""
    if not CLIPBOARD_CLIENTS:
        return 0
    message = json.dumps({'type': 'image', 'content': img_b64})
    results = await asyncio.gather(*[client.send(message) for client in CLIPBOARD_CLIENTS], return_exceptions=True)
    return len([r for r in results if not isinstance(r, Exception)])

def broadcast_image_bytes(img_bytes: bytes):
    """Schedule broadcasting of raw image bytes (JPEG/PNG) to connected clients.
    This is safe to call from sync threads.
    """
    try:
        img_b64 = base64.b64encode(img_bytes).decode('utf-8')
        # set ignore signature so monitor doesn't re-broadcast the same image
        sig = hashlib.md5(img_bytes).hexdigest()
        global CLIPBOARD_IGNORE_IMAGE
        CLIPBOARD_IGNORE_IMAGE = {'sig': sig, 'ts': time.time()}

        if CLIPBOARD_LOOP and CLIPBOARD_RUNNING:
            try:
                fut = asyncio.run_coroutine_threadsafe(_broadcast_image_b64(img_b64), CLIPBOARD_LOOP)
                # don't block; optionally ensure started
            except Exception as e:
                append_clipboard_log(f"[{datetime_now_str()}] ✗ Ошибка при отправке изображения клиентам: {e}")
        else:
            append_clipboard_log(f"[{datetime_now_str()}] Нет запущенного loop для рассылки изображения (клиентов: {len(CLIPBOARD_CLIENTS)})")
    except Exception as e:
        append_clipboard_log(f"[{datetime_now_str()}] ✗ Ошибка broadcast_image_bytes: {e}")

async def account_clipboard_client():
    """Connect to cloud sync server as authenticated client using HTTP polling"""
    global ACCOUNT_WEBSOCKET, ACCOUNT_AUTHENTICATED, ACCOUNT_DEVICES
    
    if not is_authenticated():
        append_clipboard_log(f"[{datetime_now_str()}] ✗ Не авторизован для подключения к облачной синхронизации")
        return

    server_url = settings.get('sync_server_url', 'https://sea-lion-app-i3rnh.ondigitalocean.app/')
    if settings.get('use_local_server', False):
        server_url = 'http://localhost:8080'
    else:
        # Преобразуем в HTTP URL для polling
        if server_url.startswith('wss://'):
            server_url = server_url.replace('wss://', 'https://')
        elif server_url.startswith('ws://'):
            server_url = server_url.replace('ws://', 'http://')
        server_url = server_url.rstrip('/')
    
    token = settings.get('account_token', '')
    username = settings.get('account_username', '')
    last_sync_time = None
    
    # Проверяем что токен не пустой
    if not token:
        append_clipboard_log(f"[{datetime_now_str()}] ✗ Токен пуст! Требуется повторная авторизация")
        return
        
    append_clipboard_log(f"[{datetime_now_str()}] 🔍 Токен найден: {len(token)} символов")
    
    ACCOUNT_AUTHENTICATED = True
    append_clipboard_log(f"[{datetime_now_str()}] ✓ Подключен к безопасному серверу как {username}")
    append_clipboard_log(f"[{datetime_now_str()}] ℹ Используется HTTP polling для совместимости с облаком")
    
    # Start monitoring clipboard
    monitor_task = asyncio.create_task(monitor_clipboard_for_account_http(server_url, token))
    last_received_id = 0  # ID последнего полученного элемента
    
    try:
        # HTTP polling loop
        while ACCOUNT_AUTHENTICATED:
            try:
                # Получаем новые данные с сервера
                sync_url = f"{server_url}/sync"
                params = {'token': token, 'device_id': get_device_id(), 'last_id': last_received_id}
                
                # Создаем URL с параметрами
                import urllib.parse
                query_string = urllib.parse.urlencode(params)
                full_url = f"{sync_url}?{query_string}"
                
                # Отладочная информация (только первый раз)
                if not hasattr(account_clipboard_client, '_debug_logged'):
                    append_clipboard_log(f"[{datetime_now_str()}] 🔍 Отладка: URL={sync_url}, токен скрыт")
                    account_clipboard_client._debug_logged = True
                
                response = requests.get(full_url, timeout=10)
                
                if response.status_code == 200:
                    try:
                        # Обрабатываем ответ с возможными HTTP заголовками в теле
                        response_text = response.text
                        
                        # Проверяем, есть ли HTTP заголовки в теле ответа
                        if "HTTP/1.0" in response_text or "Server:" in response_text:
                            # Извлекаем JSON из ответа с заголовками
                            json_start = response_text.find('{')
                            if json_start != -1:
                                json_part = response_text[json_start:]
                                data = json.loads(json_part)
                            else:
                                continue
                        else:
                            # Обычный JSON ответ
                            data = response.json()
                        
                        if data.get('success') and data.get('content'):
                            # Получены новые данные
                            content = data.get('content')
                            device_id = data.get('device_id', '')
                            content_type = data.get('type', 'text')
                            item_id = data.get('id', 0)
                            
                            # Обновляем last_received_id чтобы не получать те же данные снова
                            if item_id > last_received_id:
                                last_received_id = item_id
                            
                            # Игнорируем данные от нашего устройства
                            if device_id != get_device_id():
                                try:
                                    if content_type == 'command':
                                        # Выполняем команду, пришедшую от другого устройства через аккаунт
                                        append_clipboard_log(f"[{datetime_now_str()}] ▶ Команда через аккаунт: {content[:80]}")
                                        threading.Thread(
                                            target=handle_command_text,
                                            args=(content, 'android', None, device_id),
                                            daemon=True,
                                        ).start()
                                    elif content_type == 'image':
                                        # Обработка изображения
                                        import base64
                                        img_bytes = base64.b64decode(content)
                                        img = Image.open(BytesIO(img_bytes))
                                        img.load()

                                        # Копируем в буфер обмена
                                        if copy_pil_image_to_clipboard(img):
                                            append_clipboard_log(f"[{datetime_now_str()}] ✓ Получено изображение: {img.size}")
                                        else:
                                            append_clipboard_log(f"[{datetime_now_str()}] ✗ Ошибка копирования изображения в буфер")
                                    else:
                                        # Обработка текста
                                        import pyperclip
                                        current_clipboard = pyperclip.paste()
                                        if current_clipboard != content:
                                            pyperclip.copy(content)
                                            append_clipboard_log(f"[{datetime_now_str()}] ✓ Получены данные: {content[:80]}")
                                            _open_https_links_from_text(content)
                                except Exception as e:
                                    append_clipboard_log(f"[{datetime_now_str()}] ✗ Ошибка установки данных: {e}")
                    except json.JSONDecodeError as e:
                        append_clipboard_log(f"[{datetime_now_str()}] ✗ Ответ сервера не JSON: {response.text[:200]}")
                
                elif response.status_code == 400:
                    try:
                        # Обрабатываем ответ с возможными HTTP заголовками в теле
                        response_text = response.text
                        
                        # Проверяем, есть ли HTTP заголовки в теле ответа
                        if "HTTP/1.0" in response_text or "Server:" in response_text:
                            # Извлекаем JSON из ответа с заголовками
                            json_start = response_text.find('{')
                            if json_start != -1:
                                json_part = response_text[json_start:]
                                error_data = json.loads(json_part)
                            else:
                                append_clipboard_log(f"[{datetime_now_str()}] ✗ JSON не найден в ответе ошибки с заголовками")
                                # При неизвестной ошибке 400 сбрасываем токен
                                settings['account_logged_in'] = False
                                settings['account_token'] = ''
                                save_settings()
                                ACCOUNT_AUTHENTICATED = False
                                break
                        else:
                            # Обычный JSON ответ
                            error_data = response.json()
                        
                        error_message = error_data.get('error', '')
                        if 'token' in error_message.lower():
                            append_clipboard_log(f"[{datetime_now_str()}] ✗ Токен недействителен: {error_message}")
                            append_clipboard_log(f"[{datetime_now_str()}] ℹ Требуется повторная авторизация через GUI")
                            # Сбрасываем статус авторизации
                            settings['account_logged_in'] = False
                            settings['account_token'] = ''
                            save_settings()
                            ACCOUNT_AUTHENTICATED = False
                            break
                        else:
                            append_clipboard_log(f"[{datetime_now_str()}] ✗ Ошибка сервера: {error_message}")
                    except json.JSONDecodeError:
                        append_clipboard_log(f"[{datetime_now_str()}] ✗ HTTP 400, ответ: {response.text[:200]}")
                        # При неизвестной ошибке 400 тоже сбрасываем токен
                        settings['account_logged_in'] = False
                        settings['account_token'] = ''
                        save_settings()
                        ACCOUNT_AUTHENTICATED = False
                        break
                        
                elif response.status_code == 404:
                    append_clipboard_log(f"[{datetime_now_str()}] ✗ Endpoint /sync не найден - сервер не обновлен!")
                    ACCOUNT_AUTHENTICATED = False
                    break
                    
                else:
                    append_clipboard_log(f"[{datetime_now_str()}] ✗ Неожиданный код: {response.status_code}, ответ: {response.text[:200]}")
                
            except Exception as e:
                append_clipboard_log(f"[{datetime_now_str()}] ✗ Ошибка HTTP polling: {e}")
            
            # Пауза между запросами
            await asyncio.sleep(2)  # Опрашиваем каждые 2 секунды
            
    except Exception as e:
        append_clipboard_log(f"[{datetime_now_str()}] ✗ Ошибка подключения к серверу: {e}")
    finally:
        monitor_task.cancel()
        ACCOUNT_WEBSOCKET = None
        ACCOUNT_AUTHENTICATED = False
        append_clipboard_log(f"[{datetime_now_str()}] Отключен от сервера")

async def handle_account_message(data: dict):
    """Handle message from cloud sync server"""
    msg_type = data.get('type', '')
    device_id = data.get('device_id', '')
    
    # Ignore messages from our own device
    if device_id == get_device_id():
        return
    
    append_clipboard_log(f"[{datetime_now_str()}] Получено сообщение типа: {msg_type} от устройства: {device_id[:8] if device_id else 'неизвестно'}")
    
    if msg_type == 'clipboard_sync':
        content = data.get('content', '')
        if content:
            try:
                pyperclip.copy(content)
                append_clipboard_log(f"[{datetime_now_str()}] ✓ Содержимое установлено в буфер ПК: {content[:80]}")
                _open_https_links_from_text(content)
            except Exception as e:
                append_clipboard_log(f"[{datetime_now_str()}] ✗ Ошибка установки содержимого в буфер: {e}")
    
    elif msg_type == 'auth_success':
        append_clipboard_log(f"[{datetime_now_str()}] ✓ Аутентификация успешна: {data.get('message', '')}")
    
    elif msg_type == 'error':
        error_msg = data.get('message', 'Неизвестная ошибка')
        append_clipboard_log(f"[{datetime_now_str()}] ✗ Ошибка сервера: {error_msg}")

async def monitor_clipboard_for_account_http(server_url: str, token: str):
    """Monitor PC clipboard changes and send to cloud server via HTTP"""
    last_clipboard_text = None
    last_clipboard_image_hash = None
    append_clipboard_log(f"[{datetime_now_str()}] HTTP мониторинг буфера обмена запущен")
    
    while ACCOUNT_AUTHENTICATED:
        try:
            await asyncio.sleep(1)  # Проверяем каждую секунду
            
            # Check for image changes first
            try:
                img = ImageGrab.grabclipboard()
                if img is not None and hasattr(img, 'tobytes'):
                    # Вычисляем хеш изображения
                    import hashlib
                    img_bytes = img.tobytes()
                    img_hash = hashlib.md5(img_bytes).hexdigest()
                    
                    if img_hash != last_clipboard_image_hash:
                        last_clipboard_image_hash = img_hash
                        
                        # Конвертируем в PNG и Base64
                        import base64
                        buffer = BytesIO()
                        img.save(buffer, format='PNG')
                        img_base64 = base64.b64encode(buffer.getvalue()).decode('utf-8')
                        
                        # Отправляем изображение
                        push_data = {
                            'token': token,
                            'content': img_base64,
                            'device_id': get_device_id(),
                            'type': 'image'
                        }
                        
                        response = requests.post(f"{server_url}/push", json=push_data, timeout=30)
                        
                        if response.status_code == 200:
                            try:
                                result = response.json()
                                if result.get('success'):
                                    append_clipboard_log(f"[{datetime_now_str()}] ✓ Отправлено изображение: {img.size}")
                                else:
                                    append_clipboard_log(f"[{datetime_now_str()}] ✗ Ошибка отправки изображения: {result.get('error')}")
                            except json.JSONDecodeError:
                                pass
                        continue  # Не проверяем текст если отправили изображение
            except Exception as e:
                pass  # Нет изображения в буфере
            
            # Check for text changes
            try:
                current_text = pyperclip.paste()
                if current_text != last_clipboard_text and current_text.strip():
                    # Игнорируем системные сообщения
                    if current_text.strip().startswith("---------------------------- PROCESS STARTED"):
                        continue
                        
                    last_clipboard_text = current_text
                    
                    # Отправляем данные через HTTP POST
                    push_data = {
                        'token': token,
                        'content': current_text,
                        'device_id': get_device_id(),
                        'type': 'text'
                    }
                    
                    response = requests.post(f"{server_url}/push", json=push_data, timeout=5)
                    
                    if response.status_code == 200:
                        try:
                            result = response.json()
                            if result.get('success'):
                                append_clipboard_log(f"[{datetime_now_str()}] ✓ Отправлен текст: {current_text[:80]}")
                            else:
                                append_clipboard_log(f"[{datetime_now_str()}] ✗ Ошибка отправки: {result.get('error')}")
                        except json.JSONDecodeError:
                            append_clipboard_log(f"[{datetime_now_str()}] ✗ Отправка: ответ не JSON: {response.text[:100]}")
                    else:
                        append_clipboard_log(f"[{datetime_now_str()}] ✗ HTTP ошибка: {response.status_code}, ответ: {response.text[:100]}")
                        if response.status_code == 404:
                            append_clipboard_log(f"[{datetime_now_str()}] ✗ Endpoint /push не найден - сервер не обновлен!")
                            break
                        
            except Exception as e:
                # Игнорируем ошибки чтения буфера обмена
                pass
            
        except Exception as e:
            append_clipboard_log(f"[{datetime_now_str()}] ✗ Ошибка мониторинга: {e}")
            break

async def monitor_clipboard_for_account():
    """Monitor PC clipboard changes and send to cloud server (WebSocket mode - deprecated)"""
    last_clipboard_text = None
    last_clipboard_image_hash = None
    append_clipboard_log(f"[{datetime_now_str()}] Мониторинг буфера обмена для аккаунта запущен (WebSocket)")
    
    while ACCOUNT_AUTHENTICATED and ACCOUNT_WEBSOCKET:
        try:
            await asyncio.sleep(0.5)
            
            # Check for text changes
            try:
                current_text = pyperclip.paste()
                if (
                    current_text != last_clipboard_text
                    and current_text.strip()
                    and not current_text.strip().startswith("---------------------------- PROCESS STARTED")
                ):
                    last_clipboard_text = current_text
                    print(f"[DEBUG] Clipboard text to push via WebSocket: {current_text}")
                    append_clipboard_log(f"[DEBUG] Clipboard text to push via WebSocket: {current_text}")
                    
                    # Отправляем через WebSocket если подключены
                    if ACCOUNT_WEBSOCKET:
                        try:
                            message = json.dumps({'type': 'push', 'content': current_text, 'device_id': get_device_id()})
                            await ACCOUNT_WEBSOCKET.send(message)
                            append_clipboard_log(f"[{datetime_now_str()}] ✓ Отправлен текст через WebSocket: {current_text[:80]}")
                        except Exception as e:
                            append_clipboard_log(f"[{datetime_now_str()}] ✗ Ошибка WebSocket: {e}")
            except Exception:
                pass
                
        except Exception as e:
            append_clipboard_log(f"[{datetime_now_str()}] ✗ Ошибка мониторинга буфера: {e}")
            await asyncio.sleep(1)
    
    append_clipboard_log(f"[{datetime_now_str()}] Мониторинг буфера для аккаунта остановлен")

async def clipboard_handler(websocket):
    """Handle clipboard sync for a connected client (legacy local mode)"""
    global CLIPBOARD_CLIENTS
    CLIPBOARD_CLIENTS.add(websocket)
    client_addr = websocket.remote_address
    ts = datetime_now_str()
    append_clipboard_log(f"[{ts}] Клиент подключён: {client_addr}. Всего клиентов: {len(CLIPBOARD_CLIENTS)}")
    try:
        async for message in websocket:
            try:
                data = json.loads(message)
                msg_type = data.get('type', '')
                append_clipboard_log(f"[{datetime_now_str()}] Получено сообщение типа: {msg_type} от {client_addr}")
                
                if msg_type == 'text':
                    text = data.get('content', '')
                    if text:
                        try:
                            pyperclip.copy(text)
                            ts = datetime_now_str()
                            append_clipboard_log(f"[{ts}] ✓ Текст установлен в буфер ПК: {text[:80]}")
                        except Exception as e:
                            append_clipboard_log(f"[{datetime_now_str()}] ✗ Ошибка установки текста в буфер: {e}")
                            import traceback
                            append_clipboard_log(traceback.format_exc())
                        # broadcast to other clients
                        if len(CLIPBOARD_CLIENTS) > 1:
                            await asyncio.gather(*[client.send(message) for client in CLIPBOARD_CLIENTS if client != websocket], return_exceptions=True)
                
                elif msg_type == 'image':
                    img_data = data.get('content', '')
                    if img_data:
                        try:
                            size_b64_kb = len(img_data) / 1024
                            append_clipboard_log(f"[{datetime_now_str()}] Получено изображение (base64: {size_b64_kb:.1f} КБ)")
                            
                            img_bytes = base64.b64decode(img_data)
                            size_kb = len(img_bytes) / 1024
                            append_clipboard_log(f"[{datetime_now_str()}] Декодировано: {size_kb:.1f} КБ")
                            
                            img = Image.open(BytesIO(img_bytes))
                            append_clipboard_log(f"[{datetime_now_str()}] Изображение: {img.size}, {img.mode}")
                            
                            # save temp
                            timestamp_str = time.strftime("%Y%m%d_%H%M%S")
                            temp_path = os.path.join(SCRIPT_DIR, f'clipboard_img_{timestamp_str}.png')
                            img.save(temp_path)
                            append_clipboard_log(f"[{datetime_now_str()}] Сохранено: {temp_path}")
                            
                            # copy to clipboard
                            append_clipboard_log(f"[{datetime_now_str()}] Копирование в буфер обмена ПК...")
                            try:
                                if copy_image_to_clipboard(temp_path):
                                    append_clipboard_log(f"[{datetime_now_str()}] ✓ Изображение скопировано в буфер обмена ПК!")
                                else:
                                    append_clipboard_log(f"[{datetime_now_str()}] ✗ Не удалось скопировать в буфер")
                            except Exception as copy_err:
                                append_clipboard_log(f"[{datetime_now_str()}] ✗ Ошибка при копировании: {copy_err}")
                            
                            # broadcast to others
                            if len(CLIPBOARD_CLIENTS) > 1:
                                await asyncio.gather(*[client.send(message) for client in CLIPBOARD_CLIENTS if client != websocket], return_exceptions=True)
                        except Exception as e:
                            append_clipboard_log(f"[{datetime_now_str()}] ✗ Ошибка обработки изображения: {e}")
                            import traceback
                            append_clipboard_log(traceback.format_exc())
            except json.JSONDecodeError as e:
                append_clipboard_log(f"[{datetime_now_str()}] ✗ JSON ошибка от {client_addr}: {e}")
            except Exception as e:
                append_clipboard_log(f"[{datetime_now_str()}] ✗ Ошибка обработки сообщения от {client_addr}: {e}")
                import traceback
                append_clipboard_log(traceback.format_exc())
    except Exception as e:
        append_clipboard_log(f"[{datetime_now_str()}] ✗ Ошибка соединения с {client_addr}: {e}")
    finally:
        CLIPBOARD_CLIENTS.discard(websocket)
        append_clipboard_log(f"[{datetime_now_str()}] Клиент отключён: {client_addr}. Всего клиентов: {len(CLIPBOARD_CLIENTS)}")

async def monitor_clipboard_async():
    """Monitor PC clipboard changes and send to clients"""
    last_clipboard_text = None
    last_clipboard_image_hash = None
    append_clipboard_log(f"[{datetime_now_str()}] Мониторинг буфера обмена запущен")
    while CLIPBOARD_RUNNING:
        try:
            await asyncio.sleep(0.5)
            # Text
            try:
                current_text = pyperclip.paste()
            except Exception as e:
                # append_clipboard_log(f"[{datetime_now_str()}] Ошибка чтения текста: {e}")
                current_text = None
            
            if current_text and current_text != last_clipboard_text:
                last_clipboard_text = current_text
                ts = datetime_now_str()
                append_clipboard_log(f"[{ts}] ✓ Буфер ПК изменён (текст): {current_text[:120]}")
                if CLIPBOARD_CLIENTS:
                    message = json.dumps({'type': 'text', 'content': current_text})
                    results = await asyncio.gather(*[client.send(message) for client in CLIPBOARD_CLIENTS], return_exceptions=True)
                    append_clipboard_log(f"[{datetime_now_str()}] Отправлено {len([r for r in results if not isinstance(r, Exception)])} клиентам")
            
            # Image (if supported)
            if WIN32_CLIPBOARD:
                try:
                    current_image = get_image_from_clipboard()
                    if current_image:
                        # Prepare image bytes (JPEG optimized) and compute signature
                        buffer = BytesIO()
                        img_to_send = current_image
                        max_size = 2000
                        if current_image.size[0] > max_size or current_image.size[1] > max_size:
                            ratio = min(max_size / current_image.size[0], max_size / current_image.size[1])
                            new_size = (int(current_image.size[0] * ratio), int(current_image.size[1] * ratio))
                            img_to_send = current_image.resize(new_size, Image.Resampling.LANCZOS)
                            append_clipboard_log(f"[{datetime_now_str()}] Изображение уменьшено с {current_image.size} до {new_size}")

                        img_to_send.save(buffer, format='JPEG', quality=85, optimize=True)
                        img_bytes = buffer.getvalue()
                        sig = hashlib.md5(img_bytes).hexdigest()

                        # If same signature as last sent, skip
                        if sig != last_clipboard_image_hash:
                            last_clipboard_image_hash = sig

                            # If we recently programmatically set this image (e.g., from screenshot()), skip broadcast
                            global CLIPBOARD_IGNORE_IMAGE
                            if CLIPBOARD_IGNORE_IMAGE is not None and CLIPBOARD_IGNORE_IMAGE.get('sig') == sig and (time.time() - CLIPBOARD_IGNORE_IMAGE.get('ts', 0)) < 3:
                                append_clipboard_log(f"[{datetime_now_str()}] Пропуск отправки: изображение было установлено локально программно")
                                # clear the flag to avoid permanent ignore
                                CLIPBOARD_IGNORE_IMAGE = None
                            else:
                                img_b64 = base64.b64encode(img_bytes).decode('utf-8')
                                ts = datetime_now_str()
                                size_kb = len(img_bytes) / 1024
                                append_clipboard_log(f"[{ts}] ✓ Обнаружено изображение в буфере ПК: {current_image.size}, размер: {size_kb:.1f} КБ")
                                if CLIPBOARD_CLIENTS:
                                    append_clipboard_log(f"[{datetime_now_str()}] Отправка изображения ({len(img_b64)/1024:.1f} КБ)...")
                                    results = await asyncio.gather(*[client.send(json.dumps({'type': 'image', 'content': img_b64})) for client in CLIPBOARD_CLIENTS], return_exceptions=True)
                                    success_count = len([r for r in results if not isinstance(r, Exception)])
                                    append_clipboard_log(f"[{datetime_now_str()}] ✓ Отправлено {success_count} клиентам")
                except Exception as e:
                    # Не логируем каждый раз, когда нет изображения
                    pass
        except Exception as e:
            append_clipboard_log(f"[{datetime_now_str()}] ✗ Ошибка мониторинга буфера: {e}")
            import traceback
            append_clipboard_log(traceback.format_exc())
            await asyncio.sleep(1)
    append_clipboard_log(f"[{datetime_now_str()}] Мониторинг буфера остановлен")

async def start_clipboard_server_async(host="0.0.0.0", port=8765):
    """Start WebSocket clipboard sync server (async)"""
    global CLIPBOARD_TASK
    local_host = get_local_host_addr()
    append_clipboard_log("")
    append_clipboard_log("═" * 50)
    append_clipboard_log(f"Сервер буфера запущен на ws://{local_host}:{port}")
    append_clipboard_log("═" * 50)
    
    try:
        # Увеличиваем лимит размера сообщения до 10 МБ
        server = await websockets.serve(clipboard_handler, host, port, max_size=10 * 1024 * 1024)
        CLIPBOARD_TASK = asyncio.create_task(monitor_clipboard_async())
        
        try:
            # run until explicitly cancelled
            while CLIPBOARD_RUNNING:
                await asyncio.sleep(0.5)
        finally:
            # cancel monitor first
            if CLIPBOARD_TASK:
                CLIPBOARD_TASK.cancel()
                try:
                    await CLIPBOARD_TASK
                except asyncio.CancelledError:
                    pass
            
            # Close server
            server.close()
            await server.wait_closed()
            
    except Exception as e:
        append_clipboard_log(f"[{datetime_now_str()}] ✗ Ошибка сервера: {e}")
        raise

def run_clipboard_server_thread(host="0.0.0.0", port=8765):
    """Run clipboard server in its own thread with its own loop"""
    global CLIPBOARD_LOOP, CLIPBOARD_RUNNING, CLIPBOARD_SERVER_TASK
    if CLIPBOARD_RUNNING:
        return
    CLIPBOARD_RUNNING = True
    def _runner():
        global CLIPBOARD_LOOP
        CLIPBOARD_LOOP = asyncio.new_event_loop()
        asyncio.set_event_loop(CLIPBOARD_LOOP)
        try:
            CLIPBOARD_LOOP.run_until_complete(start_clipboard_server_async(host, port))
        except Exception as e:
            append_clipboard_log(f"[{datetime_now_str()}] Clipboard server error: {e}")
        finally:
            try:
                CLIPBOARD_LOOP.run_until_complete(CLIPBOARD_LOOP.shutdown_asyncgens())
            except Exception:
                pass
            CLIPBOARD_LOOP.close()
            CLIPBOARD_LOOP = None
            append_clipboard_log(f"[{datetime_now_str()}] Clipboard server thread finished")
    t = threading.Thread(target=_runner, daemon=True)
    t.start()
    append_clipboard_log(f"[{datetime_now_str()}] Старт сервера буфера (поток)")

def stop_clipboard_server():
    """Stop clipboard server gracefully"""
    global CLIPBOARD_RUNNING, CLIPBOARD_LOOP, ACCOUNT_AUTHENTICATED
    if not CLIPBOARD_RUNNING and not ACCOUNT_AUTHENTICATED:
        append_clipboard_log(f"[{datetime_now_str()}] Сервер буфера не запущен")
        return
    
    # Stop local server
    if CLIPBOARD_RUNNING:
        CLIPBOARD_RUNNING = False
        append_clipboard_log(f"[{datetime_now_str()}] Останавливаю локальный сервер буфера...")
        
        # Clear clients first
        CLIPBOARD_CLIENTS.clear()
        
        # Give event loop time to process
        time.sleep(0.1)
    
    # Stop account sync
    if ACCOUNT_AUTHENTICATED:
        stop_account_sync()

def start_account_sync():
    """Start account-based clipboard synchronization"""
    if not is_authenticated():
        append_clipboard_log(f"[{datetime_now_str()}] ✗ Необходимо войти в аккаунт для синхронизации")
        return False
    
    def _runner():
        global CLIPBOARD_LOOP
        CLIPBOARD_LOOP = asyncio.new_event_loop()
        asyncio.set_event_loop(CLIPBOARD_LOOP)
        try:
            CLIPBOARD_LOOP.run_until_complete(account_clipboard_client())
        except Exception as e:
            append_clipboard_log(f"[{datetime_now_str()}] ✗ Ошибка аккаунтной синхронизации: {e}")
        finally:
            CLIPBOARD_LOOP.close()
            CLIPBOARD_LOOP = None
    
    t = threading.Thread(target=_runner, daemon=True)
    t.start()
    append_clipboard_log(f"[{datetime_now_str()}] Старт аккаунтной синхронизации буфера обмена")
    return True

def stop_account_sync():
    """Stop account-based clipboard synchronization"""
    global ACCOUNT_AUTHENTICATED, ACCOUNT_WEBSOCKET
    if ACCOUNT_AUTHENTICATED:
        ACCOUNT_AUTHENTICATED = False
        append_clipboard_log(f"[{datetime_now_str()}] Останавливаю аккаунтную синхронизацию...")
        if ACCOUNT_WEBSOCKET:
            try:
                if CLIPBOARD_LOOP:
                    asyncio.run_coroutine_threadsafe(ACCOUNT_WEBSOCKET.close(), CLIPBOARD_LOOP)
            except Exception:
                pass
            ACCOUNT_WEBSOCKET = None

# Remote access (RustDesk launcher) functions
def start_remote_client():
    """Open RustDesk only (no custom remote client)."""
    global REMOTE_CLIENT_PROCESS, REMOTE_CLIENT_RUNNING

    if REMOTE_CLIENT_PROCESS and REMOTE_CLIENT_PROCESS.poll() is None:
        REMOTE_CLIENT_RUNNING = True
        append_remote_log("RustDesk уже открыт")
        return True

    try:
        rustdesk_cmd = shutil.which('rustdesk')
        if not rustdesk_cmd:
            append_remote_log("✗ RustDesk не найден в PATH")
            return False

        REMOTE_CLIENT_PROCESS = subprocess.Popen(
            [rustdesk_cmd],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW if platform.system() == 'Windows' else 0,
            start_new_session=True,
        )

        REMOTE_CLIENT_RUNNING = True
        append_remote_log(f"✓ RustDesk открыт (PID: {REMOTE_CLIENT_PROCESS.pid})")

        def watch_process():
            global REMOTE_CLIENT_RUNNING
            try:
                REMOTE_CLIENT_PROCESS.wait()
            except Exception:
                pass
            finally:
                REMOTE_CLIENT_RUNNING = False
                append_remote_log("RustDesk закрыт")

        threading.Thread(target=watch_process, daemon=True).start()
        return True

    except Exception as e:
        append_remote_log(f"✗ Ошибка запуска RustDesk: {e}")
        REMOTE_CLIENT_RUNNING = False
        return False

def stop_remote_client():
    """No-op: remote mode only opens RustDesk."""
    global REMOTE_CLIENT_PROCESS, REMOTE_CLIENT_RUNNING

    if REMOTE_CLIENT_PROCESS and REMOTE_CLIENT_PROCESS.poll() is None:
        append_remote_log("ℹ RustDesk уже открыт. Закройте окно RustDesk вручную при необходимости.")
        REMOTE_CLIENT_RUNNING = True
    else:
        REMOTE_CLIENT_RUNNING = False
        append_remote_log("ℹ RustDesk не запущен")

def append_remote_log(text: str):
    """Add text to remote access log"""
    global GUI_REMOTE_LOG
    timestamp = time.strftime("%H:%M:%S")
    log_text = f"[{timestamp}] {text}"
    print(log_text)
    if GUI_REMOTE_LOG:
        try:
            GUI_REMOTE_LOG.insert('end', log_text + '\n')
            GUI_REMOTE_LOG.see('end')
        except:
            pass

# ========== БУДИЛЬНИКИ И ТАЙМЕРЫ ==========
from datetime import datetime, timedelta
import winsound

def play_alarm_sound():
    """Воспроизвести звук будильника"""
    try:
        # Пробуем воспроизвести системный звук
        for _ in range(3):
            winsound.Beep(1000, 500)  # 1000 Hz, 500 ms
            time.sleep(0.2)
            winsound.Beep(1500, 500)  # 1500 Hz, 500 ms
            time.sleep(0.2)
    except:
        pass

def set_alarm(hour: int, minute: int, message: str = "Будильник!") -> dict:
    """Установить будильник на определённое время"""
    global ALARM_COUNTER, ALARMS
    
    now = datetime.now()
    alarm_time = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    
    # Если время уже прошло сегодня - ставим на завтра
    if alarm_time <= now:
        alarm_time += timedelta(days=1)
    
    ALARM_COUNTER += 1
    alarm = {
        'id': ALARM_COUNTER,
        'time': alarm_time,
        'message': message,
        'active': True
    }
    ALARMS.append(alarm)
    
    # Запускаем поток проверки если не запущен
    start_alarm_timer_thread()
    
    time_str = alarm_time.strftime("%H:%M")
    return {'success': True, 'id': ALARM_COUNTER, 'time': time_str, 'message': f'Будильник установлен на {time_str}'}

def set_timer(minutes: int = 0, seconds: int = 0, message: str = "Таймер завершён!") -> dict:
    """Установить таймер на определённое количество минут/секунд"""
    global TIMER_COUNTER, TIMERS
    
    total_seconds = minutes * 60 + seconds
    if total_seconds <= 0:
        return {'success': False, 'message': 'Укажите время для таймера'}
    
    end_time = datetime.now() + timedelta(seconds=total_seconds)
    
    TIMER_COUNTER += 1
    timer = {
        'id': TIMER_COUNTER,
        'end_time': end_time,
        'duration': total_seconds,
        'message': message,
        'active': True
    }
    TIMERS.append(timer)
    
    # Запускаем поток проверки если не запущен
    start_alarm_timer_thread()
    
    if minutes > 0:
        time_str = f"{minutes} мин" + (f" {seconds} сек" if seconds > 0 else "")
    else:
        time_str = f"{seconds} сек"
    
    return {'success': True, 'id': TIMER_COUNTER, 'duration': time_str, 'message': f'Таймер установлен на {time_str}'}

def cancel_alarm(alarm_id: int = None) -> dict:
    """Отменить будильник"""
    global ALARMS
    
    if alarm_id is None:
        # Отменяем последний активный будильник
        for alarm in reversed(ALARMS):
            if alarm['active']:
                alarm['active'] = False
                return {'success': True, 'message': f'Будильник #{alarm["id"]} отменён'}
        return {'success': False, 'message': 'Нет активных будильников'}
    
    for alarm in ALARMS:
        if alarm['id'] == alarm_id and alarm['active']:
            alarm['active'] = False
            return {'success': True, 'message': f'Будильник #{alarm_id} отменён'}
    
    return {'success': False, 'message': f'Будильник #{alarm_id} не найден'}

def cancel_timer(timer_id: int = None) -> dict:
    """Отменить таймер"""
    global TIMERS
    
    if timer_id is None:
        # Отменяем последний активный таймер
        for timer in reversed(TIMERS):
            if timer['active']:
                timer['active'] = False
                return {'success': True, 'message': f'Таймер #{timer["id"]} отменён'}
        return {'success': False, 'message': 'Нет активных таймеров'}
    
    for timer in TIMERS:
        if timer['id'] == timer_id and timer['active']:
            timer['active'] = False
            return {'success': True, 'message': f'Таймер #{timer_id} отменён'}
    
    return {'success': False, 'message': f'Таймер #{timer_id} не найден'}

def list_alarms_timers() -> dict:
    """Получить список активных будильников и таймеров"""
    active_alarms = [a for a in ALARMS if a['active']]
    active_timers = [t for t in TIMERS if t['active']]
    
    result = []
    
    for alarm in active_alarms:
        result.append(f"⏰ Будильник #{alarm['id']}: {alarm['time'].strftime('%H:%M')} - {alarm['message']}")
    
    for timer in active_timers:
        remaining = (timer['end_time'] - datetime.now()).total_seconds()
        if remaining > 0:
            mins = int(remaining // 60)
            secs = int(remaining % 60)
            result.append(f"⏱️ Таймер #{timer['id']}: осталось {mins}:{secs:02d} - {timer['message']}")
    
    if not result:
        return {'success': True, 'list': [], 'message': 'Нет активных будильников и таймеров'}
    
    return {'success': True, 'list': result, 'message': '\n'.join(result)}

def alarm_timer_checker():
    """Поток проверки будильников и таймеров"""
    global ALARMS, TIMERS
    
    while True:
        try:
            now = datetime.now()
            
            # Проверяем будильники
            for alarm in ALARMS:
                if alarm['active'] and now >= alarm['time']:
                    alarm['active'] = False
                    trigger_alarm(alarm['message'], 'alarm')
            
            # Проверяем таймеры
            for timer in TIMERS:
                if timer['active'] and now >= timer['end_time']:
                    timer['active'] = False
                    trigger_alarm(timer['message'], 'timer')
            
            # Очищаем старые неактивные записи
            ALARMS = [a for a in ALARMS if a['active'] or (now - a['time']).total_seconds() < 3600]
            TIMERS = [t for t in TIMERS if t['active'] or (now - t['end_time']).total_seconds() < 3600]
            
            time.sleep(1)  # Проверяем каждую секунду
            
        except Exception as e:
            print(f"[ALARM] Ошибка в потоке проверки: {e}")
            time.sleep(5)

def trigger_alarm(message: str, alarm_type: str = 'alarm'):
    """Сработал будильник или таймер"""
    print(f"[{alarm_type.upper()}] {message}")
    
    # Воспроизводим звук в отдельном потоке
    threading.Thread(target=play_alarm_sound, daemon=True).start()
    
    # Озвучиваем сообщение
    speak(message)
    
    # Отправляем в Telegram если есть chat_id
    if TG_BOT and TG_LOOP and settings.get('last_chat_id'):
        try:
            emoji = "⏰" if alarm_type == 'alarm' else "⏱️"
            coro = TG_BOT.send_message(
                chat_id=settings['last_chat_id'],
                text=f"{emoji} {message}"
            )
            asyncio.run_coroutine_threadsafe(coro, TG_LOOP)
        except:
            pass
    
    # Показываем уведомление
    notify_success(message)

def start_alarm_timer_thread():
    """Запустить поток проверки будильников/таймеров если не запущен"""
    global ALARM_TIMER_THREAD
    
    if ALARM_TIMER_THREAD is None or not ALARM_TIMER_THREAD.is_alive():
        ALARM_TIMER_THREAD = threading.Thread(target=alarm_timer_checker, daemon=True)
        ALARM_TIMER_THREAD.start()
        print("[INFO] Поток будильников/таймеров запущен")

def parse_time_from_text(text: str) -> tuple:
    """Распарсить время из текста (возвращает hour, minute или minutes, seconds)"""
    import re
    
    text = text.lower()
    
    # Формат HH:MM или H:MM
    match = re.search(r'(\d{1,2}):(\d{2})', text)
    if match:
        return int(match.group(1)), int(match.group(2)), 'time'
    
    # "на 15:30" или "в 8 30"
    match = re.search(r'(?:на|в)\s*(\d{1,2})\s*(?::|часов?|ч)?\s*(\d{1,2})?\s*(?:минут)?', text)
    if match:
        hour = int(match.group(1))
        minute = int(match.group(2)) if match.group(2) else 0
        return hour, minute, 'time'
    
    # "через X минут" или "на X минут"
    match = re.search(r'(?:через|на)\s*(\d+)\s*(?:минут|мин)', text)
    if match:
        return int(match.group(1)), 0, 'duration'
    
    # "через X секунд" или "на X секунд"
    match = re.search(r'(?:через|на)\s*(\d+)\s*(?:секунд|сек)', text)
    if match:
        return 0, int(match.group(1)), 'duration'
    
    # Просто число - считаем минуты для таймера
    match = re.search(r'(\d+)', text)
    if match:
        return int(match.group(1)), 0, 'duration'
    
    return None, None, None

# helper for logging clipboard events to GUI/console
def datetime_now_str():
    return time.strftime("%H:%M:%S")

def append_clipboard_log(text: str):
    # append text to GUI log if available (do not raise)
    try:
        if GUI_CLIPBOARD_LOG is not None:
            GUI_CLIPBOARD_LOG.insert('end', text + "\n")
            GUI_CLIPBOARD_LOG.see('end')
    except Exception:
        pass
    # also print to console
    print(text)

# ----------------- Utilities -----------------
def save_settings():
    settings['history'] = user_history[-500:]
    with open(SETTINGS_FILE, 'w', encoding='utf-8') as f:
        json.dump(settings, f, ensure_ascii=False, indent=2)


# ----------------- OpenAI integration -----------------
def _split_long_message(text: str, chunk_size: int = 3900):
    return [text[i:i+chunk_size] for i in range(0, len(text), chunk_size)]

def openai_call(question: str, timeout: int = 30) -> Optional[str]:
    """Call OpenAI API and return a textual answer or None on failure."""
    url_base = settings.get('openai_api_url', 'https://api.openai.com').rstrip('/')
    api_key = settings.get('openai_api_key')
    if not api_key:
        print("[OPENAI] No API key found")
        return None

    # OpenAI API endpoint
    endpoint = '/v1/chat/completions'
    url = url_base + endpoint
    
    headers = {
        'Content-Type': 'application/json',
        'Authorization': f'Bearer {api_key}'
    }
    
    payload = {
        'model': 'gpt-3.5-turbo',
        'messages': [
            {'role': 'user', 'content': question}
        ],
        'max_tokens': 1000,
        'temperature': 0.7
    }

    try:
        print(f"[OPENAI] Sending request to: {url}")
        print(f"[OPENAI] Question: {question}")
        
        resp = requests.post(url, json=payload, headers=headers, timeout=timeout)
        
        print(f"[OPENAI] Status code: {resp.status_code}")
        
        if resp.status_code == 200:
            try:
                j = resp.json()
                print(f"[OPENAI] Response JSON: {json.dumps(j, ensure_ascii=False, indent=2)}")
                
                # OpenAI response format
                if 'choices' in j and isinstance(j['choices'], list) and j['choices']:
                    first_choice = j['choices'][0]
                    if 'message' in first_choice and 'content' in first_choice['message']:
                        return first_choice['message']['content'].strip()
                    elif 'text' in first_choice:
                        return first_choice['text'].strip()
                
                print("[OPENAI] No valid content found in response")
                return None
                
            except Exception as e:
                print(f"[OPENAI] JSON parse error: {e}")
                return resp.text
        elif resp.status_code == 402:
            print("[OPENAI] Payment Required: Insufficient quota or credits")
            return None
        elif resp.status_code == 401:
            print("[OPENAI] Unauthorized: Invalid API key")
            return None
        elif resp.status_code == 404:
            print("[OPENAI] Not Found: Check API URL configuration")
            return None
        elif resp.status_code == 429:
            print("[OPENAI] Too Many Requests: Rate limit exceeded")
            return None
        else:
            print(f"[OPENAI] Error response ({resp.status_code}): {resp.text}")
            return None
            
    except Exception as e:
        print(f"[OPENAI] Request error: {e}")
        return None


def openai_vision_call(question: str, image_base64: str, timeout: int = 30) -> Optional[str]:
    """Call OpenAI Vision API with an image and return a textual answer or None on failure."""
    url_base = settings.get('openai_api_url', 'https://api.openai.com').rstrip('/')
    api_key = settings.get('openai_api_key')
    if not api_key:
        print("[OPENAI-VISION] No API key found")
        return None

    # OpenAI API endpoint
    endpoint = '/v1/chat/completions'
    url = url_base + endpoint
    
    headers = {
        'Content-Type': 'application/json',
        'Authorization': f'Bearer {api_key}'
    }
    
    payload = {
        'model': 'gpt-4-vision-preview',  # or 'gpt-4-turbo' with vision
        'messages': [
            {
                'role': 'user', 
                'content': [
                    {'type': 'text', 'text': question},
                    {
                        'type': 'image_url',
                        'image_url': {
                            'url': f"data:image/jpeg;base64,{image_base64}"
                        }
                    }
                ]
            }
        ],
        'max_tokens': 1000,
        'temperature': 0.7
    }

    try:
        print(f"[OPENAI-VISION] Sending image request to: {url}")
        print(f"[OPENAI-VISION] Question: {question}")
        print(f"[OPENAI-VISION] Image size: {len(image_base64)} chars")
        
        resp = requests.post(url, json=payload, headers=headers, timeout=timeout)
        
        print(f"[OPENAI-VISION] Status code: {resp.status_code}")
        
        if resp.status_code == 200:
            try:
                j = resp.json()
                print(f"[OPENAI-VISION] Response JSON: {json.dumps(j, ensure_ascii=False, indent=2)}")
                
                # OpenAI response format
                if 'choices' in j and isinstance(j['choices'], list) and j['choices']:
                    first_choice = j['choices'][0]
                    if 'message' in first_choice and 'content' in first_choice['message']:
                        return first_choice['message']['content'].strip()
                
                print("[OPENAI-VISION] No valid content found in response")
                return None
                
            except Exception as e:
                print(f"[OPENAI-VISION] JSON parse error: {e}")
                return resp.text
        elif resp.status_code == 402:
            print("[OPENAI-VISION] Payment Required: Insufficient quota or credits")
            return None
        elif resp.status_code == 401:
            print("[OPENAI-VISION] Unauthorized: Invalid API key")
            return None
        elif resp.status_code == 404:
            print("[OPENAI-VISION] Not Found: Check API URL configuration or model availability")
            return None
        elif resp.status_code == 429:
            print("[OPENAI-VISION] Too Many Requests: Rate limit exceeded")
            return None
        else:
            print(f"[OPENAI-VISION] Error response ({resp.status_code}): {resp.text}")
            return None
            
    except Exception as e:
        print(f"[OPENAI-VISION] Request error: {e}")
        return None


def openai_query(question: str, source: str = 'local', tg_chat_id: Optional[int] = None):
    """High-level wrapper: send question to OpenAI, speak/print/send result.
    """
    if not question:
        notify_error('Пустой вопрос для OpenAI')
        return

    notify_success('Отправляю запрос в OpenAI...')
    ans = None
    try:
        ans = openai_call(question)
    except Exception as e:
        notify_error('Ошибка при обращении к OpenAI: ' + str(e))
        return

    if not ans:
        notify_error('Не удалось получить ответ от OpenAI. Возможные причины: недостаточно средств на аккаунте, неверный API-ключ, или проблемы с сетью.')
        return

    # Speak and log
    def speak_response():
        try:
            speak(ans)
        except Exception:
            pass
    
    # Run TTS in background thread so it can be interrupted
    threading.Thread(target=speak_response, daemon=True).start()

    notify_success('OpenAI ответ получен')
    try:
        print('[OpenAI]', ans)
    except Exception:
        pass

    # Send to telegram if requested
    if source == 'telegram' and tg_chat_id and TG_BOT and TG_LOOP:
        parts = _split_long_message(ans)
        for p in parts:
            coro = TG_BOT.send_message(chat_id=tg_chat_id, text=p)
            try:
                asyncio.run_coroutine_threadsafe(coro, TG_LOOP)
            except Exception:
                pass
    else:
        # show in GUI log if present
        try:
            if GUI_LOG_TEXT is not None:
                GUI_LOG_TEXT.insert('end', f"[OpenAI] {ans}\n")
                GUI_LOG_TEXT.see('end')
        except Exception:
            pass


def openai_image_query(question: str, source: str = 'local'):
    """Query OpenAI Vision API with image from clipboard."""
    print("[OPENAI-IMAGE] Начинаю обработку изображения...")
    
    # Get image from clipboard
    img = get_image_from_clipboard()
    if img is None:
        notify_error('Нет изображения в буфере обмена')
        return
    
    # Convert image to base64
    try:
        # Resize image if too large (OpenAI has limits)
        max_size = 1024
        if img.width > max_size or img.height > max_size:
            print(f"[OPENAI-IMAGE] Изменяю размер изображения с {img.width}x{img.height}")
            img.thumbnail((max_size, max_size), Image.Resampling.LANCZOS)
            print(f"[OPENAI-IMAGE] Новый размер: {img.width}x{img.height}")
        
        # Convert to RGB if needed
        if img.mode in ('RGBA', 'LA'):
            background = Image.new('RGB', img.size, (255, 255, 255))
            if img.mode == 'RGBA':
                background.paste(img, mask=img.split()[3])  # 3 is the alpha channel
            else:  # LA
                background.paste(img, mask=img.split()[1])  # 1 is the alpha channel
            img = background
        elif img.mode != 'RGB':
            img = img.convert('RGB')
        
        # Save to BytesIO as JPEG
        from io import BytesIO
        import base64
        
        buffer = BytesIO()
        img.save(buffer, format='JPEG', quality=85)
        image_bytes = buffer.getvalue()
        image_base64 = base64.b64encode(image_bytes).decode('utf-8')
        
        print(f"[OPENAI-IMAGE] Изображение готово: {len(image_base64)} символов base64")
        
    except Exception as e:
        notify_error(f'Ошибка при обработке изображения: {e}')
        return
    
    # Send to OpenAI Vision
    notify_success('Отправляю изображение в OpenAI...')
    ans = None
    try:
        ans = openai_vision_call(question, image_base64)
    except Exception as e:
        notify_error('Ошибка при обращении к OpenAI Vision: ' + str(e))
        return

    if not ans:
        notify_error('Не удалось получить ответ от OpenAI Vision. Возможные причины: недостаточно средств на аккаунте, неверный API-ключ, или модель не поддерживает изображения.')
        return

    # Speak and log
    def speak_response():
        try:
            speak(ans)
        except Exception:
            pass
    
    # Run TTS in background thread so it can be interrupted
    threading.Thread(target=speak_response, daemon=True).start()

    notify_success('OpenAI Vision ответ получен')
    try:
        print('[OpenAI-Vision]', ans)
    except Exception:
        pass

    # Show in GUI log if present
    try:
        if GUI_LOG_TEXT is not None:
            GUI_LOG_TEXT.insert('end', f"[OpenAI-Vision] {ans}\n")
            GUI_LOG_TEXT.see('end')
    except Exception:
        pass


# ----------------- TTS (SAPI via win32com) - integrated from ozvuchka.py -----------------
try:
    import win32com.client
except Exception as e:
    win32com = None
    print("[WARN] win32com.client не доступен. Установите pywin32 (pip install pywin32).")

def init_sapi_voice():
    """Инициализировать объект SAPI.SpVoice, вернуть None при ошибке."""
    if win32com is None:
        return None
    try:
        speaker = win32com.client.Dispatch("SAPI.SpVoice")
        # Попробуем выбрать русскоязычный голос (Ирина/Irina/Pavel)
        try:
            for v in speaker.GetVoices():
                vid = str(v.GetDescription())
                if ("Ирина" in vid) or ("Irina" in vid) or ("Pavel" in vid) or ("Михаил" in vid):
                    try:
                        speaker.Voice = v
                    except Exception:
                        pass
                    break
        except Exception:
            pass
        # Rate: -10..10, volume 0..100
        try:
            speaker.Rate = 0
            speaker.Volume = 100
        except Exception:
            pass
        return speaker
    except Exception as e:
        print("[TTS INIT ERROR]", e)
        return None

SPEAKER = init_sapi_voice()

def speak(text: str):
    """Озвучить текст через SAPI (win32com). Если SAPI недоступен — вывести в консоль."""
    global tts_in_progress, tts_should_stop
    
    if not text:
        return
    
    # лог в консоль
    print("[TTS]", text)
    
    with tts_thread_lock:
        if tts_should_stop:
            print("[TTS] Остановлено пользователем")
            return
        tts_in_progress = True
    
    try:
        if SPEAKER:
            try:
                # Split long text into words for more frequent interruption checks
                words = text.split()
                current_sentence = []
                
                for word in words:
                    with tts_thread_lock:
                        if tts_should_stop:
                            print("[TTS] Прервано пользователем")
                            break
                    
                    current_sentence.append(word)
                    
                    # Speak every 5-7 words or at sentence end
                    if (len(current_sentence) >= 5 or 
                        word.endswith('.') or word.endswith('!') or word.endswith('?')):
                        
                        sentence_text = ' '.join(current_sentence)
                        SPEAKER.Speak(sentence_text)
                        current_sentence = []
                        
                        # Small pause between chunks to allow interruption
                        time.sleep(0.1)
                
                # Speak remaining words
                if current_sentence:
                    with tts_thread_lock:
                        if not tts_should_stop:
                            SPEAKER.Speak(' '.join(current_sentence))
                    
            except Exception as e:
                print("[TTS ERROR]", e)
        else:
            # fallback: вызвать PowerShell для синтеза (медленнее)
            try:
                with tts_thread_lock:
                    if tts_should_stop:
                        return
                ps = f'Add-Type –AssemblyName System.Speech; $s = New-Object System.Speech.Synthesis.SpeechSynthesizer; $s.Speak("{text}")'
                subprocess.Popen(["powershell", "-Command", ps], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except Exception:
                pass
    finally:
        with tts_thread_lock:
            tts_in_progress = False
            # Don't reset tts_should_stop here - let stop_tts() handle it

def stop_tts():
    """Остановить текущее озвучивание"""
    global tts_should_stop
    
    with tts_thread_lock:
        tts_should_stop = True
        print("[TTS] Получена команда остановки")
        
        # Try to stop SAPI speaker immediately
        if SPEAKER:
            try:
                # Stop current speech
                SPEAKER.Skip("Sentence", 999999)  # Skip many sentences ahead
                print("[TTS] SAPI остановлен")
            except Exception as e:
                print(f"[TTS] Ошибка остановки SAPI: {e}")
        
        if tts_in_progress:
            # Reset the flag after a short delay
            def reset_flag():
                time.sleep(0.5)
                global tts_should_stop
                with tts_thread_lock:
                    tts_should_stop = False
            threading.Thread(target=reset_flag, daemon=True).start()
            return True  # TTS was actively running
        else:
            # Even if TTS is not currently active, we still accept the command
            # because user might have said it right after TTS finished
            print("[TTS] Команда остановки принята (TTS завершился недавно)")
            tts_should_stop = False  # Reset immediately if not running
            return True

# ----------------- Core actions -----------------
def notify_success(text: str):
    print('[OK]', text)
    # speak feedback (now via SAPI)
    try:
        speak(text)
    except Exception:
        pass
    # append to GUI log if present
    try:
        if GUI_LOG_TEXT is not None:
            GUI_LOG_TEXT.insert('end', f"[OK] {text}\n")
            GUI_LOG_TEXT.see('end')
    except Exception:
        pass
    # send to telegram if available and last_chat set
    if TG_BOT and settings.get('last_chat_id') and TG_LOOP:
        try:
            coro = TG_BOT.send_message(chat_id=settings['last_chat_id'], text=text)
            asyncio.run_coroutine_threadsafe(coro, TG_LOOP)
        except Exception:
            pass

def notify_error(text: str):
    print('[ERROR]', text)
    try:
        speak('Ошибка: ' + text)
    except Exception:
        pass
    # append to GUI log if present
    try:
        if GUI_LOG_TEXT is not None:
            GUI_LOG_TEXT.insert('end', f"[ERROR] {text}\n")
            GUI_LOG_TEXT.see('end')
    except Exception:
        pass
    if TG_BOT and settings.get('last_chat_id') and TG_LOOP:
        try:
            coro = TG_BOT.send_message(chat_id=settings['last_chat_id'], text='Ошибка: ' + text)
            asyncio.run_coroutine_threadsafe(coro, TG_LOOP)
        except Exception:
            pass

def open_youtube():
    try:
        webbrowser.open('https://www.youtube.com')
        user_history.append('open_youtube')
        save_settings()
        notify_success('YouTube открыт')
    except Exception as e:
        notify_error('Не удалось открыть YouTube: ' + str(e))

def google_search(query: str):
    try:
        if not query:
            notify_error('Пустой запрос')
            return
        url = 'https://www.google.com/search?q=' + query.replace(' ', '+')
        webbrowser.open(url)
        user_history.append('google_search:' + query)
        save_settings()
        notify_success('Поиск в Google выполнен')
    except Exception as e:
        notify_error('Ошибка поиска: ' + str(e))

def type_text(text: str, press_enter: bool = False):
    try:
        pyperclip.copy(text)
        keyboard.press_and_release('ctrl+v')
        if press_enter:
            pyautogui.press('enter')
        user_history.append('type:' + text)
        save_settings()
        notify_success('Текст введён')
    except Exception as e:
        notify_error('Ошибка при вводе текста: ' + str(e))

def close_all_except_assistant(grace_timeout: float = 2.0):
    """
    Попытаться закрыть все процессы текущего пользователя, кроме самой Милы.
    Завершается мягко (terminate), а оставшиеся процессы форсируются (kill).
    """
    current_pid = os.getpid()
    whitelist = {
        'assistant.exe',
        'mila.exe',
        'python.exe',
        'pythonw.exe',
        os.path.basename(sys.argv[0]).lower(),
        os.path.basename(sys.executable).lower() if sys.executable else '',
    }
    try:
        whitelist.add(psutil.Process(current_pid).name().lower())
    except Exception:
        pass
    whitelist = {name for name in whitelist if name}

    current_username = None
    try:
        current_username = psutil.Process(current_pid).username()
    except Exception:
        pass

    to_wait = []
    for proc in psutil.process_iter(['pid', 'name', 'username']):
        try:
            if proc.info['pid'] == current_pid:
                continue
            if current_username and proc.info.get('username') and proc.info['username'] != current_username:
                continue
            name = (proc.info.get('name') or '').lower()
            if name in whitelist:
                continue
            proc.terminate()
            to_wait.append(proc)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue

    try:
        _, alive = psutil.wait_procs(to_wait, timeout=grace_timeout)
    except Exception:
        alive = []
    for proc in alive:
        try:
            proc.kill()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue

def _run_shutdown_command(mode: str) -> bool:
    cmd = ['shutdown', '/s', '/t', '0'] if mode == 'shutdown' else ['shutdown', '/r', '/t', '0']
    verb = 'выключить' if mode == 'shutdown' else 'перезагрузить'
    try:
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode == 0:
            return True
        err = (result.stderr or result.stdout or '').strip()
        notify_error(f"Не удалось {verb} через shutdown (код {result.returncode})" + (f": {err}" if err else ''))
    except Exception as e:
        notify_error(f"Не удалось {verb} через shutdown: {e}")
    # Fallback: try elevated shutdown (UAC prompt)
    try:
        params = '/s /t 0' if mode == 'shutdown' else '/r /t 0'
        rc = ctypes.windll.shell32.ShellExecuteW(None, 'runas', 'shutdown', params, None, 0)
        if rc <= 32:
            notify_error('Не удалось запустить shutdown с правами администратора.')
            return False
        return True
    except Exception as e:
        notify_error('Не удалось запустить shutdown с правами администратора: ' + str(e))
        return False

def system_shutdown():
    try:
        notify_success('Запускаю выключение компьютера.')
        print('[SYSTEM] Запускаю выключение компьютера.')
        _run_shutdown_command('shutdown')
    except Exception as e:
        notify_error('Не удалось выключить: ' + str(e))

def system_restart():
    try:
        notify_success('Запускаю перезагрузку компьютера.')
        print('[SYSTEM] Запускаю перезагрузку компьютера.')
        _run_shutdown_command('restart')
    except Exception as e:
        notify_error('Не удалось перезагрузить: ' + str(e))

def system_sleep():
    try:
        notify_success('Перевожу в спящий режим...')
        ctypes.windll.PowrProf.SetSuspendState(False, True, True)
    except Exception as e:
        notify_error('Не удалось отправить в сон: ' + str(e))

def _shutdown_with_voice_stop():
    try:
        stop_voice()
    except Exception:
        pass
    system_shutdown()

def _restart_with_voice_stop():
    try:
        stop_voice()
    except Exception:
        pass
    system_restart()

def _sleep_with_voice_stop():
    try:
        stop_voice()
    except Exception:
        pass
    system_sleep()

def volume_up():
    try:
        ctypes.windll.user32.keybd_event(VK_VOLUME_UP, 0, 0, 0)
        ctypes.windll.user32.keybd_event(VK_VOLUME_UP, 0, 2, 0)
        notify_success('Громкость увеличена')
    except Exception as e:
        notify_error('Не удалось увеличить громкость: ' + str(e))

def volume_down():
    try:
        ctypes.windll.user32.keybd_event(VK_VOLUME_DOWN, 0, 0, 0)
        ctypes.windll.user32.keybd_event(VK_VOLUME_DOWN, 0, 2, 0)
        notify_success('Громкость уменьшена')
    except Exception as e:
        notify_error('Не удалось уменьшить громкость: ' + str(e))

def volume_mute():
    try:
        ctypes.windll.user32.keybd_event(VK_VOLUME_MUTE, 0, 0, 0)
        ctypes.windll.user32.keybd_event(VK_VOLUME_MUTE, 0, 2, 0)
        notify_success('Звук переключён')
    except Exception as e:
        notify_error('Не удалось переключить звук: ' + str(e))

def press_space():
    try:
        pyautogui.press('space')
        notify_success('Пробел нажат')
    except Exception as e:
        notify_error('Не удалось нажать пробел: ' + str(e))

def press_enter():
    try:
        pyautogui.press('enter')
        notify_success('Enter нажат')
    except Exception as e:
        notify_error('Не удалось нажать Enter: ' + str(e))

def open_cmd():
    try:
        subprocess.Popen('start cmd', shell=True)
        notify_success('Открыт cmd')
    except Exception as e:
        notify_error('Ошибка при открытии cmd: ' + str(e))

def new_browser_tab():
    try:
        keyboard.send('ctrl+t')
        notify_success('Открыта новая вкладка')
    except Exception as e:
        notify_error('Не удалось открыть вкладку: ' + str(e))

def close_browser_tab():
    try:
        keyboard.send('ctrl+w')
        notify_success('Вкладка закрыта')
    except Exception as e:
        notify_error('Не удалось закрыть вкладку: ' + str(e))

def close_current_window():
    try:
        keyboard.send('alt+f4')
        notify_success('Окно закрыто')
    except Exception as e:
        notify_error('Не удалось закрыть окно: ' + str(e))


def switch_window_alt_tab():
    try:
        keyboard.send('alt+tab')
        notify_success('Переключение окна выполнено')
    except Exception as e:
        notify_error('Не удалось переключить окно: ' + str(e))


def open_file_explorer():
    try:
        keyboard.send('win+e')
        notify_success('Проводник открыт')
    except Exception as e:
        notify_error('Не удалось открыть проводник: ' + str(e))


def open_task_manager():
    try:
        keyboard.send('ctrl+shift+esc')
        notify_success('Диспетчер задач открыт')
    except Exception as e:
        notify_error('Не удалось открыть диспетчер задач: ' + str(e))

def screenshot_and_send(bot: Optional[Bot] = None, chat_id: Optional[int] = None):
    try:
        img = pyautogui.screenshot()
        bio = BytesIO()
        img.save(bio, format='PNG')
        bio.seek(0)
        png_data = bio.getvalue()
        user_history.append('screenshot')
        save_settings()

        # Send to Telegram if requested
        if bot and chat_id:
            try:
                input_file = BufferedInputFile(png_data, filename=f'screenshot_{int(time.time())}.png')
                coro = bot.send_photo(chat_id=chat_id, photo=input_file)
                if TG_LOOP:
                    asyncio.run_coroutine_threadsafe(coro, TG_LOOP)
            except Exception as e:
                notify_error('Не удалось отправить скриншот в Telegram: ' + str(e))

        # Try to copy to Windows clipboard (if available)
        try:
            copied = False
            if WIN32_CLIPBOARD:
                try:
                    copied = copy_pil_image_to_clipboard(img)
                    if copied:
                        append_clipboard_log(f"[{datetime_now_str()}] ✓ Скриншот скопирован в буфер ПК")
                except Exception:
                    copied = False
            # As fallback attempt, save temp file and use existing helper
            if not copied:
                try:
                    tmp = tempfile.NamedTemporaryFile(delete=False, suffix='.png')
                    tmp.write(png_data)
                    tmp.close()
                    copy_image_to_clipboard(tmp.name)
                    try:
                        os.unlink(tmp.name)
                    except Exception:
                        pass
                except Exception:
                    pass
        except Exception:
            pass

        # Broadcast to connected clipboard clients
        try:
            # prepare jpeg bytes for transfer (smaller)
            jb = BytesIO()
            img_j = img
            max_size = 2000
            if img.size[0] > max_size or img.size[1] > max_size:
                ratio = min(max_size / img.size[0], max_size / img.size[1])
                new_size = (int(img.size[0] * ratio), int(img.size[1] * ratio))
                img_j = img.resize(new_size, Image.Resampling.LANCZOS)
            img_j.save(jb, format='JPEG', quality=85, optimize=True)
            img_bytes = jb.getvalue()
            broadcast_image_bytes(img_bytes)
            append_clipboard_log(f"[{datetime_now_str()}] Скриншот отправлен клиентам (если подключены)")
        except Exception as e:
            append_clipboard_log(f"[{datetime_now_str()}] ✗ Ошибка при отправке скриншота клиентам: {e}")

        # Push screenshot to account sync so Android gets image through cloud channel.
        try:
            acc_buf = BytesIO()
            img_acc = img
            max_side = 1600
            if img_acc.size[0] > max_side or img_acc.size[1] > max_side:
                ratio = min(max_side / img_acc.size[0], max_side / img_acc.size[1])
                new_size = (max(1, int(img_acc.size[0] * ratio)), max(1, int(img_acc.size[1] * ratio)))
                img_acc = img_acc.resize(new_size, Image.Resampling.LANCZOS)
            if img_acc.mode != 'RGB':
                img_acc = img_acc.convert('RGB')
            img_acc.save(acc_buf, format='JPEG', quality=85, optimize=True)
            img_b64 = base64.b64encode(acc_buf.getvalue()).decode('utf-8')
            if push_content_via_account(img_b64, content_type='image'):
                append_clipboard_log(f"[{datetime_now_str()}] ✓ Скриншот отправлен в аккаунтный канал")
            else:
                append_clipboard_log(f"[{datetime_now_str()}] ✗ Не удалось отправить скриншот в аккаунтный канал")
        except Exception as e:
            append_clipboard_log(f"[{datetime_now_str()}] ✗ Ошибка отправки скриншота в аккаунтный канал: {e}")

        notify_success('Скриншот сделан')
    except Exception as e:
        notify_error('Не удалось сделать скриншот: ' + str(e))

def list_open_apps():
    procs = []
    try:
        for p in psutil.process_iter(['pid', 'name']):
            procs.append(f"{p.info.get('name')} (pid={p.info.get('pid')})")
        notify_success('Список процессов получен')
        return procs
    except Exception as e:
        notify_error('Не удалось получить список процессов: ' + str(e))
        return []

def close_app_by_name(name: str):
    name = name.lower().strip()
    closed = 0
    try:
        for p in psutil.process_iter(['pid', 'name']):
            try:
                pname = (p.info.get('name') or '').lower()
                if pname.startswith(name):
                    p.terminate()
                    closed += 1
            except Exception:
                pass
        notify_success(f'Закрыто {closed} процессов, начинающихся с \"{name}\"')
    except Exception as e:
        notify_error('Ошибка при закрытии приложения: ' + str(e))

def close_all_except(exceptions: List[str]):
    ex = [e.lower().strip() for e in exceptions]
    killed = 0
    try:
        for p in psutil.process_iter(['pid', 'name']):
            try:
                pname = (p.info.get('name') or '').lower()
                if not pname:
                    continue
                if any(pname.startswith(e) for e in ex):
                    continue
                if pname in ('system', 'system idle process', 'explorer.exe', 'svchost.exe'):
                    continue
                p.terminate()
                killed += 1
            except Exception:
                pass
        notify_success(f'Закрыто {killed} процессов (исключения: {exceptions})')
    except Exception as e:
        notify_error('Не удалось закрыть процессы: ' + str(e))

def clear_history():
    global user_history
    user_history = []
    save_settings()
    notify_success('История очищена')

def ctrl_combo(keys: List[str]):
    try:
        keyboard.send('ctrl+' + '+'.join(keys))
        notify_success('Выполнено Ctrl+' + '+'.join(keys))
    except Exception as e:
        notify_error('Не удалось выполнить Ctrl+комбинацию: ' + str(e))

def open_user_app(name: str):
    try:
        apps_file = _resolve_apps_file()
        if not os.path.exists(apps_file):
            notify_error('Файл app.txt не найден')
            return
        target = (name or '').strip().strip('"').strip("'").lower()
        if not target:
            notify_error('Не указано имя приложения')
            return
        candidates = []
        with open(apps_file, 'r', encoding='utf-8') as f:
            for line in f:
                if ',' in line:
                    cmd, path = line.split(',', 1)
                    cmd_norm = cmd.strip().lower()
                    path = os.path.expandvars(os.path.expanduser(path.strip().strip('"').strip("'")))
                    if not os.path.isabs(path):
                        path = os.path.join(SCRIPT_DIR, path)
                    candidates.append((cmd_norm, path))
        # try exact match first
        for cmd_norm, path in candidates:
            if cmd_norm == target:
                notify_success(f'Запускаю: {cmd_norm}')
                _launch_path(path)
                notify_success(f'Открыто приложение: {cmd_norm}')
                return
        # then startswith/contains tolerant match
        for cmd_norm, path in candidates:
            if cmd_norm.startswith(target) or target.startswith(cmd_norm) or target in cmd_norm:
                notify_success(f'Запускаю: {cmd_norm}')
                _launch_path(path)
                notify_success(f'Открыто приложение: {cmd_norm}')
                return
        # not found -> report available keys to help user
        available = ', '.join(cmd for cmd, _ in candidates[:20])
        notify_error('Команда приложения не найдена: ' + name + (f' (доступно: {available})' if available else ''))
    except Exception as e:
        notify_error('Не удалось открыть приложение: ' + str(e))

def _launch_path(path: str):
    raw_path = str(path or '').strip()
    normalized_path = raw_path.strip('"').strip("'")

    try:
        if IS_WINDOWS:
            # .lnk and many document-like targets should be opened via ShellExecute (os.startfile).
            if normalized_path:
                os.startfile(normalized_path)
                return
        if os.path.exists(normalized_path):
            os.startfile(normalized_path)
        else:
            subprocess.Popen(normalized_path, shell=True)
    except Exception:
        if IS_WINDOWS and normalized_path:
            try:
                subprocess.Popen(['cmd', '/c', 'start', '', normalized_path], shell=False)
                return
            except Exception:
                pass
        subprocess.Popen(normalized_path, shell=True)

def win_plus_digit(digit: str):
    try:
        digit_s = str(digit).strip()
        key_digit = '0' if digit_s in ('10', '0') else digit_s
        keyboard.send('win+' + key_digit)
        notify_success('Win+' + digit_s + ' выполнено')
    except Exception as e:
        notify_error('Не удалось выполнить Win+digit: ' + str(e))

def open_tab_by_number(num: str):
    """Opens browser tab by number using Ctrl+number"""
    try:
        keyboard.send('ctrl+' + num)
        notify_success('Вкладка ' + num + ' открыта')
    except Exception as e:
        notify_error('Не удалось открыть вкладку: ' + str(e))

def translate_clipboard(src='auto', dest='ru'):
    try:
        text = pyperclip.paste()
        if not text:
            notify_error('Буфер обмена пуст')
            return None
        if translator:
            res = translator.translate(text, src=src, dest=dest)
            pyperclip.copy(res.text)
            notify_success('Перевод скопирован в буфер')
            return res.text
        else:
            notify_error('Переводчик не установлен')
            return None
    except Exception as e:
        notify_error('Ошибка перевода: ' + str(e))
        return None

def screen_display():
    """Переключить дисплей (Win+P)"""
    try:
        # Press Win key
        ctypes.windll.user32.keybd_event(0x5B, 0, 0, 0)  # VK_LWIN down
        time.sleep(0.05)
        # Press P key
        ctypes.windll.user32.keybd_event(0x50, 0, 0, 0)  # VK_P down
        time.sleep(0.05)
        # Release P key
        ctypes.windll.user32.keybd_event(0x50, 0, 2, 0)  # VK_P up
        time.sleep(0.05)
        # Release Win key
        ctypes.windll.user32.keybd_event(0x5B, 0, 2, 0)  # VK_LWIN up
        notify_success('Переключение дисплея')
    except Exception as e:
        notify_error('Не удалось переключить дисплей: ' + str(e))

def minimize_all():
    """Минимизировать все окна (Win+D)"""
    try:
        # Press Win key
        ctypes.windll.user32.keybd_event(0x5B, 0, 0, 0)  # VK_LWIN down
        time.sleep(0.05)
        # Press D key
        ctypes.windll.user32.keybd_event(0x44, 0, 0, 0)  # VK_D down
        time.sleep(0.05)
        # Release D key
        ctypes.windll.user32.keybd_event(0x44, 0, 2, 0)  # VK_D up
        time.sleep(0.05)
        # Release Win key
        ctypes.windll.user32.keybd_event(0x5B, 0, 2, 0)  # VK_LWIN up
        notify_success('Все окна минимизированы')
    except Exception as e:
        notify_error('Не удалось минимизировать окна: ' + str(e))

# ----------------- Command handler (shared) -----------------
def ask_confirm_gui(title: str, message: str) -> Optional[str]:
    try:
        choice = messagebox.askyesnocancel(
            title,
            message + "\n\nНажмите Да/Нет или Отмена для ручного ввода да/нет.",
        )
        if choice is True:
            return 'yes'
        if choice is False:
            return 'no'
    except Exception:
        pass

    try:
        typed = simpledialog.askstring(title, message + "\n\nВведите вручную: да или нет")
    except Exception:
        return None

    return _confirmation_intent(typed or '')

CONFIRM_YES_WORDS = {
    'да', 'давай', 'выполняй', 'выполни', 'подтверждаю', 'согласен', 'ок', 'yes', 'ага'
}
CONFIRM_NO_WORDS = {
    'нет', 'отмена', 'не', 'не надо', 'отмени', 'отменить', 'нет спасибо', 'неа'
}

def _normalize_confirmation_text(text: str) -> str:
    txt = (text or '').lower().strip()
    if not txt:
        return ''
    txt = re.sub(r'[^0-9a-zа-яё\s]', ' ', txt, flags=re.IGNORECASE)
    txt = re.sub(r'\s+', ' ', txt).strip()
    return txt

def _confirmation_intent(text: str) -> Optional[str]:
    norm = _normalize_confirmation_text(text)
    if not norm:
        return None
    parts = norm.split()
    first = parts[0] if parts else ''
    if first in CONFIRM_YES_WORDS or norm in CONFIRM_YES_WORDS:
        return 'yes'
    if first in CONFIRM_NO_WORDS or norm in CONFIRM_NO_WORDS:
        return 'no'
    if any(w in parts for w in CONFIRM_YES_WORDS):
        return 'yes'
    if any(w in parts for w in CONFIRM_NO_WORDS):
        return 'no'
    return None

def handle_confirmation_response(answer: str, source='voice', chat_id: Optional[int] = None):
    """
    Handles voice reply or telegram inline keyboard 'Yes/No' for pending confirmation.
    This function runs the actual action that was pending.
    """
    global pending_confirmation, pending_origin
    global pending_deepseek, pending_deepseek_origin, pending_deepseek_chat_id
    if not pending_confirmation:
        return

    a = (answer or '').strip().lower()
    action = pending_confirmation
    origin = pending_origin
    pending_confirmation = None
    pending_origin = None

    if a in ('да', 'давай', 'выполняй', 'выполни', 'подтверждаю', 'yes', 'ага', 'подтверждаю действие', 'согласен', 'ок'):
        # proceed with action
        if action == 'shutdown':
            notify_success('Подтверждение получено. Выключаю компьютер.')
            speak('Подтверждение получено. Выключаю компьютер через 3 секунды.')
            time.sleep(3)
            try:
                stop_voice()
            except Exception:
                pass
            system_shutdown()
        elif action == 'restart':
            notify_success('Подтверждение получено. Перезагружаю компьютер.')
            speak('Подтверждение получено. Перезагружаю компьютер через 3 секунды.')
            time.sleep(3)
            try:
                stop_voice()
            except Exception:
                pass
            system_restart()
        elif action == 'sleep':
            notify_success('Подтверждение получено. Перевожу в спящий режим.')
            speak('Подтверждение получено. Перевожу в спящий режим через 3 секунды.')
            time.sleep(3)
            try:
                stop_voice()
            except Exception:
                pass
            system_sleep()
    else:
        # User said no or something else -> cancel
        notify_success('Действие отменено пользователем.')
        speak('Хорошо, действие отменено.')

def handle_command_text(text: str, source='local', tg_chat_id: Optional[int] = None, requester_device_id: Optional[str] = None):
    """
    Unified command handler. 'source' can be 'local', 'voice' or 'telegram'.
    """
    global pending_confirmation, pending_origin
    global pending_openai, pending_openai_origin, pending_openai_chat_id
    global last_command_was_think
    
    raw_text = (text or '').strip()
    t = raw_text.lower()
    if not t:
        return

    # Machine command from Android/account: request current clipboard snapshot.
    if raw_text.upper() == 'GET_CLIPBOARD':
        try:
            clipboard_text = pyperclip.paste() or ''
        except Exception:
            clipboard_text = ''

        if clipboard_text.strip():
            if push_content_via_account(clipboard_text, content_type='text', target_device_id=requester_device_id):
                notify_success('Текстовый буфер отправлен в аккаунт')
            else:
                notify_error('Не удалось отправить текстовый буфер через аккаунт')
            return

        clipboard_img = None
        try:
            clipboard_img = get_image_from_clipboard()
        except Exception:
            clipboard_img = None

        if clipboard_img is None:
            try:
                img_candidate = ImageGrab.grabclipboard()
                if hasattr(img_candidate, 'save'):
                    clipboard_img = img_candidate
            except Exception:
                clipboard_img = None

        if clipboard_img is not None:
            try:
                img_buf = BytesIO()
                img_to_send = clipboard_img.convert('RGB') if getattr(clipboard_img, 'mode', '') not in ('RGB', 'L') else clipboard_img

                # Reduce payload size for command-response path to improve /push reliability.
                max_side = 1600
                w, h = img_to_send.size
                if max(w, h) > max_side:
                    scale = max_side / float(max(w, h))
                    new_size = (max(1, int(w * scale)), max(1, int(h * scale)))
                    img_to_send = img_to_send.resize(new_size, Image.Resampling.LANCZOS)

                img_to_send.save(img_buf, format='JPEG', quality=85, optimize=True)
                img_b64 = base64.b64encode(img_buf.getvalue()).decode('utf-8')
                if push_content_via_account(img_b64, content_type='image', target_device_id=requester_device_id):
                    notify_success('Изображение из буфера отправлено в аккаунт')
                else:
                    notify_error('Не удалось отправить изображение из буфера через аккаунт')
            except Exception as exc:
                notify_error('Ошибка подготовки изображения из буфера: ' + str(exc))
            return

        notify_error('Буфер обмена пуст')
        return
    
    # Handle "мила стоп" command - works immediately after "подумай"
    if t in ('мила стоп', 'стоп', 'остановись', 'прекрати'):
        if last_command_was_think:
            stopped = stop_tts()
            print("[STOP] Остановка озвучивания по команде пользователя")
            last_command_was_think = False  # Reset flag after stopping
            speak("Озвучивание остановлено")
        else:
            speak("Команда стоп работает только после команды подумай")
        return
    
    # Reset the think flag only for OpenAI pending responses, not for other commands
    # This allows "стоп" to work until explicitly used
    
    user_history.append(t)
    save_settings()

    # If there is a pending confirmation and the user replies with yes/no via voice, route it
    if pending_confirmation and source in ('voice', 'local', 'telegram', 'android'):
        intent = _confirmation_intent(t)
        if intent:
            handle_confirmation_response('да' if intent == 'yes' else 'нет', source=source, chat_id=tg_chat_id)
            return

    # If there is a pending OpenAI question, accept the next utterance as the question
    with pending_openai_lock:
        if pending_openai:
            # Handle special keywords for clipboard integration
            if 'копия текста' in t or 'копия_текста' in t or t == 'копия текста':
                # Replace "копия текста" with actual clipboard text
                clipboard_text = pyperclip.paste()
                if clipboard_text:
                    t = t.replace('копия текста', clipboard_text).replace('копия_текста', clipboard_text)
                    if t.strip() == clipboard_text.strip():
                        # If the whole command was just "копия текста", use clipboard as question
                        q = clipboard_text
                    else:
                        # Replace the phrase within larger text
                        q = t
                else:
                    q = "В буфере обмена пусто"
                    
                pending_openai = False
                origin = pending_openai_origin
                chatid = pending_openai_chat_id
                pending_openai_origin = None
                pending_openai_chat_id = None
                # run openai in background thread so handler returns quickly
                threading.Thread(target=openai_query, args=(q, 'telegram' if origin == 'telegram' else source, chatid), daemon=True).start()
                return
                
            elif 'картинк' in t:
                # Handle image from clipboard + text question using OpenAI Vision API
                img = get_image_from_clipboard()
                if img:
                    # Remove "картинка/картинке/картинки" words from question text
                    text_question = t
                    text_question = re.sub(r'\bна\s+(картинк[а-я]*)\b', 'на изображении', text_question, flags=re.IGNORECASE)
                    text_question = re.sub(r'\bс\s+(картинк[а-я]*)\b', 'с изображения', text_question, flags=re.IGNORECASE)
                    text_question = re.sub(r'\bв\s+(картинк[а-я]*)\b', 'в изображении', text_question, flags=re.IGNORECASE) 
                    text_question = re.sub(r'\b(картинк[а-я]*)\b', 'изображение', text_question, flags=re.IGNORECASE)
                    text_question = text_question.strip()
                    
                    if not text_question:
                        text_question = "Опиши это изображение"
                    
                    pending_openai = False
                    origin = pending_openai_origin
                    chatid = pending_openai_chat_id
                    pending_openai_origin = None
                    pending_openai_chat_id = None
                    
                    # Send image to OpenAI Vision API in background thread
                    threading.Thread(target=openai_image_query, args=(text_question, 'telegram' if origin == 'telegram' else source), daemon=True).start()
                    return
                else:
                    # No image in clipboard
                    pending_openai = False
                    notify_error('Нет изображения в буфере обмена')
                    speak('Нет изображения в буфере обмена')
                    return
            else:
                # Regular question without special keywords
                q = t
                pending_openai = False
                origin = pending_openai_origin
                chatid = pending_openai_chat_id
                pending_openai_origin = None
                pending_openai_chat_id = None
                # run openai in background thread so handler returns quickly
                threading.Thread(target=openai_query, args=(q, 'telegram' if origin == 'telegram' else source, chatid), daemon=True).start()
                return

    try:
        # --- Activation helper: treat direct phrases flexibly ---
        # OpenAI activation: 'подумай' optionally followed by question.
        if t.startswith('подумай'):
            # Mark that the last command was "think"
            last_command_was_think = True
            
            rest = t[len('подумай'):].strip()
            if rest:
                # Handle special keywords in immediate question
                if 'копия текста' in rest or 'копия_текста' in rest:
                    # Replace "копия текста" with actual clipboard text
                    clipboard_text = pyperclip.paste()
                    if clipboard_text:
                        processed_rest = rest.replace('копия текста', clipboard_text).replace('копия_текста', clipboard_text)
                        threading.Thread(target=openai_query, args=(processed_rest, 'telegram' if source == 'telegram' else source, tg_chat_id), daemon=True).start()
                    else:
                        threading.Thread(target=openai_query, args=('В буфере обмена пусто', 'telegram' if source == 'telegram' else source, tg_chat_id), daemon=True).start()
                    return
                elif 'картинк' in rest:
                    # Handle image from clipboard + text question using OpenAI Vision API
                    img = get_image_from_clipboard()
                    if img:
                        # Remove "картинка/картинке/картинки" words from question text
                        text_question = rest
                        text_question = re.sub(r'\bна\s+(картинк[а-я]*)\b', 'на изображении', text_question, flags=re.IGNORECASE)
                        text_question = re.sub(r'\bс\s+(картинк[а-я]*)\b', 'с изображения', text_question, flags=re.IGNORECASE) 
                        text_question = re.sub(r'\bв\s+(картинк[а-я]*)\b', 'в изображении', text_question, flags=re.IGNORECASE)
                        text_question = re.sub(r'\b(картинк[а-я]*)\b', 'изображение', text_question, flags=re.IGNORECASE)
                        text_question = text_question.strip()
                        
                        if not text_question:
                            text_question = "Опиши это изображение"
                        
                        # Send image to OpenAI Vision API in background thread
                        threading.Thread(target=openai_image_query, args=(text_question, 'telegram' if source == 'telegram' else source), daemon=True).start()
                        return
                    else:
                        # No image in clipboard
                        notify_error('Нет изображения в буфере обмена')
                        speak('Нет изображения в буфере обмена')
                        return
                else:
                    # regular immediate question supplied: send to OpenAI
                    threading.Thread(target=openai_query, args=(rest, 'telegram' if source == 'telegram' else source, tg_chat_id), daemon=True).start()
                    return
            else:
                # ask user for the question (pending state)
                with pending_openai_lock:
                    pending_openai = True
                    pending_openai_origin = source
                    pending_openai_chat_id = tg_chat_id
                # prompt the user depending on source
                if source == 'telegram' and tg_chat_id and TG_BOT and TG_LOOP:
                    coro = TG_BOT.send_message(chat_id=tg_chat_id, text='Хорошо. Задайте, пожалуйста, вопрос для поиска (ответьте этим сообщением).')
                    try:
                        asyncio.run_coroutine_threadsafe(coro, TG_LOOP)
                    except Exception:
                        pass
                else:
                    speak('Хорошо. Задайте, пожалуйста, вопрос.')
                return
        # Voice control explicit phrases to avoid collision with power-off commands
        if any(x in t for x in ('голос', 'голосовое')):
            if any(x in t for x in ('выключи', 'отключи', 'останови', 'стоп')):
                stop_voice(); return
            if any(x in t for x in ('включи', 'запусти', 'активируй')):
                start_voice(); return
            if any(x in t for x in ('переключи', 'toggle', 'смен')):
                toggle_voice(); return

        # Commands that can be said in many forms: check keywords
        if 'youtube' in t or 'ютуб' in t or t.startswith('открой ютуб') or t.startswith('открой youtube'):
            open_youtube(); return

        if t.startswith('найди ') or t.startswith('поиск ') or 'найди' in t or 'поиск' in t:
            # get search phrase after keyword
            for kw in ('найди', 'поиск', 'поиск:'):
                if kw in t:
                    q = t.split(kw, 1)[1].strip()
                    if q:
                        google_search(q)
                        return
            # fallback
            google_search(t); return

        if t.startswith('напиши'):
            parts = t.split(' ', 1)
            if len(parts) > 1:
                text_to_type = parts[1]
                type_text(text_to_type, press_enter=False)
            else:
                # ask for text via GUI if local
                if source in ('local', 'voice'):
                    try:
                        inp = simpledialog.askstring('Напиши', 'Введите текст для ввода:')
                        if inp:
                            type_text(inp, press_enter=False)
                    except Exception:
                        pass
            return

        # system power actions require confirmation (avoid misfire on 'выключи звук')
        if (
            'выключи компьютер' in t or
            'выключить компьютер' in t or
            'выключай компьютер' in t or
            (("выключи" in t or 'выключение' in t or 'выключить' in t or 'выключай' in t)
             and 'звук' not in t and 'мут' not in t and 'голос' not in t and 'голосовое' not in t)
        ):
            if source == 'telegram':
                ask_confirm_tg(tg_chat_id, 'shutdown')
            elif source == 'local':
                pending_confirmation = 'shutdown'
                pending_origin = source
                intent = ask_confirm_gui('Подтверждение', 'Вы желаете выключить компьютер?')
                handle_confirmation_response('да' if intent == 'yes' else 'нет', source=source, chat_id=tg_chat_id)
            else:
                pending_confirmation = 'shutdown'
                pending_origin = source
                print('[INFO] Запрос на выключение компьютера получен')
                # Новая озвучка подтверждения (SAPI)
                speak('Вы желаете выключить компьютер? Скажите да для подтверждения или нет для отмены.')
            return

        if any(x in t for x in ('перезагрузи', 'перезагрузка', 'перезагрузить')):
            if source == 'telegram':
                ask_confirm_tg(tg_chat_id, 'restart')
            elif source == 'local':
                pending_confirmation = 'restart'
                pending_origin = source
                intent = ask_confirm_gui('Подтверждение', 'Вы хотите перезагрузить компьютер?')
                handle_confirmation_response('да' if intent == 'yes' else 'нет', source=source, chat_id=tg_chat_id)
            else:
                pending_confirmation = 'restart'
                pending_origin = source
                print('[INFO] Запрос на перезагрузку компьютера получен')
                # Воспроизводим вопрос о подтверждении
                speak('Внимание! Вы хотите перезагрузить компьютер. Подтвердите действие. Скажите да для перезагрузки, или нет для отмены.')
            return

        if any(x in t for x in ('сон', 'усни', 'спящий', 'спящий режим', 'в сон')):
            system_sleep()
            return

        # volume
        if any(x in t for x in ('громче', 'увеличь громкость', 'больше громкости')):
            volume_up(); return
        if any(x in t for x in ('тише', 'уменьши громкость', 'потише')):
            volume_down(); return
        if any(x in t for x in ('выключи звук', 'включи без звука', 'мут', 'mute')):
            volume_mute(); return

        # ========== БУДИЛЬНИКИ И ТАЙМЕРЫ ==========
        # Установка будильника: "поставь будильник на 7:30", "будильник на 8 часов"
        if any(x in t for x in ('будильник', 'разбуди', 'буди меня')):
            if any(x in t for x in ('отмени', 'удали', 'выключи', 'сними', 'убери')):
                result = cancel_alarm()
                speak(result['message'])
                if source == 'telegram' and tg_chat_id:
                    send_tg_message(tg_chat_id, result['message'])
                return
            
            if any(x in t for x in ('список', 'покажи', 'какие')):
                result = list_alarms_timers()
                speak(result['message'] if result['list'] else 'Нет активных будильников')
                if source == 'telegram' and tg_chat_id:
                    send_tg_message(tg_chat_id, result['message'])
                return
            
            # Парсим время
            hour, minute, time_type = parse_time_from_text(t)
            if hour is not None and time_type == 'time':
                result = set_alarm(hour, minute)
                speak(result['message'])
                if source == 'telegram' and tg_chat_id:
                    send_tg_message(tg_chat_id, f"⏰ {result['message']}")
            else:
                speak('Укажите время для будильника, например: будильник на 7:30')
                if source == 'telegram' and tg_chat_id:
                    send_tg_message(tg_chat_id, 'Укажите время для будильника, например: будильник на 7:30')
            return
        
        # Установка таймера: "таймер на 5 минут", "поставь таймер на 30 секунд"
        if any(x in t for x in ('таймер', 'засеки', 'отсчёт', 'отсчет')):
            if any(x in t for x in ('отмени', 'удали', 'выключи', 'останови', 'стоп')):
                result = cancel_timer()
                speak(result['message'])
                if source == 'telegram' and tg_chat_id:
                    send_tg_message(tg_chat_id, result['message'])
                return
            
            if any(x in t for x in ('список', 'покажи', 'какие')):
                result = list_alarms_timers()
                speak(result['message'] if result['list'] else 'Нет активных таймеров')
                if source == 'telegram' and tg_chat_id:
                    send_tg_message(tg_chat_id, result['message'])
                return
            
            # Парсим время
            minutes, seconds, time_type = parse_time_from_text(t)
            if minutes is not None or seconds is not None:
                minutes = minutes or 0
                seconds = seconds or 0
                result = set_timer(minutes, seconds)
                speak(result['message'])
                if source == 'telegram' and tg_chat_id:
                    send_tg_message(tg_chat_id, f"⏱️ {result['message']}")
            else:
                speak('Укажите время для таймера, например: таймер на 5 минут')
                if source == 'telegram' and tg_chat_id:
                    send_tg_message(tg_chat_id, 'Укажите время для таймера, например: таймер на 5 минут')
            return

        # media/navigation
        if any(x in t for x in ('пауза', 'пробел', 'поставь на паузу')):
            press_space(); return
        if any(x in t for x in ('enter', 'энтер', 'отправь')):
            press_enter(); return
        if any(x in t for x in ('командную строку', 'открой cmd', 'открой командную строку', 'cmd')):
            open_cmd(); return
        # Browser tabs control (works in all modes)
        if any(x in t for x in ('новая вкладка', 'новая вклада', 'открой вкладку', 'открыть вкладку')):
            new_browser_tab(); return
        if any(x in t for x in ('закрой вкладку', 'закрой вклад', 'закрыть вкладку', 'закрыть вклад')):
            close_browser_tab(); return

        # Tab by number: "вкладка 1", "вклада 2", "вкладка номер 3" (works in all modes)
        if 'вкладка' in t or 'вклад' in t or 'вклада' in t:
            # Try to extract number after вкладка
            m = re.search(r'(?:вкладк\w*|вклад\w*)\s*(?:номер\s*)?(10|[1-9])\b', t)
            if m:
                open_tab_by_number(m.group(1)); return
        # close current window via keyword
        if 'убери' in t:
            close_current_window(); return

        # screenshot
        if any(x in t for x in ('скрин', 'скриншот', 'сделай скрин', 'сделай снимок')):
            # if telegram, send to chat; else just save file and notify
            if source == 'telegram' and TG_BOT and settings.get('last_chat_id') and TG_LOOP:
                screenshot_and_send(TG_BOT, settings.get('last_chat_id'))
            else:
                # save locally
                try:
                    fn = f'screenshot_{int(time.time())}.png'
                    img = pyautogui.screenshot()
                    img.save(fn)
                    notify_success(f'Скриншот сохранён: {fn}')
                    # copy to clipboard and broadcast to clients
                    # copy to clipboard
                    try:
                        if WIN32_CLIPBOARD:
                            copy_pil_image_to_clipboard(img)
                            append_clipboard_log(f"[{datetime_now_str()}] Скриншот скопирован в буфер ПК: {fn}")
                    except NameError:
                        # fallback if WIN32_CLIPBOARD var missing
                        pass
                    except Exception:
                        pass
                    # broadcast
                    try:
                        jb = BytesIO()
                        img_j = img
                        max_size = 2000
                        if img.size[0] > max_size or img.size[1] > max_size:
                            ratio = min(max_size / img.size[0], max_size / img.size[1])
                            new_size = (int(img.size[0] * ratio), int(img.size[1] * ratio))
                            img_j = img.resize(new_size, Image.Resampling.LANCZOS)
                        img_j.save(jb, format='JPEG', quality=85, optimize=True)
                        broadcast_image_bytes(jb.getvalue())
                        append_clipboard_log(f"[{datetime_now_str()}] Скриншот отправлен клиентам (если подключены)")
                    except Exception:
                        pass
                    # push screenshot through account sync for Android/cloud clients
                    try:
                        ab = BytesIO()
                        img_acc = img
                        max_side = 1600
                        if img_acc.size[0] > max_side or img_acc.size[1] > max_side:
                            ratio = min(max_side / img_acc.size[0], max_side / img_acc.size[1])
                            new_size = (max(1, int(img_acc.size[0] * ratio)), max(1, int(img_acc.size[1] * ratio)))
                            img_acc = img_acc.resize(new_size, Image.Resampling.LANCZOS)
                        if img_acc.mode != 'RGB':
                            img_acc = img_acc.convert('RGB')
                        img_acc.save(ab, format='JPEG', quality=85, optimize=True)
                        image_b64 = base64.b64encode(ab.getvalue()).decode('utf-8')
                        if push_content_via_account(image_b64, content_type='image', target_device_id=requester_device_id):
                            append_clipboard_log(f"[{datetime_now_str()}] ✓ Скриншот отправлен в аккаунтный канал")
                        else:
                            append_clipboard_log(f"[{datetime_now_str()}] ✗ Не удалось отправить скриншот в аккаунтный канал")
                    except Exception as e:
                        append_clipboard_log(f"[{datetime_now_str()}] ✗ Ошибка отправки скриншота в аккаунтный канал: {e}")
                except Exception as e:
                    notify_error('Не удалось сохранить скриншот: ' + str(e))
            return

        # process listing
        if any(x in t for x in ('список процессов', 'список приложений', 'процессы', 'отобрази процессы')):
            procs = list_open_apps()
            if source == 'telegram' and TG_BOT and settings.get('last_chat_id') and TG_LOOP:
                coro = TG_BOT.send_message(chat_id=settings.get('last_chat_id'), text='\n'.join(procs[:100]))
                asyncio.run_coroutine_threadsafe(coro, TG_LOOP)
            else:
                try:
                    messagebox.showinfo('Процессы', '\n'.join(procs[:200]))
                except Exception:
                    notify_success('Список процессов получен (см. консоль).')
            return

        # close all except: "закрой все /main, telegram"
        if t.startswith('закрой все'):
            ex = []
            if '/' in t:
                try:
                    ex = [x.strip() for x in t.split('/', 1)[1].split(',') if x.strip()]
                except Exception:
                    ex = []
            close_all_except(ex)
            return

        # close specific app: "закрой chrome"
        if t.startswith('закрой '):
            name = t.split(' ', 1)[1].strip()
            if name:
                close_app_by_name(name)
            return

        # clear history
        if 'очист' in t and 'истор' in t:
            clear_history(); return

        # ctrl combos: "ctrl c", "ctrl alt delete" -> we'll parse tokens after ctrl
        if t.startswith('ctrl '):
            parts = t.split()
            keys = parts[1:]
            if keys:
                ctrl_combo(keys)
            return

        # Android quick actions can arrive as machine tokens: ALT_TAB, WIN_E, TASK_MANAGER, VOLUME_UP, etc.
        command_token = re.sub(r'[^0-9a-zа-яё]+', '_', t, flags=re.IGNORECASE).strip('_')
        android_quick_actions = {
            'alt_f4': close_current_window,
            'close_window': close_current_window,
            'close_current_window': close_current_window,
            'alt_tab': switch_window_alt_tab,
            'switch_window': switch_window_alt_tab,
            'win_d': minimize_all,
            'show_desktop': minimize_all,
            'desktop': minimize_all,
            'win_e': open_file_explorer,
            'explorer': open_file_explorer,
            'file_explorer': open_file_explorer,
            'task_manager': open_task_manager,
            'taskmgr': open_task_manager,
            'диспетчер_задач': open_task_manager,
            'volume_up': volume_up,
            'volume_down': volume_down,
            'volume_mute': volume_mute,
            'mute': volume_mute,
            'громче': volume_up,
            'тише': volume_down,
            'мут': volume_mute,
        }
        action = android_quick_actions.get(command_token)
        if action:
            action()
            return

        # Generic hotkey support for Android custom combo, e.g. "alt+tab", "win+e", "ctrl+shift+n".
        combo_raw = re.sub(r'\s+', '', t)
        if re.fullmatch(r'[a-z0-9]+(?:\+[a-z0-9]+)+', combo_raw):
            try:
                keyboard.send(combo_raw)
                notify_success('Выполнена комбинация: ' + combo_raw)
            except Exception as e:
                notify_error('Не удалось выполнить комбинацию: ' + str(e))
            return

        # open user app: "открой приложение <name>"
        if t.startswith('открой приложение '):
            parts = t.split(' ', 2)
            if len(parts) >= 3:
                open_user_app(parts[2]); return

        # open user app by plain name: support voice and telegram equally
        for _prefix in (
            'открой ', 'запусти ', 'open ', 'launch ',
            'открой программу ', 'открой приложение ',
            'open app ', 'open application ', 'launch app ', 'launch application '
        ):
            if t.startswith(_prefix):
                name = t[len(_prefix):].strip().strip('"').strip("'")
                # avoid conflicts with known phrases like "вкладка", handled above
                if name and not name.startswith('вклад') and name not in ('youtube', 'ютуб'):
                    open_user_app(name)
                    return

        # direct app.txt command without prefixes (single-word/phrase defined in app.txt)
        try:
            apps_file = _resolve_apps_file()
            if os.path.exists(apps_file):
                with open(apps_file, 'r', encoding='utf-8') as f:
                    for line in f:
                        if ',' in line:
                            cmd, _ = line.split(',', 1)
                            if t == cmd.strip().lower():
                                open_user_app(cmd.strip())
                                return
        except Exception:
            pass

        # win + digit for Telegram and Android quick shortcuts
        # "win 1" or "вин 1"; also allow numeric triggers
        if source in ('telegram', 'android'):
            if t.startswith('win ') or t.startswith('вин '):
                parts = t.split(' ', 1)
                if len(parts) > 1:
                    win_plus_digit(parts[1].strip()); return

            # Enhanced number detection for apps (more stable)
            # Direct numbers 0-10
            if re.match(r'^(10|[0-9])$', t):
                win_plus_digit(t); return

            # Numbers with prefixes
            m = re.match(r'^(?:win\+?\s*|вин\s*|№|#|номер\s*|приложени\w*\s*|app\s*)\s*(10|[0-9])$', t)
            if m:
                win_plus_digit(m.group(1)); return

            # Extract numbers from text when taskbar/apps mentioned
            if any(x in t for x in ('номер', 'приложени', 'панел', 'задач', 'app', 'приложение')):
                num = extract_number_one_to_ten(t)
                if num is not None:
                    win_plus_digit(str(num)); return

        # translate clipboard: "переведи", "переведи en", "переведи на ru"
        if t.startswith('переведи') or t.startswith('перевод'):
            # try to extract dest language token (last token)
            parts = t.split()
            dest = 'ru'
            if len(parts) >= 2:
                last = parts[-1]
                # if looks like language code (2 letters) or "на" preceding
                if len(last) <= 3:
                    dest = last
                elif parts[-2] == 'на':
                    dest = last
            res = translate_clipboard(dest=dest)
            if res:
                # send to tg if telegram
                if source == 'telegram' and TG_BOT and settings.get('last_chat_id') and TG_LOOP:
                    coro = TG_BOT.send_message(chat_id=settings.get('last_chat_id'), text='Перевод:\n' + res)
                    asyncio.run_coroutine_threadsafe(coro, TG_LOOP)
                else:
                    try:
                        messagebox.showinfo('Перевод', res)
                    except Exception:
                        notify_success('Перевод скопирован в буфер.')
            return

        # "Мила ты тут" или "Мила, ты здесь?"
        if any(x in t for x in ('ты тут', 'ты здесь', 'на месте')):
            speak('Да, я тут')
            notify_success('Да, я тут')
            return

        # Display and desktop (strict word matching)
        words = t.split()
        if any(w in words for w in ['домой', 'рабочий', 'минимизировать', 'свернуть', 'минимизир']):
            minimize_all(); return
        if any(w in words for w in ['экран', 'дисплей', 'монитор', 'экран']):
            screen_display(); return

        # fallback: unknown command
        notify_error('Команда не распознана: ' + text)

    except Exception as e:
        notify_error('Ошибка в обработчике: ' + str(e))


# ----------------- Telegram helpers (aiogram 3.x) -----------------
def ask_confirm_tg(chat_id: int, action: str):
    global pending_confirmation, pending_origin
    if not TG_BOT or not TG_LOOP:
        return
    pending_confirmation = action
    pending_origin = 'telegram'
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text='Да', callback_data=f'confirm_yes|{action}'),
         InlineKeyboardButton(text='Нет', callback_data=f'confirm_no|{action}')]
    ])
    coro = TG_BOT.send_message(
        chat_id=chat_id,
        text='Подтвердите действие: ' + action + '\nМожно нажать кнопку или написать вручную: да / нет',
        reply_markup=kb,
    )
    asyncio.run_coroutine_threadsafe(coro, TG_LOOP)

def send_tg_message(chat_id: int, text: str):
    """Отправить сообщение в Telegram"""
    if not TG_BOT or not TG_LOOP or not chat_id:
        return
    try:
        coro = TG_BOT.send_message(chat_id=chat_id, text=text)
        asyncio.run_coroutine_threadsafe(coro, TG_LOOP)
    except:
        pass

def send_main_menu(chat_id: int):
    if not TG_BOT or not TG_LOOP:
        return
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text='YouTube', callback_data='do_youtube'), InlineKeyboardButton(text='Скриншот', callback_data='do_screenshot')],
        [InlineKeyboardButton(text='Процессы', callback_data='do_list_procs'), InlineKeyboardButton(text='Открыть CMD', callback_data='do_open_cmd')],
        [InlineKeyboardButton(text='Громче', callback_data='vol_up'), InlineKeyboardButton(text='Тише', callback_data='vol_down'), InlineKeyboardButton(text='Мут', callback_data='vol_mute')],
        [InlineKeyboardButton(text='Новая вкладка', callback_data='tab_new'), InlineKeyboardButton(text='Закрыть вкладку', callback_data='tab_close')],
        [InlineKeyboardButton(text='⏰ Будильник', callback_data='menu_alarm'), InlineKeyboardButton(text='⏱️ Таймер', callback_data='menu_timer')],
        [InlineKeyboardButton(text='Очистить историю', callback_data='do_clear_history')],
        [InlineKeyboardButton(text='Голос: Вкл', callback_data='voice_on'), InlineKeyboardButton(text='Выкл', callback_data='voice_off'), InlineKeyboardButton(text='Перекл', callback_data='voice_toggle')],
        [InlineKeyboardButton(text='Питание', callback_data='menu_power')]
    ])
    coro = TG_BOT.send_message(chat_id=chat_id, text='Меню действий:', reply_markup=kb)
    asyncio.run_coroutine_threadsafe(coro, TG_LOOP)

async def _on_start(message: types.Message):
    settings['last_chat_id'] = message.chat.id
    save_settings()
    await message.answer('Привет! Я управляю ПК. Отправляй команды текстом или голосом (через имя ассистента).')

async def _on_text(message: types.Message):
    settings['last_chat_id'] = message.chat.id
    save_settings()
    # explicit handling of voice power commands in case command filters didn't trigger
    txt = (message.text or '').strip().lower()
    if txt.startswith('/menu'):
        send_main_menu(message.chat.id)
        return
    if txt.startswith('/voice_on'):
        threading.Thread(target=start_voice, daemon=True).start()
        await message.answer('Голосовое управление включено')
        return
    if txt.startswith('/voice_off'):
        threading.Thread(target=stop_voice, daemon=True).start()
        await message.answer('Голосовое управление выключено')
        return
    if txt.startswith('/voice'):
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text='Включить', callback_data='voice_on'), InlineKeyboardButton(text='Выключить', callback_data='voice_off')],
            [InlineKeyboardButton(text='Переключить', callback_data='voice_toggle')]
        ])
        await message.answer('Управление голосом:', reply_markup=kb)
        return

    if pending_confirmation:
        intent = _confirmation_intent(txt)
        if intent:
            answer = 'да' if intent == 'yes' else 'нет'
            threading.Thread(
                target=handle_confirmation_response,
                args=(answer, 'telegram', message.chat.id),
                daemon=True,
            ).start()
            await message.answer('Подтверждение принято.')
            return

    # dispatch to sync handler in thread, with source 'telegram'
    threading.Thread(target=handle_command_text, args=(message.text, 'telegram', message.chat.id), daemon=True).start()
    await message.answer('Команда получена: ' + message.text)

async def _on_callback(query: types.CallbackQuery):
    global pending_confirmation, pending_origin
    data = query.data or ''
    if data.startswith('confirm_yes'):
        action = data.split('|', 1)[1]
        pending_confirmation = action
        pending_origin = 'telegram'
        await query.answer('Подтверждаю: ' + action)
        threading.Thread(
            target=handle_confirmation_response,
            args=('да', 'telegram', query.message.chat.id),
            daemon=True,
        ).start()
    elif data.startswith('confirm_no'):
        action = data.split('|', 1)[1]
        pending_confirmation = action
        pending_origin = 'telegram'
        await query.answer('Отменено')
        threading.Thread(
            target=handle_confirmation_response,
            args=('нет', 'telegram', query.message.chat.id),
            daemon=True,
        ).start()
        await query.message.reply('Действие отменено.')
    elif data == 'voice_on':
        await query.answer('Включаю голос')
        threading.Thread(target=start_voice, daemon=True).start()
        await query.message.reply('Голосовое управление включено')
    elif data == 'voice_off':
        await query.answer('Выключаю голос')
        threading.Thread(target=stop_voice, daemon=True).start()
        await query.message.reply('Голосовое управление выключено')
    elif data == 'voice_toggle':
        await query.answer('Переключаю голос')
        threading.Thread(target=toggle_voice, daemon=True).start()
        await query.message.reply('Голосовое управление переключено')
    elif data == 'menu_power':
        # power submenu
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text='Выключить ПК', callback_data='power_shutdown')],
            [InlineKeyboardButton(text='Перезагрузка', callback_data='power_restart')],
            [InlineKeyboardButton(text='Сон', callback_data='power_sleep')],
            [InlineKeyboardButton(text='Назад', callback_data='menu_main')]
        ])
        await query.message.reply('Питание:', reply_markup=kb)
    elif data == 'menu_main':
        send_main_menu(query.message.chat.id)
    elif data == 'do_youtube':
        threading.Thread(target=open_youtube, daemon=True).start()
        await query.answer('Открываю YouTube')
    elif data == 'do_screenshot':
        if TG_BOT and settings.get('last_chat_id'):
            screenshot_and_send(TG_BOT, settings.get('last_chat_id'))
            await query.answer('Скриншот отправлен')
        else:
            threading.Thread(target=screenshot_and_send, daemon=True).start()
            await query.answer('Скриншот сделан')
    elif data == 'do_list_procs':
        procs = list_open_apps()
        try:
            await query.message.reply('\n'.join(procs[:100]) or 'Пусто')
        except Exception:
            pass
    elif data == 'do_open_cmd':
        threading.Thread(target=open_cmd, daemon=True).start()
        await query.answer('Открываю CMD')
    elif data == 'vol_up':
        threading.Thread(target=volume_up, daemon=True).start()
        await query.answer('Громче')
    elif data == 'vol_down':
        threading.Thread(target=volume_down, daemon=True).start()
        await query.answer('Тише')
    elif data == 'vol_mute':
        threading.Thread(target=volume_mute, daemon=True).start()
        await query.answer('Мут')
    elif data == 'tab_new':
        threading.Thread(target=new_browser_tab, daemon=True).start()
        await query.answer('Новая вкладка')
    elif data == 'tab_close':
        threading.Thread(target=close_browser_tab, daemon=True).start()
        await query.answer('Закрываю вкладку')
    elif data == 'power_shutdown':
        ask_confirm_tg(query.message.chat.id, 'shutdown')
    elif data == 'power_restart':
        ask_confirm_tg(query.message.chat.id, 'restart')
    elif data == 'power_sleep':
        threading.Thread(target=system_sleep, daemon=True).start()
        await query.answer('Перевожу в сон')
    # ===== ALARM SUBMENU =====
    elif data == 'menu_alarm':
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text='📋 Список будильников', callback_data='alarm_list')],
            [InlineKeyboardButton(text='❌ Отменить все', callback_data='alarm_cancel_all')],
            [InlineKeyboardButton(text='⬅️ Назад', callback_data='menu_main')]
        ])
        await query.message.reply('⏰ Будильники:\n\nЧтобы установить будильник, напишите:\n"будильник на 7:30"\n"будильник 14:00"', reply_markup=kb)
    elif data == 'alarm_list':
        result = list_alarms_timers()
        await query.message.reply(result['message'])
    elif data == 'alarm_cancel_all':
        if ALARMS:
            ALARMS.clear()
            await query.message.reply('✅ Все будильники отменены')
        else:
            await query.message.reply('Нет активных будильников')
    # ===== TIMER SUBMENU =====
    elif data == 'menu_timer':
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text='📋 Список таймеров', callback_data='timer_list')],
            [InlineKeyboardButton(text='❌ Отменить все', callback_data='timer_cancel_all')],
            [InlineKeyboardButton(text='⬅️ Назад', callback_data='menu_main')]
        ])
        await query.message.reply('⏱️ Таймеры:\n\nЧтобы установить таймер, напишите:\n"таймер на 5 минут"\n"засеки 30 секунд"', reply_markup=kb)
    elif data == 'timer_list':
        result = list_alarms_timers()
        await query.message.reply(result['message'])
    elif data == 'timer_cancel_all':
        if TIMERS:
            TIMERS.clear()
            await query.message.reply('✅ Все таймеры отменены')
        else:
            await query.message.reply('Нет активных таймеров')

# ----------------- Start Telegram bot in thread -----------------
def start_telegram_bot(token: str):
    global TG_BOT, TG_DISPATCHER, TG_LOOP

    def _run():
        nonlocal token
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        globals()['TG_LOOP'] = loop
        bot = Bot(token=token)
        dp = Dispatcher()
        # register handlers
        dp.message.register(_on_start, Command('start'))
        # voice power commands
        async def _on_voice_on(message: types.Message):
            threading.Thread(target=start_voice, daemon=True).start()
            await message.answer('Голосовое управление включено')
        async def _on_voice_off(message: types.Message):
            threading.Thread(target=stop_voice, daemon=True).start()
            await message.answer('Голосовое управление выключено')
        async def _on_voice_menu(message: types.Message):
            kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text='Включить', callback_data='voice_on'), InlineKeyboardButton(text='Выключить', callback_data='voice_off')],
                [InlineKeyboardButton(text='Переключить', callback_data='voice_toggle')]
            ])
            await message.answer('Управление голосом:', reply_markup=kb)
        dp.message.register(_on_voice_on, Command('voice_on'))
        dp.message.register(_on_voice_off, Command('voice_off'))
        dp.message.register(_on_voice_menu, Command('voice'))
        async def _on_menu(message: types.Message):
            send_main_menu(message.chat.id)
        dp.message.register(_on_menu, Command('menu'))
        # generic text handler
        dp.message.register(_on_text)
        dp.callback_query.register(_on_callback)

        globals()['TG_BOT'] = bot
        globals()['TG_DISPATCHER'] = dp

        try:
            loop.run_until_complete(dp.start_polling(bot))
        except (asyncio.CancelledError, KeyboardInterrupt):
            pass
        finally:
            loop.run_until_complete(bot.session.close())

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    # give a brief moment for initialization
    time.sleep(1)

# ----------------- Voice listener / parsing -----------------
recognizer = sr.Recognizer()

NUM_WORDS = {
    'one': 1, 'two': 2, 'three': 3, 'four': 4, 'five': 5,
    'six': 6, 'seven': 7, 'eight': 8, 'nine': 9, 'ten': 10,
    'один': 1, 'две': 2, 'два': 2, 'три': 3, 'четыре': 4, 'пять': 5,
    'шесть': 6, 'семь': 7, 'восемь': 8, 'девять': 9, 'десять': 10,
}

def extract_number_one_to_ten(text: str) -> Optional[int]:
    import re
    if not text:
        return None
    txt = text.lower()
    # digits first
    m = re.search(r'\b(10|[1-9])\b', txt)
    if m:
        try:
            val = int(m.group(1))
            if 1 <= val <= 10:
                return val
        except Exception:
            pass
    # words
    for w, n in NUM_WORDS.items():
        if re.search(rf'\b{re.escape(w)}\b', txt):
            return n
    return None

def is_activation_phrase(text: str) -> bool:
    """
    True if text contains activation name or is a direct yes/no response when pending.
    """
    txt = (text or '').lower().strip()
    if not txt:
        return False
    # direct yes/no when confirmation pending should be allowed (handled separately)
    if pending_confirmation and _confirmation_intent(txt):
        return True
    # check if starts with assistant name or contains it
    if txt.startswith(ASSISTANT_NAME) or ASSISTANT_NAME in txt.split():
        return True
    return False

def parse_voice_command(text: str):
    """
    From recognized raw text decide whether it's activation -> extract core command.
    Returns the command string to pass to handle_command_text or None.
    """
    if not text:
        return None
    txt = text.lower().strip()
    # If there is pending confirmation, accept short yes/no without assistant name
    if pending_confirmation:
        intent = _confirmation_intent(txt)
        if intent:
            return 'да' if intent == 'yes' else 'нет'
    # If starts with assistant name - strip it
    if txt.startswith(ASSISTANT_NAME):
        core = txt[len(ASSISTANT_NAME):].strip()
        # if begins with comma or punctuation, strip
        if core.startswith(',') or core.startswith(':') or core.startswith('—'):
            core = core[1:].strip()
        return core if core else None
    # If assistant name present as first word
    parts = txt.split()
    if parts and parts[0] == ASSISTANT_NAME:
        core = ' '.join(parts[1:]).strip()
        return core if core else None
    # otherwise not addressed
    return None

def voice_listener_loop(stop_event: threading.Event):
    """
    Continuous listening loop running in background thread.
    It tries to recognize speech, parse activation phrase and send commands to handler.
    """
    try:
        mic = sr.Microphone()
    except Exception as e:
        notify_error('Голосовое управление отключено: ' + str(e))
        return

    speak(f"{settings.get('assistant_name', 'Мила')} запущена, голосовое управление активно")
    while True:
        if stop_event.is_set():
            break
        try:
            with mic as source:
                recognizer.adjust_for_ambient_noise(source, duration=0.5)
                audio = recognizer.listen(source, timeout=5, phrase_time_limit=7)
            try:
                cmd_text = recognizer.recognize_google(audio, language='ru-RU')
            except sr.UnknownValueError:
                continue
            except sr.RequestError as e:
                notify_error('Ошибка сервиса распознавания: ' + str(e))
                time.sleep(2)
                continue

            if not cmd_text:
                continue
            cmd_text = cmd_text.strip()
            print('[VOICE] распознано:', cmd_text)

            # Only process if activation or confirmation
            if is_activation_phrase(cmd_text):
                core_cmd = parse_voice_command(cmd_text)
                if core_cmd:
                    # For pending confirmation, parse_voice_command may return 'да'/'нет'
                    # and handle_command_text will route it.
                    # Call handler synchronously in a thread to avoid blocking listener
                    threading.Thread(target=handle_command_text, args=(core_cmd, 'voice', None), daemon=True).start()
                else:
                    # Nothing after activation (e.g., user said only "мила") -> ignore
                    pass

        except sr.WaitTimeoutError:
            continue
        except Exception as e:
            notify_error('Voice loop error: ' + str(e))
            time.sleep(1)
        if stop_event.is_set():
            break

def start_voice():
    global VOICE_THREAD, VOICE_ENABLED
    if VOICE_THREAD and VOICE_THREAD.is_alive():
        return
    VOICE_STOP_EVENT.clear()
    VOICE_THREAD = threading.Thread(target=voice_listener_loop, args=(VOICE_STOP_EVENT,), daemon=True)
    VOICE_THREAD.start()
    VOICE_ENABLED = True
    notify_success('Голосовое управление включено')

def stop_voice():
    global VOICE_THREAD, VOICE_ENABLED
    if VOICE_THREAD and VOICE_THREAD.is_alive():
        VOICE_STOP_EVENT.set()
        # give the thread a brief moment to exit
        try:
            VOICE_THREAD.join(timeout=0.5)
        except Exception:
            pass
    VOICE_THREAD = None
    VOICE_ENABLED = False
    notify_success('Голосовое управление выключено')

def toggle_voice():
    if VOICE_ENABLED:
        stop_voice()
    else:
        start_voice()

# ----------------- GUI -----------------
# We'll add a new tab "Буфер" and minimal controls there.
GUI_CLIPBOARD_LOG = None  # Text widget for clipboard-specific logs

class MilaGUI(ctk.CTk):
    def __init__(self):
        super().__init__()
        self.title('Мила — ассистент')
        self.geometry('920x560')
        ctk.set_appearance_mode('System')
        ctk.set_default_color_theme('blue')

        self.frame_left = ctk.CTkFrame(self, width=300)
        self.frame_left.pack(side='left', fill='y', padx=10, pady=10)

        # main area with tabs: Controls and Logs
        self.tabs = ctk.CTkTabview(self)
        self.tabs.pack(side='right', expand=True, fill='both', padx=10, pady=10)
        self.tab_controls = self.tabs.add('Аккаунт')
        self.tab_remote = self.tabs.add('Удалённый доступ')
        self.tab_clipboard = self.tabs.add('Буфер')
        self.tab_logs = self.tabs.add('Логи')

        self._build_left()
        self._build_main()
        self._build_remote()
        self._build_logs()
        self._build_clipboard()
        self.after(300, self.auto_restore_session)

    def _build_left(self):
        ctk.CTkLabel(self.frame_left, text='Настройки', font=('Arial', 16)).pack(pady=8)
        self.token_entry = ctk.CTkEntry(self.frame_left, placeholder_text='Telegram token')
        self.token_entry.pack(pady=6)
        self.token_entry.insert(0, settings.get('telegram_token', ''))
        # Bind auto-save on typing
        self.token_entry.bind('<KeyRelease>', self.on_token_change)
        # token clipboard helpers
        btn_row = ctk.CTkFrame(self.frame_left)
        btn_row.pack(pady=2)
        ctk.CTkButton(btn_row, width=100, text='Вставить', command=self.paste_token).pack(side='left', padx=4)
        ctk.CTkButton(btn_row, width=100, text='Скопировать', command=self.copy_token).pack(side='left', padx=4)
        ctk.CTkButton(self.frame_left, text='Сохранить токен и запустить бота', command=lambda: self.save_token(show_message=True)).pack(pady=4)
        ctk.CTkButton(self.frame_left, text='Загрузить app.txt', command=self.load_appfile).pack(pady=4)
        ctk.CTkButton(self.frame_left, text='Очистить историю', command=clear_history).pack(pady=4)
        ctk.CTkButton(self.frame_left, text='Открыть папку с программой', command=lambda: os.startfile(os.getcwd())).pack(pady=4)
        ctk.CTkLabel(self.frame_left, text='Имя ассистента:').pack(pady=(12, 4))
        self.name_entry = ctk.CTkEntry(self.frame_left, placeholder_text='Имя ассистента')
        self.name_entry.pack(pady=2)
        self.name_entry.insert(0, settings.get('assistant_name', 'Мила'))
        ctk.CTkButton(self.frame_left, text='Сохранить имя', command=self.save_name).pack(pady=4)

    def _build_main(self):
        """Build account/login tab - вкладка Аккаунт"""
        ctk.CTkLabel(self.tab_controls, text='Вход в аккаунт', font=('Arial', 18)).pack(pady=8)
        
        # Account status
        self.main_account_status = ctk.CTkLabel(self.tab_controls, text='', font=('Arial', 14))
        self.main_account_status.pack(pady=(0, 8))
        
        # Login form
        login_frame = ctk.CTkFrame(self.tab_controls)
        login_frame.pack(fill='x', padx=20, pady=10)
        
        ctk.CTkLabel(login_frame, text='Имя пользователя:', font=('Arial', 12)).pack(anchor='w', padx=12, pady=(12, 0))
        self.main_username = ctk.CTkEntry(login_frame, width=250, placeholder_text='Введите логин')
        self.main_username.pack(padx=12, pady=(0, 8))
        
        ctk.CTkLabel(login_frame, text='Пароль:', font=('Arial', 12)).pack(anchor='w', padx=12, pady=(4, 0))
        self.main_password = ctk.CTkEntry(login_frame, width=250, placeholder_text='Введите пароль', show='*')
        self.main_password.pack(padx=12, pady=(0, 12))
        
        # Buttons
        btn_frame = ctk.CTkFrame(self.tab_controls)
        btn_frame.pack(pady=10)
        
        self.main_login_btn = ctk.CTkButton(btn_frame, text='Войти', width=120, command=self.do_main_login)
        self.main_login_btn.pack(side='left', padx=6)
        
        self.main_register_btn = ctk.CTkButton(btn_frame, text='Регистрация', width=120, command=self.do_main_register)
        self.main_register_btn.pack(side='left', padx=6)
        
        self.main_logout_btn = ctk.CTkButton(btn_frame, text='Выйти', width=100, command=self.do_main_logout)
        self.main_logout_btn.pack(side='left', padx=6)
        
        # Server URL
        ctk.CTkLabel(self.tab_controls, text='URL сервера:', font=('Arial', 12)).pack(anchor='w', padx=20, pady=(20, 0))
        self.main_server_url = ctk.CTkEntry(self.tab_controls, width=350)
        self.main_server_url.pack(padx=20, pady=(0, 10))
        self.main_server_url.insert(0, settings.get('sync_server_url', 'https://sea-lion-app-i3rnh.ondigitalocean.app/'))
        
        # Info
        info_text = '''Этот аккаунт используется для:
• Синхронизации буфера обмена между устройствами
• Удалённого доступа к компьютеру

Синхронизация запускается автоматически при входе.'''
        ctk.CTkLabel(self.tab_controls, text=info_text, font=('Arial', 11), justify='left').pack(pady=15)
        
        # Load saved credentials
        saved_username = settings.get('account_username', '')
        if saved_username:
            self.main_username.insert(0, saved_username)
        
        # Update status
        self.update_main_account_status()

    def _build_remote(self):
        """Build remote access tab - вкладка Удалённый доступ"""
        ctk.CTkLabel(self.tab_remote, text='Удалённый доступ', font=('Arial', 18)).pack(pady=8)
        
        # Status
        self.remote_status = ctk.CTkLabel(self.tab_remote, text='● Готов открыть RustDesk', font=('Arial', 14), text_color='gray')
        self.remote_status.pack(pady=(0, 10))
        
        # Info frame
        info_frame = ctk.CTkFrame(self.tab_remote)
        info_frame.pack(fill='x', padx=20, pady=10)
        
        info_text = '''Удалённый доступ теперь работает через RustDesk.

    Нажмите кнопку ниже — приложение просто откроет
    установленный RustDesk на этом ПК.'''
        
        ctk.CTkLabel(info_frame, text=info_text, font=('Arial', 11), justify='left').pack(padx=12, pady=12)
        
        # Control buttons
        btn_frame = ctk.CTkFrame(self.tab_remote)
        btn_frame.pack(pady=15)
        
        self.remote_start_btn = ctk.CTkButton(btn_frame, text='▶ Открыть RustDesk', width=220, 
                                               fg_color='green', command=self.start_remote_client)
        self.remote_start_btn.pack(side='left', padx=8)
        
        # Log
        global GUI_REMOTE_LOG
        ctk.CTkLabel(self.tab_remote, text='Лог удалённого доступа:').pack(pady=(15, 4))
        self.remote_log = ctk.CTkTextbox(self.tab_remote, height=150)
        self.remote_log.pack(expand=True, fill='both', padx=10, pady=10)
        GUI_REMOTE_LOG = self.remote_log
        
        self.update_remote_status()

    def _build_logs(self):
        global GUI_LOG_TEXT
        ctk.CTkLabel(self.tab_logs, text='Журнал событий', font=('Arial', 16)).pack(pady=(8, 4))
        txt = ctk.CTkTextbox(self.tab_logs)
        txt.pack(expand=True, fill='both', padx=8, pady=8)
        GUI_LOG_TEXT = txt

    def _build_clipboard(self):
        global GUI_CLIPBOARD_LOG
        ctk.CTkLabel(self.tab_clipboard, text='Синхронизация буфера обмена', font=('Arial', 16)).pack(pady=(8, 6))
        
        # URL сервера
        server_frame = ctk.CTkFrame(self.tab_clipboard)
        server_frame.pack(fill='x', padx=8, pady=(0, 8))
        ctk.CTkLabel(server_frame, text='URL сервера:').pack(anchor='w', padx=8, pady=(8, 0))
        self.server_url_entry = ctk.CTkEntry(server_frame, width=300)
        self.server_url_entry.pack(padx=8, pady=(0, 8))
        self.server_url_entry.insert(0, settings.get('sync_server_url', 'https://sea-lion-app-i3rnh.ondigitalocean.app/'))
        
        # Account section
        account_frame = ctk.CTkFrame(self.tab_clipboard)
        account_frame.pack(fill='x', padx=8, pady=(0, 8))
        
        ctk.CTkLabel(account_frame, text='Аккаунт для синхронизации:', font=('Arial', 14, 'bold')).pack(pady=(8, 4))
        
        # Current account status
        self.account_status_label = ctk.CTkLabel(account_frame, text='')
        self.account_status_label.pack(pady=(0, 4))
        
        # Login form
        login_frame = ctk.CTkFrame(account_frame)
        login_frame.pack(fill='x', padx=8, pady=4)
        
        ctk.CTkLabel(login_frame, text='Имя пользователя:').pack(anchor='w', padx=8, pady=(8, 0))
        self.username_entry = ctk.CTkEntry(login_frame, width=200, placeholder_text='Введите имя пользователя')
        self.username_entry.pack(padx=8, pady=(0, 4))
        
        ctk.CTkLabel(login_frame, text='Пароль:').pack(anchor='w', padx=8, pady=(4, 0))
        self.password_entry = ctk.CTkEntry(login_frame, width=200, placeholder_text='Введите пароль', show='*')
        self.password_entry.pack(padx=8, pady=(0, 8))
        
        # Account buttons
        acc_btn_frame = ctk.CTkFrame(account_frame)
        acc_btn_frame.pack(pady=4)
        
        self.login_btn = ctk.CTkButton(acc_btn_frame, text='Войти', command=self.login_account)
        self.login_btn.pack(side='left', padx=4)
        
        self.register_btn = ctk.CTkButton(acc_btn_frame, text='Регистрация', command=self.register_account)
        self.register_btn.pack(side='left', padx=4)
        
        self.logout_btn = ctk.CTkButton(acc_btn_frame, text='Выйти', command=self.logout_account)
        self.logout_btn.pack(side='left', padx=4)
        
        # Sync control buttons
        sync_frame = ctk.CTkFrame(self.tab_clipboard)
        sync_frame.pack(fill='x', padx=8, pady=4)
        
        ctk.CTkLabel(sync_frame, text='Управление синхронизацией:', font=('Arial', 14, 'bold')).pack(pady=(8, 4))
        
        sync_btn_frame = ctk.CTkFrame(sync_frame)
        sync_btn_frame.pack(pady=4)
        
        self.start_account_sync_btn = ctk.CTkButton(sync_btn_frame, text='Запустить синхронизацию', command=self.start_account_sync)
        self.start_account_sync_btn.pack(side='left', padx=6)
        
        self.stop_sync_btn = ctk.CTkButton(sync_btn_frame, text='Остановить синхронизацию', command=self.stop_sync)
        self.stop_sync_btn.pack(side='left', padx=6)
        
        
        # Log section
        ctk.CTkLabel(self.tab_clipboard, text='Лог синхронизации:').pack(pady=(8, 4))
        txt = ctk.CTkTextbox(self.tab_clipboard, height=200)
        txt.pack(expand=True, fill='both', padx=8, pady=8)
        GUI_CLIPBOARD_LOG = txt
        
        # Initialize account status and load saved credentials
        self.update_account_status()
        saved_username = settings.get('account_username', '')
        if saved_username:
            self.username_entry.insert(0, saved_username)
        
        # Initialize with status
        if is_authenticated():
            append_clipboard_log(f"[{datetime_now_str()}] Авторизован как: {saved_username}")
            append_clipboard_log(f"[{datetime_now_str()}] Нажмите 'Запустить синхронизацию' для начала работы")
        else:
            append_clipboard_log(f"[{datetime_now_str()}] Готов к работе. Войдите в аккаунт для синхронизации")

    def update_account_status(self):
        """Update account status display"""
        if is_authenticated():
            username = settings.get('account_username', 'Unknown')
            self.account_status_label.configure(text=f"✓ Авторизован: {username}", text_color="green")
            self.login_btn.configure(state="disabled")
            self.register_btn.configure(state="disabled")
            self.logout_btn.configure(state="normal")
            self.start_account_sync_btn.configure(state="normal")
        else:
            self.account_status_label.configure(text="✗ Не авторизован", text_color="red")
            self.login_btn.configure(state="normal")
            self.register_btn.configure(state="normal")
            self.logout_btn.configure(state="disabled")
            self.start_account_sync_btn.configure(state="disabled")

    def auto_restore_session(self):
        """Restore session from saved token and auto-start services."""
        if not settings.get('account_token'):
            return

        def _worker():
            ok = validate_saved_account_token()

            def _update():
                self.update_main_account_status()
                self.update_account_status()
                if ok:
                    self._append_remote_log("✓ Сессия восстановлена по токену")
                    self._auto_start_services()
                else:
                    self._append_remote_log("⚠ Сессия не восстановлена, требуется вход")

            self.after(0, _update)

        threading.Thread(target=_worker, daemon=True).start()

    def login_account(self):
        """Handle account login"""
        username = self.username_entry.get().strip()
        password = self.password_entry.get().strip()
        
        if not username or not password:
            messagebox.showerror('Ошибка', 'Введите имя пользователя и пароль')
            return
        
        def _login():
            try:
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                result = loop.run_until_complete(authenticate_account(username, password))
                loop.close()
                
                if result.get('success'):
                    self.update_account_status()
                    messagebox.showinfo('Успех', f'Успешно авторизован как {username}')
                    self.password_entry.delete(0, 'end')  # Clear password for security
                else:
                    messagebox.showerror('Ошибка', f'Ошибка авторизации: {result.get("error", "Неизвестная ошибка")}')
            except Exception as e:
                messagebox.showerror('Ошибка', f'Ошибка подключения: {str(e)}')
        
        threading.Thread(target=_login, daemon=True).start()

    def register_account(self):
        """Handle account registration"""
        username = self.username_entry.get().strip()
        password = self.password_entry.get().strip()
        
        if not username or not password:
            messagebox.showerror('Ошибка', 'Введите имя пользователя и пароль')
            return
        
        # Simple dialog for email (optional)
        email = simpledialog.askstring("Email", "Введите email (необязательно):", parent=self)
        if email is None:  # User cancelled
            return
        
        def _register():
            try:
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                result = loop.run_until_complete(register_account(username, password, email or ""))
                loop.close()
                
                if result.get('success'):
                    self.update_account_status()
                    messagebox.showinfo('Успех', f'Аккаунт создан и авторизован как {username}')
                    self.password_entry.delete(0, 'end')  # Clear password for security
                else:
                    messagebox.showerror('Ошибка', f'Ошибка регистрации: {result.get("error", "Неизвестная ошибка")}')
            except Exception as e:
                messagebox.showerror('Ошибка', f'Ошибка подключения: {str(e)}')
        
        threading.Thread(target=_register, daemon=True).start()

    def logout_account(self):
        """Handle account logout"""
        logout_account()
        self.update_account_status()
        self.password_entry.delete(0, 'end')
        messagebox.showinfo('Выход', 'Вы вышли из аккаунта')

    # ======== Main account tab methods ========
    def update_main_account_status(self):
        """Update main account tab status"""
        if is_authenticated():
            username = settings.get('account_username', 'Unknown')
            self.main_account_status.configure(text=f"✓ Авторизован: {username}", text_color="green")
            self.main_login_btn.configure(state="disabled")
            self.main_register_btn.configure(state="disabled")
            self.main_logout_btn.configure(state="normal")
        else:
            self.main_account_status.configure(text="✗ Не авторизован", text_color="red")
            self.main_login_btn.configure(state="normal")
            self.main_register_btn.configure(state="normal")
            self.main_logout_btn.configure(state="disabled")
    
    def do_main_login(self):
        """Login from main account tab"""
        username = self.main_username.get().strip()
        password = self.main_password.get().strip()
        
        if not username or not password:
            messagebox.showerror('Ошибка', 'Введите имя пользователя и пароль')
            return
        
        # Update server URL
        server_url = self.main_server_url.get().strip()
        if server_url:
            settings['sync_server_url'] = server_url
            save_settings()
        
        def _login():
            try:
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                result = loop.run_until_complete(authenticate_account(username, password))
                loop.close()
                
                if result.get('success'):
                    self.update_main_account_status()
                    self.update_account_status()  # Also update clipboard tab
                    self.main_password.delete(0, 'end')
                    messagebox.showinfo('Успех', f'Успешно авторизован как {username}')
                    # Auto-start sync and remote access
                    self.after(500, self._auto_start_services)
                else:
                    messagebox.showerror('Ошибка', f'Ошибка авторизации: {result.get("error", "Неизвестная ошибка")}')
            except Exception as e:
                messagebox.showerror('Ошибка', f'Ошибка подключения: {str(e)}')
        
        threading.Thread(target=_login, daemon=True).start()
    
    def do_main_register(self):
        """Register from main account tab"""
        username = self.main_username.get().strip()
        password = self.main_password.get().strip()
        
        if not username or not password:
            messagebox.showerror('Ошибка', 'Введите имя пользователя и пароль')
            return
        
        if len(password) < 4:
            messagebox.showerror('Ошибка', 'Пароль должен быть минимум 4 символа')
            return
        
        # Update server URL
        server_url = self.main_server_url.get().strip()
        if server_url:
            settings['sync_server_url'] = server_url
            save_settings()
        
        def _register():
            try:
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                result = loop.run_until_complete(register_account(username, password))
                loop.close()
                
                if result.get('success'):
                    self.update_main_account_status()
                    self.update_account_status()
                    self.main_password.delete(0, 'end')
                    messagebox.showinfo('Успех', f'Аккаунт {username} создан и авторизован')
                    # Auto-start sync and remote access
                    self.after(500, self._auto_start_services)
                else:
                    messagebox.showerror('Ошибка', f'Ошибка регистрации: {result.get("error", "Неизвестная ошибка")}')
            except Exception as e:
                messagebox.showerror('Ошибка', f'Ошибка подключения: {str(e)}')
        
        threading.Thread(target=_register, daemon=True).start()
    
    def do_main_logout(self):
        """Logout from main account tab"""
        logout_account()
        stop_account_sync()
        stop_remote_client()
        self.update_main_account_status()
        self.update_account_status()
        self.update_remote_status()
        self.main_password.delete(0, 'end')
        messagebox.showinfo('Выход', 'Вы вышли из аккаунта')
    
    def _auto_start_services(self):
        """Auto-start clipboard sync and RustDesk after login."""
        if is_authenticated():
            # Start clipboard sync
            start_account_sync()
        # Start RustDesk remote access launcher
        self.start_remote_client()
    
    # ======== Remote access tab methods ========
    def update_remote_status(self):
        """Update remote access status"""
        global REMOTE_CLIENT_RUNNING
        if REMOTE_CLIENT_RUNNING:
            self.remote_status.configure(text="● RustDesk открыт", text_color="green")
            self.remote_start_btn.configure(state="normal")
        else:
            self.remote_status.configure(text="● Готов открыть RustDesk", text_color="gray")
            self.remote_start_btn.configure(state="normal")
    
    def start_remote_client(self):
        """Start remote access client"""
        if start_remote_client():
            self.update_remote_status()
            self._append_remote_log("✓ RustDesk открыт")
        else:
            self._append_remote_log("✗ Не удалось открыть RustDesk")
    
    def stop_remote_client(self):
        """Stop remote access client"""
        stop_remote_client()
        self.update_remote_status()
        self._append_remote_log("ℹ Для закрытия просто закройте окно RustDesk")
    
    def _append_remote_log(self, text: str):
        """Append text to remote log textbox"""
        if hasattr(self, 'remote_log'):
            timestamp = time.strftime("%H:%M:%S")
            self.remote_log.insert('end', f"[{timestamp}] {text}\n")
            self.remote_log.see('end')

    def start_account_sync(self):
        """Start account-based synchronization"""
        if start_account_sync():
            messagebox.showinfo('Синхронизация', 'Синхронизация буфера обмена запущена')
        else:
            messagebox.showerror('Ошибка', 'Не удалось запустить синхронизацию. Проверьте авторизацию.')

    def stop_sync(self):
        """Stop all synchronization"""
        stop_clipboard_server()
        messagebox.showinfo('Синхронизация', 'Синхронизация остановлена')

    def change_mode(self):
        """Change between local and cloud server mode"""
        mode = self.mode_var.get()
        
        if mode == "local":
            settings['use_local_server'] = True
            # Сохраняем текущий облачный URL, не перезаписываем на localhost
            if not settings['sync_server_url'] or 'localhost' in settings['sync_server_url']:
                settings['sync_server_url'] = 'https://sea-lion-app-i3rnh.ondigitalocean.app/'
            self.server_frame.pack_forget()
            append_clipboard_log(f"[{datetime_now_str()}] Переключено на локальный режим")
        else:
            settings['use_local_server'] = False
            # Убеждаемся что используется правильный облачный URL
            if not settings['sync_server_url'] or 'localhost' in settings['sync_server_url']:
                settings['sync_server_url'] = 'https://sea-lion-app-i3rnh.ondigitalocean.app/'
            self.server_frame.pack(fill='x', padx=8, pady=4)
            append_clipboard_log(f"[{datetime_now_str()}] Переключено на облачный режим: {settings['sync_server_url']}")
            # Устанавливаем URL облачного сервера
            cloud_url = self.server_url_entry.get().strip()
            if not cloud_url or cloud_url.startswith(('ws://', 'wss://')):
                cloud_url = 'https://sea-lion-app-i3rnh.ondigitalocean.app/'
                self.server_url_entry.delete(0, 'end')
                self.server_url_entry.insert(0, cloud_url)
            settings['sync_server_url'] = cloud_url
            append_clipboard_log(f"[{datetime_now_str()}] Переключено на облачный режим: {cloud_url}")
        
        save_settings()
        self.update_account_status()

    def save_token(self, show_message=False, auto_start_bot=True):
        token = self.token_entry.get().strip()
        settings['telegram_token'] = token
        # Always save to both settings and token file
        with open(TOKEN_FILE, 'w', encoding='utf-8') as f:
            f.write(token)
        save_settings()
        # Only start bot if explicitly requested (to avoid multiple starts on typing)
        if token and auto_start_bot and show_message:
            start_telegram_bot(token)
            messagebox.showinfo('Telegram', 'Бот запущен (если токен корректен)')
        elif not show_message and GUI_LOG_TEXT:
            # Silent auto-save notification in logs
            GUI_LOG_TEXT.insert('end', '[INFO] Токен автоматически сохранён\n')
            GUI_LOG_TEXT.see('end')

    def paste_token(self):
        try:
            val = pyperclip.paste()
            if val:
                self.token_entry.delete(0, 'end')
                self.token_entry.insert(0, val)
                # Auto-save when pasted (without starting bot)
                self.save_token(show_message=False, auto_start_bot=False)
        except Exception:
            notify_error('Не удалось вставить из буфера обмена')

    def copy_token(self):
        try:
            pyperclip.copy(self.token_entry.get())
            notify_success('Токен скопирован в буфер обмена')
        except Exception:
            notify_error('Не удалось скопировать в буфер обмена')

    def on_token_change(self, event=None):
        # Auto-save token when typing (with small delay, without starting bot)
        if hasattr(self, '_token_timer'):
            self.after_cancel(self._token_timer)
        self._token_timer = self.after(1000, lambda: self.save_token(show_message=False, auto_start_bot=False))

    def load_appfile(self):
        path = filedialog.askopenfilename(filetypes=[('Text files', '*.txt')])
        if path:
            with open(path, 'r', encoding='utf-8') as src, open(APPS_FILE, 'w', encoding='utf-8') as dst:
                dst.write(src.read())
            messagebox.showinfo('App file', 'app.txt загружен')

    def save_name(self):
        global ASSISTANT_NAME
        name = self.name_entry.get().strip()
        if name:
            settings['assistant_name'] = name
            ASSISTANT_NAME = name.lower()
            save_settings()
            messagebox.showinfo('Имя', f'Имя ассистента сохранено: {name}')
        else:
            messagebox.showwarning('Имя', 'Введите имя ассистента')

# ----------------- Entry point -----------------
def main():
    # Start Telegram bot if token present
    token = settings.get('telegram_token')
    if token:
        try:
            start_telegram_bot(token)
        except Exception as e:
            print('Telegram start error:', e)

    # Register global hotkey to toggle voice control
    try:
        hk = settings.get('hotkey', 'f8')
        keyboard.add_hotkey(hk, toggle_voice)
        notify_success(f'Горячая клавиша для голоса: {hk.upper()}')
    except Exception:
        pass

    # Start voice listener via controller
    start_voice()

    # Auto-start services if already authenticated
    def auto_start_on_launch():
        time.sleep(2)  # Wait for GUI to initialize
        if is_authenticated():
            append_clipboard_log(f"[{datetime_now_str()}] Авто-запуск сервисов...")
            # Start clipboard sync
            start_account_sync()
            # Start remote access
            start_remote_client()
            append_clipboard_log(f"[{datetime_now_str()}] ✓ Сервисы запущены автоматически")
    
    threading.Thread(target=auto_start_on_launch, daemon=True).start()

    # Start GUI
    app = MilaGUI()
    app.mainloop()

if __name__ == '__main__':
    main()
