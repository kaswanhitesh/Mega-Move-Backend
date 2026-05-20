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

def send_whatsapp_message(to_number, text):
    phone_id = os.getenv("WHATSAPP_PHONE_ID")
    url = f"https://graph.facebook.com/v18.0/{phone_id}/messages"
    headers = {"Authorization": f"Bearer {os.getenv('WHATSAPP_ACCESS_TOKEN')}", "Content-Type": "application/json"}
    payload = {"messaging_product": "whatsapp", "to": to_number, "type": "text", "text": {"body": text}}
    requests.post(url, headers=headers, json=payload)

def upload_and_send_pdf(to_number, file_bytes, filename, caption):
    """Uploads a PDF to Meta and sends it to the user."""
    phone_id = os.getenv("WHATSAPP_PHONE_ID")
    access_token = os.getenv("WHATSAPP_ACCESS_TOKEN")
    
    # 1. Upload Media
    upload_url = f"https://graph.facebook.com/v18.0/{phone_id}/media"
    headers = {"Authorization": f"Bearer {access_token}"}
    files = {
        'file': (filename, file_bytes, 'application/pdf'),
        'messaging_product': (None, 'whatsapp')
    }
    upload_res = requests.post(upload_url, headers=headers, files=files)
    media_id = upload_res.json().get("id")
    
    if not media_id:
        send_whatsapp_message(to_number, "❌ Failed to upload PDF quotation to WhatsApp.")
        return

    # 2. Send Document Message
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

def search_lowest_rate(pol, pod):
    """Searches Zoho CRM for the lowest rate matching the route."""
    access_token = get_zoho_access_token()
    # Using Zoho's search API syntax
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
            # Return the dictionary with the lowest price
            return min(valid_rates, key=lambda x: x['price'])
    return None

def generate_quotation_pdf(pol, pod, equipment, sell_price):
    """Generates a clean PDF quotation document."""
    pdf = FPDF()
    pdf.add_page()
    
    # Header
    pdf.set_font("Arial", 'B', 16)
    pdf.cell(200, 10, txt="MEGA MOVE INDIA PRIVATE LIMITED", ln=1, align='C')
    pdf.set_font("Arial", size=12)
    pdf.cell(200, 10, txt="FREIGHT QUOTATION", ln=1, align='C')
    pdf.ln(10)
    
    # Body
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
    
    # Pricing
    pdf.set_font("Arial", 'B', 14)
    pdf.cell(200, 10, txt=f"Total Sell Price: USD {sell_price:.2f}", ln=1, align='L')
    pdf.ln(20)
    
    # Footer
    pdf.set_font("Arial", 'I', 10)
    pdf.cell(200, 10, txt="* Rates are subject to space and equipment availability.", ln=1, align='L')
    pdf.cell(200, 10, txt="* Generated automatically by Mega Move OS.", ln=1, align='L')
    
    # Save to temp file and read bytes
    file_path = "/tmp/quotation.pdf"
    pdf.output(file_path)
    with open(file_path, "rb") as f:
        return f.read()

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
                send_whatsapp_message(sender_phone, "👋 *Hello! I am your Mega Move AI OS.*\nSend me an RFQ, or upload Rates/Bills/Leads with a caption.")
                return
            
            # 1. Extract Details
            prompt = f"Extract freight details from this text. Return JSON: pol, pod, commodity, container_type, weight. JSON only.\n\n{msg_text}"
            response = client.chat.completions.create(
                model="gpt-4o",
                messages=[{"role": "system", "content": "You are a logistics parser. Output only valid JSON."},{"role": "user", "content": prompt}],
                response_format={ "type": "json_object" }
            )
            extracted = json.loads(response.choices[0].message.content)
            pol = extracted.get('pol')
            pod = extracted.get('pod')
            
            if pol and pod:
                # 2. Log to CRM
                push_to_zoho_crm("Deals", [{
                    "Deal_Name": f"RFQ - {pol} to {pod}",
                    "Stage": "Qualification",
                    "Description": f"Commodity: {extracted.get('commodity')}\nContainer: {extracted.get('container_type')}"
                }])
                send_whatsapp_message(sender_phone, f"🔍 Logged RFQ for {pol} to {pod}. Searching our rate database...")
                
                # 3. Search Rates & Generate Quotation
                best_rate = search_lowest_rate(pol, pod)
                
                if best_rate:
                    # Calculate Profit Margin
                    margin_pct = float(os.getenv("PROFIT_MARGIN_PERCENT", 20))
                    buy_price = best_rate['price']
                    sell_price = buy_price * (1 + (margin_pct / 100))
                    
                    # Generate PDF
                    pdf_bytes = generate_quotation_pdf(pol, pod, best_rate['vehicle'], sell_price)
                    
                    # Send PDF back to WhatsApp
                    caption = f"✅ *Quotation Ready*\n\nRoute: {pol} ➡️ {pod}\nCheapest Vendor: {best_rate['vendor']}\nBuy Rate: {buy_price}\nSell Rate ({margin_pct}% margin): *{sell_price:.2f}*\n\nForward the document above to your client."
                    upload_and_send_pdf(sender_phone, pdf_bytes, f"Quote_{pol}_{pod}.pdf", caption)
                else:
                    send_whatsapp_message(sender_phone, f"⚠️ I checked the database, but we do not have any valid vendor rates on file for {pol} to {pod}. Please upload a new rate sheet for this route.")
            else:
                send_whatsapp_message(sender_phone, "🤖 I didn't recognize routing details in that message.")
                
    except Exception as e:
        send_whatsapp_message(sender_phone, f"⚠️ Error: {str(e)}")
