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

def load_json(filename):
    try:
        with open(filename, 'r', encoding='utf-8-sig') as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return []

def save_json(filename, data):
    with open(filename, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=4)

def scrape_moodle():
    chrome_options = Options()
    chrome_options.add_argument("--headless")
    chrome_options.add_argument("--disable-gpu")
    chrome_options.add_argument("--no-sandbox")
    chrome_options.add_argument("--disable-dev-shm-usage")
    chrome_options.add_argument("--window-size=1920,1080")
    
    # تحسينات السرعة لضمان عدم توقف السكربت (Timeouts)
    chrome_options.page_load_strategy = 'eager'
    chrome_options.add_argument("--blink-settings=imagesEnabled=false")
    
    driver = webdriver.Chrome(options=chrome_options)
    wait = WebDriverWait(driver, 10) # استخدام الانتظار الديناميكي
    
    new_events = []
    new_grades = []

    try:
        # ==========================================
        # 1. تسجيل الدخول
        # ==========================================
        driver.get("https://elearning-01.mti.edu.eg/login/index.php?loginredirect=1")
        wait.until(EC.presence_of_element_located((By.ID, "username"))).send_keys(UNI_USER)
        driver.find_element(By.ID, "password").send_keys(UNI_PASS)
        login_btn = driver.find_element(By.ID, "loginbtn")
        driver.execute_script("arguments[0].click();", login_btn)
        
        # انتظار ظهور لوحة المنصة بدلاً من الوقت الثابت
        wait.until(EC.presence_of_element_located((By.CLASS_NAME, "event")))

        # ==========================================
        # 2. سحب التكليفات (Upcoming Events)
        # ==========================================
        saved_events = load_json('sent_events.json')
        try:
            events = driver.find_elements(By.CLASS_NAME, "event")
            for i in range(len(events)):
                current_events = wait.until(EC.presence_of_all_elements_located((By.CLASS_NAME, "event")))
                event = current_events[i]
                
                title_el = event.find_element(By.CSS_SELECTOR, "a.text-truncate")
                raw_title = title_el.get_attribute("title") or title_el.get_attribute("textContent")
                raw_title = raw_title.strip()
                event_link = title_el.get_attribute("href") or "https://elearning-01.mti.edu.eg/"
                
                date_el = event.find_element(By.CSS_SELECTOR, "div.date.small")
                date_time = " ".join(date_el.get_attribute("textContent").strip().split())
                
                event_id = f"{raw_title}_{date_time}"
                
                if event_id not in saved_events:
                    driver.execute_script("arguments[0].click();", title_el)
                    
                    course_name = None
                    description = ""
                    try:
                        # انتظار ديناميكي للنافذة المنبثقة
                        active_modal = wait.until(EC.visibility_of_element_located((By.CLASS_NAME, "modal-content")))
                        desc_elements = active_modal.find_elements(By.CLASS_NAME, "description-content")
                        if desc_elements:
                            description = " ".join(desc_elements[0].get_attribute("textContent").strip().split())
                        
                        links = active_modal.find_elements(By.TAG_NAME, "a")
                        for link in links:
                            href = link.get_attribute("href")
                            if href and "course/view.php" in href:
                                course_name = link.get_attribute("textContent").strip()
                                break
                    except:
                        pass
                    
                    try:
                        ActionChains(driver).send_keys(Keys.ESCAPE).perform()
                        wait.until(EC.invisibility_of_element_located((By.CLASS_NAME, "modal-backdrop")))
                    except:
                        driver.refresh()
                        wait.until(EC.presence_of_element_located((By.CLASS_NAME, "event")))

                    new_events.append({
                        "raw_title": raw_title, "date": date_time, 
                        "link": event_link, "id": event_id,
                        "course_name_from_modal": course_name, "description": description
                    })
                    saved_events.append(event_id)
            save_json('sent_events.json', saved_events)
        except Exception as e:
            print(f"Error checking events: {e}")

        # ==========================================
        # 3. سحب درجات الميدتيرم (Grades Scraper)
        # ==========================================
        saved_grades = load_json('sent_grades.json')
        try:
            driver.get("https://elearning-01.mti.edu.eg/my/")
            wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, "a[href*='course/view.php?id=']")))
            
            course_links = driver.find_elements(By.CSS_SELECTOR, "a[href*='course/view.php?id=']")
            courses = {}
            for link in course_links:
                href = link.get_attribute("href")
                name = link.get_attribute("textContent").strip()
                try:
                    c_id = href.split("id=")[1].split("&")[0]
                    if c_id.isdigit() and name:
                        courses[c_id] = name
                except:
                    continue

            keywords = ["mid", "mt", "ميد"]
            for c_id, c_name in courses.items():
                grade_id = f"{c_id}_midterm"
                if grade_id in saved_grades:
                    continue 
                
                grade_url = f"https://elearning-01.mti.edu.eg/grade/report/user/index.php?id={c_id}"
                driver.get(grade_url)
                
                try:
                    wait.until(EC.presence_of_element_located((By.TAG_NAME, "tr")))
                    rows = driver.find_elements(By.TAG_NAME, "tr")
                    for row in rows:
                        text_content = row.get_attribute("textContent").lower()
                        if any(kw in text_content for kw in keywords):
                            grade_cell = row.find_element(By.CSS_SELECTOR, "[class*='column-grade']")
                            grade_val = grade_cell.get_attribute("textContent").strip()
                            
                            if grade_val and grade_val != "-" and grade_val != "":
                                new_grades.append({
                                    "course_name": c_name,
                                    "link": grade_url
                                })
                                saved_grades.append(grade_id)
                                break 
                except:
                    continue
            save_json('sent_grades.json', saved_grades)
        except Exception as e:
            print(f"Error checking grades: {e}")

    except Exception as e:
        print(f"Scraping error: {e}")
    finally:
        driver.quit()
        
    return {"events": new_events, "grades": new_grades}

def send_telegram_messages(data):
    for event in data.get("events", []):
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
        send_to_telegram(message)

    for grade in data.get("grades", []):
        course_name = grade['course_name']
        link = grade['link']
        
        message = f" <b>منورين يا شباب...</b>\n\n"
        message += f"📊 <b>درجه امتحان</b> <code>{course_name}</code> <b>نزل.</b>\n\n"
        message += f"🔗 <a href='{link}'><b>اضغط هنا لمعرفة النتيجة</b></a>"
        
        send_to_telegram(message)

def send_to_telegram(message):
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
            print("Message sent successfully.")
        else:
            print(f"Failed to send: {response.text}")
    except Exception as e:
        print(f"Telegram API error: {e}")

if __name__ == "__main__":
    print("Starting Optimized MTI Telegram Scraper...")
    scraped_data = scrape_moodle()
    
    if scraped_data["events"] or scraped_data["grades"]:
        send_telegram_messages(scraped_data)
    else:
        print("No new events or grades found.")
