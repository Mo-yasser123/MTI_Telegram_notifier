import discord
import os
import json
import asyncio
from dotenv import load_dotenv
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

load_dotenv()
# تأكد أن المتغيرات السرية في GitHub تحمل هذه الأسماء بالضبط
DISCORD_TOKEN = os.getenv('DISCORD_TOKEN')
CHANNEL_ID = os.getenv('CHANNEL_ID')
UNI_USER = os.getenv('UNI_USER')
UNI_PASS = os.getenv('UNI_PASS')

def load_sent_events():
    try:
        # التعديل الجوهري لتخطي بصمة BOM
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
        
        WebDriverWait(driver, 15).until(EC.presence_of_element_located((By.CLASS_NAME, "event")))
        events = driver.find_elements(By.CLASS_NAME, "event")
        
        saved_events = load_sent_events()
        
        for event in events:
            title = event.find_element(By.CSS_SELECTOR, "a.text-truncate").text
            date_time = event.find_element(By.CSS_SELECTOR, "div.date.small").text.strip()
            event_id = f"{title}_{date_time}"
            
            if event_id not in saved_events:
                new_events.append({"title": title, "date": date_time, "id": event_id})
                save_sent_event(event_id)
                
    except Exception as e:
        print(f"Scraping error: {e}")
    finally:
        driver.quit()
        
    return new_events

intents = discord.Intents.default()
client = discord.Client(intents=intents)

@client.event
async def on_ready():
    print(f'Logged in as {client.user}')
    
    upcoming_events = scrape_moodle()
    
    if upcoming_events:
        try:
            # تحويل معرف القناة إلى رقم صحيح (Integer)
            channel = client.get_channel(int(CHANNEL_ID))
            if channel:
                for event in upcoming_events:
                    course_name = event['title']
                    task_type = "تحديث جامعي"
                    
                    if "-" in course_name:
                        parts = course_name.split("-")
                        course_name
