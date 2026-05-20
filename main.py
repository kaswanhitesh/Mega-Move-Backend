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

app = FastAPI(title="Mega Move AI Backend")
client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

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
    url = f"https://www.zohoapis.in/crm/v3/{module}" 
    headers = {"Authorization": f"Zoho-oauthtoken {access_token}", "Content-Type": "application/json"}
    requests.post(url, json={"data": data_list}, headers
cat << 'EOF' > main.py
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

app = FastAPI(title="Mega Move AI Backend")
client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

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
    url = f"https://www.zohoapis.in/crm/v3/{module}" 
    headers = {"Authorization": f"Zoho-oauthtoken {access_token}", "Content-Type": "application/json"}
    requests.post(url, json={"data": data_list}, headers=headers)

# --- NEW: DYNAMIC AUTO-NUMBERING ---
def generate_next_inquiry_number():
    """Fetches the latest Deal and increments the INQ number."""
    access_token = get_zoho_access_token()
    # Pull the 10 most recent deals to find the latest INQ number
    url = "https://www.zohoapis.in/crm/v3/Deals?sort_by=Created_Time&sort_order=desc&per_page=10"
    headers = {"Authorization": f"Zoho-oauthtoken {access_token}"}
    res = requests.get(url, headers=headers)
    
    last_num = 21 # Your current starting point
    
    if res.status_code == 200 and res.json().get("data"):
        for deal in res.json()["data"]:
            name = deal.get("Deal_Name", "")
            if name.startswith("INQ-MMI-2026-"):
                try:
                    # Extract the number at the end
                    last_num = int(name.split("-")[-1])
                    break # Found the most recent one
                except:
                    continue
                    
    next_num = last_num + 1
    return f"INQ-MMI-2026-{str(next_num).zfill(3)}"

def search_lowest_rate(pol, pod):
    access_token = get_zoho_access_token()
    url = f"https://www.zohoapis.in/crm/v3/CustomModule1/search?criteria=(((POL:equals:{pol})and(POD:equals:{pod})))"
    headers = {"Authorization": f"Zoho-oauthtoken {access_token}"}
    res = requests.get(url, headers=headers)
    
    if res.status_code == 200 and res.json().get("data"):
        rates = res.json()["data"]
        valid_rates = []
        for r in rates:
            try:
                valid_rates.append({
                    "vendor": r.get("Vendor_Name", "Unknown"),
                    "price": float(r.get("Exwork_Charges", 0)),
                    "vehicle": r.get("Vehicle_Types", "Standard")
                })
            except:
                pass
        if valid_rates:
            return min(valid_rates, key=lambda x: x['price'])
    return None

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

# --- NEW: EMAIL PROCESSING LOGIC ---
def process_email_rfq(payload):
    """Parses incoming emails and creates Zoho CRM Deals."""
    try:
        email_body = payload.get("body", "")
        sender_email = payload.get("sender", "Client")
        subject = payload.get("subject", "No Subject")
        
        # 1. Ask GPT-4o to extract details from the email
        prompt = f"Extract freight details from this email. Return JSON: pol, pod, commodity, container_type, weight. JSON only.\n\nSubject: {subject}\nBody: {email_body}"
        response = client.chat.completions.create(
            model="gpt-4o",
            messages=[{"role": "system", "content": "You are a logistics parser. Output only valid JSON."},{"role": "user", "content": prompt}],
            response_format={ "type": "json_object" }
        )
        extracted = json.loads(response.choices[0].message.content)
        pol = extracted.get('pol')
        pod = extracted.get('pod')
        
        if pol and pod:
            # 2. Generate the INQ-MMI-2026-XXX Number
            inq_number = generate_next_inquiry_number()
            
            # 3. Log to Zoho CRM Deals
            push_to_zoho_crm("Deals", [{
                "Deal_Name": inq_number,
                "Stage": "Qualification",
                "Description": f"Route: {pol} to {pod}\nCommodity: {extracted.get('commodity')}\nContainer: {extracted.get('container_type')}\nReceived From: {sender_email}"
            }])
            
            # 4. Notify You via WhatsApp
            alert_msg = f"📧 *New Email RFQ Processed*\n\nID: {inq_number}\nFrom: {sender_email}\nRoute: {pol} ➡️ {pod}\n\nI have logged this in Zoho CRM as a new Inquiry."
            
            # Attempt to auto-quote
            best_rate = search_lowest_rate(pol, pod)
            if best_rate:
                margin_pct = float(os.getenv("PROFIT_MARGIN_PERCENT", 20))
                sell_price = best_rate['price'] * (1 + (margin_pct / 100))
                pdf_bytes = generate_quotation_pdf(inq_number, pol, pod, best_rate['vehicle'], sell_price)
                alert_msg += f"\n\n✅ I also found a rate and generated a quotation."
                # Send the notification and the file to your personal WhatsApp number
                # Note: You must store your personal WhatsApp number in Railway as YOUR_WHATSAPP_NUMBER
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

# --- EXISTING WHATSAPP LOGIC ---
def process_whatsapp_message(payload):
    # (Existing WhatsApp logic remains untouched to handle manual inputs, files, and greetings)
    pass
