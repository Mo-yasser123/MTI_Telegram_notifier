import requests
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
import os
import json
import time  # ضروري لانتظار تحميل البلوكات
from dotenv import load_dotenv

load_dotenv()
TELEGRAM_TOKEN = os.getenv('TELEGRAM_TOKEN')
CHAT_ID = os.getenv('CHAT_ID')
UNI_USER = os.getenv('UNI_USER')
UNI_PASS = os.getenv('UNI_PASS')

def load_sent_events():
    try:
        with open('sent_events.json', 'r', encoding='utf-8-sig') as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return []

def save_sent_event(event_id):
    events = load_sent_events()
    if event_id not in events:
        events.append(event_id)
        with open('sent_events.json', 'w', encoding='utf-8') as f:
            json.dump(events, f, ensure_ascii=False, indent=4)

def scrape_moodle():
    chrome_options = Options()
    chrome_options.add_argument("--headless")
    chrome_options.add_argument("--disable-gpu")
    chrome_options.add_argument("--no-sandbox")
    chrome_options.add_argument("--disable-dev-shm-usage")
    chrome_options.add_argument("--window-size=1920,1080")
    
    driver = webdriver.Chrome(options=chrome_options)
    new_events = []

    try:
        driver.get("https://elearning-01.mti.edu.eg/login/index.php?loginredirect=1")
        
        WebDriverWait(driver, 10).until(EC.presence_of_element_located((By.ID, "username"))).send_keys(UNI_USER)
        driver.find_element(By.ID, "password").send_keys(UNI_PASS)
        
        login_btn = driver.find_element(By.ID, "loginbtn")
        driver.execute_script("arguments[0].click();", login_btn)
        
        # إجبار البوت على الانتظار 5 ثوانٍ ليتم تحميل Upcoming events بالكامل
        time.sleep(5)
        
        WebDriverWait(driver, 15).until(EC.presence_of_element_located((By.CLASS_NAME, "event")))
        events = driver.find_elements(By.CLASS_NAME, "event")
        
        saved_events = load_sent_events()
        
        for event in events:
            try:
                # استخدام title و textContent لضمان جلب النص حتى لو كان مخفياً
                title_el = event.find_element(By.CSS_SELECTOR, "a.text-truncate")
                title = title_el.get_attribute("title")
                if not title:
                    title = title_el.get_attribute("textContent")
                title = title.strip()
                
                date_el = event.find_element(By.CSS_SELECTOR, "div.date.small")
                date_time = date_el.get_attribute("textContent").strip()
                
                # تنظيف النص من أي مسافات زائدة
                date_time = " ".join(date_time.split())
                
                event_id = f"{title}_{date_time}"
                
                if event_id not in saved_events:
                    new_events.append({"title": title, "date": date_time, "id": event_id})
                    save_sent_event(event_id)
            except Exception as inner_e:
                print(f"Error parsing single event: {inner_e}")
                continue
                
    except Exception as e:
        print(f"Scraping error: {e}")
    finally:
        driver.quit()
        
    return new_events

def send_telegram_message(events):
    for event in events:
        course_name = event['title']
        task_type = "تحديث جامعي"
        
        if "-" in course_name:
            parts = course_name.split("-")
            course_name = parts[0].replace("F26", "").strip()
            task_type = parts[1].strip()

        message = f"🚨 <b>{task_type}</b>\n\n"
        message += f"📚 <b>المادة:</b> <code>{course_name}</code>\n"
        message += f"⏳ <b>الموعد:</b> {event['date']}\n\n"
        message += f"🔗 <a href='https://elearning-01.mti.edu.eg/'>الدخول لمنصة MTI</a>"

        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        payload = {
            "chat_id": CHAT_ID,
            "text": message,
            "parse_mode": "HTML"
        }
        
        try:
            response = requests.post(url, json=payload)
            if response.status_code == 200:
                print(f"Successfully sent: {course_name}")
            else:
                print(f"Failed to send: {response.text}")
        except Exception as e:
            print(f"Telegram API error: {e}")

if __name__ == "__main__":
    upcoming_events = scrape_moodle()
    if upcoming_events:
        send_telegram_message(upcoming_events)
    else:
        print("No new events to save")
