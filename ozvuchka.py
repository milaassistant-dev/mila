# main.py — Mila Assistant (SAPI via win32com)
# Требования:
# pip install pywin32 SpeechRecognition customtkinter pyautogui psutil pyperclip keyboard pillow

import os
import time
import threading
import re
import sys
import subprocess
import ctypes
import psutil
import speech_recognition as sr

# GUI (минимальный для теста)
import customtkinter as ctk
from tkinter import messagebox

# Windows TTS via COM (win32com)
try:
    import win32com.client
except Exception as e:
    win32com = None
    print("[WARN] win32com.client не доступен. Установите pywin32 (pip install pywin32).")

# ---------------- Настройки ----------------
ASSISTANT_NAME = "мила"  # имя ассистента в нижнем регистре

# pending confirmation: None or one of 'shutdown' / 'restart'
pending_confirmation = None
pending_lock = threading.Lock()

# ---------------- TTS через win32com (SAPI.SpVoice) ----------------
def init_sapi_voice():
    """Инициализировать объект SAPI.SpVoice, вернуть None при ошибке."""
    if win32com is None:
        return None
    try:
        speaker = win32com.client.Dispatch("SAPI.SpVoice")
        # Попробуем выбрать русскоязычный голос (Ирина/Irina/Павел)
        try:
            for v in speaker.GetVoices():
                vid = str(v.GetDescription())
                if any(name in vid for name in ("Ирина", "Irina", "Pavel", "Павел", "Михаил", "Mikhail")):
                    speaker.Voice = v
                    break
        except Exception:
            pass
        # Настройки голоса
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
    if not text:
        return
    print("[TTS]", text)
    if SPEAKER:
        try:
            SPEAKER.Speak(str(text))
        except Exception as e:
            print("[TTS ERROR]", e)
    else:
        try:
            ps = f'Add-Type –AssemblyName System.Speech; $s = New-Object System.Speech.Synthesis.SpeechSynthesizer; $s.Speak("{text}")'
            subprocess.Popen(["powershell", "-Command", ps], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception:
            pass


# ---------------- Закрытие всех программ ----------------
def close_all_except_mila():
    """Закрыть все процессы, кроме самой Милы и системных."""
    current_pid = os.getpid()
    whitelist = {"main.exe", "mila.exe", "python.exe", "pythonw.exe"}

    for proc in psutil.process_iter(['pid', 'name']):
        try:
            if proc.info['pid'] == current_pid:
                continue
            name = (proc.info['name'] or "").lower()
            if not any(w in name for w in whitelist):
                proc.terminate()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue


# ---------------- Системные действия ----------------
def _run_shutdown_command(mode: str) -> bool:
    cmd = ['shutdown', '/s', '/t', '0'] if mode == 'shutdown' else ['shutdown', '/r', '/t', '0']
    verb = 'выключить' if mode == 'shutdown' else 'перезагрузить'
    try:
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode == 0:
            return True
        err = (result.stderr or result.stdout or '').strip()
        print(f"[SHUTDOWN ERROR] Не удалось {verb} через shutdown (код {result.returncode})" + (f": {err}" if err else ''))
    except Exception as e:
        print(f"[SHUTDOWN ERROR] Не удалось {verb} через shutdown: {e}")
    # Fallback: try elevated shutdown (UAC prompt)
    try:
        params = '/s /t 0' if mode == 'shutdown' else '/r /t 0'
        rc = ctypes.windll.shell32.ShellExecuteW(None, 'runas', 'shutdown', params, None, 0)
        if rc <= 32:
            print('[SHUTDOWN ERROR] Не удалось запустить shutdown с правами администратора.')
            return False
        return True
    except Exception as e:
        print('[SHUTDOWN ERROR] Не удалось запустить shutdown с правами администратора: ' + str(e))
        return False

def system_shutdown():
    """Закрыть все приложения и выключить ПК."""
    speak("Подтверждение получено. Закрываю все программы.")
    try:
        close_all_except_mila()
        time.sleep(2)
        speak("Выключаю компьютер.")
        _run_shutdown_command('shutdown')
    except Exception as e:
        print("[SHUTDOWN ERROR]", e)


def system_restart():
    """Закрыть все приложения и перезагрузить ПК."""
    speak("Подтверждение получено. Закрываю все программы.")
    try:
        close_all_except_mila()
        time.sleep(2)
        speak("Перезагружаю компьютер.")
        _run_shutdown_command('restart')
    except Exception as e:
        print("[RESTART ERROR]", e)


# ---------------- Обработчик текста команд ----------------
def handle_command_text(text: str):
    """Обработать текстовую команду (нижний регистр)."""
    global pending_confirmation
    if not text:
        return
    t = text.lower().strip()
    print("[CMD]", t)

    with pending_lock:
        if pending_confirmation:
            if t in ("да", "давай", "выполняй", "выполни", "подтверждаю", "ок"):
                act = pending_confirmation
                pending_confirmation = None
                if act == 'shutdown':
                    system_shutdown()
                elif act == 'restart':
                    system_restart()
                return
            if t in ("нет", "отмена", "не надо"):
                pending_confirmation = None
                speak("Хорошо, действие отменено.")
                print("[OK] Действие отменено пользователем.")
                return

    # Команда "выключи компьютер"
    if ("выключи компьютер" in t) or ("выключить компьютер" in t) or (
        ("выключи" in t or "выключить" in t) and "звук" not in t and "мут" not in t
    ):
        with pending_lock:
            pending_confirmation = 'shutdown'
        print("[INFO] Запрос на выключение компьютера получен")
        speak("Вы желаете выключить компьютер? Скажите да для подтверждения или нет для отмены.")
        return

    # Команда "перезагрузи компьютер"
    if "перезагрузи" in t or "перезагрузить" in t:
        with pending_lock:
            pending_confirmation = 'restart'
        print("[INFO] Запрос на перезагрузку компьютера получен")
        speak("Вы желаете перезагрузить компьютер? Скажите да для подтверждения или нет для отмены.")
        return

    if any(k in t for k in ("ты тут", "ты здесь", "на месте")):
        speak("Да, я тут")
        print("[OK] Да, я тут")
        return

    if t in ("да", "нет"):
        speak("Я не уверена, к чему относится подтверждение.")
        return

    speak("Команда не распознана.")
    print("[UNKNOWN COMMAND]", t)


# ---------------- Голосовой слушатель ----------------
recognizer = sr.Recognizer()


def is_activation_phrase(recognized_text: str) -> bool:
    """True если речь адресована ассистенту или это короткий ответ при подтверждении."""
    if not recognized_text:
        return False
    txt = recognized_text.lower().strip()
    with pending_lock:
        if pending_confirmation and txt in ("да", "нет", "отмена", "не надо"):
            return True
    words = re.split(r'\s+', txt)
    if words and words[0] == ASSISTANT_NAME:
        return True
    if ASSISTANT_NAME in words:
        return True
    return False


def parse_core_command(recognized_text: str):
    """Убрать имя ассистента и вернуть остальную часть команды (или короткий ответ)."""
    if not recognized_text:
        return None
    txt = recognized_text.lower().strip()
    with pending_lock:
        if pending_confirmation and txt in ("да", "нет", "отмена", "не надо", "выполни", "выполняй"):
            return txt
    if txt.startswith(ASSISTANT_NAME):
        core = txt[len(ASSISTANT_NAME):].strip(" ,:—-")
        return core if core else None
    parts = txt.split()
    if ASSISTANT_NAME in parts:
        parts.remove(ASSISTANT_NAME)
        core = " ".join(parts).strip()
        return core if core else None
    return None


def listen_loop():
    """Основной цикл прослушивания микрофона."""
    try:
        mic = sr.Microphone()
    except Exception as e:
        print("[VOICE ERROR] Не удалось получить микрофон:", e)
        speak("Не удалось получить доступ к микрофону.")
        return

    speak("Голосовое управление включено.")
    with mic as source:
        while True:
            try:
                recognizer.adjust_for_ambient_noise(source, duration=0.5)
                audio = recognizer.listen(source, timeout=5, phrase_time_limit=7)
                try:
                    text = recognizer.recognize_google(audio, language="ru-RU")
                except sr.UnknownValueError:
                    continue
                except sr.RequestError as e:
                    print("[VOICE REQ ERROR]", e)
                    speak("Ошибка сервиса распознавания речи.")
                    time.sleep(2)
                    continue

                print("[VOICE] распознано:", text)
                if is_activation_phrase(text):
                    core = parse_core_command(text)
                    if core:
                        threading.Thread(target=handle_command_text, args=(core,), daemon=True).start()
            except sr.WaitTimeoutError:
                continue
            except KeyboardInterrupt:
                break
            except Exception as e:
                print("[VOICE LOOP EX]", e)
                time.sleep(1)


# ---------------- Простое GUI для теста ----------------
class MilaGUI(ctk.CTk):
    def __init__(self):
        super().__init__()
        self.title("Мила — тестовый ассистент")
        self.geometry("380x240")
        ctk.set_appearance_mode("System")
        ctk.set_default_color_theme("blue")

        ctk.CTkLabel(self, text="Мила — тестовый ассистент", font=("Arial", 16)).pack(pady=10)
        ctk.CTkButton(self, text="Тест: Мила, ты тут", command=lambda: handle_command_text("мила ты тут")).pack(pady=8)
        ctk.CTkButton(self, text="Тест: Мила, выключи компьютер", command=lambda: handle_command_text("мила выключи компьютер")).pack(pady=8)
        ctk.CTkButton(self, text="Тест: Мила, перезагрузи компьютер", command=lambda: handle_command_text("мила перезагрузи компьютер")).pack(pady=8)
        ctk.CTkButton(self, text="Запустить слушатель", command=lambda: threading.Thread(target=listen_loop, daemon=True).start()).pack(pady=8)


# ---------------- Entry point ----------------
def main():
    app = MilaGUI()
    app.mainloop()


if __name__ == "__main__":
    main()
