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
    response = requests.post(url, json={"data": data_list}, headers=headers)
    
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

# --- RATE SHEET PROCESSING ---
def process_rate_sheet(file_content, filename, vendor_name):
    """Parses Excel/PDF rate sheets using a calibrated GPT-4o prompt."""
    try:
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
        
        VENDOR NAME: {vendor_name}
        FILE NAME: {filename}

        ### CORE EXTRACTION RULES:
        1. **POL INFERENCE (CRITICAL):** 
           - Logistics rates are often grouped under a Port of Loading (POL). 
           - Look for POL in: Tab Names, Section Headers (e.g., "RATES FROM MUNDRA"), or the first column.
           - If a POL is found at the top of a table/section, apply it to EVERY row in that section until a new POL is explicitly mentioned.
        
        2. **PRICE SCRUBBING:** 
           - Extract ONLY the base Ocean Freight. 
           - Ignore surcharges, THC, documentation fees, or any text following symbols like "+", "&", "/", or "AND".
           - Example: "$1200 + THC" -> 1200.
           - Example: "USD 500 & 200" -> 500.
           - Example: "Included" or "0" -> 0.0.
           - If a cell contains "20/40" rates (e.g., "500/800"), split them into two separate JSON objects.

        3. **CONTAINER TYPE MAPPING:**
           - Standardize types: "20ft Standard", "40ft Standard", "40ft High Cube".
           - Recognize variants: "20DV", "20GP", "40HC", "40HQ", "HC", "HQ".

        4. **COMMODITY & HAZ:**
           - If the row/column indicates "HAZ", "Hazardous", or "DG", append "(HAZ)" to the container_type.

        ### OUTPUT FORMAT:
        Return ONLY a JSON object with a "rates" key:
        {{
          "rates": [
            {{
              "pol": "Origin Port",
              "pod": "Destination Port",
              "container_type": "Standardized Type",
              "ocean_freight": 0.0,
              "validity": "YYYY-MM-DD or descriptive string"
            }}
          ]
        }}

        ### RAW DATA:
        {raw_data[:12000]}
        """
        
        response = client.chat.completions.create(
            model="gpt-4o",
            messages=[{"role": "system", "content": "You are a specialized logistics data parser. Output ONLY valid JSON."},
                      {"role": "user", "content": prompt}],
            response_format={ "type": "json_object" }
        )
        
        extracted_rates = json.loads(response.choices[0].message.content).get("rates", [])
        
        # Log to Zoho CRM (CustomModule1 for Rates)
        zoho_data = []
        for rate in extracted_rates:
            zoho_data.append({
                "Vendor_Name": vendor_name,
                "POL": rate.get("pol"),
                "POD": rate.get("pod"),
                "Vehicle_Types": rate.get("container_type"),
                "Exwork_Charges": rate.get("ocean_freight"),
                "Validity": rate.get("validity")
            })
        
        if zoho_data:
            try:
                push_to_zoho_crm("CustomModule1", zoho_data)
                return f"Successfully processed {len(zoho_data)} rates for {vendor_name}."
            except Exception as zoho_err:
                return f"⚠️ Zoho CRM Error: {str(zoho_err)}"
        return "No rates could be extracted."

    except Exception as e:
        return f"Error processing rate sheet: {e}"

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
                alert_msg += f"\n\n✅ I also found a rate and generated a quotation."
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
    try:
        # SAFETY CHECK FOR EMPTY PAYLOADS (Meta Delivery Receipts / Status Updates)
        entries = payload.get("entry", [])
        if not entries:
            return
            
        changes = entries[0].get("changes", [])
        if not changes:
            return
            
        value = changes[0].get("value", {})
        messages = value.get("messages", [])
        
        if not messages:
            return 

        message = messages[0]
        from_number = message.get("from")
        
        # 1. HANDLE FILES (Rate Sheets)
        if message.get("type") == "document":
            doc = message.get("document")
            media_id = doc.get("id")
            filename = doc.get("filename")
            
            access_token = os.getenv("WHATSAPP_ACCESS_TOKEN")
            media_url_res = requests.get(f"https://graph.facebook.com/v18.0/{media_id}", 
                                         headers={"Authorization": f"Bearer {access_token}"})
            media_url = media_url_res.json().get("url")
            file_bytes = requests.get(media_url, headers={"Authorization": f"Bearer {access_token}"}).content
            
            vendor_name = filename.split(" ")[0] if " " in filename else "WhatsApp Vendor"
            status = process_rate_sheet(file_bytes, filename, vendor_name)
            send_whatsapp_message(from_number, status)

        # 2. HANDLE TEXT (Greetings or Inquiries)
        elif message.get("type") == "text":
            text = message.get("text", {}).get("body", "").lower()
            
            if text in ["hi", "hello", "hey"]:
                send_whatsapp_message(from_number, "👋 Hello! I am the Mega Move AI. Send me a rate sheet or an inquiry to get started.")
            else:
                prompt = f"Extract freight details from this WhatsApp message. Return JSON: pol, pod, commodity. JSON only.\n\nMessage: {text}"
                response = client.chat.completions.create(
                    model="gpt-4o",
                    messages=[{"role": "system", "content": "You are a logistics parser. Output only valid JSON."},{"role": "user", "content": prompt}],
                    response_format={ "type": "json_object" }
                )
                extracted = json.loads(response.choices[0].message.content)
                pol, pod = extracted.get('pol'), extracted.get('pod')
                
                if pol and pod:
                    inq_number = generate_next_inquiry_number()
                    push_to_zoho_crm("Deals", [{
                        "Deal_Name": inq_number,
                        "Stage": "Qualification",
                        "Description": f"Source: WhatsApp\nRoute: {pol} to {pod}"
                    }])
                    best_rate = search_lowest_rate(pol, pod)
                    if best_rate:
                        margin_pct = float(os.getenv("PROFIT_MARGIN_PERCENT", 20))
                        sell_price = best_rate['price'] * (1 + (margin_pct / 100))
                        msg = f"✅ Rate Found for {pol} ➡️ {pod}\n\nPrice: USD {sell_price:.2f}\nEquipment: {best_rate['vehicle']}\nInquiry: {inq_number}"
                        send_whatsapp_message(from_number, msg)
                    else:
                        send_whatsapp_message(from_number, f"⚠️ I've logged your inquiry {inq_number}, but I couldn't find an instant rate for {pol} to {pod}.")

    except Exception as e:
        print(f"WhatsApp Processing Error: {e}")
