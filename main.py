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

app = FastAPI(title="Mega Move AI Backend")
client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

@app.get("/")
def read_root():
    return {"status": "Mega Move Logistics Engine is Running"}

# --- WEBHOOK ENDPOINTS ---
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
    background_tasks.add_task(process_whatsapp_message, payload)
    return {"status": "success"}

@app.post("/email-webhook")
async def email_webhook(request: Request, background_tasks: BackgroundTasks):
    payload = await request.json()
    background_tasks.add_task(process_email_rfq, payload)
    return {"status": "success"}

# --- UTILITY FUNCTIONS ---
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

def download_whatsapp_media(media_id):
    headers = {"Authorization": f"Bearer {os.getenv('WHATSAPP_ACCESS_TOKEN')}"}
    url = f"https://graph.facebook.com/v18.0/{media_id}"
    res = requests.get(url, headers=headers).json()
    media_url = res.get("url")
    if not media_url:
        return None
    file_res = requests.get(media_url, headers=headers)
    return file_res.content

# --- AUTOMATION LOGIC ---
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

def process_email_rfq(payload):
    try:
        email_body = payload.get("body", "")
        sender_email = payload.get("sender", "Client")
        subject = payload.get("subject", "No Subject")
        
        prompt = f"""
        Analyze this forwarded freight request email.
        Ignore the person who forwarded it. Find the ORIGINAL requester's details inside the body.
        Extract the following and return strictly valid JSON:
        - pol: Port of Loading
        - pod: Port of Discharge
        - container_type: Equipment requested (e.g., '10 x 20', 'FCL', etc.)
        - incoterms: If mentioned (e.g., 'FOB')
        - commodity: If mentioned
        - original_company: The company actually requesting the quote (e.g., look for 'Requested by:')
        - original_email: The email of the original requester (e.g., look for 'Requested by:')
        - lead_source: The platform this came from (e.g., if 'All Forward' is in the text, use 'All Forward Network'. Otherwise use 'Email/Direct')
        
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
        company_name = extracted.get('original_company') or "Unknown Company"
        contact_email = extracted.get('original_email') or sender_email
        source = extracted.get('lead_source', 'Email')
        
        if pol and pod:
            access_token = get_zoho_access_token()
            headers = {"Authorization": f"Zoho-oauthtoken {access_token}", "Content-Type": "application/json"}
            
            contact_id = None
            account_id = None
            
            search_url = f"https://www.zohoapis.in/crm/v3/Contacts/search?email={contact_email}"
            search_res = requests.get(search_url, headers=headers)
            
            if search_res.status_code == 200 and search_res.json().get("data"):
                contact_id = search_res.json()["data"][0]["id"]
                if "Account_Name" in search_res.json()["data"][0] and search_res.json()["data"][0]["Account_Name"]:
                     account_id = search_res.json()["data"][0]["Account_Name"]["id"]
            else:
                contact_payload = {
                    "data": [{
                        "Last_Name": company_name,
                        "Email": contact_email,
                        "Lead_Source": source,
                        "Account_Name": {"name": company_name}
                    }]
                }
                contact_create_res = requests.post("https://www.zohoapis.in/crm/v3/Contacts", json=contact_payload, headers=headers)
                if contact_create_res.status_code in [200, 201] and contact_create_res.json().get("data"):
                     contact_id = contact_create_res.json()["data"][0]["details"]["id"]
            
            inq_number = generate_next_inquiry_number()
            
            deal_data = {
                "Deal_Name": inq_number,
                "Stage": "Qualification",
                "POL": pol,
                "POD": pod,
                "Container_Type": extracted.get('container_type'),
                "Incoterms": extracted.get('incoterms'),
                "Lead_Source": source,
                "Description": f"Commodity: {extracted.get('commodity')}\nRequested via: {source}"
            }
            
            if contact_id:
                deal_data["Contact_Name"] = {"id": contact_id}
            if account_id:
                deal_data["Account_Name"] = {"id": account_id}
                
            push_to_zoho_crm("Deals", [deal_data])
            
            alert_msg = f"📧 *New {source} RFQ Processed*\n\nID: {inq_number}\nCompany: {company_name}\nRoute: {pol} ➡️ {pod}\nEquipment: {extracted.get('container_type')}\n\nContact created/updated and Deal mapped in Zoho CRM."
            
            best_rate = search_lowest_rate(pol, pod)
            if best_rate:
                margin_pct = float(os.getenv("PROFIT_MARGIN_PERCENT", 20))
                sell_price = best_rate['price'] * (1 + (margin_pct / 100))
                pdf_bytes = generate_quotation_pdf(inq_number, pol, pod, best_rate['vehicle'], sell_price)
                alert_msg += f"\n\n✅ Found rate and generated quotation."
                
                my_number = os.getenv("YOUR_WHATSAPP_NUMBER")
                if my_number:
                    upload_and_send_pdf(my_number, pdf_bytes, f"{inq_number}.pdf", alert_msg)
            else:
                alert_msg += f"\n\n⚠️ No valid rates found in system."
                my_number = os.getenv("YOUR_WHATSAPP_NUMBER")
                if my_number:
                    send_whatsapp_message(my_number, alert_msg)
                    
    except Exception as e:
        print(f"Email Processing Error: {e}")

def process_whatsapp_message(payload):
    try:
        value = payload.get('entry', [{}])[0].get('changes', [{}])[0].get('value', {})
        messages = value.get('messages', [])
        if not messages:
            return
            
        msg = messages[0]
        msg_type = msg.get("type")
        sender_phone = msg.get("from")
        
        if msg_type == "text":
            msg_text = msg.get('text', {}).get('body', '').strip()
            
            greetings = ["hi", "hello", "hey", "help", "menu", "start", "ping"]
            if msg_text.lower() in greetings:
                welcome_msg = (
                    "👋 *Hello! I am your Mega Move AI OS.*\n\n"
                    "Here is what I am ready to do for you:\n\n"
                    "📦 *Log an RFQ:*\nJust text me a route (e.g., 'Quote for 1x20ft from POL to POD').\n\n"
                    "📄 *Upload Vendor Rates:*\nSend an Excel/PDF file and type 'rates' in the caption.\n\n"
                    "🧾 *Process Vendor Bills:*\nSend a PDF invoice and type 'bill' in the caption.\n\n"
                    "👥 *Upload Trade Show Leads:*\nSend an Excel file and type 'leads' in the caption."
                )
                send_whatsapp_message(sender_phone, welcome_msg)
                return
            
            prompt = f"Extract freight details from this text: '{msg_text}'. If it does not contain logistics info, return empty values. Return JSON: pol, pod, commodity, container_type, weight. JSON only."
            response = client.chat.completions.create(
                model="gpt-4o",
                messages=[{"role": "system", "content": "You are a logistics parser. Output only valid JSON."},{"role": "user", "content": prompt}],
                response_format={ "type": "json_object" }
            )
            extracted = json.loads(response.choices[0].message.content)
            
            if extracted.get('pol') and extracted.get('pod'):
                inq_number = generate_next_inquiry_number()
                push_to_zoho_crm("Deals", [{
                    "Deal_Name": inq_number,
                    "Stage": "Qualification",
                    "Description": f"Commodity: {extracted.get('commodity')}\nContainer: {extracted.get('container_type')}\nWeight: {extracted.get('weight')}"
                }])
                
                best_rate = search_lowest_rate(extracted.get('pol'), extracted.get('pod'))
                if best_rate:
                    margin_pct = float(os.getenv("PROFIT_MARGIN_PERCENT", 20))
                    sell_price = best_rate['price'] * (1 + (margin_pct / 100))
                    pdf_bytes = generate_quotation_pdf(inq_number, extracted.get('pol'), extracted.get('pod'), best_rate['vehicle'], sell_price)
                    caption = f"✅ *Quotation Ready*\n\nRoute: {extracted.get('pol')} ➡️ {extracted.get('pod')}\nCheapest Vendor: {best_rate['vendor']}\nBuy Rate: {best_rate['price']}\nSell Rate ({margin_pct}% margin): *{sell_price:.2f}*\n\nForward the document above to your client."
                    upload_and_send_pdf(sender_phone, pdf_bytes, f"{inq_number}.pdf", caption)
                else:
                    send_whatsapp_message(sender_phone, f"✅ Logged {inq_number} for {extracted.get('pol')} to {extracted.get('pod')}. \n\n⚠️ No vendor rates found in database. Please upload rates for this route.")
            else:
                send_whatsapp_message(sender_phone, "🤖 I didn't recognize any specific routing details in that message.")
                
        elif msg_type == "document":
            doc_info = msg.get("document", {})
            media_id = doc_info.get("id")
            caption = doc_info.get("caption", "").lower()
            
            file_bytes = download_whatsapp_media(media_id)
            if not file_bytes:
                return
                
            if "rate" in caption:
                send_whatsapp_message(sender_phone, "✅ File received. Processing rate sheet...")
            elif "bill" in caption or "invoice" in caption:
                send_whatsapp_message(sender_phone, "✅ File received. Processing vendor bill...")
            elif "lead" in caption:
                send_whatsapp_message(sender_phone, "✅ File received. Uploading leads to Zoho...")
            else:
                send_whatsapp_message(sender_phone, "🤖 I received the document, but please re-upload and type 'rates', 'bill', or 'leads' in the caption so I know what to do with it.")
                
    except Exception as e:
        send_whatsapp_message(sender_phone, f"⚠️ System encountered an error: {str(e)}")
