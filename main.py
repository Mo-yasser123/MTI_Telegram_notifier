import requests
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.common.action_chains import ActionChains
import os
import json
import time
from dotenv import load_dotenv

load_dotenv()
TELEGRAM_TOKEN = os.getenv('TELEGRAM_TOKEN')
CHAT_ID = os.getenv('CHAT_ID')
UNI_USER = os.getenv('UNI_USER')
UNI_PASS = os.getenv('UNI_PASS')

def get_professor_name(course_name):
    name_lower = course_name.lower()
    if "english language" in name_lower:
        return "Dr. Rasha Saeed"
    elif "introduction to programming" in name_lower:
        return "Dr. HAFEZ M.S. ABDEL-WAHAB"
    elif "introduction to computer science" in name_lower:
        return "Dr. Mohamed M. Elgazzar"
    elif "linear algebra" in name_lower:
        return "Walid Basiony"
    elif "mathematical analysis" in name_lower:
        return "Dr. Rania Ahmed"
    elif "entrepreneur" in name_lower:
        return "Dr. El-sayed Orabi"
    return "Not Specified"

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
        
        time.sleep(5)
        
        WebDriverWait(driver, 15).until(EC.presence_of_element_located((By.CLASS_NAME, "event")))
        events = driver.find_elements(By.CLASS_NAME, "event")
        
        saved_events = load_sent_events()
        
        for i in range(len(events)):
            try:
                current_events = driver.find_elements(By.CLASS_NAME, "event")
                event = current_events[i]
                
                title_el = event.find_element(By.CSS_SELECTOR, "a.text-truncate")
                raw_title = title_el.get_attribute("title")
                if not raw_title:
                    raw_title = title_el.get_attribute("textContent")
                raw_title = raw_title.strip()
                
                event_link = title_el.get_attribute("href")
                if not event_link:
                    event_link = "https://elearning-01.mti.edu.eg/"
                
                date_el = event.find_element(By.CSS_SELECTOR, "div.date.small")
                date_time = date_el.get_attribute("textContent").strip()
                date_time = " ".join(date_time.split())
                
                event_id = f"{raw_title}_{date_time}"
                
                if event_id not in saved_events:
                    driver.execute_script("arguments[0].click();", title_el)
                    time.sleep(2)
                    
                    course_name = None
                    description = ""
                    
                    try:
                        modals = driver.find_elements(By.CLASS_NAME, "modal-content")
                        active_modal = None
                        for m in modals:
                            if m.is_displayed():
                                active_modal = m
                                break
                        
                        if active_modal:
                            desc_elements = active_modal.find_elements(By.CLASS_NAME, "description-content")
                            if desc_elements:
                                raw_desc = desc_elements[0].get_attribute("textContent").strip()
                                # استبدال المسافات المتعددة والأسطر الجديدة بمسافة واحدة لتنظيف النص
                                description = " ".join(raw_desc.split())
                            
                            links = active_modal.find_elements(By.TAG_NAME, "a")
                            for link in links:
                                href = link.get_attribute("href")
                                # الفلترة الدقيقة: اسم المادة دائماً يحتوي على course/view.php في رابطه
                                if href and "course/view.php" in href:
                                    course_name = link.get_attribute("textContent").strip()
                                    break
                                
                    except Exception as modal_e:
                        print(f"Could not parse modal for {raw_title}: {modal_e}")
                    
                    try:
                        ActionChains(driver).send_keys(Keys.ESCAPE).perform()
                        time.sleep(1)
                    except:
                        driver.refresh()
                        time.sleep(5)

                    new_events.append({
                        "raw_title": raw_title, 
                        "date": date_time, 
                        "link": event_link,
                        "id": event_id,
                        "course_name_from_modal": course_name,
                        "description": description
                    })
                    save_sent_event(event_id)
            except Exception as inner_e:
                print(f"Error parsing event: {inner_e}")
                continue
                
    except Exception as e:
        print(f"Scraping error: {e}")
    finally:
        driver.quit()
        
    return new_events

def send_telegram_message(events):
    for event in events:
        raw_title = event['raw_title']
        task_name = raw_title
        
        if event.get("course_name_from_modal"):
            course_name = event["course_name_from_modal"]
        elif "-" in raw_title:
             parts = raw_title.split("-", 1)
             course_name = parts[0].replace("F26", "").strip()
             task_name = parts[1].strip()
        else:
            course_name = "Check Link For Details"

        if " is due" in raw_title:
            task_name = raw_title.replace(" is due", "").strip()

        professor = get_professor_name(course_name)

        message = f"🚨 <b>New Academic Update</b>\n\n"
        message += f"📚 <b>Course:</b> <code>{course_name}</code>\n"
        message += f"👨‍🏫 <b>Professor:</b> {professor}\n"
        message += f"📝 <b>Task:</b> {task_name}\n"
        message += f"⏳ <b>Deadline:</b> {event['date']}\n"
        
        if event.get("description"):
            desc = event["description"]
            if len(desc) > 100:
                desc = desc[:97] + "..."
            message += f"ℹ️ <b>Details:</b> <i>{desc}</i>\n"

        message += f"\n🔗 <a href='{event['link']}'><b>Click here to view details</b></a>"

        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        payload = {
            "chat_id": CHAT_ID,
            "text": message,
            "parse_mode": "HTML",
            "disable_web_page_preview": True
        }
        
        try:
            response = requests.post(url, json=payload)
            if response.status_code == 200:
                print(f"Successfully sent: {task_name}")
            else:
                print(f"Failed to send: {response.text}")
        except Exception as e:
            print(f"Telegram API error: {e}")

if __name__ == "__main__":
    print("Starting MTI Telegram Scraper...")
    upcoming_events = scrape_moodle()
    if upcoming_events:
        send_telegram_message(upcoming_events)
    else:
        print("No new events found.")
