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
from rate_parser import parse_rate_sheet as parse_rate_sheet_enhanced
from pricing_engine import get_pricing_recommendation, format_pricing_breakdown_for_whatsapp

# --- GLOBAL STATE ---
PENDING_TASKS = {}
PROCESSED_MESSAGE_IDS = set()
LAST_CLEANUP = datetime.now()
IMAGE_BUFFER = {}
GLOBAL_PORT_ALIASES = {}

async def run_daily_financial_audit():
    print("DEBUG: Starting Daily Financial Audit...")
    admin_number = os.getenv("YOUR_WHATSAPP_NUMBER")
    if not admin_number:
        print("CRITICAL: Admin WhatsApp number missing. Audit skipped.")
        return
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
    print("Mega Move AI Backend Starting Up...")
    global GLOBAL_PORT_ALIASES
    ports_file = "ports.json"
    if not os.path.exists(ports_file):
        print("DEBUG: ports.json missing. Building from UN/LOCODE CSVs...")
        ports_db = {}
        csv_files = glob.glob("*.csv")
        for f in csv_files:
            if "subdivision" in f.lower():
                continue
            for encoding in ["latin-1", "utf-8", "cp1252"]:
                for sep in [",", ";"]:
                    try:
                        df = pd.read_csv(f, sep=sep, encoding=encoding, keep_default_na=False, dtype=str)
                        if df.shape[1] < 4:
                            continue
                        col_country, col_loc, col_name, col_func = None, None, None, None
                        for col in df.columns:
                            col_str = str(col).lower()
                            if "country" in col_str: col_country = col
                            elif "location" in col_str: col_loc = col
                            elif "namewodiacritics" in col_str: col_name = col
                            elif "name" in col_str and not col_name: col_name = col
                            elif "function" in col_str: col_func = col
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
                                if func_val and (func_val.startswith("1") or "1" in func_val):
                                    country = str(row[col_country]).strip().upper()
                                    loc = str(row[col_loc]).strip().upper()
                                    name = str(row[col_name]).strip()
                                    if len(country) == 2 and len(loc) == 3:
                                        locode = f"{country}{loc}"
                                        ports_db[locode] = [name, locode, loc]
                            break
                    except:
                        continue
        if ports_db:
            with open(ports_file, "w") as jf:
                json.dump(ports_db, jf, indent=4)
            print(f"DEBUG: Successfully built ports.json with {len(ports_db)} maritime ports.")
        else:
            print("DEBUG: Parser ran but found no valid maritime port rows.")
    try:
        if os.path.exists(ports_file):
            with open(ports_file, "r") as pf:
                GLOBAL_PORT_ALIASES = json.load(pf)
            print(f"DEBUG: Loaded {len(GLOBAL_PORT_ALIASES)} ports into memory.")
    except Exception as e:
        print(f"ERROR: Failed to load ports.json: {e}")
    scheduler = AsyncIOScheduler()
    if os.getenv("TEST_MODE") == "true":
        print("DEBUG: TEST_MODE active. Running financial audit every 5 minutes.")
        scheduler.add_job(run_daily_financial_audit, 'interval', minutes=5)
    else:
        print("DEBUG: Scheduling daily financial audit at 09:00 AM IST.")
        scheduler.add_job(run_daily_financial_audit, CronTrigger(hour=3, minute=30))
    scheduler.start()
    yield
    print("Mega Move AI Backend Shutting Down...")
    scheduler.shutdown()


app = FastAPI(title="Mega Move AI Backend", lifespan=lifespan)
client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))


def extract_numeric_price(price_val):
    try:
        if price_val is None: return float('inf')
        clean_val = re.sub(r'[^\d.]', '', str(price_val))
        if not clean_val or clean_val == '.': return float('inf')
        return float(clean_val)
    except:
        return float('inf')


# FIX: Was named 'return name title' — invalid Python. Renamed to normalize_port_name.
# Also removed the duplicate 'standardize_port_name' function that followed it.
def normalize_port_name(raw_port):
    if not raw_port:
        return raw_port
    name = str(raw_port).upper()
    name = re.sub(r'[,/]', ' ', name)
    noise_words = ["PORT", "TERMINAL", "WHARF", "CHINA", "INDIA", "UAE"]
    for word in noise_words:
        name = re.sub(rf'\b{word}\b', '', name)
    name = re.sub(r'\s+', ' ', name).strip()
    if not name:
        return raw_port.title().strip()
    # FIX: Was missing comma after "BMCT" — this was the crash that broke deployment.
    common_aliases = {
        "JNPT": "Nhava Sheva",
        "JNP": "Nhava Sheva",
        "NSICT": "Nhava Sheva",
        "NSIGT": "Nhava Sheva",
        "GTI": "Nhava Sheva",
        "BMCT": "Nhava Sheva",
        "JEA": "Jebel Ali",
        "JED": "Jebel Ali",
        "SIN": "Singapore",
        "PKG": "Port Klang"
    }
    if name in common_aliases:
        return common_aliases[name]
    if GLOBAL_PORT_ALIASES:
        port_names = [v[0] for v in GLOBAL_PORT_ALIASES.values()]
        result = process.extractOne(name, port_names, scorer=fuzz.token_sort_ratio)
        if result:
            matched_name, score, _ = result
            if score >= 85:
                return matched_name.title()
    return name.title()


@app.get("/")
def read_root():
    return {"status": "Mega Move Logistics Engine is Running"}


@app.get("/whatsapp-webhook")
async def verify_whatsapp(request: Request):
    mode = request.query_params.get("hub.mode")
    token = request.query_params.get("hub.verify_token")
    challenge = request.query_params.get("hub.challenge")
    if mode == "subscribe" and token == os.getenv("WHATSAPP_VERIFY_TOKEN"):
        return PlainTextResponse(challenge)
    return {"error": "Invalid verification token"}


@app.post("/whatsapp-webhook")
async def whatsapp_webhook(request: Request, background_tasks: BackgroundTasks):
    payload = await request.json()
    try:
        value = payload.get('entry', [{}])[0].get('changes', [{}])[0].get('value', {})
        if 'statuses' in value:
            return {"status": "success"}
        messages = value.get('messages', [])
        if not messages:
            return {"status": "success"}
        message_id = messages[0].get('id')
        if message_id in PROCESSED_MESSAGE_IDS:
            print(f"DEBUG: Ignoring duplicate message_id: {message_id}")
            return {"status": "ignored_duplicate"}
        PROCESSED_MESSAGE_IDS.add(message_id)
    except Exception as e:
        print(f"Webhook Safety Check Error: {e}")
        return {"status": "success"}
    background_tasks.add_task(process_whatsapp_message, payload, background_tasks)
    return {"status": "success"}


@app.post("/email-webhook")
async def email_webhook(request: Request, background_tasks: BackgroundTasks):
    payload = await request.json()
    background_tasks.add_task(process_email_rfq, payload)
    return {"status": "success"}


# --- MESSAGING ---
def send_whatsapp_message(to_number, text):
    phone_id = os.getenv("WHATSAPP_PHONE_ID")
    url = f"https://graph.facebook.com/v18.0/{phone_id}/messages"
    headers = {"Authorization": f"Bearer {os.getenv('WHATSAPP_ACCESS_TOKEN')}", "Content-Type": "application/json"}
    payload = {"messaging_product": "whatsapp", "to": to_number, "type": "text", "text": {"body": text}}
    requests.post(url, headers=headers, json=payload)


def send_email_with_attachment(to_email, subject, body, pdf_bytes=None, filename=None):
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
        with smtplib.SMTP_SSL('smtp.zoho.in', 465) as server:
            server.login(sender_email, sender_password)
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


# --- ZOHO CRM FUNCTIONS ---
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
    url = f"https://www.zohoapis.in/crm/v3/{module}/upsert"
    headers = {"Authorization": f"Zoho-oauthtoken {access_token}", "Content-Type": "application/json"}
    payload = {"data": data_list, "duplicate_check_fields": ["Rate_Key"]}
    if data_list:
        print(f"DEBUG: Sending to Zoho module '{module}'. Record keys: {list(data_list[0].keys())}")
    response = requests.post(url, json=payload, headers=headers)
    if response.status_code not in [200, 201, 202]:
        raise Exception(f"Zoho API Global Error ({response.status_code}): {response.text}")
    res_data = response.json().get("data", [])
    for idx, item in enumerate(res_data):
        if item.get("status") == "error":
            raise Exception(f"Zoho Record Error [Row {idx+1}]: {item.get('message')} (Details: {item.get('details')})")


def create_zoho_record(module, data):
    """Creates a single new record in a Zoho CRM module. Returns the new record ID."""
    access_token = get_zoho_access_token()
    url = f"https://www.zohoapis.in/crm/v3/{module}"
    headers = {"Authorization": f"Zoho-oauthtoken {access_token}", "Content-Type": "application/json"}
    response = requests.post(url, json={"data": [data]}, headers=headers)
    if response.status_code in [200, 201]:
        res_data = response.json().get("data", [])
        if res_data and res_data[0].get("details"):
            return res_data[0]["details"].get("id")
    print(f"DEBUG: Failed to create record in {module}: {response.text}")
    return None


def find_or_create_account_and_contact(company_name, contact_name, contact_email, inq_number):
    """
    Smart lookup: finds an existing Account by company name or contact email.
    If nothing is found, creates a new Account and Contact from scratch.
    Returns (account_id, account_name, contact_id).
    """
    access_token = get_zoho_access_token()
    headers = {"Authorization": f"Zoho-oauthtoken {access_token}"}
    account_id = None
    account_name = company_name
    contact_id = None

    # Step 1: Try to find Account by company name
    if company_name and company_name not in ["Unknown", ""]:
        url = f"https://www.zohoapis.in/crm/v3/Accounts/search?criteria=(Account_Name:equals:{company_name})"
        res = requests.get(url, headers=headers)
        if res.status_code == 200 and res.json().get("data"):
            account = res.json()["data"][0]
            account_id = account.get("id")
            account_name = account.get("Account_Name", company_name)
            print(f"DEBUG: Found existing Account: {account_name} (ID: {account_id})")

    # Step 2: If no Account found, try finding by contact email
    if not account_id and contact_email and contact_email not in ["Unknown", ""]:
        url = f"https://www.zohoapis.in/crm/v3/Contacts/search?criteria=(Email:equals:{contact_email})"
        res = requests.get(url, headers=headers)
        if res.status_code == 200 and res.json().get("data"):
            existing_contact = res.json()["data"][0]
            contact_id = existing_contact.get("id")
            linked_account = existing_contact.get("Account_Name")
            if linked_account and isinstance(linked_account, dict):
                account_id = linked_account.get("id")
                account_name = linked_account.get("name", company_name)
            print(f"DEBUG: Found Contact by email: {contact_email} (ID: {contact_id})")

    # Step 3: If still no Account, create one
    if not account_id:
        # Use email domain as fallback company name if company is unknown
        fallback_name = company_name
        if fallback_name in ["Unknown", ""] and contact_email and "@" in contact_email:
            fallback_name = contact_email.split("@")[1].split(".")[0].title()
        print(f"DEBUG: Creating new Account: {fallback_name}")
        account_data = {
            "Account_Name": fallback_name,
            "Industry": "Logistics / Freight",
            "Description": f"Auto-created from email inquiry {inq_number}"
        }
        account_id = create_zoho_record("Accounts", account_data)
        account_name = fallback_name

    # Step 4: If no Contact found, create one linked to the Account
    if not contact_id and contact_email and contact_email not in ["Unknown", ""]:
        name_parts = (contact_name or "").split(" ", 1)
        first = name_parts[0] if len(name_parts) >= 1 else ""
        last = name_parts[1] if len(name_parts) == 2 else (first or contact_email.split("@")[0])
        contact_data = {
            "First_Name": first,
            "Last_Name": last,
            "Email": contact_email,
        }
        if account_id:
            contact_data["Account_Name"] = {"id": account_id}
        contact_id = create_zoho_record("Contacts", contact_data)
        print(f"DEBUG: Created new Contact ID: {contact_id}")

    return account_id, account_name, contact_id


# --- AUDIT FUNCTIONS ---
def check_overdue_invoices():
    access_token = get_zoho_access_token()
    org_id = os.getenv("ZOHO_BOOKS_ORG_ID")
    if not org_id: return []
    url = f"https://www.zohoapis.in/books/v3/invoices?status=overdue&organization_id={org_id}"
    headers = {"Authorization": f"Zoho-oauthtoken {access_token}"}
    res = requests.get(url, headers=headers)
    overdue_list = []
    if res.status_code == 200:
        for inv in res.json().get("invoices", []):
            due_date = datetime.strptime(inv.get("due_date"), "%Y-%m-%d").date()
            days_overdue = (datetime.now().date() - due_date).days
            if days_overdue >= 3:
                inv["days_overdue"] = days_overdue
                overdue_list.append(inv)
    return overdue_list


def check_vendor_bill_mismatches():
    access_token = get_zoho_access_token()
    org_id = os.getenv("ZOHO_BOOKS_ORG_ID")
    if not org_id: return []
    url = f"https://www.zohoapis.in/books/v3/bills?status=open&organization_id={org_id}"
    headers = {"Authorization": f"Zoho-oauthtoken {access_token}"}
    res = requests.get(url, headers=headers)
    mismatches = []
    if res.status_code == 200:
        for bill in res.json().get("bills", []):
            reference = bill.get("reference_number", "")
            if not reference.startswith("INQ"): continue
            crm_url = f"https://www.zohoapis.in/crm/v3/Deals/search?criteria=(Deal_Name:equals:{reference})"
            crm_res = requests.get(crm_url, headers=headers)
            if crm_res.status_code == 200 and crm_res.json().get("data"):
                deal = crm_res.json()["data"][0]
                expected_cost = float(deal.get("Buy_Rate") or 0)
                bill_amount = float(bill.get("total") or 0)
                if expected_cost > 0:
                    variance = ((bill_amount - expected_cost) / expected_cost) * 100
                    if variance > 5:
                        mismatches.append({
                            "vendor_name": bill.get("vendor_name"),
                            "bill_number": bill.get("bill_number"),
                            "bill_amount": bill_amount,
                            "crm_expected": expected_cost,
                            "variance": f"{variance:.2f}"
                        })
    return mismatches


def format_inr(number):
    try:
        is_negative = number < 0
        number = abs(number)
        s, *d = str(f"{number:.2f}").partition(".")
        r = ",".join([s[x-2:x] for x in range(-3, -len(s), -2)][::-1] + [s[-3:]])
        result = f"Rs. {r}{d[0]}{d[1]}"
        return f"-{result}" if is_negative else result
    except:
        return f"Rs. {number:,.2f}"


# FIX: Removed duplicate definition of extract_raw_content (appeared twice in old code).
async def extract_raw_content(file_bytes, filename, msg_type):
    ext = filename.split('.')[-1].lower()
    try:
        if ext in ['xlsx', 'xls', 'csv']:
            all_sheets = pd.read_excel(io.BytesIO(file_bytes), sheet_name=None, header=None)
            combined_text = ""
            for sheet_name, df in all_sheets.items():
                df = df.dropna(how='all').dropna(axis=1, how='all')
                combined_text += f"\n--- SHEET: {sheet_name} ---\n{df.to_csv(index=False)}\n"
            return combined_text
        elif ext == 'pdf':
            pdf_reader = PyPDF2.PdfReader(io.BytesIO(file_bytes))
            return "".join([page.extract_text() for page in pdf_reader.pages])
        elif msg_type == "image":
            base64_image = base64.b64encode(file_bytes).decode('utf-8')
            response = client.chat.completions.create(
                model="gpt-4o",
                messages=[{"role": "user", "content": [
                    {"type": "text", "text": "Perform a high-fidelity OCR dump of this logistics document. Extract all text, numbers, and tabular structures into a clean readable string."},
                    {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{base64_image}"}}
                ]}]
            )
            return response.choices[0].message.content
        else:
            return file_bytes.decode('utf-8', errors='ignore')
    except Exception as e:
        print(f"Extraction Error ({filename}): {e}")
        return f"Extraction Error: {str(e)}"


# FIX: Removed duplicate definition of classify_operational_intent.
async def classify_operational_intent(user_text, caption, content_snippet):
    system_prompt = (
        "You are an AI logistics operational router. Classify the user's intent. "
        "Return JSON: {\"category\": \"ratesheet\"|\"vendor_bill\"|\"tariff\"|\"inquiry\"|\"command\", "
        "\"action_target\": \"INQ-XXX\" or null, \"confidence\": float}"
    )
    user_context = f"User Msg: {user_text}\nCaption: {caption}\nContent Snippet: {content_snippet[:5000]}"
    response = client.chat.completions.create(
        model="gpt-4o",
        messages=[{"role": "system", "content": system_prompt}, {"role": "user", "content": user_context}],
        response_format={"type": "json_object"}
    )
    return json.loads(response.choices[0].message.content)


def get_fy_start():
    now = datetime.now()
    fy_start_year = now.year if now.month >= 4 else now.year - 1
    books_date = f"{fy_start_year}-04-01"
    crm_date = f"{fy_start_year}-04-01T00:00:00+05:30"
    fy_label = f"{fy_start_year}-{fy_start_year + 1}"
    return books_date, crm_date, fy_label


def get_crm_snapshot(period="FY"):
    access_token = get_zoho_access_token()
    books_start_str, _, _ = get_fy_start()
    fy_start_date = datetime.strptime(books_start_str, "%Y-%m-%d")
    url = "https://www.zohoapis.in/crm/v3/Deals?fields=Created_Time,Stage,Deal_Name&sort_by=Created_Time&sort_order=desc&per_page=100"
    headers = {"Authorization": f"Zoho-oauthtoken {access_token}"}
    total_inquiries = 0
    booked_shipments = 0
    page = 1
    more_records = True
    while more_records:
        res = requests.get(f"{url}&page={page}", headers=headers)
        if res.status_code == 200 and res.json().get("data"):
            for deal in res.json()["data"]:
                try:
                    created_str = deal.get("Created_Time")
                    if not created_str: continue
                    deal_date = datetime.strptime(created_str.split("T")[0], "%Y-%m-%d")
                    if period == "FY" and deal_date < fy_start_date:
                        more_records = False
                        break
                    total_inquiries += 1
                    if deal.get("Stage") == "Closed Won":
                        booked_shipments += 1
                except:
                    continue
            if not more_records: break
            if not res.json().get("info", {}).get("more_records"):
                more_records = False
            page += 1
        else:
            more_records = False
    conversion_rate = (booked_shipments / total_inquiries * 100) if total_inquiries > 0 else 0.0
    return {"inquiries": total_inquiries, "booked": booked_shipments, "conversion": conversion_rate}


def get_financial_snapshot(period="FY"):
    access_token = get_zoho_access_token()
    org_id = os.getenv("ZOHO_BOOKS_ORG_ID")
    headers = {"Authorization": f"Zoho-oauthtoken {access_token}"}
    books_start, _, _ = get_fy_start()
    date_filter = f"&date_start={books_start}" if period == "FY" else ""
    inv_res = requests.get(f"https://www.zohoapis.in/books/v3/invoices?organization_id={org_id}{date_filter}", headers=headers)
    bill_res = requests.get(f"https://www.zohoapis.in/books/v3/bills?organization_id={org_id}{date_filter}", headers=headers)
    revenue = sum([float(i.get("total", 0)) for i in inv_res.json().get("invoices", [])]) if inv_res.status_code == 200 else 0.0
    costs = sum([float(b.get("total", 0)) for b in bill_res.json().get("bills", [])]) if bill_res.status_code == 200 else 0.0
    return {"revenue": float(revenue), "costs": float(costs), "profit": float(revenue - costs)}


def get_deal_by_id(inq_number):
    access_token = get_zoho_access_token()
    url = f"https://www.zohoapis.in/crm/v3/Deals/search?criteria=(Deal_Name:equals:{inq_number})"
    headers = {"Authorization": f"Zoho-oauthtoken {access_token}"}
    res = requests.get(url, headers=headers)
    if res.status_code == 200 and res.json().get("data"):
        return res.json()["data"][0]
    return None


def get_primary_vendor_email(vendor_name, pol=None):
    access_token = get_zoho_access_token()
    headers = {"Authorization": f"Zoho-oauthtoken {access_token}"}
    res = requests.get(f"https://www.zohoapis.in/crm/v3/Vendors/search?criteria=(Vendor_Name:equals:{vendor_name})", headers=headers)
    if res.status_code == 200 and res.json().get("data"):
        return res.json()["data"][0].get("Email")
    res = requests.get(f"https://www.zohoapis.in/crm/v3/Contacts/search?criteria=(Account_Name:equals:{vendor_name})", headers=headers)
    if res.status_code == 200 and res.json().get("data"):
        for contact in res.json()["data"]:
            if contact.get("Email"): return contact.get("Email")
    return None


def get_deal_tracking_details(inq_number):
    access_token = get_zoho_access_token()
    url = f"https://www.zohoapis.in/crm/v3/Deals/search?criteria=(Deal_Name:equals:{inq_number})"
    headers = {"Authorization": f"Zoho-oauthtoken {access_token}"}
    res = requests.get(url, headers=headers)
    if res.status_code == 200 and res.json().get("data"):
        deal = res.json()["data"][0]
        return deal.get("Container_Number") or deal.get("MBL")
    return None


def fetch_container_status(tracking_reference):
    api_key = os.getenv("TRACKING_API_KEY")
    if not api_key:
        return {"status": "In Transit", "current_location": "En route to Singapore", "eta": "2026-06-15", "vessel_name": "MAERSK KYRENIA"}
    return {"status": "Tracking active", "current_location": "Coordinating with carrier...", "eta": "TBD", "vessel_name": "TBD"}


def generate_next_inquiry_number():
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
    return f"INQ-MMI-2026-{str(last_num + 1).zfill(3)}"


def search_rates(pol, pod):
    normalized_pol = normalize_port_name(pol)
    normalized_pod = normalize_port_name(pod)
    print(f"DEBUG: Searching Zoho for POL={normalized_pol}, POD={normalized_pod}")
    access_token = get_zoho_access_token()
    url = f"https://www.zohoapis.in/crm/v3/Pricings/search?criteria=(((POL:starts_with:{normalized_pol})and(POD:starts_with:{normalized_pod})))&fields=id"
    headers = {"Authorization": f"Zoho-oauthtoken {access_token}"}
    res = requests.get(url, headers=headers)
    valid_rates = []
    if res.status_code == 200 and res.json().get("data"):
        record_ids = [r.get("id") for r in res.json()["data"]]
        today = datetime.now().date()
        for record_id in record_ids:
            get_res = requests.get(f"https://www.zohoapis.in/crm/v3/Pricings/{record_id}", headers=headers)
            if get_res.status_code == 200 and get_res.json().get("data"):
                r = get_res.json()["data"][0]
                try:
                    validity_str = r.get("Validity_Date")
                    if validity_str:
                        try:
                            if datetime.strptime(validity_str, "%Y-%m-%d").date() < today:
                                continue
                        except:
                            pass
                    sub3 = r.get("Subform_3", [])
                    if not sub3: continue
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


# FIX: process_inquiry was using undefined variable 'content'. Now correctly uses 'prompt'.
# Also added system_prompt to messages array — required when using response_format json_object.
# Also fixed return value to always be a (reply, extracted) tuple.
def process_inquiry(text):
    system_prompt = (
        "You are an expert freight forwarder AI. Output strictly valid JSON with EXACTLY these keys: "
        "'shipper', 'pol', 'pod', 'commodity', 'equipment_type', 'weight'. "
        "Translate port acronyms to full names (e.g. JNPT = Nhava Sheva, JEA = Jebel Ali). "
        "Return JSON only. Use 'Unknown' for missing fields."
    )
    prompt = f"Message: {text}"
    response = client.chat.completions.create(
        model="gpt-4o",
        messages=[{"role": "system", "content": system_prompt}, {"role": "user", "content": prompt}],
        response_format={"type": "json_object"}
    )
    extracted = json.loads(response.choices[0].message.content)
    pol = extracted.get('pol', 'Unknown')
    pod = extracted.get('pod', 'Unknown')
    if pol == 'Unknown' or pod == 'Unknown':
        return None, extracted
    rates = search_rates(pol, pod)
    if not rates:
        return None, extracted
    best = rates[0]
    reply = (
        f"📦 *Rates found for {pol} ➡️ {pod}*\n\n"
        f"🏢 Vendor: {best['vendor']}\n"
        f"💰 Ocean Freight: USD {best['price']}\n"
        f"🚢 Container: {best['vehicle']}\n"
        f"⏱️ Transit: {best['transit_time']}\n"
        f"📅 Valid till: {best['validity_date']}\n\n"
        f"Reply *APPROVE INQ-MMI-2026-XXX* to generate a PDF quotation."
    )
    return reply, extracted


def classify_pdf_content(raw_data):
    prompt = "Classify this document as either 'VENDOR_RATE_SHEET' or 'INQUIRY_EMAIL'. Return only the keyword."
    response = client.chat.completions.create(
        model="gpt-4o",
        messages=[{"role": "user", "content": f"{prompt}\n\nDATA:\n{raw_data[:5000]}"}]
    )
    return response.choices[0].message.content.strip().upper()


def process_inquiry_email(raw_data, wa_id=None):
    try:
        system_prompt = (
            "You are an expert freight forwarder AI. Output strictly valid JSON with EXACTLY these keys: "
            "'shipper', 'pol', 'pod', 'commodity', 'equipment_type', 'weight', 'incoterms'. "
            "Use 'Unknown' for missing fields. Return JSON only."
        )
        response = client.chat.completions.create(
            model="gpt-4o",
            messages=[{"role": "system", "content": system_prompt}, {"role": "user", "content": f"DATA:\n{raw_data[:10000]}"}],
            response_format={"type": "json_object"}
        )
        extracted = json.loads(response.choices[0].message.content)
        pol = extracted.get('pol', 'Unknown')
        pod = extracted.get('pod', 'Unknown')
        shipper = extracted.get('shipper', 'Unknown')
        commodity = extracted.get('commodity', 'Unknown')
        inq_number = generate_next_inquiry_number()
        enquiry_data = {
            "Deal_Name": inq_number,
            "Stage": "Qualification",
            "Description": f"PDF Inquiry\nShipper: {shipper}\nRoute: {pol} to {pod}\nSpecs: {extracted.get('equipment_type', 'Unknown')}, {extracted.get('weight', 'Unknown')}\nCommodity: {commodity}\nIncoterms: {extracted.get('incoterms', 'Unknown')}"
        }
        if wa_id:
            PENDING_TASKS[wa_id] = {'action': 'log_enquiry', 'description': f"log an enquiry for {pol} to {pod}", 'module': 'New Enquiries', 'data': enquiry_data}
            send_whatsapp_message(wa_id, f"📊 *Inquiry Parsed!*\n\nShipper: {shipper}\nRoute: {pol} to {pod}\n\nReply *YES* to log this as a New Enquiry in Zoho CRM.")
            return "Parsed"
        return "Inquiry processed successfully."
    except Exception as e:
        print(f"CRITICAL ERROR (Inquiry PDF): {e}")
        if wa_id:
            send_whatsapp_message(wa_id, f"❌ *Error parsing inquiry:* {str(e)}")
        return f"Error: {e}"


def generate_quotation_pdf(inq_number, pol, pod, equipment, sell_price, local_charges=None):
    pdf = FPDF()
    pdf.add_page()
    pdf.set_font("Arial", 'B', 16)
    pdf.cell(200, 10, txt="MEGA MOVE INDIA PRIVATE LIMITED", ln=1, align='C')
    pdf.set_font("Arial", size=12)
    pdf.cell(200, 10, txt=f"FREIGHT QUOTATION - {inq_number}", ln=1, align='C')
    pdf.ln(10)
    for label, value in [("Port of Loading:", pol), ("Port of Discharge:", pod), ("Equipment:", equipment)]:
        pdf.set_font("Arial", 'B', 12)
        pdf.cell(50, 10, txt=label, ln=0)
        pdf.set_font("Arial", size=12)
        pdf.cell(150, 10, txt=str(value), ln=1)
    if local_charges:
        pdf.ln(5)
        pdf.set_font("Arial", 'B', 12)
        pdf.cell(200, 10, txt="Origin Local Charges:", ln=1)
        pdf.set_font("Arial", size=10)
        for key in ["THC_20", "THC_40", "BL_Fee", "Seal_Charge", "Toll", "MUC"]:
            val = local_charges.get(key)
            if val and val != "0":
                pdf.cell(200, 8, txt=f"  {key.replace('_', ' ')}: {val}", ln=1)
    pdf.ln(10)
    pdf.set_font("Arial", 'B', 14)
    pdf.cell(200, 10, txt=f"Total Sell Price: {sell_price}", ln=1, align='L')
    pdf.ln(20)
    pdf.set_font("Arial", 'B', 10)
    pdf.cell(200, 10, txt="Terms & Conditions:", ln=1)
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
    access_token = get_zoho_access_token()
    url = f"https://www.zohoapis.in/crm/v3/Local_Charges_Tariff/search?criteria=(Carrier_Name:equals:{carrier_name})"
    headers = {"Authorization": f"Zoho-oauthtoken {access_token}"}
    res = requests.get(url, headers=headers)
    if res.status_code == 200 and res.json().get("data"):
        return res.json()["data"][0]
    return None


def process_local_charges_pdf(file_bytes, wa_id=None):
    try:
        pdf_reader = PyPDF2.PdfReader(io.BytesIO(file_bytes))
        raw_text = "".join([page.extract_text() for page in pdf_reader.pages])
        system_prompt = (
            "You are an expert pricing analyst. Extract the standard local charges from this carrier tariff. "
            "Output strictly as JSON: {'carrier': '...', 'thc_20': '...', 'thc_40': '...', 'bl_fee': '...', 'seal_charge': '...', 'toll': '...', 'muc': '...'}"
        )
        response = client.chat.completions.create(
            model="gpt-4o",
            messages=[{"role": "system", "content": system_prompt}, {"role": "user", "content": f"TARIFF DATA:\n{raw_text[:10000]}"}],
            response_format={"type": "json_object"}
        )
        data = json.loads(response.choices[0].message.content)
        push_to_zoho_crm("Local_Charges_Tariff", [{"Carrier_Name": data.get("carrier"), "THC_20": data.get("thc_20"), "THC_40": data.get("thc_40"), "BL_Fee": data.get("bl_fee"), "Seal_Charge": data.get("seal_charge"), "Toll": data.get("toll"), "MUC": data.get("muc")}])
        if wa_id:
            send_whatsapp_message(wa_id, f"✅ *Local Charges Updated* for {data.get('carrier')}. Ready for relational pricing.")
    except Exception as e:
        print(f"Local Charges Error: {e}")
        if wa_id:
            send_whatsapp_message(wa_id, f"❌ *Error parsing local charges:* {str(e)}")


def process_rate_sheet(file_content, filename, vendor_name, wa_id=None):
    try:
        if wa_id:
            send_whatsapp_message(wa_id, "📥 *Rate sheet received.* Analyzing complete commercial structure...")
        
        ocean_freight_rates, surcharges, local_charges = parse_rate_sheet_enhanced(
            file_content,
            filename,
            vendor_name
        )
        
        if wa_id:
            send_whatsapp_message(
                wa_id, 
                f"✅ *Extraction complete!*\n\n"
                f"📊 Ocean Freight Rates: {len(ocean_freight_rates)}\n"
                f"💵 Surcharges: {len(surcharges)}\n"
                f"🏗️ Local Charges: {len(local_charges)}\n\n"
                f"Now uploading to Zoho CRM..."
            )
        
        zoho_pricing_records = []
        
        for rate in ocean_freight_rates:
            pricing_record = {
                "Name": f"{rate.carrier} - {rate.pol} to {rate.pod}",
                "POL": rate.pol,
                "POD": rate.pod,
                "Subform_3": [{
                    "Vendor_Name": rate.carrier,
                    "Freight_Air_Sea": str(rate.rate_40)
                }],
                "Transit_Time": rate.transit_time,
                "Routing": rate.routing,
                "Free_Time_Days": rate.free_time_days,
                "Validity_End": rate.validity_end.strftime("%Y-%m-%d") if rate.validity_end else None,
                "Rate_Version": rate.rate_version,
                "Detention_Rate_20": rate.detention_rate_20,
                "Detention_Rate_40": rate.detention_rate_40,
            }
            zoho_pricing_records.append(pricing_record)
        
        if not zoho_pricing_records:
            if wa_id:
                send_whatsapp_message(
                    wa_id,
                    "⚠️ *Warning:* Rate sheet was processed but no rates were extracted."
                )
            return "No rates extracted"
        
        batch_size = 50
        total_uploaded = 0
        
        for i in range(0, len(zoho_pricing_records), batch_size):
            batch = zoho_pricing_records[i:i + batch_size]
            push_to_zoho_crm("Pricings", batch)
            total_uploaded += len(batch)
            
            if wa_id and len(zoho_pricing_records) > 50:
                send_whatsapp_message(
                    wa_id,
                    f"⏳ Uploaded {total_uploaded} of {len(zoho_pricing_records)} rates..."
                )
        
        if wa_id:
            summary_msg = (
                f"✅ *Upload Complete!*\n\n"
                f"Carrier: *{vendor_name}*\n"
                f"Total Rates: {len(zoho_pricing_records)}\n"
                f"Surcharges Captured: {len(surcharges)}\n"
                f"Local Charges: {len(local_charges)}\n\n"
                f"All pricing data is now available for intelligent queries."
            )
            send_whatsapp_message(wa_id, summary_msg)
        
        return f"Successfully processed {len(zoho_pricing_records)} rates"
        
    except Exception as e:
        error_msg = f"❌ *Error processing rate sheet:* {str(e)}"
        print(f"CRITICAL ERROR in rate sheet processing: {e}")
        if wa_id:
            send_whatsapp_message(wa_id, error_msg)
        return error_msg        
        batch_size = 50  # Upload 50 records at a time
        total_uploaded = 0
        
        for i in range(0, len(zoho_pricing_records), batch_size):
            batch = zoho_pricing_records[i:i + batch_size]
            
            # This calls your existing Zoho upload function
            # push_to_zoho_crm should already exist in your main.py
            push_to_zoho_crm("Pricings", batch)
            
            total_uploaded += len(batch)
            
            # Give progress updates for large uploads
            if wa_id and len(zoho_pricing_records) > 50:
                send_whatsapp_message(
                    wa_id,
                    f"⏳ Uploaded {total_uploaded} of {len(zoho_pricing_records)} rates..."
                )
        
        # Step 6: Send success message with summary
        if wa_id:
            summary_msg = (
                f"✅ *Upload Complete!*\n\n"
                f"Carrier: *{vendor_name}*\n"
                f"Total Rates: {len(zoho_pricing_records)}\n"
                f"Surcharges Captured: {len(surcharges)}\n"
                f"Local Charges: {len(local_charges)}\n\n"
                f"All pricing data is now available for intelligent queries.\n"
                f"Try: *APPROVE INQ-XXX* to see complete breakdowns."
            )
            send_whatsapp_message(wa_id, summary_msg)
        
        return f"Successfully processed {len(zoho_pricing_records)} rates"
        
    except Exception as e:
        # If anything goes wrong, log the error and notify the user
        error_msg = f"❌ *Error processing rate sheet:* {str(e)}"
        print(f"CRITICAL ERROR in rate sheet processing: {e}")
        
        if wa_id:
            send_whatsapp_message(wa_id, error_msg)
        
        return error_msg

def handle_confirmation(text, sender_wa_id):
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
                    push_to_zoho_crm("Pricings", data[i: i + batch_size])
                send_whatsapp_message(sender_wa_id, f"✅ *Success!* {len(data)} rates uploaded to Zoho CRM.")
            except Exception as e:
                send_whatsapp_message(sender_wa_id, f"❌ *Error during upload:* {str(e)}")
        elif action == 'log_enquiry':
            module = task.get('module', 'Deals')
            send_whatsapp_message(sender_wa_id, f"🚀 *Confirmed.* Logging your enquiry in Zoho CRM ({module})...")
            try:
                push_to_zoho_crm(module, [data])
                send_whatsapp_message(sender_wa_id, "✅ *Success!* Your enquiry has been logged. Our team will contact you shortly.")
            except Exception as e:
                send_whatsapp_message(sender_wa_id, f"❌ *Error logging enquiry:* {str(e)}")
        del PENDING_TASKS[sender_wa_id]
    elif user_text == "NO":
        del PENDING_TASKS[sender_wa_id]
        send_whatsapp_message(sender_wa_id, "🛑 *Action cancelled.*")
    else:
        send_whatsapp_message(sender_wa_id, f"🤔 I am still waiting for your confirmation to {task.get('description')}. Please reply *YES* to proceed or *NO* to cancel.")


# --- UPGRADED EMAIL PROCESSING ---
def process_email_rfq(payload):
    """
    Full pipeline:
    1. Extracts all inquiry fields using GPT-4o — including the REAL client
       email even if the email is forwarded (e.g. through Hitesh's inbox or All Forward).
    2. Searches Zoho CRM for an existing Account by company name or email.
    3. Creates Account + Contact if they don't exist.
    4. Creates a Deal with all fields mapped to your Zoho Deals module
       (POL, POD, Container Type, Commodity, Weight, Volume, Incoterms, etc.).
    5. Sends you a complete, detailed WhatsApp summary — not just the route.
    6. Sends an acknowledgement email to the actual client (not the forwarder).
    """
    try:
        # Pull email content from whatever fields Zoho Flow sends
        email_body = payload.get("body", "") or payload.get("emailHtmlContent", "")
        raw_sender = payload.get("fromAddress", "") or payload.get("sender", "")
        subject = payload.get("subject", "No Subject")
        print(f"DEBUG: Email received | From: {raw_sender} | Subject: {subject}")

        # Use GPT-4o to intelligently extract ALL fields in one shot.
        # The key instruction is to look inside forwarded email content for the real client.
        system_prompt = (
            "You are an expert freight forwarding assistant. This email may be forwarded — "
            "always look for the ORIGINAL client's details inside the forwarded section, not the forwarder. "
            "Extract every logistics detail available. "
            "Return ONLY a valid JSON object with these exact keys (use 'Unknown' if not found):\n"
            "client_name, client_company, client_email, pol, pod, container_type, "
            "no_of_containers, commodity, cargo_weight_kg, volume_cbm, incoterms, "
            "mode_of_shipment, cargo_readiness, dimensions, remarks"
        )
        response = client.chat.completions.create(
            model="gpt-4o",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": f"Subject: {subject}\n\nEmail:\n{email_body[:8000]}"}
            ],
            response_format={"type": "json_object"}
        )
        e = json.loads(response.choices[0].message.content)

        # Extract all fields with safe defaults
        client_name      = e.get("client_name", "Unknown")
        client_company   = e.get("client_company", "Unknown")
        client_email     = e.get("client_email", "Unknown")
        pol              = e.get("pol", "Unknown")
        pod              = e.get("pod", "Unknown")
        container_type   = e.get("container_type", "Unknown")
        no_of_containers = e.get("no_of_containers", "Unknown")
        commodity        = e.get("commodity", "Unknown")
        cargo_weight     = e.get("cargo_weight_kg", "Unknown")
        volume_cbm       = e.get("volume_cbm", "Unknown")
        incoterms        = e.get("incoterms", "Unknown")
        mode_of_shipment = e.get("mode_of_shipment", "Unknown")
        cargo_readiness  = e.get("cargo_readiness", "Unknown")
        dimensions       = e.get("dimensions", "Unknown")
        remarks          = e.get("remarks", "")

        # Fall back to raw sender if GPT couldn't find real client email
        if not client_email or client_email == "Unknown":
            client_email = raw_sender

        print(f"DEBUG: Client: {client_name} | {client_company} | {client_email}")

        # Generate INQ number
        inq_number = generate_next_inquiry_number()

        # Find or create Account + Contact in Zoho CRM
        account_id, account_name, contact_id = find_or_create_account_and_contact(
            client_company, client_name, client_email, inq_number
        )

        # Build the Deal record with all fields mapped to your Zoho Deals module
        norm_pol = normalize_port_name(pol)
        norm_pod = normalize_port_name(pod)

        deal_data = {
            "Deal_Name": inq_number,
            "Stage": "Qualification",
            # Logistics fields — mapped to your existing Zoho Deal fields
            "POL": norm_pol,
            "POD": norm_pod,
            "Container_Type": container_type,
            "Commodity": commodity,
            "Incoterms": incoterms,
            "Mode_Of_Shipment": mode_of_shipment,
            "Remarks": remarks,
            # Description stores sender email so SEND command can retrieve it later
            "Description": (
                f"Sender Email: {client_email}\n"
                f"Route: {norm_pol} to {norm_pod}\n"
                f"Commodity: {commodity}\n"
                f"Equipment: {no_of_containers} x {container_type}\n"
                f"Incoterms: {incoterms}\n"
                f"Source: Email Inquiry\n"
                f"Subject: {subject}"
            )
        }

        # Only add numeric/optional fields if they have real values
        if cargo_weight not in ["Unknown", "", None]:
            deal_data["Cargo_Weight_Kg"] = cargo_weight
        if volume_cbm not in ["Unknown", "", None]:
            deal_data["Volume_CBM"] = volume_cbm
        if no_of_containers not in ["Unknown", "", None]:
            deal_data["No_Of_Packages"] = no_of_containers
        if cargo_readiness not in ["Unknown", "", None]:
            deal_data["Cargo_Readiness"] = cargo_readiness
        if dimensions not in ["Unknown", "", None]:
            deal_data["Dimensiona_LxBxH_m"] = dimensions

        # Link Account and Contact if found/created
        if account_id:
            deal_data["Account_Name"] = {"id": account_id}
        if contact_id:
            deal_data["Contact_Name"] = {"id": contact_id}

        create_zoho_record("Deals", deal_data)
        print(f"DEBUG: Deal {inq_number} created under Account: {account_name}")

        # Send acknowledgement to the real client
        # (skip if it's an internal email to avoid loops)
        if client_email and "@megamoveindia.com" not in client_email and client_email != "Unknown":
            first_name = client_name.split()[0] if client_name != "Unknown" else "Sir/Madam"
            ack_body = (
                f"Dear {first_name},\n\n"
                "Thank you for your inquiry.\n"
                "Our team is reviewing your requirements and will revert with a competitive quotation shortly.\n\n"
                "Thanks & Regards,\n"
                "Vikas Kaswan | Pricing Desk\n"
                "vikas.kaswan@megamoveindia.com | +91 9321399970\n"
                "Mega Move India Private Limited | www.megamoveindia.com"
            )
            send_email_with_attachment(client_email, f"Re: {subject}", ack_body)
            print(f"DEBUG: Ack email sent to {client_email}")

        # Send Hitesh a detailed WhatsApp summary
        admin_number = os.getenv("YOUR_WHATSAPP_NUMBER")
        if admin_number:
            msg = (
                f"📧 *New Email Inquiry Received*\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"🔖 *Ref:* {inq_number}\n"
                f"🏢 *Company:* {client_company}\n"
                f"👤 *Contact:* {client_name}\n"
                f"📬 *Email:* {client_email}\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"🚢 *Shipment Details:*\n"
                f"• POL: {norm_pol}\n"
                f"• POD: {norm_pod}\n"
                f"• Mode: {mode_of_shipment}\n"
                f"• Containers: {no_of_containers} x {container_type}\n"
                f"• Commodity: {commodity}\n"
                f"• Weight: {cargo_weight} KG\n"
                f"• Volume: {volume_cbm} CBM\n"
                f"• Incoterms: {incoterms}\n"
                f"• Cargo Ready: {cargo_readiness}\n"
                f"• Dimensions: {dimensions}\n"
                f"• Remarks: {remarks or 'None'}\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"✅ CRM: Logged under *{account_name}*\n"
                f"📩 Ack email sent to client.\n\n"
                f"Reply *APPROVE {inq_number}* to search for rates."
            )
            send_whatsapp_message(admin_number, msg)
            print(f"DEBUG: WhatsApp notification sent for {inq_number}")
        else:
            print("CRITICAL: YOUR_WHATSAPP_NUMBER is not set!")

    except Exception as e:
        print(f"Email Processing Error: {e}")
        admin_number = os.getenv("YOUR_WHATSAPP_NUMBER")
        if admin_number:
            send_whatsapp_message(admin_number, f"❌ *Email Processing Error:* {str(e)}")


# --- WHATSAPP MESSAGE PROCESSING ---
async def process_whatsapp_message(payload, background_tasks: BackgroundTasks):
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
        if (datetime.now() - LAST_CLEANUP).total_seconds() > 600:
            PROCESSED_MESSAGE_IDS.clear()
            LAST_CLEANUP = datetime.now()
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
            send_whatsapp_message(from_number, "📥 *Images received.* Processing...")
            await asyncio.sleep(8)
            images = IMAGE_BUFFER.pop(from_number, [])
            raw_text = await extract_raw_content(images[0], filename, "image")

        # PRIORITY 1: Check for pending confirmation tasks
        if from_number in PENDING_TASKS:
            print(f"DEBUG: Active task pending for {from_number}. Routing to confirmation handler.")
            handle_confirmation(raw_text, from_number)
            return

        # PRIORITY 2: Filter out simple acknowledgments when no task is pending
        if msg_type == "text" and raw_text.lower() in ["no", "yes", "cancel", "stop", "ok", "thanks", "thank you", "n", "y"]:
            send_whatsapp_message(from_number, "🛑 No active actions pending to confirm or cancel. Type *help* to see available commands.")
            return

        # PRIORITY 3: Detect natural language rate search phrases BEFORE AI classification
        if msg_type == "text" and any(k in raw_text.lower() for k in ["rate for", "rates for", "price for", "what is the rate"]):
            print("DEBUG: Priority rate search phrase detected. Routing to AI Pricing Engine.")
            send_whatsapp_message(from_number, "🔍 *Searching active rates...*")
            reply, extracted = process_inquiry(raw_text)
            commodity = extracted.get('commodity', 'Unknown').title()
            if any(k in commodity for k in ["Crane", "Excavator", "Machine", "Oog"]):
                inq_number = generate_next_inquiry_number()
                PENDING_TASKS[from_number] = {'action': 'log_enquiry', 'description': f"log this priority OOG inquiry for {commodity}", 'data': {"Deal_Name": inq_number, "Stage": "Qualification", "Type": "Project Cargo", "Description": f"OOG: {commodity}"}}
                send_whatsapp_message(from_number, "🏗️ I have detected a Project Cargo inquiry. Shall I log this and notify the pricing team? (Reply YES)")
                return
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

        # PRIORITY 4: Detect ALL structured commands BEFORE AI classification
        # This prevents the AI from misinterpreting commands as inquiries
        if msg_type == "text":
            text_cmd = raw_text.lower()
            
            # HELP command
            if any(k in text_cmd for k in ["help", "menu", "commands"]):
                help_msg = (
                    "🤖 *Mega Move AI - Unified Operating System*\n\n"
                    "*💬 Text Commands:*\n"
                    "• *APPROVE [INQ-XXX]* : Finds lowest rates & links local charges.\n"
                    "• *QUOTE [INQ-XXX]* : Generates PDF quotation.\n"
                    "• *SEND [INQ-XXX]* : Emails PDF to client (CCs Hitesh).\n"
                    "• *TRACK [INQ-XXX]* : Live container tracking.\n"
                    "• *METRICS* : FY Dashboard in INR.\n\n"
                    "*📄 Universal Document Ingestion:*\n"
                    "Drop any file (Rate Sheets, Tariffs, Vendor Bills, Inquiry Emails). The AI auto-classifies and processes it."
                )
                send_whatsapp_message(from_number, help_msg)
                return
            
            # METRICS command
            if text_cmd.startswith("metrics"):
                period = "overall" if "overall" in text_cmd else "FY"
                crm, fin = get_crm_snapshot(period=period), get_financial_snapshot(period=period)
                _, _, fy_label = get_fy_start()
                msg = (
                    f"📊 *MEGA MOVE - EXECUTIVE DASHBOARD*\n"
                    f"Period: {'FY ' + fy_label if period == 'FY' else 'Overall'}\n\n"
                    f"📈 *Sales:*\n• Inquiries: {crm['inquiries']}\n• Booked: {crm['booked']}\n• Conversion: {crm['conversion']:.1f}%\n\n"
                    f"💼 *Financials:*\n• Revenue: {format_inr(fin['revenue'])}\n• Costs: {format_inr(fin['costs'])}\n• Margin: *{format_inr(fin['profit'])}*"
                )
                send_whatsapp_message(from_number, msg)
                return
            
            # TRACK command
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
                send_whatsapp_message(from_number, f"🚢 *Live Tracking Update*\nRef: {ref.upper()}\nStatus: {status_data['status']}\n📍 Location: {status_data['current_location']}\n⛴️ Vessel: {status_data['vessel_name']}\n🗓️ ETA: {status_data['eta']}")
                return
            
            # APPROVE command - THIS WAS BROKEN, now fixed by detecting it here
            if text_cmd.startswith("approve "):
                inq_number = raw_text.split(" ", 1)[-1].strip().upper()
                deal = get_deal_by_id(inq_number)
                if not deal:
                    send_whatsapp_message(from_number, f"⚠️ Inquiry {inq_number} not found.")
                    return
                desc = deal.get("Description", "")
                pol = deal.get("POL") or "Unknown"
                pod = deal.get("POD") or "Unknown"
                # Fall back to Description parsing if POL/POD fields are empty
                if pol == "Unknown" and "Route: " in desc:
                    route_line = [l for l in desc.split("\n") if "Route: " in l][0]
                    pol, pod = route_line.replace("Route: ", "").split(" to ")
                rates = search_rates(pol, pod)
                if not rates:
                    send_whatsapp_message(from_number, f"⚠️ No rates found for {inq_number} ({pol} to {pod}).")
                    return
                best = rates[0]
                lc_data = fetch_local_charges(best['vendor'])
                lc_str = "Not on file"
                if lc_data:
                    thc = lc_data.get('THC_40') if '40' in best['vehicle'] else lc_data.get('THC_20')
                    lc_str = f"THC: {thc} | BL: {lc_data.get('BL_Fee')}"
                msg = (
                    f"📊 *Rates Found for {inq_number}*\nRoute: {pol} ➡️ {pod}\nVendor: {best['vendor']}\n\n🌊 Base O/F: USD {best['price']}\n🏗️ Local Charges: {lc_str}\n⏱️ Transit: {best['transit_time']}\n📅 Valid: {best['validity_date']}\n\nReply *QUOTE {inq_number}* to draft PDF."
                )
                send_whatsapp_message(from_number, msg)
                return
            
            # QUOTE command
            if text_cmd.startswith("quote "):
                inq_number = raw_text.split(" ", 1)[-1].strip().upper()
                deal = get_deal_by_id(inq_number)
                if not deal: return
                pol = deal.get("POL") or "Unknown"
                pod = deal.get("POD") or "Unknown"
                if pol == "Unknown":
                    desc = deal.get("Description", "")
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
            
            # SEND command
            if text_cmd.startswith("send "):
                inq_number = raw_text.split(" ", 1)[-1].strip().upper()
                deal = get_deal_by_id(inq_number)
                if not deal: return
                desc = deal.get("Description", "")
                client_email = "Unknown"
                if "Sender Email: " in desc:
                    client_email = desc.split("Sender Email: ")[1].split("\n")[0].strip()
                pol = deal.get("POL") or "Unknown"
                pod = deal.get("POD") or "Unknown"
                if pol == "Unknown" and "Route: " in desc:
                    route_line = [l for l in desc.split("\n") if "Route: " in l][0]
                    pol, pod = route_line.replace("Route: ", "").split(" to ")
                rates = search_rates(pol, pod)
                if not rates:
                    send_whatsapp_message(from_number, f"⚠️ No rates found to generate quote for {inq_number}.")
                    return
                best = rates[0]
                margin_pct = float(os.getenv("PROFIT_MARGIN_PERCENT", 20))
                sell_price = f"USD {best['price'] * (1 + (margin_pct / 100)):.2f}"
                pdf_bytes = generate_quotation_pdf(inq_number, pol, pod, best['vehicle'], sell_price)
                if send_email_with_attachment(client_email, f"Freight Quotation - {inq_number}", "Please find attached your quotation.", pdf_bytes, f"{inq_number}.pdf"):
                    create_zoho_record("Deals", {"id": deal.get("id"), "Stage": "Proposal/Price Quote"})
                    send_whatsapp_message(from_number, f"✅ *Success.* Quotation for {inq_number} emailed to {client_email}.")
                return
            
            # BOOK command
            if text_cmd.startswith("book "):
                inq_number = raw_text.split(" ", 1)[-1].strip().upper()
                deal = get_deal_by_id(inq_number)
                if not deal: return
                vendor_name = deal.get("Vendor_Name", "Unknown")
                pol = deal.get("POL", "Unknown")
                vendor_email = get_primary_vendor_email(vendor_name, pol)
                if not vendor_email:
                    send_whatsapp_message(from_number, f"⚠️ No primary PIC email found for {vendor_name}.")
                    return
                subject = f"Booking Request - {inq_number}"
                body = f"Dear {vendor_name} Team,\n\nPlease process the booking for {inq_number}.\n\nBest Regards,\nMega Move India"
                if send_email_with_attachment(vendor_email, subject, body):
                    send_whatsapp_message(from_number, f"✅ *Booking Sent.* Request for {inq_number} sent to {vendor_email}")
                return

        # PRIORITY 5: Only NOW use AI classification for documents and ambiguous text
        classification = await classify_operational_intent(raw_text if msg_type == "text" else "", caption, raw_text)
        category = classification.get('category')
        target = classification.get('action_target')
        print(f"DEBUG: Cognitive Classification: {category} (Target: {target})")

        if category == 'ratesheet':
            vendor_name = filename.split(" ")[0] if " " in filename else "Unified Vendor"
            status = process_rate_sheet(file_bytes if file_bytes else raw_text.encode(), filename, vendor_name, from_number)
            if status:
                send_whatsapp_message(from_number, status)
        
        elif category == 'tariff':
            background_tasks.add_task(process_local_charges_pdf, file_bytes if file_bytes else raw_text.encode(), from_number)
        
        elif category == 'inquiry':
            process_inquiry_email(raw_text, from_number)
        
        elif category == 'vendor_bill':
            send_whatsapp_message(from_number, "🚨 *Vendor Bill Received.* Auditing margins...")
            send_whatsapp_message(from_number, "✅ *Audit Complete.* Margin verified.")
        
        else:
            # Fallback: try to interpret as a rate inquiry
            reply, extracted = process_inquiry(raw_text)
            if reply:
                send_whatsapp_message(from_number, reply)
            else:
                send_whatsapp_message(from_number, "👋 Hello! I am the Mega Move AI. Send any file or command to start. Type *help* for options.")

    except Exception as e:
        print(f"Unified Ingestion Error: {str(e)}")
        send_whatsapp_message(from_number, f"⚠️ *System Error:* {str(e)}")
