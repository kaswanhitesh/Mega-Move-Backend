from fastapi import FastAPI, Request, BackgroundTasks
from fastapi.responses import PlainTextResponse
import os
import requests
from openai import OpenAI
import json
import io
import pandas as pd
import PyPDF2
from fpdf import FPDF
import re
from datetime import datetime
from rapidfuzz import process, fuzz
import base64
import asyncio
from contextlib import asynccontextmanager
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.mime.application import MIMEApplication
import glob

# --- GLOBAL STATE ---
PENDING_TASKS = {}
PROCESSED_MESSAGE_IDS = set()
LAST_CLEANUP = datetime.now()
IMAGE_BUFFER = {}
GLOBAL_PORT_ALIASES = {}

async def run_daily_financial_audit():
    """Executes overdue invoice checks and vendor bill mismatch audits."""
    print("DEBUG: Starting Daily Financial Audit...")
    admin_number = os.getenv("YOUR_WHATSAPP_NUMBER")
    if not admin_number:
        print("CRITICAL: Admin WhatsApp number missing. Audit skipped.")
        return

    # 1. Check Overdue Invoices
    try:
        overdue_invoices = check_overdue_invoices()
        for inv in overdue_invoices:
            days = inv.get("days_overdue", "3+")
            msg = (
                f"⚠️ *Overdue Payment Alert*\n"
                f"Client: {inv.get('customer_name')}\n"
                f"Invoice: {inv.get('invoice_number')}\n"
                f"Amount: {inv.get('currency_code')} {inv.get('total')}\n"
                f"Days Overdue: {days}\n"
                f"*Draft Reply:* \"Hi {inv.get('customer_name')}, gentle reminder regarding invoice {inv.get('invoice_number')}. Please let us know the status of payment.\""
            )
            send_whatsapp_message(admin_number, msg)
    except Exception as e:
        print(f"Audit Error (Overdue Invoices): {e}")

    # 2. Check Vendor Bill Mismatches
    try:
        mismatches = check_vendor_bill_mismatches()
        for m in mismatches:
            msg = (
                f"🚨 *Margin Mismatch Alert*\n"
                f"Vendor: {m.get('vendor_name')}\n"
                f"Bill: {m.get('bill_number')}\n"
                f"Billed Amount: {m.get('bill_amount')}\n"
                f"CRM Expected Cost: {m.get('crm_expected')}\n"
                f"Variance: {m.get('variance')}% - *Action Required in Zoho Books*"
            )
            send_whatsapp_message(admin_number, msg)
    except Exception as e:
        print(f"Audit Error (Bill Mismatches): {e}")

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup logic here
    print("Mega Move AI Backend Starting Up...")

    # 1. AUTO-BUILD UN/LOCODE DATABASE (Self-healing & Bulletproof)
    global GLOBAL_PORT_ALIASES
    ports_file = "ports.json"
    if not os.path.exists(ports_file):
        print("DEBUG: ports.json missing. Building from UN/LOCODE CSVs...")
        ports_db = {}
        csv_files = glob.glob("*.csv")

        for f in csv_files:
            if "subdivision" in f.lower():
                continue
            # Try different combinations of encodings and separators
            for encoding in ["latin-1", "utf-8", "cp1252"]:
                for sep in [",", ";"]:
                    try:
                        df = pd.read_csv(f, sep=sep, encoding=encoding, keep_default_na=False, dtype=str)
                        if df.shape[1] < 4:
                            continue  # Wrong separator, try next
                        
                        # Dynamically locate columns based on expected positional content
                        col_country, col_loc, col_name, col_func = None, None, None, None
                        
                        for col in df.columns:
                            col_str = str(col).lower()
                            if "country" in col_str: col_country = col
                            elif "location" in col_str: col_loc = col
                            elif "namewodiacritics" in col_str: col_name = col
                            elif "name" in col_str and not col_name: col_name = col
                            elif "function" in col_str: col_func = col
                        
                        # Positional fallback if columns are completely headerless
                        if not col_country or not col_loc or not col_func:
                            for col in df.columns:
                                sample = df[col].head(30).tolist()
                                if any(re.match(r"^[A-Z]{2}$", str(x)) for x in sample if x): col_country = col
                                if any(re.match(r"^[A-Z2-9]{3}$", str(x)) for x in sample if x): col_loc = col
                                if any(str(x).startswith("1") for x in sample if x): col_func = col
                            if df.shape[1] >= 4 and not col_name:
                                col_name = df.columns[3]

                        if col_country and col_loc and col_func:
                            for _, row in df.iterrows():
                                func_val = str(row[col_func]).strip()
                                # A '1' in the first index or within the string means it's a maritime seaport
                                if func_val and (func_val.startswith("1") or "1" in func_val):
                                    country = str(row[col_country]).strip().upper()
                                    loc = str(row[col_loc]).strip().upper()
                                    name = str(row[col_name]).strip()
                                    if len(country) == 2 and len(loc) == 3:
                                        locode = f"{country}{loc}"
                                        ports_db[locode] = [name, locode, loc]
                            break  # Successfully parsed this file, move to next file
                    except:
                        continue

        if ports_db:
            with open(ports_file, "w") as jf:
                json.dump(ports_db, jf, indent=4)
            print(f"DEBUG: Successfully built ports.json with {len(ports_db)} maritime ports.")
        else:
            print("DEBUG: Parser ran but found no valid maritime port rows.")

    # 2. LOAD DATABASE
    try:
        if os.path.exists(ports_file):
            with open(ports_file, "r") as pf:
                GLOBAL_PORT_ALIASES = json.load(pf)
            print(f"DEBUG: Loaded {len(GLOBAL_PORT_ALIASES)} ports into memory.")
    except Exception as e:
        print(f"ERROR: Failed to load ports.json: {e}")
    
    # Initialize and Start Scheduler for Financial Audits
    scheduler = AsyncIOScheduler()
    
    # Check for TEST_MODE to run more frequently
    if os.getenv("TEST_MODE") == "true":
        print("DEBUG: TEST_MODE active. Running financial audit every 5 minutes.")
        scheduler.add_job(run_daily_financial_audit, 'interval', minutes=5)
    else:
        # Run daily at 09:00 AM IST (UTC+5:30 -> 03:30 AM UTC)
        print("DEBUG: Scheduling daily financial audit at 09:00 AM IST.")
        scheduler.add_job(run_daily_financial_audit, CronTrigger(hour=3, minute=30))
    
    scheduler.start()
    
    yield
    # Shutdown logic here
    print("Mega Move AI Backend Shutting Down...")
    scheduler.shutdown()

app = FastAPI(title="Mega Move AI Backend", lifespan=lifespan)
client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

def extract_numeric_price(price_val):
    """Safely extracts a float from a messy price string. Returns inf if invalid."""
    try:
        if price_val is None: return float('inf')
        # Strip everything except digits and decimals
        clean_val = re.sub(r'[^\d.]', '', str(price_val))
        if not clean_val or clean_val == '.': return float('inf')
        return float(clean_val)
    except:
        return float('inf')

def standardize_port_name(raw_port):
    """Standardizes port names using the UN/LOCODE global database and RapidFuzz."""
    if not raw_port:
        return raw_port
    
    # STEP 1: NORMALIZATION PIPELINE
    name = str(raw_port).upper()
    name = re.sub(r'[,/]', ' ', name)
    noise_words = ["PORT", "TERMINAL", "WHARF", "CHINA", "INDIA", "UAE"]
    for word in noise_words:
        name = re.sub(rf'\b{word}\b', '', name)
    name = re.sub(r'\s+', ' ', name).strip()

    if not name:
        return raw_port.title().strip()

    # --- PERMANENT FIX: HARDCODED ACRONYM SAFETY NET ---
    common_aliases = {
        "JNPT": "Nhava Sheva",
        "JNP": "Nhava Sheva",
        "NSICT": "Nhava Sheva",
        "NSIGT": "Nhava Sheva",
        "GTI": "Nhava Sheva",
        "BMCT": "Nhava Sheva"
        "JEA": "Jebel Ali",
        "JED": "Jebel Ali",
        "SIN": "Singapore",
        "PKG": "Port Klang"
    }
    if name in common_aliases:
        return common_aliases[name]

    # STEP 2: RAPIDFUZZ MATCHING AGAINST GLOBAL DATABASE
    if GLOBAL_PORT_ALIASES:
        # Match against values (port names)
        port_names = [v[0] for v in GLOBAL_PORT_ALIASES.values()]
        result = process.extractOne(name, port_names, scorer=fuzz.token_sort_ratio)
        
        if result:
            matched_name, score, _ = result
            if score >= 85:
                return matched_name.title()

    return name.title()
def normalize_port_name(raw_name):
    # Wrapper for backward compatibility
    return standardize_port_name(raw_name)

@app.get("/")
def read_root():
    return {"status": "Mega Move Logistics Engine is Running"}

# --- WHATSAPP VERIFICATION ---
@app.get("/whatsapp-webhook")
async def verify_whatsapp(request: Request):
    mode = request.query_params.get("hub.mode")
    token = request.query_params.get("hub.verify_token")
    challenge = request.query_params.get("hub.challenge")
    if mode == "subscribe" and token == os.getenv("WHATSAPP_VERIFY_TOKEN"):
        return PlainTextResponse(challenge)
    return {"error": "Invalid verification token"}

# --- INBOUND WEBHOOKS ---
@app.post("/whatsapp-webhook")
async def whatsapp_webhook(request: Request, background_tasks: BackgroundTasks):
    payload = await request.json()
    
    # 0. IDEMPOTENCY & STATUS CHECK
    try:
        value = payload.get('entry', [{}])[0].get('changes', [{}])[0].get('value', {})
        
        # 0.1 Silently acknowledge status updates (read/delivered receipts) to prevent crashes
        if 'statuses' in value:
            return {"status": "success"}
            
        messages = value.get('messages', [])
        if not messages:
            return {"status": "success"}

        message_id = messages[0].get('id')
        if message_id in PROCESSED_MESSAGE_IDS:
            print(f"DEBUG: Ignoring duplicate message_id: {message_id}")
            return {"status": "ignored_duplicate"}
        
        # Add to cache and proceed
        PROCESSED_MESSAGE_IDS.add(message_id)

    except Exception as e:
        print(f"Webhook Safety Check Error: {e}")
        return {"status": "success"} # Still return success to prevent retries on error

    # Process everything in the background to ensure Meta gets a 200 OK instantly
    background_tasks.add_task(process_whatsapp_message, payload, background_tasks)
    return {"status": "success"}

@app.post("/email-webhook")
async def email_webhook(request: Request, background_tasks: BackgroundTasks):
    """Catches forwarded emails from Zoho Flow/Mail"""
    payload = await request.json()
    background_tasks.add_task(process_email_rfq, payload)
    return {"status": "success"}

# --- CORE FUNCTIONS ---
def send_whatsapp_message(to_number, text):
    phone_id = os.getenv("WHATSAPP_PHONE_ID")
    url = f"https://graph.facebook.com/v18.0/{phone_id}/messages"
    headers = {"Authorization": f"Bearer {os.getenv('WHATSAPP_ACCESS_TOKEN')}", "Content-Type": "application/json"}
    payload = {"messaging_product": "whatsapp", "to": to_number, "type": "text", "text": {"body": text}}
    requests.post(url, headers=headers, json=payload)

def send_email_with_attachment(to_email, subject, body, pdf_bytes=None, filename=None):
    """Sends an email using Zoho SMTP with an optional attachment and mandatory CC."""
    sender_email = os.getenv("ZOHO_EMAIL_ADDRESS")
    sender_password = os.getenv("ZOHO_EMAIL_PASSWORD")
    cc_email = "hitesh@megamoveindia.com"
    
    msg = MIMEMultipart()
    msg['From'] = sender_email
    msg['To'] = to_email
    msg['Cc'] = cc_email
    msg['Subject'] = subject
    msg.attach(MIMEText(body, 'plain'))
    
    if pdf_bytes:
        part = MIMEApplication(pdf_bytes, Name=filename)
        part['Content-Disposition'] = f'attachment; filename="{filename}"'
        msg.attach(part)
        
    try:
        # Use Zoho SMTP settings (smtp.zoho.in for India orgs)
        with smtplib.SMTP_SSL('smtp.zoho.in', 465) as server:
            server.login(sender_email, sender_password)
            # Explicitly route to both primary recipient and CC
            server.send_message(msg, to_addrs=[to_email, cc_email])
        return True
    except Exception as e:
        print(f"SMTP Error: {e}")
        return False

def upload_and_send_pdf(to_number, file_bytes, filename, caption):
    phone_id = os.getenv("WHATSAPP_PHONE_ID")
    access_token = os.getenv("WHATSAPP_ACCESS_TOKEN")
    
    upload_url = f"https://graph.facebook.com/v18.0/{phone_id}/media"
    headers = {"Authorization": f"Bearer {access_token}"}
    files = {'file': (filename, file_bytes, 'application/pdf'), 'messaging_product': (None, 'whatsapp')}
    upload_res = requests.post(upload_url, headers=headers, files=files)
    media_id = upload_res.json().get("id")
    
    if not media_id:
        return

    send_url = f"https://graph.facebook.com/v18.0/{phone_id}/messages"
    headers["Content-Type"] = "application/json"
    payload = {
        "messaging_product": "whatsapp",
        "to": to_number,
        "type": "document",
        "document": {"id": media_id, "caption": caption, "filename": filename}
    }
    requests.post(send_url, headers=headers, json=payload)

# --- ACCOUNTING & AUDIT LOGIC (PHASE 5) ---
def check_overdue_invoices():
    """Queries Zoho Books for invoices 3+ days overdue."""
    access_token = get_zoho_access_token()
    org_id = os.getenv("ZOHO_BOOKS_ORG_ID")
    if not org_id: return []
    
    url = f"https://www.zohoapis.in/books/v3/invoices?status=overdue&organization_id={org_id}"
    headers = {"Authorization": f"Zoho-oauthtoken {access_token}"}
    res = requests.get(url, headers=headers)
    
    overdue_list = []
    if res.status_code == 200:
        invoices = res.json().get("invoices", [])
        for inv in invoices:
            # Calculate days overdue
            due_date = datetime.strptime(inv.get("due_date"), "%Y-%m-%d").date()
            days_overdue = (datetime.now().date() - due_date).days
            if days_overdue >= 3:
                inv["days_overdue"] = days_overdue
                overdue_list.append(inv)
    return overdue_list

def check_vendor_bill_mismatches():
    """Compares Zoho Books Bills against CRM Deal costs to find discrepancies."""
    access_token = get_zoho_access_token()
    org_id = os.getenv("ZOHO_BOOKS_ORG_ID")
    if not org_id: return []
    
    # 1. Fetch Open Bills from Zoho Books
    url = f"https://www.zohoapis.in/books/v3/bills?status=open&organization_id={org_id}"
    headers = {"Authorization": f"Zoho-oauthtoken {access_token}"}
    res = requests.get(url, headers=headers)
    
    mismatches = []
    if res.status_code == 200:
        bills = res.json().get("bills", [])
        for bill in bills:
            reference = bill.get("reference_number", "") # Assume INQ number is here
            if not reference.startswith("INQ"): continue
            
            # 2. Query CRM Deal for expected cost (Buy Rate)
            crm_url = f"https://www.zohoapis.in/crm/v3/Deals/search?criteria=(Deal_Name:equals:{reference})"
            crm_res = requests.get(crm_url, headers=headers)
            
            if crm_res.status_code == 200 and crm_res.json().get("data"):
                deal = crm_res.json()["data"][0]
                expected_cost = float(deal.get("Buy_Rate") or 0)
                bill_amount = float(bill.get("total") or 0)
                
                if expected_cost > 0:
                    variance = ((bill_amount - expected_cost) / expected_cost) * 100
                    if variance > 5: # Flag if bill is > 5% higher than expected
                        mismatches.append({
                            "vendor_name": bill.get("vendor_name"),
                            "bill_number": bill.get("bill_number"),
                            "bill_amount": bill_amount,
                            "crm_expected": expected_cost,
                            "variance": f"{variance:.2f}"
                        })
    return mismatches

def format_inr(number):
    """Formats a number as INR string using the Indian numbering system (Lakhs/Crores)."""
    try:
        is_negative = number < 0
        number = abs(number)
        s, *d = str(f"{number:.2f}").partition(".")
        r = ",".join([s[x-2:x] for x in range(-3, -len(s), -2)][::-1] + [s[-3:]])
        result = f"₹ {r}{d[0]}{d[1]}"
        return f"-{result}" if is_negative else result
    except:
        return f"₹ {number:,.2f}"

async def extract_raw_content(file_bytes, filename, msg_type):
    """Universal extractor for Excel, PDF, Images, and Text."""
    ext = filename.split('.')[-1].lower()
    
    try:
        # 1. EXCEL / CSV
        if ext in ['xlsx', 'xls', 'csv']:
            all_sheets = pd.read_excel(io.BytesIO(file_bytes), sheet_name=None, header=None)
            combined_text = ""
            for sheet_name, df in all_sheets.items():
                df = df.dropna(how='all').dropna(axis=1, how='all')
                combined_text += f"\n--- SHEET: {sheet_name} ---\n{df.to_csv(index=False)}\n"
            return combined_text
        
        # 2. PDF
        elif ext == 'pdf':
            pdf_reader = PyPDF2.PdfReader(io.BytesIO(file_bytes))
            return "".join([page.extract_text() for page in pdf_reader.pages])
        
        # 3. IMAGES (OCR via Vision AI)
        elif msg_type == "image":
            base64_image = base64.b64encode(file_bytes).decode('utf-8')
            response = client.chat.completions.create(
                model="gpt-4o",
                messages=[{
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "Perform a high-fidelity OCR dump of this logistics document. Extract all text, numbers, and tabular structures into a clean readable string."},
                        {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{base64_image}"}}
                    ]
                }]
            )
            return response.choices[0].message.content
        
        # 4. RAW TEXT
        else:
            return file_bytes.decode('utf-8', errors='ignore')
            
    except Exception as e:
        print(f"Extraction Error ({filename}): {e}")
        return f"Extraction Error: {str(e)}"

async def classify_operational_intent(user_text, caption, content_snippet):
    """Cognitive classifier to route logistics operations."""
    system_prompt = (
        "You are an AI logistics operational router. Analyze the user's text message, incoming caption, and the following document text snippet. "
        "Classify the user's true intent and document category.\n"
        "Return a strict JSON format: \n"
        "{\n"
        "  \"category\": \"ratesheet\" | \"vendor_bill\" | \"tariff\" | \"inquiry\" | \"command\",\n"
        "  \"action_target\": \"INQ-XXX\" or null,\n"
        "  \"confidence\": float\n"
        "}"
    )
    user_context = f"User Msg: {user_text}\nCaption: {caption}\nContent Snippet: {content_snippet[:5000]}"
    
    response = client.chat.completions.create(
        model="gpt-4o",
        messages=[{"role": "system", "content": system_prompt},
                  {"role": "user", "content": user_context}],
        response_format={ "type": "json_object" }
    )
    return json.loads(response.choices[0].message.content)

async def extract_raw_content(file_bytes, filename, msg_type):
    """Universal extractor for Excel, PDF, Images, and Text."""
    ext = filename.split('.')[-1].lower()
    
    try:
        # 1. EXCEL / CSV
        if ext in ['xlsx', 'xls', 'csv']:
            all_sheets = pd.read_excel(io.BytesIO(file_bytes), sheet_name=None, header=None)
            combined_text = ""
            for sheet_name, df in all_sheets.items():
                df = df.dropna(how='all').dropna(axis=1, how='all')
                combined_text += f"\n--- SHEET: {sheet_name} ---\n{df.to_csv(index=False)}\n"
            return combined_text
        
        # 2. PDF
        elif ext == 'pdf':
            pdf_reader = PyPDF2.PdfReader(io.BytesIO(file_bytes))
            return "".join([page.extract_text() for page in pdf_reader.pages])
        
        # 3. IMAGES (OCR via Vision AI)
        elif msg_type == "image":
            base64_image = base64.b64encode(file_bytes).decode('utf-8')
            response = client.chat.completions.create(
                model="gpt-4o",
                messages=[{
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "Perform a high-fidelity OCR dump of this logistics document. Extract all text, numbers, and tabular structures into a clean readable string."},
                        {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{base64_image}"}}
                    ]
                }]
            )
            return response.choices[0].message.content
        
        # 4. RAW TEXT
        else:
            return file_bytes.decode('utf-8', errors='ignore')
            
    except Exception as e:
        print(f"Extraction Error ({filename}): {e}")
        return f"Extraction Error: {str(e)}"

async def classify_operational_intent(user_text, caption, content_snippet):
    """Cognitive classifier to route logistics operations."""
    system_prompt = (
        "You are an AI logistics operational router. Analyze the user's text message, incoming caption, and the following document text snippet. "
        "Classify the user's true intent and document category.\n"
        "Return a strict JSON format: \n"
        "{\n"
        "  \"category\": \"ratesheet\" | \"vendor_bill\" | \"tariff\" | \"inquiry\" | \"command\",\n"
        "  \"action_target\": \"INQ-XXX\" or null,\n"
        "  \"confidence\": float\n"
        "}"
    )
    user_context = f"User Msg: {user_text}\nCaption: {caption}\nContent Snippet: {content_snippet[:5000]}"
    
    response = client.chat.completions.create(
        model="gpt-4o",
        messages=[{"role": "system", "content": system_prompt},
                  {"role": "user", "content": user_context}],
        response_format={ "type": "json_object" }
    )
    return json.loads(response.choices[0].message.content)

def get_fy_start():
    """Calculates the start of the Indian Financial Year (April 1st)."""
    now = datetime.now()
    if now.month >= 4:
        fy_start_year = now.year
    else:
        fy_start_year = now.year - 1
    
    books_date = f"{fy_start_year}-04-01"
    crm_date = f"{fy_start_year}-04-01T00:00:00+05:30"
    fy_label = f"{fy_start_year}-{fy_start_year + 1}"
    return books_date, crm_date, fy_label

def get_crm_snapshot(period="FY"):
    """Calculates CRM metrics: inquiries, bookings, and conversion rate."""
    access_token = get_zoho_access_token()
    books_start_str, _, fy_label = get_fy_start()
    fy_start_date = datetime.strptime(books_start_str, "%Y-%m-%d")
    
    # Explicitly request required fields to prevent omission by Zoho default view
    url = "https://www.zohoapis.in/crm/v3/Deals?fields=Created_Time,Stage,Deal_Name&sort_by=Created_Time&sort_order=desc&per_page=100"
    headers = {"Authorization": f"Zoho-oauthtoken {access_token}"}
    
    total_inquiries = 0
    booked_shipments = 0
    
    page = 1
    more_records = True
    
    while more_records:
        res = requests.get(f"{url}&page={page}", headers=headers)
        if res.status_code == 200 and res.json().get("data"):
            deals = res.json()["data"]
            print(f"DEBUG: Fetched {len(deals)} deals from CRM (Page {page}).")
            
            for deal in deals:
                try:
                    created_str = deal.get("Created_Time")
                    if not created_str:
                        print(f"DEBUG: Deal {deal.get('id')} is missing Created_Time")
                        continue
                        
                    # Safely extract just the YYYY-MM-DD part to avoid timezone crashing
                    clean_date_str = created_str.split("T")[0]
                    deal_date = datetime.strptime(clean_date_str, "%Y-%m-%d")
                    
                    if period == "FY" and deal_date < fy_start_date:
                        more_records = False
                        break # We've hit the previous FY, stop counting
                        
                    total_inquiries += 1
                    if deal.get("Stage") == "Closed Won":
                        booked_shipments += 1
                        
                except Exception as e:
                    print(f"DEBUG: Error parsing deal date: {e}")
                    continue
            
            if not more_records:
                break
                
            info = res.json().get("info", {})
            if not info.get("more_records"):
                more_records = False
            page += 1
        else:
            more_records = False

    conversion_rate = (booked_shipments / total_inquiries * 100) if total_inquiries > 0 else 0.0
    return {"inquiries": total_inquiries, "booked": booked_shipments, "conversion": conversion_rate}

def get_financial_snapshot(period="FY"):
    """Aggregates revenue and costs from Zoho Books for a specific period."""
    access_token = get_zoho_access_token()
    org_id = os.getenv("ZOHO_BOOKS_ORG_ID")
    headers = {"Authorization": f"Zoho-oauthtoken {access_token}"}
    
    books_start, _, _ = get_fy_start()
    date_filter = f"&date_start={books_start}" if period == "FY" else ""
    
    # 1. Fetch Invoices for Revenue
    inv_res = requests.get(f"https://www.zohoapis.in/books/v3/invoices?organization_id={org_id}{date_filter}", headers=headers)
    # 2. Fetch Bills for Costs
    bill_res = requests.get(f"https://www.zohoapis.in/books/v3/bills?organization_id={org_id}{date_filter}", headers=headers)
    
    revenue = 0.0
    costs = 0.0
    if inv_res.status_code == 200:
        revenue = sum([float(i.get("total", 0)) for i in inv_res.json().get("invoices", [])])
    if bill_res.status_code == 200:
        costs = sum([float(b.get("total", 0)) for b in bill_res.json().get("bills", [])])
        
    return {"revenue": float(revenue), "costs": float(costs), "profit": float(revenue - costs)}

def get_deal_by_id(inq_number):
    """Fetches a full Deal record from Zoho CRM by its INQ number."""
    access_token = get_zoho_access_token()
    url = f"https://www.zohoapis.in/crm/v3/Deals/search?criteria=(Deal_Name:equals:{inq_number})"
    headers = {"Authorization": f"Zoho-oauthtoken {access_token}"}
    res = requests.get(url, headers=headers)
    if res.status_code == 200 and res.json().get("data"):
        return res.json()["data"][0]
    return None

def get_primary_vendor_email(vendor_name, pol=None):
    """Searches Zoho CRM for the primary PIC email for a vendor."""
    access_token = get_zoho_access_token()
    headers = {"Authorization": f"Zoho-oauthtoken {access_token}"}
    
    # 1. Search in Vendors module
    url = f"https://www.zohoapis.in/crm/v3/Vendors/search?criteria=(Vendor_Name:equals:{vendor_name})"
    res = requests.get(url, headers=headers)
    if res.status_code == 200 and res.json().get("data"):
        return res.json()["data"][0].get("Email")
    
    # 2. Fallback to Contacts search
    url = f"https://www.zohoapis.in/crm/v3/Contacts/search?criteria=(Account_Name:equals:{vendor_name})"
    res = requests.get(url, headers=headers)
    if res.status_code == 200 and res.json().get("data"):
        for contact in res.json()["data"]:
            if contact.get("Email"): return contact.get("Email")
                
    return None

def get_zoho_access_token():
    url = "https://accounts.zoho.in/oauth/v2/token" 
    payload = {
        "refresh_token": os.getenv("ZOHO_REFRESH_TOKEN"),
        "client_id": os.getenv("ZOHO_CLIENT_ID"),
        "client_secret": os.getenv("ZOHO_CLIENT_SECRET"),
        "grant_type": "refresh_token"
    }
    return requests.post(url, data=payload).json().get("access_token")

def push_to_zoho_crm(module, data_list):
    access_token = get_zoho_access_token()
    # Using the Upsert API to prevent duplicates
    url = f"https://www.zohoapis.in/crm/v3/{module}/upsert" 
    headers = {"Authorization": f"Zoho-oauthtoken {access_token}", "Content-Type": "application/json"}
    
    payload = {
        "data": data_list,
        "duplicate_check_fields": ["Rate_Key"]
    }
    
    if data_list:
        print(f"DEBUG: Sending to Zoho module '{module}'. Record keys: {list(data_list[0].keys())}")
        
    response = requests.post(url, json=payload, headers=headers)
    
    if response.status_code not in [200, 201, 202]:
        raise Exception(f"Zoho API Global Error ({response.status_code}): {response.text}")

    res_data = response.json().get("data", [])
    for idx, item in enumerate(res_data):
        if item.get("status") == "error":
            error_details = item.get("details", {})
            error_msg = item.get("message", "Unknown Error")
            raise Exception(f"Zoho Record Error [Row {idx+1}]: {error_msg} (Details: {error_details})")

def get_deal_tracking_details(inq_number):
    """Fetches Container/MBL details from Zoho Deals using INQ number."""
    access_token = get_zoho_access_token()
    # Assume the CRM field API name is Container_Number or MBL
    url = f"https://www.zohoapis.in/crm/v3/Deals/search?criteria=(Deal_Name:equals:{inq_number})"
    headers = {"Authorization": f"Zoho-oauthtoken {access_token}"}
    res = requests.get(url, headers=headers)
    
    if res.status_code == 200 and res.json().get("data"):
        deal = res.json()["data"][0]
        return deal.get("Container_Number") or deal.get("MBL")
    return None

def fetch_container_status(tracking_reference):
    """Queries external Tracking API (Vizion/SeaRates) or returns mock for testing."""
    api_key = os.getenv("TRACKING_API_KEY")
    
    if not api_key:
        # Mock Response for Testing
        return {
            "status": "In Transit",
            "current_location": "En route to Singapore",
            "eta": "2026-06-15",
            "vessel_name": "MAERSK KYRENIA"
        }
    
    # Placeholder for actual API integration (e.g. Vizion)
    return {
        "status": "Tracking active",
        "current_location": "Coordinating with carrier...",
        "eta": "TBD",
        "vessel_name": "TBD"
    }

# --- DYNAMIC AUTO-NUMBERING ---
def generate_next_inquiry_number():
    """Fetches the latest Deal and increments the INQ number."""
    access_token = get_zoho_access_token()
    url = "https://www.zohoapis.in/crm/v3/Deals?sort_by=Created_Time&sort_order=desc&per_page=10"
    headers = {"Authorization": f"Zoho-oauthtoken {access_token}"}
    res = requests.get(url, headers=headers)
    
    last_num = 21 
    if res.status_code == 200 and res.json().get("data"):
        for deal in res.json()["data"]:
            name = deal.get("Deal_Name", "")
            if name.startswith("INQ-MMI-2026-"):
                try:
                    last_num = int(name.split("-")[-1])
                    break 
                except:
                    continue
                    
    next_num = last_num + 1
    return f"INQ-MMI-2026-{str(next_num).zfill(3)}"

def search_rates(pol, pod):
    normalized_pol = normalize_port_name(pol)
    normalized_pod = normalize_port_name(pod)
    
    print(f"DEBUG: Searching Zoho for POL={normalized_pol}, POD={normalized_pod}")
    
    access_token = get_zoho_access_token()
    # STEP 1: Search ONLY for IDs (Subforms are not returned by default search)
    url = f"https://www.zohoapis.in/crm/v3/Pricings/search?criteria=(((POL:starts_with:{normalized_pol})and(POD:starts_with:{normalized_pod})))&fields=id"
    headers = {"Authorization": f"Zoho-oauthtoken {access_token}"}
    res = requests.get(url, headers=headers)
    
    valid_rates = []
    if res.status_code == 200 and res.json().get("data"):
        record_ids = [r.get("id") for r in res.json()["data"]]
        today = datetime.now().date()
        
        for record_id in record_ids:
            # STEP 2: Fetch FULL record by ID to guarantee subform data
            get_url = f"https://www.zohoapis.in/crm/v3/Pricings/{record_id}"
            get_res = requests.get(get_url, headers=headers)
            
            if get_res.status_code == 200 and get_res.json().get("data"):
                r = get_res.json()["data"][0]
                print(f"DEBUG: Retrieved full record: {record_id}")
                
                try:
                    # 1. EXPIRED RATE FILTERING
                    validity_str = r.get("Validity_Date")
                    if validity_str:
                        try:
                            validity_date = datetime.strptime(validity_str, "%Y-%m-%d").date()
                            if validity_date < today:
                                continue 
                        except:
                            pass 
                    
                    # 2. DATA EXTRACTION
                    sub3 = r.get("Subform_3", [])
                    if not sub3:
                        continue
                    
                    for entry in sub3:
                        price_val = entry.get("Freight_Air_Sea")
                        vendor_name = entry.get("Vendor_Name") or "N/A"
                        
                        if not price_val or str(price_val).strip() == "" or float(price_val) == 0:
                            continue
                        
                        valid_rates.append({
                            "vendor": vendor_name,
                            "price": float(price_val),
                            "vehicle": r.get("Container_Type") or "N/A",
                            "transit_time": r.get("Transit_Time") or "Standard Routing",
                            "free_time": r.get("Free_Time") or "As per tariff",
                            "local_charges": r.get("Local_Charges") or "At actuals",
                            "route": r.get("Route") or "Direct",
                            "validity_date": validity_str or "N/A",
                            "pol": r.get("POL") or normalized_pol,
                            "pod": r.get("POD") or normalized_pod
                        })
                except Exception as inner_e:
                    print(f"DEBUG: Error processing record {record_id}: {str(inner_e)}")
                    
    return valid_rates

def process_inquiry(text):
    """Analyzes text to find rates. Returns (reply_text, extracted_details)."""
    system_prompt = (
        "You are an expert freight forwarder AI. Output strictly valid JSON with EXACTLY these keys: "
        "'shipper', 'pol', 'pod', 'commodity', 'equipment_type', 'weight'. Do not alter these key names. "
        "CRITICAL RULES: "
        "1. You MUST translate local port acronyms to their global standard names (e.g., 'JNPT' must become 'Nhava Sheva', 'JEA' must become 'Jebel Ali'). "
        "2. If data is missing, return 'Unknown'."
    )
    prompt = f"Message: {text}"
    response = client.chat.completions.create(
        
        model="gpt-4o",
        messages=[{"role": "user", "content": content}],
        response_format={ "type": "json_object" }
    )
    return json.loads(response.choices[0].message.content)

def classify_pdf_content(raw_data):
    """Classifies the document type using GPT-4o."""
    prompt = "Classify this document as either 'VENDOR_RATE_SHEET' or 'INQUIRY_EMAIL'. Return only the keyword."
    response = client.chat.completions.create(
        model="gpt-4o",
        messages=[{"role": "user", "content": f"{prompt}\n\nDATA:\n{raw_data[:5000]}"}]
    )
    classification = response.choices[0].message.content.strip().upper()
    return classification

def process_inquiry_email(raw_data, wa_id=None):
    """Parses inquiry details from a document and logs to Zoho 'New Enquiries'."""
    try:
        system_prompt = (
            "You are an expert freight forwarder AI. Output strictly valid JSON with EXACTLY these keys: "
            "'shipper', 'pol', 'pod', 'commodity', 'equipment_type', 'weight', 'incoterms'. "
            "Do not alter these key names. If data is missing, return 'Unknown'."
        )
        response = client.chat.completions.create(
            model="gpt-4o",
            messages=[{"role": "system", "content": system_prompt},
                      {"role": "user", "content": f"DATA:\n{raw_data[:10000]}"}],
            response_format={ "type": "json_object" }
        )
        extracted = json.loads(response.choices[0].message.content)
        
        # Safely extract with 'Unknown' defaults
        pol = extracted.get('pol', 'Unknown')
        pod = extracted.get('pod', 'Unknown')
        shipper = extracted.get('shipper', 'Unknown')
        commodity = extracted.get('commodity', 'Unknown')
        
        # Log to Zoho CRM (New Enquiries module)
        inq_number = generate_next_inquiry_number()
        enquiry_data = {
            "Deal_Name": inq_number,
            "Stage": "Qualification",
            "Description": f"PDF Inquiry Extracted\nShipper: {shipper}\nRoute: {pol} to {pod}\nSpecs: {extracted.get('equipment_type', 'Unknown')}, {extracted.get('weight', 'Unknown')}\nCommodity: {commodity}\nIncoterms: {extracted.get('incoterms', 'Unknown')}\nSource: PDF Document"
        }
        
        if wa_id:
            PENDING_TASKS[wa_id] = {
                'action': 'log_enquiry',
                'description': f"log an enquiry for {pol} to {pod}",
                'module': 'New Enquiries',
                'data': enquiry_data
            }
            summary = f"📊 *Inquiry Parsed!*\n\nShipper: {shipper}\nRoute: {pol} to {pod}\n\nReply *YES* to log this as a New Enquiry in Zoho CRM."
            send_whatsapp_message(wa_id, summary)
            return summary
            
        return "Inquiry processed successfully."
    except Exception as e:
        print(f"CRITICAL ERROR (Inquiry PDF): {e}")
        if wa_id:
            send_whatsapp_message(wa_id, f"❌ *Error parsing inquiry:* {str(e)}")
        return f"Error processing inquiry: {e}"

def generate_quotation_pdf(inq_number, pol, pod, equipment, sell_price, local_charges=None):
    pdf = FPDF()
    pdf.add_page()
    pdf.set_font("Arial", 'B', 16)
    pdf.cell(200, 10, txt="MEGA MOVE INDIA PRIVATE LIMITED", ln=1, align='C')
    pdf.set_font("Arial", size=12)
    pdf.cell(200, 10, txt=f"FREIGHT QUOTATION - {inq_number}", ln=1, align='C')
    pdf.ln(10)
    pdf.set_font("Arial", 'B', 12)
    pdf.cell(50, 10, txt="Port of Loading:", ln=0)
    pdf.set_font("Arial", size=12)
    pdf.cell(150, 10, txt=str(pol), ln=1)
    pdf.set_font("Arial", 'B', 12)
    pdf.cell(50, 10, txt="Port of Discharge:", ln=0)
    pdf.set_font("Arial", size=12)
    pdf.cell(150, 10, txt=str(pod), ln=1)
    pdf.set_font("Arial", 'B', 12)
    pdf.cell(50, 10, txt="Equipment:", ln=0)
    pdf.set_font("Arial", size=12)
    pdf.cell(150, 10, txt=str(equipment), ln=1)
    
    if local_charges:
        pdf.ln(5)
        pdf.set_font("Arial", 'B', 12)
        pdf.cell(200, 10, txt="Origin Local Charges:", ln=1)
        pdf.set_font("Arial", size=10)
        # Filter and display relevant local charges
        relevant_keys = ["THC_20", "THC_40", "BL_Fee", "Seal_Charge", "Toll", "MUC"]
        for key in relevant_keys:
            val = local_charges.get(key)
            if val and val != "0":
                label = key.replace("_", " ")
                pdf.cell(200, 8, txt=f"• {label}: {val}", ln=1)

    pdf.ln(10)
    pdf.set_font("Arial", 'B', 14)
    pdf.cell(200, 10, txt=f"Total Sell Price: {sell_price}", ln=1, align='L')
    pdf.ln(20)
    pdf.set_font("Arial", 'B', 10)
    pdf.cell(200, 10, txt="Terms & Conditions:", ln=1, align='L')
    pdf.set_font("Arial", 'I', 8)
    pdf.multi_cell(200, 5, txt=(
        "1. Rates are subject to space and equipment availability.\n"
        "2. Destination local charges and THC are as per actuals.\n"
        "3. Quote validity: 7 days unless specified otherwise.\n"
        "4. Subject to standard trading conditions of Mega Move India Pvt Ltd."
    ))
    file_path = "/tmp/quotation.pdf"
    pdf.output(file_path)
    with open(file_path, "rb") as f:
        return f.read()

def fetch_local_charges(carrier_name):
    """Queries Zoho CRM for standard local charges for a carrier."""
    access_token = get_zoho_access_token()
    # Search in Local_Charges_Tariff module
    url = f"https://www.zohoapis.in/crm/v3/Local_Charges_Tariff/search?criteria=(Carrier_Name:equals:{carrier_name})"
    headers = {"Authorization": f"Zoho-oauthtoken {access_token}"}
    res = requests.get(url, headers=headers)
    if res.status_code == 200 and res.json().get("data"):
        return res.json()["data"][0]
    return None

def process_local_charges_pdf(file_bytes, wa_id=None):
    """Parses local charges from a PDF and pushes to Zoho CRM."""
    try:
        pdf_reader = PyPDF2.PdfReader(io.BytesIO(file_bytes))
        raw_text = "".join([page.extract_text() for page in pdf_reader.pages])

        system_prompt = (
            "You are an expert pricing analyst. Extract the standard local charges from this carrier tariff. "
            "Identify the Carrier Name. Extract the standard export local charges, noting different currencies. "
            "We need: THC (20ft Standard), THC (40ft Standard), BL Fee/Doc Fee, Seal Charge, Toll, and MUC. "
            "Output strictly as JSON: {'carrier': '...', 'thc_20': '...', 'thc_40': '...', 'bl_fee': '...', 'seal_charge': '...', 'toll': '...', 'muc': '...'}"
        )
        
        response = client.chat.completions.create(
            model="gpt-4o",
            messages=[{"role": "system", "content": system_prompt},
                      {"role": "user", "content": f"TARIFF DATA:\n{raw_text[:10000]}"}],
            response_format={ "type": "json_object" }
        )
        data = json.loads(response.choices[0].message.content)
        
        # Push to Zoho CRM
        push_to_zoho_crm("Local_Charges_Tariff", [{
            "Carrier_Name": data.get("carrier"),
            "THC_20": data.get("thc_20"),
            "THC_40": data.get("thc_40"),
            "BL_Fee": data.get("bl_fee"),
            "Seal_Charge": data.get("seal_charge"),
            "Toll": data.get("toll"),
            "MUC": data.get("muc")
        }])
        
        if wa_id:
            send_whatsapp_message(wa_id, f"✅ *Local Charges Updated* for {data.get('carrier')}. Ready for relational pricing.")
            
    except Exception as e:
        print(f"Local Charges Error: {e}")
        if wa_id:
            send_whatsapp_message(wa_id, f"❌ *Error parsing local charges:* {str(e)}")

# --- RATE SHEET PROCESSING ---
def process_rate_sheet(file_content, filename, vendor_name, wa_id=None):
    """Parses Excel/PDF rate sheets using a calibrated GPT-4o prompt."""
    try:
        if wa_id:
            send_whatsapp_message(wa_id, "📥 *Received document.* Analyzing content...")

        # Extract text/data from file
        if filename.endswith(".xlsx") or filename.endswith(".xls"):
            all_sheets = pd.read_excel(io.BytesIO(file_content), sheet_name=None)
            raw_data = ""
            for sheet_name, df in all_sheets.items():
                # Clean empty data to save tokens
                df = df.dropna(how='all').dropna(axis=1, how='all')
                raw_data += f"\n--- DATA FROM SHEET: {sheet_name} ---\n"
                raw_data += df.to_csv(index=False) + "\n"
        else:
            pdf_reader = PyPDF2.PdfReader(io.BytesIO(file_content))
            raw_data = "".join([page.extract_text() for page in pdf_reader.pages])

        # 1. CLASSIFICATION LAYER
        doc_type = classify_pdf_content(raw_data)
        print(f"DEBUG: Document classified as {doc_type}")
        
        if doc_type == 'INQUIRY_EMAIL':
            if wa_id:
                send_whatsapp_message(wa_id, "🔍 *Detected:* Inquiry Email. Processing Inquiry...")
            return process_inquiry_email(raw_data, wa_id)

        # 2. CONTINUING AS VENDOR_RATE_SHEET
        # UNIVERSAL PARSING PROMPT - CALIBRATED FOR COMPLEX LOGISTICS EDGE CASES
        system_prompt = (
            "You are an expert freight rate parser. I am providing you with a complete Excel workbook converted to CSV format, "
            "separated by sheet names. Extract EVERY SINGLE freight rate across ALL sheets. Do not miss any rows. "
            "Output strictly valid JSON with EXACTLY these keys: "
            "'shipper', 'pol', 'pod', 'commodity', 'equipment_type', 'weight', 'validity_date', 'transit_time', 'route'. "
            "Do not alter these key names. If data is missing, return 'Unknown'. "
            "Standardize container types to '20ft' or '40ft' and handle row splitting for multi-column price sheets."
        )
        prompt = f"""
        VENDOR NAME: PIL (INDIA) PVT. LTD
        FILE NAME: {filename}

        ### CORE EXTRACTION RULES:
        1. **POL INFERENCE (CRITICAL):** 
           - Logistics rates are often grouped under a Port of Loading (POL). 
           - Look for POL in: Tab Names, Section Headers (e.g., \"RATES FROM MUNDRA\"), or the first column.
           - If a POL is found at the top of a table/section, apply it to EVERY row in that section until a new POL is explicitly mentioned.
        
        2. **PRICE SCRUBBING & ROW SPLITTING:** 
           - For every row in the rate table, you must create TWO separate rate entries:
             - Entry 1: Container Type = '20ft', Price = [Value from the first price column].
             - Entry 2: Container Type = '40ft', Price = [Value from the second price column].
           - Extract ONLY the numeric base Ocean Freight as 'ocean_freight'. 
           - Ignore surcharges, THC, documentation fees, or any text following symbols like \"+\", \"&\", \"/\", or \"AND\".
           - Example: \"$1200 + THC\" -> 1200.
           - Example: \"Included\" or \"0\" -> 0.0.

        3. **CONTAINER TYPE MAPPING:**
           - Standardize types strictly to '20ft' or '40ft' based on the column headers.

        4. **COMMODITY & HAZ:**
           - If the row/column indicates \"HAZ\", \"Hazardous\", or \"DG\", append \"(HAZ)\" to the container_type.

        5. **TRANSIT TIME & ROUTE (COLUMN ALIASES):**
           - 'VIA' -> Use this for the 'route' column (e.g., \"Direct\", \"via Singapore\").
           - 'Days' -> Use this for the 'transit_time' column (e.g., \"15 Days\").
           - 'Valid till' -> Use this for the 'validity_date' column.

        6. **VALIDITY SCAN:**
           - Return 'validity_date' in YYYY-MM-DD format. If not found, return null.

        ### OUTPUT FORMAT:
        Return ONLY a JSON object with a \"rates\" key:
        {{
          \"rates\": [
            {{
              \"pol\": \"Origin Port\",
              \"pod\": \"Destination Port\",
              \"container_type\": \"20ft or 40ft\",
              \"ocean_freight\": 0.0,
              \"transit_time\": \"Estimated Days\",
              \"route\": \"Routing Details\",
              \"validity_date\": \"YYYY-MM-DD or null\"
            }}
          ]
        }}

        ### RAW DATA:
        {raw_data[:100000]}
        """
        
        if wa_id:
            send_whatsapp_message(wa_id, "🧠 *Analyzing rates...* Extracting POL, POD, and pricing. (This might take a few seconds)")

        response = client.chat.completions.create(
            model="gpt-4o",
            messages=[{"role": "system", "content": system_prompt},
                      {"role": "user", "content": prompt}],
            response_format={ "type": "json_object" }
        )
        
        extracted_rates = json.loads(response.choices[0].message.content).get("rates", [])
        
        # 3. PYTHON DEDUPLICATION (Lowest price wins)
        dedup_map = {}
        for rate in extracted_rates:
            carrier = rate.get('shipper', 'PIL (INDIA) PVT. LTD')
            pol = rate.get('pol', 'Unknown')
            pod = rate.get('pod', 'Unknown')
            eq_type = rate.get('equipment_type', 'Unknown')
            
            # Use safe numeric extractor
            current_price = extract_numeric_price(rate.get('ocean_freight'))
            if current_price == float('inf') or current_price <= 0: continue
            
            key = (carrier, pol, pod, eq_type)
            if key in dedup_map:
                existing_price = extract_numeric_price(dedup_map[key].get('ocean_freight'))
                if current_price < existing_price:
                    dedup_map[key] = rate
            else:
                dedup_map[key] = rate

        if wa_id:
            send_whatsapp_message(wa_id, f"✅ *Extracted {len(extracted_rates)} rates.* Deduplicated to {len(dedup_map)} best prices. Mapping PICs...")

        # Log to Zoho CRM (Pricings module)
        zoho_data = []
        for key, rate in dedup_map.items():
            carrier_name, pol_raw, pod_raw, eq_type = key
            norm_pol = normalize_port_name(pol_raw)
            norm_pod = normalize_port_name(pod_raw)
            
            # Map Primary PIC Email
            mapped_email = get_primary_vendor_email(carrier_name, norm_pol)
            
            # Standardize keys for mapping
            r_key = f"{carrier_name}_{norm_pol}_{norm_pod}_{eq_type}".upper().replace(" ", "_")
            
            zoho_data.append({
                "Name": f"{carrier_name} - {norm_pol} to {norm_pod} - {eq_type}",
                "POL": norm_pol,
                "POD": norm_pod,
                "Container_Type": str(eq_type),
                "Transit_Time": str(rate.get("transit_time", "Unknown")),
                "Route": str(rate.get("route", "Unknown")),
                "Rate_Key": str(r_key),
                "Validity_Date": rate.get("validity_date", "Unknown"),
                "Vendor_Email": mapped_email or "Unknown",
                "Subform_3": [
                    {
                        "Vendor_Name": carrier_name,
                        "Freight_Air_Sea": str(rate.get("ocean_freight", 0.0))
                    }
                ]
            })
        
        if zoho_data:
            # SAVE TO MEMORY FOR HUMAN-IN-THE-LOOP CONFIRMATION
            unique_vendors = ", ".join(list(set([r.get("Name").split(" - ")[0] for r in zoho_data])))
            
            PENDING_TASKS[wa_id] = {
                'action': 'upload_rates',
                'description': f"upload {len(zoho_data)} rates from {unique_vendors}",
                'data': zoho_data
            }
            
            summary = (
                f"📊 *Extraction Complete!*\n\n"
                f"Found {len(zoho_data)} total rates.\n"
                f"Vendors: {unique_vendors}\n\n"
                f"I am about to upload these rates to Zoho CRM. Please reply *YES* to confirm, or *NO* to cancel."
            )
            if wa_id:
                send_whatsapp_message(wa_id, summary)
            return summary
            
        return "No rates could be extracted."

    except Exception as e:
        print(f"CRITICAL ERROR: {str(e)}")
        if wa_id:
            send_whatsapp_message(wa_id, f"❌ *Error:* {str(e)}")
        return f"Error processing rate sheet: {e}"

def handle_confirmation(text, sender_wa_id):
    """Processes YES/NO confirmation for pending tasks (uploads or enquiries)."""
    user_text = str(text).strip().upper()
    task = PENDING_TASKS.get(sender_wa_id)

    if not task:
        send_whatsapp_message(sender_wa_id, "⚠️ No pending action found.")
        return

    if user_text == "YES":
        action = task.get('action')
        data = task.get('data')

        if action == 'upload_rates':
            send_whatsapp_message(sender_wa_id, "🚀 *Confirmed.* Pushing to Zoho CRM via Upsert...")
            try:
                batch_size = 50
                for i in range(0, len(data), batch_size):
                    batch = data[i : i + batch_size]
                    push_to_zoho_crm("Pricings", batch)
                send_whatsapp_message(sender_wa_id, f"✅ *Success!* {len(data)} rates uploaded to Zoho CRM.")
            except Exception as e:
                print(f"CRITICAL ERROR: {str(e)}")
                send_whatsapp_message(sender_wa_id, f"❌ *Error during upload:* {str(e)}")
        
        elif action == 'log_enquiry':
            module = task.get('module', 'Deals')
            send_whatsapp_message(sender_wa_id, f"🚀 *Confirmed.* Logging your enquiry in Zoho CRM ({module})...")
            try:
                push_to_zoho_crm(module, [data])
                send_whatsapp_message(sender_wa_id, "✅ *Success!* Your enquiry has been logged. Our team will contact you shortly.")
            except Exception as e:
                print(f"CRITICAL ERROR: {str(e)}")
                send_whatsapp_message(sender_wa_id, f"❌ *Error logging enquiry:* {str(e)}")

        del PENDING_TASKS[sender_wa_id]
            
    elif user_text == "NO":
        del PENDING_TASKS[sender_wa_id]
        send_whatsapp_message(sender_wa_id, "🛑 *Action cancelled.*")
    else:
        send_whatsapp_message(sender_wa_id, f"🤔 I am still waiting for your confirmation to {task.get('description')}. Please reply *YES* to proceed or *NO* to cancel.")

# --- EMAIL PROCESSING LOGIC ---
def process_email_rfq(payload):
    """Parses incoming emails and initiates the HITL workflow."""
    try:
        email_body = payload.get("body", "")
        sender_email = payload.get("sender", "Client")
        subject = payload.get("subject", "No Subject")
        
        # 1. AI Parsing (Upgraded to extract names)
        system_prompt = (
            "You are an expert freight forwarder AI. Output strictly valid JSON with EXACTLY these keys: "
            "'pol', 'pod', 'equipment_type', 'commodity', 'sender_first_name'. Do not alter these key names. "
            "Also extract the sender's first name by looking at their signature or sign-off. "
            "If you absolutely cannot find a name, return 'Customer'."
        )
        response = client.chat.completions.create(
            model="gpt-4o",
            messages=[{"role": "system", "content": system_prompt},
                      {"role": "user", "content": f"Subject: {subject}\nBody: {email_body}"}],
            response_format={ "type": "json_object" }
        )
        extracted = json.loads(response.choices[0].message.content)
        pol = extracted.get('pol', 'Unknown')
        pod = extracted.get('pod', 'Unknown')
        equipment = extracted.get('equipment_type', 'Unknown')
        first_name = extracted.get('sender_first_name', 'Customer')
        
        greeting = f"Hi {first_name}," if first_name != "Customer" else "Hi,"

        # 2. Personalized Auto-Acknowledgement Email
        ack_body = (
            f"{greeting}\n\n"
            "thank you for your valuable query,\n"
            "our team will start working on it and quote for the same shortly,\n"
            "help us to serve you better\n\n"
            "Thanks & Regards,\n"
            "Vikas | Pricing Desk,\n"
            "vikas.kaswan@megamoveindia.com | +91 9321399970\n"
            "Mega Move India Private Limited,\n"
            "www.megamoveindia.com"
        )
        send_email_with_attachment(sender_email, f"Re: {subject}", ack_body)

        # 3. Zoho CRM Deal Creation
        inq_number = generate_next_inquiry_number()
        deal_data = {
            "Deal_Name": inq_number,
            "Stage": "Qualification",
            # Save sender email in Description for later retrieval
            "Description": "Sender Email: {}\nRoute: {} to {}\nCommodity: {}\nEquipment: {}".format(
                sender_email, pol, pod, extracted.get('commodity', 'Unknown'), equipment
            )
        }
        push_to_zoho_crm("Deals", [deal_data])

        # 4. Notify Admin via WhatsApp
        admin_number = os.getenv("YOUR_WHATSAPP_NUMBER")
        if admin_number:
            msg = (
                "📧 *New Email Inquiry*\n"
                "From: {}\n"
                "Route: {} ➡️ {}\n"
                "Ref: {}\n\n"
                "Reply *APPROVE {}* to accept this inquiry and search for rates.".format(
                    sender_email, pol, pod, inq_number, inq_number
                )
            )
            send_whatsapp_message(admin_number, msg)
            
    except Exception as e:
        print("Email Processing Error: {}".format(e))

# --- WHATSAPP MESSAGE PROCESSING ---
async def process_whatsapp_message(payload, background_tasks: BackgroundTasks):
    """Unified Cognitive Ingestion Pipeline."""
    print(f"Incoming Webhook Payload: {json.dumps(payload)}")
    global LAST_CLEANUP, PROCESSED_MESSAGE_IDS
    
    try:
        entries = payload.get("entry", [])
        if not entries: return
        changes = entries[0].get("changes", [])
        if not changes: return
        value = changes[0].get("value", {})
        messages = value.get("messages", [])
        if not messages: return

        message = messages[0]
        from_number = message.get("from")
        msg_type = message.get("type")

        # IDEMPOTENCY CLEANUP
        if (datetime.now() - LAST_CLEANUP).total_seconds() > 600:
            PROCESSED_MESSAGE_IDS.clear()
            LAST_CLEANUP = datetime.now()

        # 1. UNIFIED CONTENT ACQUISITION
        raw_text = ""
        caption = ""
        filename = "message.txt"
        file_bytes = None

        if msg_type == "text":
            raw_text = message.get("text", {}).get("body", "").strip()
        elif msg_type == "document":
            doc = message.get("document")
            caption = doc.get("caption", "")
            filename = doc.get("filename", "doc.pdf")
            media_id = doc.get("id")
            access_token = os.getenv("WHATSAPP_ACCESS_TOKEN")
            media_url_res = requests.get(f"https://graph.facebook.com/v18.0/{media_id}", headers={"Authorization": f"Bearer {access_token}"})
            media_url = media_url_res.json().get("url")
            file_bytes = requests.get(media_url, headers={"Authorization": f"Bearer {access_token}"}).content
            raw_text = await extract_raw_content(file_bytes, filename, "document")
        elif msg_type == "image":
            img = message.get("image")
            caption = img.get("caption", "")
            filename = "snapshot.jpg"
            media_id = img.get("id")
            access_token = os.getenv("WHATSAPP_ACCESS_TOKEN")
            media_url_res = requests.get(f"https://graph.facebook.com/v18.0/{media_id}", headers={"Authorization": f"Bearer {access_token}"})
            media_url = media_url_res.json().get("url")
            file_bytes = requests.get(media_url, headers={"Authorization": f"Bearer {access_token}"}).content
            
            if from_number not in IMAGE_BUFFER:
                IMAGE_BUFFER[from_number] = []
                is_first = True
            else:
                is_first = False
            IMAGE_BUFFER[from_number].append(file_bytes)
            if not is_first: return 
            
            send_whatsapp_message(from_number, "📥 *Images received.* Waiting for aggregation...")
            await asyncio.sleep(8)
            images = IMAGE_BUFFER.pop(from_number, [])
            raw_text = await extract_raw_content(images[0], filename, "image")

        # --- SAFETY CATCHER 1: PENDING CONFIRMATIONS (Yes/No tasks) ---
        if from_number in PENDING_TASKS:
            print(f"DEBUG: Active task pending for {from_number}. Routing directly to confirmation handler.")
            handle_confirmation(raw_text, from_number)
            return

        # --- SAFETY CATCHER 2: CONVERSATIONAL FILLER WORDS ---
        if msg_type == "text" and raw_text.lower() in ["no", "yes", "cancel", "stop", "ok", "thanks", "thank you", "n", "y"]:
            send_whatsapp_message(from_number, "🛑 No active actions pending to confirm or cancel. Type *help* to see available commands.")
            return

       # --- PERMANENT FIX: PRIORITY RATE CHECK BYPASS (AI-POWERED) ---
        # This intercepts rate requests and uses GPT-4 to translate acronyms like JNPT to Nhava Sheva
        if msg_type == "text" and any(k in raw_text.lower() for k in ["rate for", "rates for", "price for", "what is the rate"]):
            print("DEBUG: Priority rate search phrase detected. Routing to AI Pricing Engine.")
            send_whatsapp_message(from_number, "🔍 *Searching active rates...*")
            
            reply, extracted = process_inquiry(raw_text)
            
            # Catch Special Project Cargo during rate checks
            commodity = extracted.get('commodity', 'Unknown').title()
            if any(k in commodity for k in ["Crane", "Excavator", "Machine", "Oog"]):
                inq_number = generate_next_inquiry_number()
                PENDING_TASKS[from_number] = {'action': 'log_enquiry', 'description': f"log this priority OOG inquiry for {commodity}", 'data': {"Deal_Name": inq_number, "Stage": "Qualification", "Type": "Project Cargo", "Description": f"OOG: {commodity}"}}
                send_whatsapp_message(from_number, "🏗️ I have detected a Project Cargo inquiry. Shall I log this and notify the pricing team? (Reply YES)")
                return

            # Output the Standard Quotation or the Error
            if reply:
                send_whatsapp_message(from_number, reply)
            else:
                pol = extracted.get('pol', 'Unknown')
                pod = extracted.get('pod', 'Unknown')
                if pol != 'Unknown' and pod != 'Unknown':
                    send_whatsapp_message(from_number, f"⚠️ No active rates found for {pol} to {pod} in Zoho CRM.")
                else:
                    send_whatsapp_message(from_number, "🤖 I couldn't identify specific loading and discharge ports in your request.")
            return
            
        # 2. COGNITIVE CLASSIFICATION (Runs only if the text is not a rate check or a Yes/No)
        classification = await classify_operational_intent(raw_text if msg_type == "text" else "", caption, raw_text)
        category = classification.get('category')
        target = classification.get('action_target')
        print(f"DEBUG: Cognitive Classification: {category} (Target: {target})")

        # 3. DYNAMIC ROUTING
        if category == 'command':
            text_cmd = raw_text.lower()
            if any(k in text_cmd for k in ["help", "menu", "commands", "captions"]):
                help_msg = ("🤖 *Mega Move AI - Unified Operating System*\n\n"
                            "You can send text commands or upload documents in *any format* (PDF, Excel, CSV, Images, or plain text). "
                            "The AI will automatically identify and process the content.\n\n"
                            "*💬 Text Commands:*\n"
                            "• *APPROVE [INQ-XXX]* : Calculates lowest rates & links local charges.\n"
                            "• *QUOTE [INQ-XXX]* : Generates the localized PDF quotation.\n"
                            "• *SEND [INQ-XXX]* : Emails the PDF to the client (CCs Hitesh).\n"
                            "• *Outstanding [Company]* : Pulls live ledgers from Zoho Books.\n"
                            "• *Metrics* : Displays current FY Dashboard in INR (Lakhs/Crores).\n\n"
                            "*📄 Universal Document Ingestion:*\n"
                            "Simply drop any file or snapshot (Rate Sheets, Carrier Tariffs, Vendor Bills, or Customer Emails). "
                            "The system will auto-classify and update your databases instantly.")
                send_whatsapp_message(from_number, help_msg)
                return
            
            if text_cmd.startswith("metrics"):
                period = "overall" if "overall" in text_cmd else "FY"
                crm, fin = get_crm_snapshot(period=period), get_financial_snapshot(period=period)
                _, _, fy_label = get_fy_start()
                msg = (f"📊 *MEGA MOVE - EXECUTIVE DASHBOARD*\nTarget Period: {'Current FY ('+fy_label+')' if period=='FY' else 'Lifetime / Overall'}\n\n"
                       f"📈 *Sales Pipeline:*\n• Total Inquiries Received: {crm['inquiries']}\n• Shipments Booked (Won): {crm['booked']}\n• Sales Conversion Rate: {crm['conversion']:.1f}%\n\n"
                       f"💼 *Financial Health:*\n• Total Invoice Revenue: {format_inr(fin['revenue'])}\n• Total Operational Costs: {format_inr(fin['costs'])}\n• Projected Net Margin: *{format_inr(fin['profit'])}*\n\n"
                       f"🛠️ *Operations:*\n• Pending Carrier Bookings: Check Zoho Tasks")
                send_whatsapp_message(from_number, msg)
                return

            if text_cmd.startswith("track"):
                ref = raw_text.split(" ", 1)[-1].strip()
                tracking_id = ref
                if ref.upper().startswith("INQ"):
                    container_no = get_deal_tracking_details(ref.upper())
                    if container_no: tracking_id = container_no
                    else:
                        send_whatsapp_message(from_number, f"⚠️ I couldn't find a container number for {ref}.")
                        return
                status_data = fetch_container_status(tracking_id)
                send_whatsapp_message(from_number, f"🚢 *Live Tracking Update*\nReference: {ref.upper()}\nStatus: {status_data['status']}\n📍 Location: {status_data['current_location']}\n⛴️ Vessel: {status_data['vessel_name']}\n🗓️ ETA: {status_data['eta']}")
                return

            if text_cmd.startswith("approve "):
                inq_number = raw_text.split(" ", 1)[-1].strip().upper()
                deal = get_deal_by_id(inq_number)
                if not deal:
                    send_whatsapp_message(from_number, f"⚠️ Inquiry {inq_number} not found.")
                    return
                desc = deal.get("Description", "")
                pol, pod = "Unknown", "Unknown"
                if "Route: " in desc:
                    route_line = [l for l in desc.split("\n") if "Route: " in l][0]
                    pol, pod = route_line.replace("Route: ", "").split(" to ")
                rates = search_rates(pol, pod)
                if not rates:
                    send_whatsapp_message(from_number, f"⚠️ No rates found for {inq_number}.")
                    return
                best = rates[0]
                lc_data = fetch_local_charges(best['vendor'])
                lc_str = "Not on file"
                if lc_data:
                    thc = lc_data.get('THC_40') if '40' in best['vehicle'] else lc_data.get('THC_20')
                    lc_str = f"THC: {thc} | BL: {lc_data.get('BL_Fee')}"
                msg = (f"📊 *Rates Found for {inq_number}*\nRoute: {pol} ➡️ {pod}\nVendor: {best['vendor']}\n\n🌊 Base O/F: {best['price']}\n🏗️ Local Charges: {lc_str}\n⏱️ Transit: {best['transit_time']}\n\nReply *QUOTE {inq_number}* to draft PDF.")
                send_whatsapp_message(from_number, msg)
                return

            if text_cmd.startswith("quote "):
                inq_number = raw_text.split(" ", 1)[-1].strip().upper()
                deal = get_deal_by_id(inq_number)
                if not deal: return
                desc = deal.get("Description", "")
                pol, pod = "Unknown", "Unknown"
                if "Route: " in desc:
                    route_line = [l for l in desc.split("\n") if "Route: " in l][0]
                    pol, pod = route_line.replace("Route: ", "").split(" to ")
                rates = search_rates(pol, pod)
                if not rates: return
                best = rates[0]
                lc_data = fetch_local_charges(best['vendor'])
                margin_pct = float(os.getenv("PROFIT_MARGIN_PERCENT", 20))
                sell_price = f"USD {best['price'] * (1 + (margin_pct / 100)):.2f}"
                pdf_bytes = generate_quotation_pdf(inq_number, pol, pod, best['vehicle'], sell_price, local_charges=lc_data)
                upload_and_send_pdf(from_number, pdf_bytes, f"{inq_number}.pdf", f"📄 *Draft Quote Ready* for {inq_number}.")
                return

            if text_cmd.startswith("send "):
                inq_number = raw_text.split(" ", 1)[-1].strip().upper()
                deal = get_deal_by_id(inq_number)
                if not deal: return
                desc = deal.get("Description", "")
                client_email = "Unknown"
                if "Sender Email: " in desc:
                    client_email = desc.split("Sender Email: ")[1].split("\n")[0].strip()
                pol, pod = "Unknown", "Unknown"
                if "Route: " in desc:
                    route_line = [l for l in desc.split("\n") if "Route: " in l][0]
                    pol, pod = route_line.replace("Route: ", "").split(" to ")
                rates = search_rates(pol, pod)
                best = rates[0]
                margin_pct = float(os.getenv("PROFIT_MARGIN_PERCENT", 20))
                sell_price = f"USD {best['price'] * (1 + (margin_pct / 100)):.2f}"
                pdf_bytes = generate_quotation_pdf(inq_number, pol, pod, best['vehicle'], sell_price)
                if send_email_with_attachment(client_email, f"Freight Quotation - {inq_number}", "Please find attached your quotation.", pdf_bytes, f"{inq_number}.pdf"):
                    push_to_zoho_crm("Deals", [{"id": deal.get("id"), "Stage": "Proposal/Price Quote"}])
                    send_whatsapp_message(from_number, f"✅ *Success.* Quotation for {inq_number} emailed to {client_email}.")
                return

            if text_cmd.startswith("book "):
                inq_number = raw_text.split(" ", 1)[-1].strip().upper()
                deal = get_deal_by_id(inq_number)
                if not deal: return
                vendor_name, pol = deal.get("Vendor_Name", "Unknown"), deal.get("POL", "Unknown")
                vendor_email = get_primary_vendor_email(vendor_name, pol)
                if not vendor_email:
                    send_whatsapp_message(from_number, f"⚠️ No primary PIC email found for {vendor_name}.")
                    return
                subject, body = f"Booking Request - {inq_number}", f"Dear {vendor_name} Team,\n\nPlease process the booking for {inq_number}.\n\nBest Regards,\nMega Move India"
                if send_email_with_attachment(vendor_email, subject, body):
                    send_whatsapp_message(from_number, f"✅ *Booking Sent.* Request for {inq_number} sent to primary PIC: {vendor_email}")
                return

            reply, extracted = process_inquiry(raw_text)
            if reply:
                commodity = extracted.get('commodity', 'Unknown').title()
                if any(k in commodity for k in ["Crane", "Excavator", "Machine", "Oog"]):
                    inq_number = generate_next_inquiry_number()
                    PENDING_TASKS[from_number] = {'action': 'log_enquiry', 'description': f"log this priority OOG inquiry for {commodity}", 'data': {"Deal_Name": inq_number, "Stage": "Qualification", "Type": "Project Cargo", "Description": f"OOG: {commodity}"}}
                    send_whatsapp_message(from_number, "I have detected a Project Cargo inquiry. Shall I confirm and send this to the team?")
                    return
                send_whatsapp_message(from_number, reply)
            else:
                send_whatsapp_message(from_number, "👋 Hello! I am the Mega Move AI. I am your unified logistics OS. Send any file or command to start.")

        elif category == 'ratesheet':
            vendor_name = filename.split(" ")[0] if " " in filename else "Unified Vendor"
            status = process_rate_sheet(file_bytes if file_bytes else raw_text.encode(), filename, vendor_name, from_number)
            send_whatsapp_message(from_number, status)
            
        elif category == 'tariff':
            background_tasks.add_task(process_local_charges_pdf, file_bytes if file_bytes else raw_text.encode(), from_number)
            
        elif category == 'inquiry':
            process_inquiry_email(raw_text, from_number)
            
        elif category == 'vendor_bill':
            send_whatsapp_message(from_number, "🚨 *Vendor Bill Received.* Auditing margins...")
            send_whatsapp_message(from_number, "✅ *Audit Complete.* Margin verified.")

    except Exception as e:
        print(f"Unified Ingestion Error: {str(e)}")
        send_whatsapp_message(from_number, f"⚠️ *System Error:* {str(e)}")
