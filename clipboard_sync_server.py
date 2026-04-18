#!/usr/bin/env python3
"""
Простой демонстрационный сервер для синхронизации буфера обмена
Поддерживает аутентификацию пользователей и синхронизацию между устройствами
"""

import asyncio
import websockets
import json
import hashlib
import time
import sqlite3
import os
from typing import Dict, Set
import secrets
import logging

# Настройка логирования
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

class ClipboardSyncServer:
    def __init__(self, db_path='clipboard_sync.db'):
        self.db_path = db_path
        self.clients: Dict[str, websockets.WebSocketServerProtocol] = {}  # token -> websocket
        self.user_devices: Dict[str, Set[str]] = {}  # username -> set of tokens
        self.init_database()
        
    def init_database(self):
        """Инициализация базы данных"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        # Таблица пользователей
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL,
                email TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        
        # Таблица сессий/токенов
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS sessions (
                token TEXT PRIMARY KEY,
                username TEXT NOT NULL,
                device_id TEXT NOT NULL,
                device_name TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                last_seen TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (username) REFERENCES users (username)
            )
        ''')
        
        conn.commit()
        conn.close()
        logger.info("База данных инициализирована")

    def hash_password(self, password: str) -> str:
        """Хеширование пароля"""
        return hashlib.sha256(password.encode()).hexdigest()

    def generate_token(self) -> str:
        """Генерация токена сессии"""
        return secrets.token_urlsafe(32)

    async def register_user(self, username: str, password: str, email: str = "", device_id: str = "", device_name: str = "") -> dict:
        """Регистрация нового пользователя"""
        try:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            
            # Проверка существования пользователя
            cursor.execute("SELECT username FROM users WHERE username = ?", (username,))
            if cursor.fetchone():
                conn.close()
                return {'success': False, 'error': 'Пользователь уже существует'}
            
            # Создание пользователя
            password_hash = self.hash_password(password)
            cursor.execute("INSERT INTO users (username, password_hash, email) VALUES (?, ?, ?)",
                         (username, password_hash, email))
            
            # Создание сессии
            token = self.generate_token()
            cursor.execute("INSERT INTO sessions (token, username, device_id, device_name) VALUES (?, ?, ?, ?)",
                         (token, username, device_id, device_name))
            
            conn.commit()
            conn.close()
            
            # Добавляем в активные сессии
            if username not in self.user_devices:
                self.user_devices[username] = set()
            self.user_devices[username].add(token)
            
            logger.info(f"Зарегистрирован новый пользователь: {username}")
            return {'success': True, 'token': token}
            
        except Exception as e:
            logger.error(f"Ошибка регистрации пользователя {username}: {e}")
            return {'success': False, 'error': 'Ошибка сервера'}

    async def authenticate_user(self, username: str, password: str, device_id: str = "", device_name: str = "") -> dict:
        """Аутентификация пользователя"""
        try:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            
            # Проверка пользователя и пароля
            password_hash = self.hash_password(password)
            cursor.execute("SELECT username FROM users WHERE username = ? AND password_hash = ?",
                         (username, password_hash))
            
            if not cursor.fetchone():
                conn.close()
                return {'success': False, 'error': 'Неверное имя пользователя или пароль'}
            
            # Создание новой сессии
            token = self.generate_token()
            cursor.execute("INSERT INTO sessions (token, username, device_id, device_name) VALUES (?, ?, ?, ?)",
                         (token, username, device_id, device_name))
            
            conn.commit()
            conn.close()
            
            # Добавляем в активные сессии
            if username not in self.user_devices:
                self.user_devices[username] = set()
            self.user_devices[username].add(token)
            
            logger.info(f"Авторизован пользователь: {username}")
            return {'success': True, 'token': token}
            
        except Exception as e:
            logger.error(f"Ошибка аутентификации пользователя {username}: {e}")
            return {'success': False, 'error': 'Ошибка сервера'}

    def get_username_by_token(self, token: str) -> str:
        """Получение имени пользователя по токену"""
        try:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            cursor.execute("SELECT username FROM sessions WHERE token = ?", (token,))
            result = cursor.fetchone()
            conn.close()
            return result[0] if result else None
        except Exception as e:
            logger.error(f"Ошибка получения пользователя по токену: {e}")
            return None

    async def handle_websocket(self, websocket, path):
        """Обработка WebSocket подключения"""
        token = None
        username = None
        
        try:
            # Получение токена из заголовков
            auth_header = websocket.request_headers.get('Authorization')
            if auth_header and auth_header.startswith('Bearer '):
                token = auth_header[7:]
                username = self.get_username_by_token(token)
                
                if not username:
                    await websocket.close(code=1008, reason='Invalid token')
                    return
                
                # Регистрируем клиента
                self.clients[token] = websocket
                logger.info(f"Подключен клиент пользователя {username} (токен: {token[:8]}...)")
                
                # Отправляем список устройств
                device_list = list(self.user_devices.get(username, set()))
                await websocket.send(json.dumps({
                    'type': 'device_list',
                    'devices': device_list
                }))
                
                # Обработка сообщений
                async for message in websocket:
                    await self.handle_message(token, username, message)
            else:
                await websocket.close(code=1008, reason='Missing or invalid authorization')
                return
                
        except websockets.exceptions.ConnectionClosed:
            logger.info(f"Клиент отключен: {username}")
        except Exception as e:
            logger.error(f"Ошибка обработки WebSocket: {e}")
        finally:
            # Очистка при отключении
            if token and token in self.clients:
                del self.clients[token]
            if username and token:
                self.user_devices.get(username, set()).discard(token)

    async def handle_message(self, sender_token: str, sender_username: str, message: str):
        """Обработка сообщения от клиента"""
        try:
            data = json.loads(message)
            msg_type = data.get('type')
            
            logger.info(f"Получено сообщение типа {msg_type} от {sender_username}")
            
            # Пересылаем сообщение всем остальным устройствам пользователя
            user_tokens = self.user_devices.get(sender_username, set())
            
            for token in user_tokens:
                if token != sender_token and token in self.clients:
                    try:
                        await self.clients[token].send(message)
                    except Exception as e:
                        logger.error(f"Ошибка отправки сообщения клиенту {token}: {e}")
                        # Удаляем неактивного клиента
                        if token in self.clients:
                            del self.clients[token]
                        user_tokens.discard(token)
                        
        except json.JSONDecodeError:
            logger.error("Получено некорректное JSON сообщение")
        except Exception as e:
            logger.error(f"Ошибка обработки сообщения: {e}")

# Веб-сервер для HTTP API (аутентификация)
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs
import threading

class AuthHandler(BaseHTTPRequestHandler):
    def __init__(self, *args, sync_server=None, **kwargs):
        self.sync_server = sync_server
        super().__init__(*args, **kwargs)
    
    def do_OPTIONS(self):
        """Обработка CORS preflight запросов"""
        self.send_response(200)
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'POST, GET, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')
        self.end_headers()
    
    def do_POST(self):
        """Обработка POST запросов"""
        try:
            content_length = int(self.headers['Content-Length'])
            post_data = self.rfile.read(content_length)
            data = json.loads(post_data.decode('utf-8'))
            
            path = self.path
            
            if path == '/register':
                result = asyncio.run(self.sync_server.register_user(
                    data.get('username', ''),
                    data.get('password', ''),
                    data.get('email', ''),
                    data.get('device_id', ''),
                    data.get('device_name', '')
                ))
            elif path == '/auth':
                result = asyncio.run(self.sync_server.authenticate_user(
                    data.get('username', ''),
                    data.get('password', ''),
                    data.get('device_id', ''),
                    data.get('device_name', '')
                ))
            else:
                result = {'success': False, 'error': 'Неизвестный путь'}
            
            self.send_response(200)
            self.send_header('Access-Control-Allow-Origin', '*')
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps(result).encode())
            
        except Exception as e:
            self.send_response(500)
            self.send_header('Access-Control-Allow-Origin', '*')
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps({'success': False, 'error': str(e)}).encode())

async def main():
    """Запуск сервера"""
    sync_server = ClipboardSyncServer()
    
    # HTTP сервер для аутентификации
    def create_handler(*args, **kwargs):
        return AuthHandler(*args, sync_server=sync_server, **kwargs)
    
    http_server = HTTPServer(('0.0.0.0', 8080), create_handler)
    http_thread = threading.Thread(target=http_server.serve_forever, daemon=True)
    http_thread.start()
    
    logger.info("HTTP сервер запущен на порту 8080 (для аутентификации)")
    logger.info("WebSocket сервер запускается на порту 8765 (для синхронизации)")
    
    # WebSocket сервер для синхронизации
    async def websocket_handler(websocket, path):
        await sync_server.handle_websocket(websocket, path)
    
    async with websockets.serve(websocket_handler, '0.0.0.0', 8765):
        logger.info("Сервер синхронизации буфера обмена запущен!")
        logger.info("Используйте Ctrl+C для остановки")
        
        try:
            await asyncio.Future()  # Запуск навсегда
        except KeyboardInterrupt:
            logger.info("Остановка сервера...")

if __name__ == '__main__':
    asyncio.run(main())