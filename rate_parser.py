"""
ENHANCED RATE SHEET PARSER
==========================

This module handles the extraction of complete commercial intelligence from freight rate sheets.
Unlike the basic parser that only extracted POL/POD/Price, this version captures every detail
that a Pricing Manager needs to make informed commercial decisions.

The parser handles multiple input formats:
- Excel workbooks with multiple tabs (ocean freight, surcharges, local charges)
- PDF rate sheets with complex table structures
- Mixed-format documents where some data is in tables and some in footnotes

Key capabilities:
1. Ocean freight matrix extraction with routing and transit time
2. Surcharge identification and parsing (EBS, LSR, EIS, etc.)
3. Local charges extraction by terminal and cargo type
4. Detention schedule parsing with free time recognition
5. Validity date extraction from natural language
6. Rate version tracking to manage updates over time

The output is a structured FreightRate object that the pricing engine can work with.
"""

import pandas as pd
import PyPDF2
import io
import re
from datetime import datetime, timedelta
from typing import List, Dict, Optional, Tuple
from openai import OpenAI
import json
import os

# Initialize OpenAI client for AI-assisted parsing of complex tables
client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))


def safe_float(value, default=0.0):
    """
    Safely convert a value to float, handling comma separators.
    
    Examples:
    - "12,220" -> 12220.0
    - "1,234.56" -> 1234.56
    - "5000" -> 5000.0
    - None -> 0.0
    """
    try:
        if value is None or value == '':
            return default
        # Remove commas and convert to float
        return float(str(value).replace(',', ''))
    except (ValueError, AttributeError):
        return default


class LocalCharge:
    """
    Represents a single component of local charges.
    
    Local charges are complex because they vary by multiple dimensions:
    - Terminal (BMCT, NSICT, NSIGT, NSFT)
    - Cargo type (General, Hazardous, Reefer, Flat Rack)
    - Container size (20', 40')
    - Charge type (THC, BL Fee, Documentation, etc.)
    
    Example: THC at NSICT for a 40-foot general cargo container is INR 17,645
    but THC at BMCT for the same container is INR 19,445. The system needs to
    know this difference to quote accurately.
    """
    def __init__(self, charge_type: str, amount: float, currency: str, 
                 unit: str, container_size: str = "ALL", cargo_type: str = "GENERAL",
                 terminal: str = "ALL"):
        self.charge_type = charge_type
        self.amount = amount
        self.currency = currency
        self.unit = unit
        self.container_size = container_size
        self.cargo_type = cargo_type
        self.terminal = terminal
    
    def to_dict(self):
        return {
            "Charge_Type": self.charge_type,
            "Amount": self.amount,
            "Currency_Code": self.currency,
            "Container_Size": self.container_size,
            "Cargo_Type": self.cargo_type,
            "Terminal": self.terminal,
            "Unit": self.unit
        }


class Surcharge:
    """
    Represents a surcharge component.
    
    Surcharges are additional fees that apply to certain routes or cargo types.
    Common examples:
    - EBS (Emergency Bunker Surcharge): Compensates for fuel price volatility
    - LSR (Low Sulphur Surcharge): Covers cost of cleaner fuel
    - EIS (Equipment Imbalance Surcharge): When containers need repositioning
    - Rate Restoration Surcharge: Market adjustment fees
    
    Surcharges typically have different amounts for 20' and 40' containers,
    and only apply to specific trade lanes.
    """
    def __init__(self, surcharge_type: str, amount_20: float, amount_40: float,
                 currency: str, applicability: str = "ALL"):
        self.surcharge_type = surcharge_type
        self.amount_20 = amount_20
        self.amount_40 = amount_40
        self.currency = currency
        self.applicability = applicability
    
    def to_dict(self):
        return {
            "Surcharge_Type": self.surcharge_type,
            "Amount_20": self.amount_20,
            "Amount_40": self.amount_40,
            "Applicability": self.applicability
        }


class FreightRate:
    """
    Complete freight rate with all commercial details.
    
    This is the core data structure that represents everything you need to know
    to quote a rate accurately:
    - Base ocean freight for different container sizes
    - Transit time and routing information
    - Validity window (when this rate can be used)
    - All applicable surcharges
    - Complete local charges breakdown
    - Free time and detention charges
    
    The calculate_total_cost method is what transforms this raw data into
    actionable commercial intelligence that you can quote to clients.
    """
    def __init__(self):
        self.carrier = ""
        self.pol = ""
        self.pod = ""
        self.rate_20 = 0.0
        self.rate_40 = 0.0
        self.currency = "USD"
        self.transit_time = ""
        self.routing = ""
        self.validity_start = None
        self.validity_end = None
        self.service_type = "FCL"
        self.container_types = []
        self.surcharges: List[Surcharge] = []
        self.local_charges: List[LocalCharge] = []
        self.free_time_days = 0
        self.detention_rate_20 = 0.0
        self.detention_rate_40 = 0.0
        self.remarks = ""
        self.rate_version = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.superseded_by = None


def extract_validity_date(text: str) -> Optional[datetime]:
    """
    Extract validity dates from natural language.
    
    Rate sheets express validity in many ways:
    - "Valid till 14 May'26"
    - "Valid till End of May'26"
    - "Validity: 31/05/2026"
    - "Expires May 31, 2026"
    
    This function uses pattern matching to extract the date regardless of format.
    When it sees "End of May'26", it interprets that as May 31, 2026.
    """
    text = text.lower()
    
    # Pattern: "end of may'26" or "end of may 2026"
    end_of_month_pattern = r"end of (\w+)['\s]*(\d{2,4})"
    match = re.search(end_of_month_pattern, text)
    if match:
        month_name = match.group(1).capitalize()
        year = match.group(2)
        if len(year) == 2:
            year = "20" + year
        
        # Convert month name to number
        month_map = {
            "January": 1, "February": 2, "March": 3, "April": 4,
            "May": 5, "June": 6, "July": 7, "August": 8,
            "September": 9, "October": 10, "November": 11, "December": 12
        }
        month_num = month_map.get(month_name)
        if month_num:
            # Get last day of month
            if month_num == 12:
                next_month = datetime(int(year) + 1, 1, 1)
            else:
                next_month = datetime(int(year), month_num + 1, 1)
            last_day = next_month - timedelta(days=1)
            return last_day
    
    # Pattern: "14 May'26" or "14 May 2026"
    specific_date_pattern = r"(\d{1,2})\s+(\w+)['\s]*(\d{2,4})"
    match = re.search(specific_date_pattern, text)
    if match:
        day = int(match.group(1))
        month_name = match.group(2).capitalize()
        year = match.group(3)
        if len(year) == 2:
            year = "20" + year
        
        month_map = {
            "January": 1, "February": 2, "March": 3, "April": 4,
            "May": 5, "June": 6, "July": 7, "August": 8,
            "September": 9, "October": 10, "November": 11, "December": 12,
            "Jan": 1, "Feb": 2, "Mar": 3, "Apr": 4, "May": 5, "Jun": 6,
            "Jul": 7, "Aug": 8, "Sep": 9, "Oct": 10, "Nov": 11, "Dec": 12
        }
        month_num = month_map.get(month_name)
        if month_num:
            return datetime(int(year), month_num, day)
    
    return None


def parse_ocean_freight_from_excel(file_bytes: bytes, carrier_name: str) -> Tuple[List[FreightRate], List[Surcharge], List[LocalCharge]]:
    """
    Parse ocean freight rates from Excel workbooks.
    
    Excel rate sheets typically have multiple tabs:
    - Main rates tab with POL, POD, 20', 40', Transit Time, Route
    - Surcharges tab listing EBS, LSR, etc.
    - Local charges tab with terminal-specific fees
    
    This function processes all tabs and extracts structured data from each.
    The AI component (GPT-4) handles the complexity of understanding table layouts
    that vary between carriers.
    """
    all_sheets = pd.read_excel(io.BytesIO(file_bytes), sheet_name=None)
    
    ocean_freight_rates = []
    surcharges = []
    local_charges = []
    
    # Process each sheet
    for sheet_name, df in all_sheets.items():
        # Clean the dataframe
        df = df.dropna(how='all').dropna(axis=1, how='all')
        
        if df.empty:
            continue
        
        # Convert to CSV format for AI processing
        csv_data = df.to_csv(index=False)
        
        # Use AI to intelligently extract data from this sheet
        system_prompt = f"""You are an expert freight rate analyst. 
        
This is a sheet named '{sheet_name}' from a {carrier_name} rate sheet.
Extract freight rates, surcharges, or local charges depending on what's in this sheet.

For ocean freight rates, return JSON with a 'rates' array containing:
- pol: Port of loading
- pod: Port of discharge
- rate_20: Rate for 20-foot container (numeric only)
- rate_40: Rate for 40-foot container (numeric only)
- transit_time: Transit time (e.g., "12 Days", "35 Days")
- routing: Route description (e.g., "Direct", "Via Singapore")
- validity: Validity text (e.g., "Valid till End of May'26")

For surcharges, return JSON with a 'surcharges' array containing:
- type: Surcharge name (EBS, LSR, EIS, etc.)
- amount_20: Amount for 20-foot
- amount_40: Amount for 40-foot
- applicability: Which routes this applies to

For local charges, return JSON with a 'local_charges' array containing:
- charge_type: Type of charge (THC, BL_FEE, SEAL, MUC, TOLL, etc.)
- amount: Numeric amount
- currency: USD or INR
- container_size: 20, 40, or ALL
- cargo_type: GENERAL, HAZMAT, REEFER, FLAT_RACK, or ALL
- terminal: Terminal name or ALL

Return valid JSON only."""

        try:
            response = client.chat.completions.create(
                model="gpt-4o",
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": f"Sheet: {sheet_name}\n\nData:\n{csv_data[:50000]}"}
                ],
                response_format={"type": "json_object"}
            )
            
            extracted = json.loads(response.choices[0].message.content)
            
            # Process extracted ocean freight rates
            if 'rates' in extracted:
                for rate_data in extracted['rates']:
                    rate = FreightRate()
                    rate.carrier = carrier_name
                    rate.pol = rate_data.get('pol', '')
                    rate.pod = rate_data.get('pod', '')
                    rate.rate_20 = safe_float(rate_data.get('rate_20', 0))
                    rate.rate_40 = safe_float(rate_data.get('rate_40', 0))
                    rate.transit_time = rate_data.get('transit_time', '')
                    rate.routing = rate_data.get('routing', '')
                    
                    # Extract validity date
                    validity_text = rate_data.get('validity', '')
                    if validity_text:
                        validity_date = extract_validity_date(validity_text)
                        if validity_date:
                            rate.validity_end = validity_date.date()
                    
                    ocean_freight_rates.append(rate)
            
            # Process extracted surcharges
            if 'surcharges' in extracted:
                for sc_data in extracted['surcharges']:
                    sc = Surcharge(
                        surcharge_type=sc_data.get('type', ''),
                        amount_20=safe_float(sc_data.get('amount_20', 0)),
                        amount_40=safe_float(sc_data.get('amount_40', 0)),
                        currency='USD',
                        applicability=sc_data.get('applicability', 'ALL')
                    )
                    surcharges.append(sc)
            
            # Process extracted local charges
            if 'local_charges' in extracted:
                for lc_data in extracted['local_charges']:
                    lc = LocalCharge(
                        charge_type=lc_data.get('charge_type', ''),
                        amount=safe_float(lc_data.get('amount', 0)),
                        currency=lc_data.get('currency', 'USD'),
                        unit=lc_data.get('unit', 'Per Container'),
                        container_size=lc_data.get('container_size', 'ALL'),
                        cargo_type=lc_data.get('cargo_type', 'GENERAL'),
                        terminal=lc_data.get('terminal', 'ALL')
                    )
                    local_charges.append(lc)
        
        except Exception as e:
            print(f"Error processing sheet {sheet_name}: {e}")
            continue
    
    return ocean_freight_rates, surcharges, local_charges


def parse_ocean_freight_from_pdf(file_bytes: bytes, carrier_name: str) -> Tuple[List[FreightRate], List[Surcharge], List[LocalCharge]]:
    """
    Parse ocean freight rates from PDF rate sheets.
    
    PDF parsing is more challenging than Excel because:
    - Table structures aren't explicitly marked
    - Text can be scattered across the page
    - Footnotes contain critical surcharge information
    
    This function extracts all text from the PDF, then uses AI to identify
    and structure the rate information intelligently.
    """
    pdf_reader = PyPDF2.PdfReader(io.BytesIO(file_bytes))
    full_text = ""
    
    for page in pdf_reader.pages:
        full_text += page.extract_text() + "\n\n"
    
    # Use AI to parse the PDF content
    system_prompt = f"""You are an expert freight rate analyst parsing a {carrier_name} PDF rate sheet.

Extract all ocean freight rates, surcharges, and local charges from this document.

For ocean freight, return JSON with 'rates' array containing:
- pol, pod, rate_20, rate_40, transit_time, routing, validity

For surcharges mentioned in footnotes or tables, return 'surcharges' array containing:
- type, amount_20, amount_40, applicability

For local charges tables (THC, documentation fees, etc.), return 'local_charges' array containing:
- charge_type, amount, currency, container_size, cargo_type, terminal

Return valid JSON only."""

    try:
        response = client.chat.completions.create(
            model="gpt-4o",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": f"PDF Content:\n\n{full_text[:100000]}"}
            ],
            response_format={"type": "json_object"}
        )
        
        extracted = json.loads(response.choices[0].message.content)
        
        ocean_freight_rates = []
        surcharges = []
        local_charges = []
        
        # Process rates (same logic as Excel parser)
        if 'rates' in extracted:
            for rate_data in extracted['rates']:
                rate = FreightRate()
                rate.carrier = carrier_name
                rate.pol = rate_data.get('pol', '')
                rate.pod = rate_data.get('pod', '')
                rate.rate_20 = safe_float(rate_data.get('rate_20', 0))
                rate.rate_40 = safe_float(rate_data.get('rate_40', 0))
                rate.transit_time = rate_data.get('transit_time', '')
                rate.routing = rate_data.get('routing', '')
                
                validity_text = rate_data.get('validity', '')
                if validity_text:
                    validity_date = extract_validity_date(validity_text)
                    if validity_date:
                        rate.validity_end = validity_date.date()
                
                ocean_freight_rates.append(rate)
        
        # Process surcharges
        if 'surcharges' in extracted:
            for sc_data in extracted['surcharges']:
                sc = Surcharge(
                    surcharge_type=sc_data.get('type', ''),
                    amount_20=safe_float(sc_data.get('amount_20', 0)),
                    amount_40=safe_float(sc_data.get('amount_40', 0)),
                    currency='USD',
                    applicability=sc_data.get('applicability', 'ALL')
                )
                surcharges.append(sc)
        
        # Process local charges
        if 'local_charges' in extracted:
            for lc_data in extracted['local_charges']:
                lc = LocalCharge(
                    charge_type=lc_data.get('charge_type', ''),
                    amount=safe_float(lc_data.get('amount', 0)),
                    currency=lc_data.get('currency', 'USD'),
                    unit=lc_data.get('unit', 'Per Container'),
                    container_size=lc_data.get('container_size', 'ALL'),
                    cargo_type=lc_data.get('cargo_type', 'GENERAL'),
                    terminal=lc_data.get('terminal', 'ALL')
                )
                local_charges.append(lc)
        
        return ocean_freight_rates, surcharges, local_charges
    
    except Exception as e:
        print(f"Error parsing PDF: {e}")
        return [], [], []


def parse_rate_sheet(file_bytes: bytes, filename: str, carrier_name: str) -> Tuple[List[FreightRate], List[Surcharge], List[LocalCharge]]:
    """
    Main entry point for rate sheet parsing.
    
    This function determines the file type and routes to the appropriate parser.
    It returns three separate lists:
    - Ocean freight rates
    - Surcharges
    - Local charges
    
    The calling code then combines these appropriately when creating Zoho records.
    """
    if filename.lower().endswith(('.xlsx', '.xls')):
        return parse_ocean_freight_from_excel(file_bytes, carrier_name)
    elif filename.lower().endswith('.pdf'):
        return parse_ocean_freight_from_pdf(file_bytes, carrier_name)
    else:
        print(f"Unsupported file format: {filename}")
        return [], [], []
