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

app = FastAPI(title="Mega Move AI Backend")
client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

# --- GLOBAL STATE ---
PENDING_TASKS = {}
PROCESSED_MESSAGE_IDS = set()
LAST_CLEANUP = datetime.now()

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

def search_lowest_rate(pol, pod):
    normalized_pol = normalize_port_name(pol)
    normalized_pod = normalize_port_name(pod)
    
    print(f"DEBUG: Searching Zoho for POL={normalized_pol}, POD={normalized_pod}")
    
    access_token = get_zoho_access_token()
    # Using starts_with for more flexible matching (API allows minor naming variations)
    url = f"https://www.zohoapis.in/crm/v3/Pricings/search?criteria=(((POL:starts_with:{normalized_pol})and(POD:starts_with:{normalized_pod})))"
    headers = {"Authorization": f"Zoho-oauthtoken {access_token}"}
    res = requests.get(url, headers=headers)
    
    if res.status_code == 200 and res.json().get("data"):
        rates = res.json()["data"]
        valid_rates = []
        today = datetime.now().date()
        
        for r in rates:
            print(f"DEBUG: Retrieved record: {r}")
            sub3 = r.get("Subform_3", [])
            print(f"DEBUG: Subform_3 contents: {sub3}")
            
            try:
                # 1. EXPIRED RATE FILTERING
                validity_str = r.get("Validity_Date")
                if validity_str:
                    try:
                        validity_date = datetime.strptime(validity_str, "%Y-%m-%d").date()
                        if validity_date < today:
                            continue # Skip expired rates
                    except:
                        pass 
                
                # 2. DATA EXTRACTION (Iterating Subform_3 for Freight_Air_Sea)
                if not sub3:
                    print("DEBUG: Subform_3 is empty, skipping record.")
                    continue
                
                for entry in sub3:
                    price_val = entry.get("Freight_Air_Sea")
                    vendor_name = entry.get("Vendor_Name") or "N/A"
                    
                    # Skip if price is missing or zero
                    if not price_val or str(price_val).strip() == "" or float(price_val) == 0:
                        print(f"DEBUG: Skipping subform entry due to invalid price: {price_val}")
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
                print(f"DEBUG: Error processing individual record: {str(inner_e)}")
                pass
                
        if valid_rates:
            return min(valid_rates, key=lambda x: x['price'])
    return None

def process_inquiry(text):
    """Analyzes text to find a rate. Returns (reply_text, extracted_details)."""
    prompt = f"Extract freight details from this WhatsApp message. Return JSON: pol, pod, commodity. JSON only.\n\nMessage: {text}"
    response = client.chat.completions.create(
        model="gpt-4o",
        messages=[{"role": "system", "content": "You are a specialized logistics parser. Output ONLY valid JSON."},
                  {"role": "user", "content": prompt}],
        response_format={ "type": "json_object" }
    )
    extracted = json.loads(response.choices[0].message.content)
    pol, pod = extracted.get('pol'), extracted.get('pod')
    
    if pol and pod:
        best_rate = search_lowest_rate(pol, pod)
        if best_rate:
            margin_pct = float(os.getenv("PROFIT_MARGIN_PERCENT", 20))
            sell_price = best_rate['price'] * (1 + (margin_pct / 100))
            inq_number = generate_next_inquiry_number()
            
            # Professional Structured Quotation
            reply = (
                f"🚢 *Quotation: {best_rate['pol']} ➡️ {best_rate['pod']}*\n\n"
                f"📦 *Equipment:* {best_rate['vehicle']}\n"
                f"🏢 *Vendor:* {best_rate['vendor']}\n"
                f"💰 *Ocean Freight:* USD {sell_price:.2f}\n"
                f"⏱️ *Transit Time:* {best_rate['transit_time']}\n"
                f"🗺️ *Routing:* {best_rate['route']}\n"
                f"⏳ *Valid until:* {best_rate['validity_date']}\n\n"
                f"Ref: {inq_number}"
            )
            return reply, extracted
            
    return None, extracted
            
    return None, extracted

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
            send_whatsapp_message(wa_id, "📥 *Received your rate sheet.* Reading the document...")

        # Extract text/data from file
        if filename.endswith(".xlsx") or filename.endswith(".xls"):
            df = pd.read_excel(io.BytesIO(file_content), sheet_name=None)
            raw_data = ""
            for sheet, data in df.items():
                raw_data += f"--- Tab: {sheet} ---\n{data.to_string()}\n"
        else:
            pdf_reader = PyPDF2.PdfReader(io.BytesIO(file_content))
            raw_data = "".join([page.extract_text() for page in pdf_reader.pages])

        # UNIVERSAL PARSING PROMPT - CALIBRATED FOR COMPLEX LOGISTICS EDGE CASES
        prompt = f"""
        ACT AS AN EXPERT LOGISTICS DATA ENGINEER. 
        Your task is to convert messy vendor rate sheets into structured JSON.
        
        VENDOR NAME: PIL (INDIA) PVT. LTD
        FILE NAME: {filename}

        ### CORE EXTRACTION RULES:
        1. **POL INFERENCE (CRITICAL):** 
           - Logistics rates are often grouped under a Port of Loading (POL). 
           - Look for POL in: Tab Names, Section Headers (e.g., "RATES FROM MUNDRA"), or the first column.
           - If a POL is found at the top of a table/section, apply it to EVERY row in that section until a new POL is explicitly mentioned.
        
        2. **PRICE SCRUBBING & ROW SPLITTING:** 
           - For every row in the rate table, you must create TWO separate rate entries:
             - Entry 1: Container Type = '20ft', Price = [Value from the first price column].
             - Entry 2: Container Type = '40ft', Price = [Value from the second price column].
           - Extract ONLY the numeric base Ocean Freight as 'ocean_freight'. 
           - Ignore surcharges, THC, documentation fees, or any text following symbols like "+", "&", "/", or "AND".
           - Example: "$1200 + THC" -> 1200.
           - Example: "Included" or "0" -> 0.0.

        3. **CONTAINER TYPE MAPPING:**
           - Standardize types strictly to '20ft' or '40ft' based on the column headers.

        4. **COMMODITY & HAZ:**
           - If the row/column indicates "HAZ", "Hazardous", or "DG", append "(HAZ)" to the container_type.

        5. **TRANSIT TIME & ROUTE (COLUMN ALIASES):**
           - 'VIA' -> Use this for the 'route' column (e.g., "Direct", "via Singapore").
           - 'Days' -> Use this for the 'transit_time' column (e.g., "15 Days").
           - 'Valid till' -> Use this for the 'validity_date' column.

        6. **VALIDITY SCAN:**
           - Return 'validity_date' in YYYY-MM-DD format. If not found, return null.

        ### OUTPUT FORMAT:
        Return ONLY a JSON object with a "rates" key:
        {{
          "rates": [
            {{
              "pol": "Origin Port",
              "pod": "Destination Port",
              "container_type": "20ft or 40ft",
              "ocean_freight": 0.0,
              "transit_time": "Estimated Days",
              "route": "Routing Details",
              "validity_date": "YYYY-MM-DD or null"
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
            messages=[{"role": "system", "content": "You are a specialized logistics data parser. Output ONLY valid JSON."},
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
            norm_pol = normalize_port_name(rate.get("pol"))
            norm_pod = normalize_port_name(rate.get("pod"))
            
            # Standardize container type and keys for mapping
            c_type = rate.get("container_type", "")
            r_key = f"{carrier_name}_{norm_pol}_{norm_pod}_{c_type}".upper().replace(" ", "_")
            
            zoho_data.append({
                "Name": f"{carrier_name} - {norm_pol} to {norm_pod} - {c_type}",
                "POL": norm_pol,
                "POD": norm_pod,
                "Container_Type": str(c_type),
                "Transit_Time": str(rate.get("transit_time", "")),
                "Route": str(rate.get("route", "")),
                "Rate_Key": str(r_key),
                "Validity_Date": rate.get("validity_date"),
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
            send_whatsapp_message(sender_wa_id, "🚀 *Confirmed.* Logging your enquiry in Zoho CRM...")
            try:
                push_to_zoho_crm("Deals", [data])
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
def process_whatsapp_message(payload):
    """Handles incoming WhatsApp messages/files and triggers AI processing."""
    print(f"Incoming Webhook Payload: {json.dumps(payload)}")
    
    global LAST_CLEANUP, PROCESSED_MESSAGE_IDS
    
    try:
        # 0. IDEMPOTENCY CHECK (Prevent double processing on retries)
        entries = payload.get("entry", [])
        if not entries: return
        changes = entries[0].get("changes", [])
        if not changes: return
        value = changes[0].get("value", {})
        messages = value.get("messages", [])
        if not messages: return

        message = messages[0]
        message_id = message.get("id")
        
        if message_id in PROCESSED_MESSAGE_IDS:
            print(f"Skipping already processed message: {message_id}")
            return
        
        PROCESSED_MESSAGE_IDS.add(message_id)

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

        # 2. HANDLE TEXT (Greetings or Inquiries)
        elif message.get("type") == "text":
            text = message.get("text", {}).get("body", "").lower()
            
            # CHECK FOR PENDING TASKS (Human-in-the-Loop)
            if from_number in PENDING_TASKS:
                handle_confirmation(text, from_number)
                return

            # 1. Quote First logic
            reply, extracted = process_inquiry(text)
            if reply:
                send_whatsapp_message(from_number, reply)
                return
                
            # 2. Fallback to Greetings or Enquiry Logging
            if text in ["hi", "hello", "hey"]:
                send_whatsapp_message(from_number, "👋 Hello! I am the Mega Move AI. Send me a rate sheet or an inquiry to get started.")
            else:
                pol, pod = extracted.get('pol'), extracted.get('pod')
                if pol and pod:
                    # PROMPT FOR CONFIRMATION instead of logging immediately
                    inq_number = generate_next_inquiry_number()
                    enquiry_data = {
                        "Deal_Name": inq_number,
                        "Stage": "Qualification",
                        "Description": f"Source: WhatsApp\nRoute: {pol} to {pod}\nCommodity: {extracted.get('commodity')}"
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
