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

# --- GLOBAL STATE ---
PENDING_TASKS = {}
PROCESSED_MESSAGE_IDS = set()
LAST_CLEANUP = datetime.now()
IMAGE_BUFFER = {}

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

# --- MASTER CONFIGURATION ---
PORT_ALIASES = {
    "JNPT": "Nhava Sheva",
    "INNSA": "Nhava Sheva",
    "JEA": "Jebel Ali",
    "AEJEA": "Jebel Ali",
    "SHA": "Shanghai",
    "CNSHA": "Shanghai",
    "NSA": "Nhava Sheva",
    "MUNDRA": "Mundra",
    "INMUN": "Mundra",
    "JEBEL ALI": "Jebel Ali",
    "DXB": "Dubai",
    "AEDXB": "Dubai"
}

def normalize_port_name(raw_name):
    if not raw_name:
        return raw_name
    
    # STEP 1: NORMALIZATION PIPELINE
    # 1.1 Convert to uppercase
    name = str(raw_name).upper()
    
    # 1.2 Replace punctuation (commas, slashes) with spaces
    name = re.sub(r'[,/]', ' ', name)
    
    # 1.3 Strip noise words
    noise_words = ["PORT", "TERMINAL", "WHARF", "CHINA", "INDIA", "UAE"]
    for word in noise_words:
        # Use regex to replace only full words to avoid partial matches (e.g., "PORT SAID")
        name = re.sub(rf'\b{word}\b', '', name)
    
    # 1.4 Clean extra whitespace
    name = re.sub(r'\s+', ' ', name).strip()

    if not name:
        return raw_name

    # STEP 2: ALIAS CHECK
    if name in PORT_ALIASES:
        return PORT_ALIASES[name]

    # STEP 3: RAPIDFUZZ MATCHING
    standard_names = list(set(PORT_ALIASES.values()))
    result = process.extractOne(name, standard_names, scorer=fuzz.token_sort_ratio)
    
    if result:
        matched_name, score, _ = result
        
        # STEP 4: THRESHOLD LOGIC
        if score > 90:
            return matched_name
        elif score >= 70:
            print(f"REVIEW_REQUIRED: '{raw_name}' matched with '{matched_name}' at {score:.1f}%")
            return matched_name
            
    return name.title()

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
    background_tasks.add_task(process_whatsapp_message, payload)
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
                            "transit_time": r.get("Transit_Time") or "N/A",
                            "route": r.get("Route") or "N/A",
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
        "If data is missing, return 'Unknown'."
    )
    prompt = f"Message: {text}"
    response = client.chat.completions.create(
        model="gpt-4o",
        messages=[{"role": "system", "content": system_prompt},
                  {"role": "user", "content": prompt}],
        response_format={ "type": "json_object" }
    )
    extracted = json.loads(response.choices[0].message.content)
    
    # Safely extract with 'Unknown' defaults
    pol = extracted.get('pol', 'Unknown')
    pod = extracted.get('pod', 'Unknown')
    commodity = extracted.get('commodity', 'Unknown')
    
    if pol != 'Unknown' and pod != 'Unknown':
        rates = search_rates(pol, pod)
        if rates:
            margin_pct = float(os.getenv("PROFIT_MARGIN_PERCENT", 20))
            inq_number = generate_next_inquiry_number()
            
            # Group by vehicle type and pick lowest price for each
            best_rates_by_type = {}
            for r in rates:
                v_type = r['vehicle']
                if v_type not in best_rates_by_type or r['price'] < best_rates_by_type[v_type]['price']:
                    best_rates_by_type[v_type] = r
            
            # Construct multi-equipment response
            rates_text = ""
            for v_type, rate in best_rates_by_type.items():
                sell_price = rate['price'] * (1 + (margin_pct / 100))
                rates_text += (
                    f"📦 *Equipment:* {v_type}\n"
                    f"💰 *Ocean Freight:* USD {sell_price:.2f}\n"
                    f"⏱️ *Transit Time:* {rate['transit_time']}\n"
                    f"🗺️ *Routing:* {rate['route']}\n"
                    f"⏳ *Valid until:* {rate['validity_date']}\n\n"
                )
            
            reply = (
                f"🚢 *Quotation: {rates[0]['pol']} ➡️ {rates[0]['pod']}*\n\n"
                f"{rates_text}"
                f"🏢 *Vendor:* {rates[0]['vendor']}\n"
                f"Ref: {inq_number}"
            )
            return reply, extracted
            
    return None, extracted

def process_image_inquiry(image_bytes_list):
    """Analyzes multiple images using GPT-4o-vision. Returns extracted JSON."""
    system_prompt = (
        "You are an expert freight forwarder AI. Output strictly valid JSON with EXACTLY these keys: "
        "'shipper', 'pol', 'pod', 'commodity', 'equipment_type', 'weight', 'readiness'. "
        "Do not alter these key names. If data is missing, return 'Unknown'."
    )
    content = [{"type": "text", "text": system_prompt}]
    
    for img_bytes in image_bytes_list:
        base64_image = base64.b64encode(img_bytes).decode('utf-8')
        content.append({
            "type": "image_url",
            "image_url": {
                "url": f"data:image/jpeg;base64,{base64_image}"
            }
        })
    
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

def generate_quotation_pdf(inq_number, pol, pod, equipment, sell_price):
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
    pdf.ln(10)
    pdf.set_font("Arial", 'B', 14)
    pdf.cell(200, 10, txt=f"Total Sell Price: USD {sell_price:.2f}", ln=1, align='L')
    pdf.ln(20)
    pdf.set_font("Arial", 'I', 10)
    pdf.cell(200, 10, txt="* Rates are subject to space and equipment availability.", ln=1, align='L')
    file_path = "/tmp/quotation.pdf"
    pdf.output(file_path)
    with open(file_path, "rb") as f:
        return f.read()

# --- RATE SHEET PROCESSING ---
def process_rate_sheet(file_content, filename, vendor_name, wa_id=None):
    """Parses Excel/PDF rate sheets using a calibrated GPT-4o prompt."""
    try:
        if wa_id:
            send_whatsapp_message(wa_id, "📥 *Received document.* Analyzing content...")

        # Extract text/data from file
        if filename.endswith(".xlsx") or filename.endswith(".xls"):
            df = pd.read_excel(io.BytesIO(file_content), sheet_name=None)
            raw_data = ""
            for sheet, data in df.items():
                raw_data += f"--- Tab: {sheet} ---\n{data.to_string()}\n"
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
            "You are an expert freight forwarder AI. Output strictly valid JSON with EXACTLY these keys: "
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
        {raw_data[:12000]}
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
        
        if wa_id:
            send_whatsapp_message(wa_id, f"✅ *Successfully extracted {len(extracted_rates)} rates.* Pushing to Zoho CRM in batches...")

        # Log to Zoho CRM (Pricings module)
        zoho_data = []
        for rate in extracted_rates:
            carrier_name = "PIL (INDIA) PVT. LTD"
            norm_pol = normalize_port_name(rate.get("pol", "Unknown"))
            norm_pod = normalize_port_name(rate.get("pod", "Unknown"))
            
            # Standardize equipment type and keys for mapping
            eq_type = rate.get("equipment_type", "Unknown")
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
    """Parses incoming emails and creates Zoho CRM Deals with Contact mapping."""
    try:
        email_body = payload.get("body", "")
        sender_email = payload.get("sender", "Client")
        subject = payload.get("subject", "No Subject")
        
        prompt = f"""
        Analyze this freight request email.
        Extract the following and return strictly valid JSON:
        - pol: Port of Loading
        - pod: Port of Discharge
        - container_type: Equipment requested
        - commodity: If mentioned
        - company: The company requesting the quote
        - contact_email: The email of the requester (use {sender_email} if not found in body)
        
        Subject: {subject}
        Body: {email_body}
        """
        response = client.chat.completions.create(
            model="gpt-4o",
            messages=[{"role": "system", "content": "You are a precise logistics parser. Output only valid JSON."},{"role": "user", "content": prompt}],
            response_format={ "type": "json_object" }
        )
        extracted = json.loads(response.choices[0].message.content)
        
        pol = extracted.get('pol')
        pod = extracted.get('pod')
        company_name = extracted.get('company') or "Unknown Company"
        contact_email = extracted.get('contact_email') or sender_email
        
        if pol and pod:
            access_token = get_zoho_access_token()
            headers = {"Authorization": f"Zoho-oauthtoken {access_token}"}
            
            # Advanced Contact Mapping
            contact_id = None
            search_res = requests.get(f"https://www.zohoapis.in/crm/v3/Contacts/search?email={contact_email}", headers=headers)
            if search_res.status_code == 200 and search_res.json().get("data"):
                contact_id = search_res.json()["data"][0]["id"]
            
            inq_number = generate_next_inquiry_number()
            deal_data = {
                "Deal_Name": inq_number,
                "Stage": "Qualification",
                "Description": f"Route: {pol} to {pod}\nCommodity: {extracted.get('commodity')}\nContainer: {extracted.get('container_type')}\nReceived From: {contact_email}"
            }
            if contact_id:
                deal_data["Contact_Name"] = {"id": contact_id}
            
            push_to_zoho_crm("Deals", [deal_data])
            
            alert_msg = f"📧 *New Email RFQ Processed*\n\nID: {inq_number}\nFrom: {contact_email}\nRoute: {pol} ➡️ {pod}\n\nI have logged this in Zoho CRM."
            
            best_rate = search_lowest_rate(pol, pod)
            if best_rate:
                margin_pct = float(os.getenv("PROFIT_MARGIN_PERCENT", 20))
                sell_price = best_rate['price'] * (1 + (margin_pct / 100))
                pdf_bytes = generate_quotation_pdf(inq_number, pol, pod, best_rate['vehicle'], sell_price)
                
                validity_info = f"⏳ Valid until: {best_rate.get('validity_date', 'Unknown')}"
                alert_msg += f"\n\n✅ I also found a rate and generated a quotation.\n{validity_info}"
                
                my_number = os.getenv("YOUR_WHATSAPP_NUMBER") 
                if my_number:
                    upload_and_send_pdf(my_number, pdf_bytes, f"{inq_number}.pdf", alert_msg)
            else:
                alert_msg += f"\n\n⚠️ No valid rates found for this route."
                my_number = os.getenv("YOUR_WHATSAPP_NUMBER")
                if my_number:
                    send_whatsapp_message(my_number, alert_msg)
                    
    except Exception as e:
        print(f"Email Processing Error: {e}")

# --- WHATSAPP MESSAGE PROCESSING ---
async def process_whatsapp_message(payload):
    """Handles incoming WhatsApp messages/files and triggers AI processing."""
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

        # 0.1 Periodic Cleanup of IDs (every 10 minutes)
        if (datetime.now() - LAST_CLEANUP).total_seconds() > 600:
            PROCESSED_MESSAGE_IDS.clear()
            LAST_CLEANUP = datetime.now()
            print("Cleared processed message IDs cache.")

        from_number = message.get("from")
        
        # 1. HANDLE FILES (Rate Sheets)
        if message.get("type") == "document":
            doc = message.get("document")
            media_id = doc.get("id")
            filename = doc.get("filename")
            caption = doc.get("caption", "")
            print(f"Received document: {filename} with caption: '{caption}'")
            
            access_token = os.getenv("WHATSAPP_ACCESS_TOKEN")
            media_url_res = requests.get(f"https://graph.facebook.com/v18.0/{media_id}", 
                                         headers={"Authorization": f"Bearer {access_token}"})
            media_url = media_url_res.json().get("url")
            file_bytes = requests.get(media_url, headers={"Authorization": f"Bearer {access_token}"}).content
            
            vendor_name = filename.split(" ")[0] if " " in filename else "WhatsApp Vendor"
            status = process_rate_sheet(file_bytes, filename, vendor_name, from_number)
            send_whatsapp_message(from_number, status)

        # 1.1 HANDLE IMAGES (Project Cargo/OOG Enquiries with Aggregation Buffer)
        elif message.get("type") == "image":
            img = message.get("image")
            media_id = img.get("id")
            
            access_token = os.getenv("WHATSAPP_ACCESS_TOKEN")
            media_url_res = requests.get(f"https://graph.facebook.com/v18.0/{media_id}", 
                                         headers={"Authorization": f"Bearer {access_token}"})
            media_url = media_url_res.json().get("url")
            image_bytes = requests.get(media_url, headers={"Authorization": f"Bearer {access_token}"}).content
            
            # Buffer management
            if from_number not in IMAGE_BUFFER:
                IMAGE_BUFFER[from_number] = []
                # First image in batch starts the timer
                is_first = True
            else:
                is_first = False
                
            IMAGE_BUFFER[from_number].append(image_bytes)
            
            if is_first:
                send_whatsapp_message(from_number, "📥 *Images received.* Waiting 10s for additional screenshots before analysis...")
                await asyncio.sleep(10)
                
                # BATCH PROCESSING
                send_whatsapp_message(from_number, "👁️ *Vision AI:* Analyzing all buffered images for shipment details...")
                
                try:
                    images_to_process = IMAGE_BUFFER[from_number]
                    extracted = process_image_inquiry(images_to_process)
                    
                    # Clear buffer early to prevent race conditions on subsequent messages
                    IMAGE_BUFFER.pop(from_number, None)
                    
                    commodity = extracted.get("commodity", "Unknown")
                    shipper = extracted.get("shipper", "Unknown")
                    pol = extracted.get("pol", "Unknown")
                    pod = extracted.get("pod", "Unknown")
                    readiness = extracted.get("readiness", "Unknown")
                    
                    inq_number = generate_next_inquiry_number()
                    
                    enquiry_data = {
                        "Deal_Name": inq_number,
                        "Stage": "Qualification",
                        "Type": "Project Cargo",
                        "Description": f"Vision AI Multi-Image Inquiry\nShipper: {shipper}\nRoute: {pol} to {pod}\nCargo: {commodity}\nSpecs: {extracted.get('equipment_type', 'Unknown')}, {extracted.get('weight', 'Unknown')}\nReadiness: {readiness}\nSource: WhatsApp Images ({len(images_to_process)} files)"
                    }
                    
                    PENDING_TASKS[from_number] = {
                        'action': 'log_enquiry',
                        'description': f"log this inquiry for {commodity}",
                        'data': enquiry_data
                    }
                    
                    send_whatsapp_message(from_number, f"📊 *Extraction Complete!*\n\nI have analyzed {len(images_to_process)} images and parsed the inquiry for {commodity}.\n\nReply *YES* to log this into Zoho CRM.")
                except Exception as e:
                    print(f"Vision Processing Error: {e}")
                    IMAGE_BUFFER.pop(from_number, None)
                    send_whatsapp_message(from_number, f"❌ *Error during image analysis:* {str(e)}")
            else:
                # Subsequent images just get a small notification or silent append
                print(f"Added additional image to buffer for {from_number}")

        # 2. HANDLE TEXT (Greetings or Inquiries)
        elif message.get("type") == "text":
            text = str(message.get("text", {}).get("body", "")).strip()
            text_lower = text.lower()
            
            # CHECK FOR PENDING TASKS (Human-in-the-Loop)
            if from_number in PENDING_TASKS:
                handle_confirmation(text, from_number)
                return

            # --- EXECUTIVE DASHBOARD (PHASE 6) ---
            if text_lower.startswith("metrics"):
                period = "overall" if "overall" in text_lower else "FY"
                _, _, fy_label = get_fy_start()
                period_header = f"Current FY ({fy_label})" if period == "FY" else "Lifetime / Overall"
                
                print(f"DEBUG: Generating Executive Dashboard ({period}) for Admin: {from_number}")
                crm = get_crm_snapshot(period=period)
                fin = get_financial_snapshot(period=period)
                
                msg = (
                    f"📊 *MEGA MOVE - EXECUTIVE DASHBOARD*\n"
                    f"Target Period: {period_header}\n\n"
                    f"📈 *Sales Pipeline:*\n"
                    f"• Total Inquiries Received: {crm['inquiries']}\n"
                    f"• Shipments Booked (Won): {crm['booked']}\n"
                    f"• Sales Conversion Rate: {crm['conversion']:.1f}%\n\n"
                    f"💼 *Financial Health:*\n"
                    f"• Total Invoice Revenue: {format_inr(fin['revenue'])}\n"
                    f"• Total Operational Costs: {format_inr(fin['costs'])}\n"
                    f"• Projected Net Margin: *{format_inr(fin['profit'])}*\n\n"
                    f"🛠️ *Operations:*\n"
                    f"• Pending Carrier Bookings: Check Zoho Tasks"
                )
                send_whatsapp_message(from_number, msg)
                return

            # --- LIVE TRACKING ENGINE (PHASE 4) ---
            if text_lower.startswith("track"):
                ref = text.split(" ", 1)[-1].strip()
                tracking_id = ref
                
                # If reference is an Inquiry ID, lookup the container number in Zoho
                if ref.upper().startswith("INQ"):
                    print(f"DEBUG: Tracking by Inquiry ID: {ref}")
                    container_no = get_deal_tracking_details(ref.upper())
                    if container_no:
                        tracking_id = container_no
                    else:
                        send_whatsapp_message(from_number, f"⚠️ I couldn't find a container number for inquiry {ref} in Zoho CRM.")
                        return

                status_data = fetch_container_status(tracking_id)
                msg = (
                    f"🚢 *Live Tracking Update*\n"
                    f"Reference: {ref.upper()}\n"
                    f"Status: {status_data['status']}\n"
                    f"📍 Current Location: {status_data['current_location']}\n"
                    f"⛴️ Vessel: {status_data['vessel_name']}\n"
                    f"🗓️ ETA: {status_data['eta']}"
                )
                send_whatsapp_message(from_number, msg)
                return

            # 1. AI Extraction (Standard Inquiries)
            reply, extracted = process_inquiry(text_lower)
            
            # 2. PROJECT CARGO / OOG DETECTION
            commodity = extracted.get('commodity', 'Unknown').title()
            pol = extracted.get('pol', 'Unknown')
            pod = extracted.get('pod', 'Unknown')
            
            if any(k in commodity for k in ["Crane", "Excavator", "Machine", "Oog"]):
                inq_number = generate_next_inquiry_number()
                enquiry_data = {
                    "Deal_Name": inq_number,
                    "Stage": "Qualification",
                    "Type": "Project Cargo",
                    "Description": f"Detected Project Cargo (OOG)\nRoute: {pol} to {pod}\nCommodity: {commodity}\nSpecs: {extracted.get('equipment_type', 'Unknown')}, {extracted.get('weight', 'Unknown')}\nSource: WhatsApp"
                }
                
                PENDING_TASKS[from_number] = {
                    'action': 'log_enquiry',
                    'description': f"log this priority OOG inquiry for {commodity}",
                    'data': enquiry_data
                }
                
                send_whatsapp_message(from_number, "I have detected a Project Cargo/OOG inquiry. I am logging this as a priority inquiry for our operations team. Shall I confirm and send this to the team?")
                return

            # 3. Standard Quote First logic
            if reply:
                send_whatsapp_message(from_number, reply)
                return
                
            # 4. Fallback to Greetings or Enquiry Logging
            if text in ["hi", "hello", "hey"]:
                send_whatsapp_message(from_number, "👋 Hello! I am the Mega Move AI. Send me a rate sheet or an inquiry to get started.")
            else:
                if pol != 'Unknown' and pod != 'Unknown':
                    # PROMPT FOR CONFIRMATION instead of logging immediately
                    inq_number = generate_next_inquiry_number()
                    enquiry_data = {
                        "Deal_Name": inq_number,
                        "Stage": "Qualification",
                        "Description": f"Source: WhatsApp\nRoute: {pol} to {pod}\nCommodity: {commodity}\nSpecs: {extracted.get('equipment_type', 'Unknown')}, {extracted.get('weight', 'Unknown')}"
                    }
                    
                    PENDING_TASKS[from_number] = {
                        'action': 'log_enquiry',
                        'description': f"log an enquiry for {pol} to {pod}",
                        'data': enquiry_data
                    }
                    
                    send_whatsapp_message(from_number, f"🔍 I couldn't find an instant rate for {pol} to {pod}. \n\nI am about to log this as an enquiry in Zoho CRM. Please reply *YES* to confirm, or *NO* to cancel.")
                else:
                    send_whatsapp_message(from_number, "👋 Hello! I am the Mega Move AI. Send me a rate sheet or an inquiry to get started.")

    except Exception as e:
        print(f"CRITICAL ERROR: {str(e)}")
        print(f"WhatsApp Processing Error: {e}")
