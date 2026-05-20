from fastapi import FastAPI, Request, BackgroundTasks
from fastapi.responses import PlainTextResponse
import os
import requests
from openai import OpenAI
import json
import io
import pandas as pd
import PyPDF2

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
    """Sends an automated text back to your WhatsApp."""
    phone_id = os.getenv("WHATSAPP_PHONE_ID")
    url = f"https://graph.facebook.com/v18.0/{phone_id}/messages"
    headers = {
        "Authorization": f"Bearer {os.getenv('WHATSAPP_ACCESS_TOKEN')}",
        "Content-Type": "application/json"
    }
    payload = {
        "messaging_product": "whatsapp",
        "to": to_number,
        "type": "text",
        "text": {"body": text}
    }
    requests.post(url, headers=headers, json=payload)

def get_zoho_access_token():
    url = "https://accounts.zoho.in/oauth/v2/token" 
    payload = {
        "refresh_token": os.getenv("ZOHO_REFRESH_TOKEN"),
        "client_id": os.getenv("ZOHO_CLIENT_ID"),
        "client_secret": os.getenv("ZOHO_CLIENT_SECRET"),
        "grant_type": "refresh_token"
    }
    response = requests.post(url, data=payload)
    return response.json().get("access_token")

def download_whatsapp_media(media_id):
    headers = {"Authorization": f"Bearer {os.getenv('WHATSAPP_ACCESS_TOKEN')}"}
    url = f"https://graph.facebook.com/v18.0/{media_id}"
    res = requests.get(url, headers=headers).json()
    media_url = res.get("url")
    if not media_url:
        return None
    file_res = requests.get(media_url, headers=headers)
    return file_res.content

def push_to_zoho_crm(module, data_list):
    access_token = get_zoho_access_token()
    url = f"https://www.zohoapis.in/crm/v3/{module}" 
    headers = {"Authorization": f"Zoho-oauthtoken {access_token}", "Content-Type": "application/json"}
    payload = {"data": data_list}
    res = requests.post(url, json=payload, headers=headers)
    return res

def process_bulk_leads(file_bytes, filename, sender_phone):
    try:
        df = pd.read_csv(io.BytesIO(file_bytes)) if filename.endswith('.csv') else pd.read_excel(io.BytesIO(file_bytes))
        leads_list = []
        for _, row in df.head(100).iterrows():
            leads_list.append({
                "Last_Name": str(row.get("Name", "Unknown Lead")),
                "Company": str(row.get("Company", "Individual")),
                "Email": str(row.get("Email", "")),
                "Phone": str(row.get("Phone", ""))
            })
        if leads_list:
            push_to_zoho_crm("Leads", leads_list)
            send_whatsapp_message(sender_phone, f"✅ Successfully imported {len(leads_list)} leads to Zoho CRM.")
    except Exception as e:
        send_whatsapp_message(sender_phone, f"❌ Error processing leads: {str(e)}")

def process_rate_sheet(file_bytes, filename, sender_phone):
    try:
        extracted_text = ""
        if filename.endswith('.pdf'):
            reader = PyPDF2.PdfReader(io.BytesIO(file_bytes))
            for page in reader.pages:
                extracted_text += page.extract_text() + "\n"
        elif filename.endswith(('.xlsx', '.xls', '.csv')):
            df = pd.read_csv(io.BytesIO(file_bytes)) if filename.endswith('.csv') else pd.read_excel(io.BytesIO(file_bytes))
            extracted_text = df.head(50).to_csv(index=False)

        prompt = f"Extract freight rates from this data. Return a JSON object with a key 'rates' containing an array of objects. Each object must have: carrier, pol, pod, container_type, price (numeric).\n\nData:\n{extracted_text}"
        
        response = client.chat.completions.create(
            model="gpt-4o",
            messages=[{"role": "system", "content": "You are a freight rate parser. Output strictly valid JSON."},{"role": "user", "content": prompt}],
            response_format={ "type": "json_object" }
        )
        
        rates_data = json.loads(response.choices[0].message.content).get("rates", [])
        zoho_rates = []
        for rate in rates_data:
            zoho_rates.append({
                "Name": f"{rate.get('carrier')} - {rate.get('pol')} to {rate.get('pod')}",
                "Vendor_Name": rate.get("carrier"),
                "POL": rate.get("pol"),
                "POD": rate.get("pod"),
                "Vehicle_Types": rate.get("container_type"),
                "Exwork_Charges": str(rate.get("price"))
            })
            
        if zoho_rates:
            push_to_zoho_crm("CustomModule1", zoho_rates)
            send_whatsapp_message(sender_phone, f"✅ Parsed rate sheet. Uploaded {len(zoho_rates)} routes to Zoho CRM.")
            
    except Exception as e:
        send_whatsapp_message(sender_phone, f"❌ Error processing rate sheet: {str(e)}")

def process_pdf_invoice(file_bytes, sender_phone):
    try:
        reader = PyPDF2.PdfReader(io.BytesIO(file_bytes))
        extracted_text = "".join([page.extract_text() + "\n" for page in reader.pages])
        prompt = f"Extract vendor bill: vendor_name, invoice_number, invoice_date (YYYY-MM-DD), due_date (YYYY-MM-DD), total_amount (numeric). JSON only.\n\n{extracted_text}"
        
        response = client.chat.completions.create(
            model="gpt-4o",
            messages=[{"role": "system", "content": "Logistics accounting parser. Output only valid JSON."},{"role": "user", "content": prompt}],
            response_format={ "type": "json_object" }
        )
        bill_data = json.loads(response.choices[0].message.content)
        
        access_token = get_zoho_access_token()
        org_id = os.getenv("ZOHO_BOOKS_ORG_ID")
        url = f"https://www.zohoapis.in/books/v3/bills?organization_id={org_id}"
        headers = {"Authorization": f"Zoho-oauthtoken {access_token}", "Content-Type": "application/json"}
        payload = {
            "vendor_name": bill_data.get("vendor_name", "Unknown Vendor"),
            "bill_number": bill_data.get("invoice_number", "Unknown"),
            "date": bill_data.get("invoice_date", ""),
            "due_date": bill_data.get("due_date", ""),
            "line_items": [{"name": "Freight Services", "rate": bill_data.get("total_amount", 0), "quantity": 1}]
        }
        requests.post(url, json=payload, headers=headers)
        send_whatsapp_message(sender_phone, f"🧾 Draft bill created in Zoho Books for {bill_data.get('vendor_name')} (Amount: {bill_data.get('total_amount')}).")
    except Exception as e:
        send_whatsapp_message(sender_phone, f"❌ Error processing invoice: {str(e)}")

def process_whatsapp_message(payload):
    try:
        value = payload.get('entry', [{}])[0].get('changes', [{}])[0].get('value', {})
        messages = value.get('messages', [])
        if not messages:
            return
            
        msg = messages[0]
        msg_type = msg.get("type")
        sender_phone = msg.get("from") # The phone number sending the message
        
        if msg_type == "text":
            msg_text = msg.get('text', {}).get('body', '')
            prompt = f"Extract freight details: {msg_text}"
            response = client.chat.completions.create(
                model="gpt-4o",
                messages=[{"role": "system", "content": "Return JSON: pol, pod, commodity, container_type, weight. JSON only."},{"role": "user", "content": prompt}],
                response_format={ "type": "json_object" }
            )
            extracted_data = json.loads(response.choices[0].message.content)
            push_to_zoho_crm("Deals", [{
                "Deal_Name": f"RFQ - {extracted_data.get('pol')} to {extracted_data.get('pod')}",
                "Stage": "Qualification",
                "Description": f"Commodity: {extracted_data.get('commodity')}\nContainer: {extracted_data.get('container_type')}\nWeight: {extracted_data.get('weight')}"
            }])
            send_whatsapp_message(sender_phone, f"✅ RFQ Logged: Deal created in Zoho CRM for {extracted_data.get('pol')} to {extracted_data.get('pod')}.")
            
        elif msg_type == "document":
            doc_info = msg.get("document", {})
            media_id = doc_info.get("id")
            filename = doc_info.get("filename", "").lower()
            caption = doc_info.get("caption", "").lower()
            
            file_bytes = download_whatsapp_media(media_id)
            if not file_bytes:
                return
                
            if "rate" in caption:
                process_rate_sheet(file_bytes, filename, sender_phone)
            elif "bill" in caption or "invoice" in caption:
                process_pdf_invoice(file_bytes, sender_phone)
            elif "lead" in caption:
                process_bulk_leads(file_bytes, filename, sender_phone)
                
    except Exception as e:
        print(f"System Error: {e}")
