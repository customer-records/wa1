import os
import sys
import time
import traceback
import argparse
import tempfile
import requests
import base64
import io
import hashlib
import shutil
from datetime import datetime, timedelta
from PIL import Image

from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from webdriver_manager.chrome import ChromeDriverManager

import pytz
import schedule

# Печать юникода в консоли (Windows-friendly)
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

# ─────── Константы ───────
TARGET_TIME = "04:45"
USER_DATA_DIR = os.path.abspath("./User_Data")

# ─────── Аргументы ───────
parser = argparse.ArgumentParser(description="Publish WhatsApp story via WhatsApp Web")
parser.add_argument("image", nargs="?", help="Path to the image to publish")
parser.add_argument("--image-url", required=False, help="URL of the image to download and publish")
parser.add_argument("--headless", action="store_true", help="Run Chrome in headless mode")
args = parser.parse_args()

# ─────── Определение изображения по дню недели ───────
def get_image_path_by_weekday():
    weekday = datetime.now(pytz.timezone("Europe/Moscow")).weekday()
    if weekday == 6:
        return None
    index = weekday if weekday < 5 else 0  # Суббота = повтор понедельника
    return os.path.abspath(f"story{index + 1}.JPEG")

# ─────── Загрузка изображения ───────
if args.image_url:
    try:
        response = requests.get(args.image_url)
        response.raise_for_status()
        suffix = os.path.splitext(args.image_url)[1] or ".jpg"
        tmpfile = tempfile.NamedTemporaryFile(delete=False, suffix=suffix)
        tmpfile.write(response.content)
        tmpfile.close()
        IMAGE_PATH = tmpfile.name
        print(f"✅ Изображение скачано во временный файл: {IMAGE_PATH}")
    except Exception as e:
        print(f"❌ Ошибка при скачивании изображения: {e}")
        sys.exit(1)
elif args.image:
    IMAGE_PATH = args.image
else:
    IMAGE_PATH = get_image_path_by_weekday()
    if IMAGE_PATH:
        print(f"ℹ️ Используется изображение по дню недели: {IMAGE_PATH}")
    else:
        print("⛔ Сегодня воскресенье — загрузка изображения пропущена.")

# ─────── Лог ───────
def log_browser_action(driver, message):
    print(message)
    with open("automation_combined_log.txt", "a", encoding="utf-8") as log_file:
        log_file.write(f"[{datetime.now().isoformat()}] {message}\n")
        try:
            log_file.write(driver.execute_script("return document.documentElement.outerHTML") + "\n\n")
        except Exception:
            pass

# ─────── Помощники авторизации ───────
QR_CANVAS_SEL = "canvas[aria-label='Scan this QR code to link a device!'], canvas[aria-label*='QR']"
AUTH_MARKERS_SEL = "span[data-icon='status-refreshed'], div[data-testid='chat-list']"

def is_authorized(driver):
    return len(driver.find_elements(By.CSS_SELECTOR, AUTH_MARKERS_SEL)) > 0

def has_qr(driver):
    return len(driver.find_elements(By.CSS_SELECTOR, QR_CANVAS_SEL)) > 0

def wait_for_qr_or_auth(driver, timeout=60):
    """Ждём либо QR на экране, либо признаки авторизации. Возвращает 'qr' или 'authorized'."""
    def _cond(d):
        if is_authorized(d):
            return "authorized"
        if has_qr(d):
            return "qr"
        return False
    return WebDriverWait(driver, timeout).until(_cond)

def wait_until_authorized(driver, timeout=180):
    WebDriverWait(driver, timeout).until(lambda d: is_authorized(d))
    return True

# ─────── Рисуем QR из PNG-байтов компактно (под ширину терминала) ───────
def draw_png_qr_to_console(png_bytes, max_width_chars=None):
    """
    Конвертирует PNG (canvas) в компактный ASCII-QR.
    Используются полублоки ▀ ▄ █, высота в 2 раза меньше ширины.
    """
    cols, _rows = shutil.get_terminal_size(fallback=(80, 24))
    if max_width_chars is None:
        max_width_chars = max(30, min(cols - 2, 100))

    img = Image.open(io.BytesIO(png_bytes)).convert("L")
    w, h = img.size
    if w > max_width_chars:
        scale = w / max_width_chars
        new_w = int(round(w / scale))
        new_h = int(round(h / scale))
    else:
        new_w, new_h = w, h
    if new_h % 2 == 1:
        new_h -= 1
    img_small = img.resize((max(1, new_w), max(2, new_h)), resample=Image.NEAREST)
    px = img_small.load()

    TH = 160
    def is_black(x, y): return px[x, y] < TH

    for y in range(0, img_small.height, 2):
        line = []
        for x in range(img_small.width):
            top = is_black(x, y)
            bot = is_black(x, y + 1) if y + 1 < img_small.height else False
            if top and bot:   ch = "█"
            elif top:         ch = "▀"
            elif bot:         ch = "▄"
            else:             ch = " "
            line.append(ch)
        print("".join(line))

# ─────── Показ/обновление QR в консоли (поллинг canvas) ───────
def show_qr_code_in_console(driver, watch_seconds=180, poll_interval=1.0):
    """
    Каждую секунду снимаем canvas -> PNG и перерисовываем ASCII-QR,
    если картинка изменилась. Останавливаемся при входе или по таймауту.
    """
    print("📸 Ожидаем/обновляем QR-код для авторизации...")
    start = time.time()
    last_sig = None

    def grab_png():
        try:
            data_url = driver.execute_script(f"""
                const c = document.querySelector("{QR_CANVAS_SEL}");
                return c ? c.toDataURL('image/png') : null;
            """)
            if not data_url or not data_url.startswith("data:image"):
                return None, None
            b64 = data_url.split(",", 1)[1]
            png_bytes = base64.b64decode(b64)
            sig = hashlib.md5(png_bytes).hexdigest()
            return png_bytes, sig
        except Exception:
            return None, None

    while time.time() - start < watch_seconds:
        if is_authorized(driver):
            print("✅ Авторизация подтверждена — QR больше не нужен.")
            return True

        png_bytes, sig = grab_png()
        if png_bytes and sig and sig != last_sig:
            last_sig = sig
            print("\n" + "─" * 52)
            print("🔁 Новый QR-код (обновился на странице):")
            draw_png_qr_to_console(png_bytes)

        time.sleep(poll_interval)

    print("⏳ Время ожидания QR истекло.")
    return False

# ─────── Проверка авторизации ───────
def check_or_authenticate_session():
    print("🔍 Проверка авторизации сессии перед стартом планировщика...")
    options = webdriver.ChromeOptions()
    options.add_argument("--window-size=1280,800")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument(f"--user-data-dir={USER_DATA_DIR}")  # сессия сохраняется, но НЕ является признаком авторизации
    if args.headless:
        options.add_argument("--headless=new")
        options.add_argument("--disable-gpu")

    driver = webdriver.Chrome(service=Service(ChromeDriverManager().install()), options=options)

    try:
        driver.get("https://web.whatsapp.com/")
        state = wait_for_qr_or_auth(driver, timeout=60)

        if state == "qr":
            print("📸 QR-код обнаружен. Необходима авторизация...")
            show_qr_code_in_console(driver)
            wait_until_authorized(driver, timeout=180)
            print("✅ Авторизация прошла успешно.")
        else:
            print("✅ Активная сессия обнаружена (без QR).")
    except Exception as e:
        print(f"❌ Ошибка при проверке авторизации: {e}")
    finally:
        driver.quit()

# ─────── Основная функция ───────
def publish_story():
    now_msk = datetime.now(pytz.timezone("Europe/Moscow"))
    print(f"\n🕒 {now_msk.strftime('%Y-%m-%d %H:%M:%S')} — запуск публикации.")

    if now_msk.weekday() == 6:
        print("⏸ Сегодня воскресенье — публикация пропущена.")
        return

    options = webdriver.ChromeOptions()
    options.add_argument("--window-size=1280,800")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument(f"--user-data-dir={USER_DATA_DIR}")  # используем для персистентности, но не доверяем ему как индикатору логина
    if args.headless:
        options.add_argument("--headless=new")
        options.add_argument("--disable-gpu")

    driver = webdriver.Chrome(service=Service(ChromeDriverManager().install()), options=options)

    try:
        log_browser_action(driver, "🚀 Открываем WhatsApp Web...")
        driver.get("https://web.whatsapp.com/")
        log_browser_action(driver, "🔐 Проверка авторизации...")

        state = wait_for_qr_or_auth(driver, timeout=60)
        if state == "qr":
            log_browser_action(driver, "📸 Обнаружен QR-код. Ждём авторизацию...")
            show_qr_code_in_console(driver)
            wait_until_authorized(driver, timeout=180)
            print("✅ Успешная авторизация.")
        else:
            log_browser_action(driver, "✅ Активная сессия WhatsApp найдена.")

        wait = WebDriverWait(driver, 60)
        status_icon = wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, "span[data-icon='status-refreshed']")))
        status_button = status_icon.find_element(By.XPATH, './ancestor::button')

        try:
            WebDriverWait(driver, 10).until_not(
                EC.presence_of_element_located((By.CSS_SELECTOR, "div[role='dialog']"))
            )
        except Exception:
            log_browser_action(driver, "ℹ️ Модальное окно не исчезло автоматически, продолжаем...")

        driver.execute_script("arguments[0].click();", status_button)
        log_browser_action(driver, "👉 Нажали на кнопку статуса")
        time.sleep(2)

        add_status_button = wait.until(EC.element_to_be_clickable((By.XPATH, "//button[@aria-label='Add Status' or contains(@title, 'статус')]")))
        try:
            add_status_button.click()
        except Exception:
            driver.execute_script("arguments[0].click();", add_status_button)

        log_browser_action(driver, "👉 Кнопка 'добавить статус'")
        time.sleep(1)

        media_button = wait.until(EC.element_to_be_clickable((By.XPATH, "//li[@role='button']//span[contains(text(), 'Фото') or contains(text(), 'Photo')]")))
        media_button.click()
        log_browser_action(driver, "👉 Кнопка 'Фото'")
        time.sleep(1)

        file_path = os.path.abspath(IMAGE_PATH)
        if not os.path.isfile(file_path):
            log_browser_action(driver, f"⚠️ Файл не найден: {file_path}")
            return

        file_input = wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, "input[type='file']")))
        file_input.send_keys(file_path)
        time.sleep(1)

        try:
            WebDriverWait(driver, 10).until(EC.presence_of_element_located((By.CSS_SELECTOR, "div[aria-label*='Просмотр']")))
            log_browser_action(driver, "👀 Preview загружен")
        except Exception:
            log_browser_action(driver, "⚠️ Preview не найден")

        try:
            preview_confirm = WebDriverWait(driver, 5).until(
                EC.element_to_be_clickable((By.XPATH, "//div[@role='button' and (@aria-label='Готово' or @aria-label='Done')]"))
            )
            preview_confirm.click()
            log_browser_action(driver, "👉 Клик по 'Готово'")
        except Exception:
            log_browser_action(driver, "ℹ️ 'Готово' не требуется")

        send_button = wait.until(EC.element_to_be_clickable((By.XPATH, "//div[@role='button' and @aria-label='Отправить']")))
        send_button.click()
        log_browser_action(driver, "✅ Сториз отправлен!")

        print("⏳ Ожидаем, пока статус выйдет из состояния 'Отправка...'")
        for _ in range(24):
            try:
                driver.find_element(By.XPATH, "//span[contains(text(),'Отправка')]")
                print("⏳ Все еще 'Отправка...' — ждем ещё...")
                time.sleep(5)
            except Exception:
                print("✅ Статус опубликован и 'Отправка...' исчезла!")
                break

        time.sleep(3)

    except Exception as e:
        log_browser_action(driver, f"⚠️ Ошибка: {e}")
        traceback.print_exc()
    finally:
        driver.quit()
        print("👋 Готово. Браузер закрыт.")

# ─────── Планировщик ───────
def log_and_publish():
    global IMAGE_PATH
    IMAGE_PATH = get_image_path_by_weekday()
    if IMAGE_PATH:
        publish_story()
    else:
        print("⏸ Сегодня воскресенье — задача публикации пропущена.")

    schedule.clear()
    schedule.every().day.at(TARGET_TIME).do(log_and_publish)

    next_run = schedule.next_run()
    if next_run:
        print(f"📆 Следующая публикация: {next_run.strftime('%Y-%m-%d %H:%M:%S')} (MSK)")

def run_schedule():
    check_or_authenticate_session()
    tz = pytz.timezone("Europe/Moscow")
    now = datetime.now(tz)
    today_target = tz.localize(datetime.combine(now.date(), datetime.strptime(TARGET_TIME, "%H:%M").time()))

    if now >= today_target:
        print("🕒 Время публикации на сегодня уже прошло — планируем на завтра.")
        schedule.every().day.at(TARGET_TIME).do(log_and_publish)
    else:
        print("🕒 Время публикации ещё впереди — планируем на сегодня.")
        schedule.every().day.at(TARGET_TIME).do(log_and_publish)

    while True:
        schedule.run_pending()
        time.sleep(1)

if __name__ == "__main__":
    run_schedule()