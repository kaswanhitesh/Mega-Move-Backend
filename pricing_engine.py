"""
PRICING INTELLIGENCE ENGINE
===========================

This module contains the commercial decision-making logic for freight pricing.
It takes structured rate data and applies business rules to generate intelligent
pricing recommendations.

Key responsibilities:
1. Find the best rates for a given route considering multiple carriers
2. Calculate complete landed costs including all fees and surcharges
3. Compare options and recommend the optimal choice
4. Apply margin calculations with forex awareness
5. Flag rates that are expiring soon
6. Detect pricing anomalies that might indicate data entry errors

This is where the system thinks like an experienced Pricing Manager who knows
that the lowest ocean freight doesn't always mean the lowest total cost, that
direct services are worth paying a premium for if the transit time matters, and
that a rate expiring in three days is risky to quote even if it's attractive.
"""

from typing import List, Dict, Optional, Tuple
from datetime import datetime, timedelta
from rate_parser import FreightRate, Surcharge, LocalCharge
import os

# Default forex rate - in production this should fetch from a live API
USD_TO_INR_RATE = float(os.getenv("USD_TO_INR_EXCHANGE_RATE", "83.0"))

# Default margin percentage
DEFAULT_MARGIN_PCT = float(os.getenv("PROFIT_MARGIN_PERCENT", "20.0"))


class PricingRecommendation:
    """
    Represents a complete pricing recommendation with commercial intelligence.
    
    This isn't just "here's the rate" - it's "here's the rate, here's why it's
    the best option, here's what else is available, and here's what you need to
    watch out for."
    """
    def __init__(self):
        self.primary_rate: Optional[FreightRate] = None
        self.alternatives: List[FreightRate] = []
        self.total_cost_breakdown: Dict = {}
        self.recommendation_reason: str = ""
        self.warnings: List[str] = []
        self.competitive_advantages: List[str] = []


def find_rates_for_route(pol: str, pod: str, all_rates: List[FreightRate], 
                        check_validity: bool = True) -> List[FreightRate]:
    """
    Find all rates that serve a specific route.
    
    This does fuzzy matching on port names because rate sheets aren't consistent:
    - "Nhava Sheva" vs "JNPT" vs "Mumbai" all refer to the same port
    - "Singapore" vs "Singapore Port" vs "SGP"
    
    If check_validity is True, filters out expired rates automatically.
    """
    matching_rates = []
    
    for rate in all_rates:
        # Fuzzy match on POL and POD
        pol_match = (pol.lower() in rate.pol.lower() or rate.pol.lower() in pol.lower())
        pod_match = (pod.lower() in rate.pod.lower() or rate.pod.lower() in pod.lower())
        
        if pol_match and pod_match:
            if check_validity:
                if rate.is_valid():
                    matching_rates.append(rate)
            else:
                matching_rates.append(rate)
    
    return matching_rates


def calculate_total_landed_cost(rate: FreightRate, container_size: str = "40HC", 
                                cargo_type: str = "GENERAL", terminal: str = "NSICT",
                                num_containers: int = 1) -> Dict:
    """
    Calculate complete landed cost with every fee itemized.
    
    This is the core pricing intelligence function. It takes a rate and calculates
    what you'll actually pay when all fees are included:
    
    1. Base ocean freight
    2. All applicable surcharges (EBS, LSR, etc.)
    3. Origin local charges (THC, documentation, ancillary fees)
    4. Conversion of INR charges to USD for total calculation
    5. Detention exposure estimate
    
    The result is a complete breakdown that you can present to clients or use
    internally to calculate your margin accurately.
    """
    size = "20" if "20" in container_size else "40"
    
    # Base ocean freight
    ocean_freight = rate.rate_20 if size == "20" else rate.rate_40
    
    # Calculate applicable surcharges
    surcharge_total = 0.0
    surcharge_details = []
    
    for sc in rate.surcharges:
        sc_amount = sc.amount_20 if size == "20" else sc.amount_40
        surcharge_total += sc_amount
        surcharge_details.append({
            "name": sc.surcharge_type,
            "amount": sc_amount,
            "currency": "USD"
        })
    
    # Calculate local charges
    local_usd = 0.0
    local_inr = 0.0
    local_details = []
    
    for lc in rate.local_charges:
        # Check if this charge applies
        size_match = (lc.container_size == "ALL" or size in lc.container_size)
        cargo_match = (lc.cargo_type == "ALL" or lc.cargo_type == cargo_type)
        terminal_match = (lc.terminal == "ALL" or lc.terminal == terminal)
        
        if size_match and cargo_match and terminal_match:
            if lc.currency == "USD":
                local_usd += lc.amount
            else:  # INR
                local_inr += lc.amount
            
            local_details.append({
                "name": lc.charge_type,
                "amount": lc.amount,
                "currency": lc.currency,
                "unit": lc.unit
            })
    
    # Convert INR to USD for total calculation
    local_usd_equivalent = local_usd + (local_inr / USD_TO_INR_RATE)
    
    # Calculate per-container and total costs
    cost_per_container = ocean_freight + surcharge_total + local_usd_equivalent
    total_cost = cost_per_container * num_containers
    
    # Apply margin
    sell_price_per_container = cost_per_container * (1 + (DEFAULT_MARGIN_PCT / 100))
    total_sell_price = sell_price_per_container * num_containers
    
    # Calculate margin in absolute terms
    margin_per_container = sell_price_per_container - cost_per_container
    total_margin = margin_per_container * num_containers
    
    return {
        "ocean_freight": ocean_freight,
        "surcharges": surcharge_details,
        "surcharge_total": surcharge_total,
        "local_charges": local_details,
        "local_usd": local_usd,
        "local_inr": local_inr,
        "local_usd_equivalent": local_usd_equivalent,
        "cost_per_container": cost_per_container,
        "total_cost": total_cost,
        "sell_price_per_container": sell_price_per_container,
        "total_sell_price": total_sell_price,
        "margin_per_container": margin_per_container,
        "total_margin": total_margin,
        "margin_percentage": DEFAULT_MARGIN_PCT,
        "num_containers": num_containers,
        "container_size": container_size,
        "cargo_type": cargo_type,
        "terminal": terminal,
        "transit_time": rate.transit_time,
        "routing": rate.routing,
        "carrier": rate.carrier,
        "free_time_days": rate.free_time_days,
        "detention_rate": rate.detention_rate_20 if size == "20" else rate.detention_rate_40,
        "validity_end": rate.validity_end.strftime("%d %b %Y") if rate.validity_end else "Open",
        "days_until_expiry": rate.days_until_expiry()
    }


def get_pricing_recommendation(pol: str, pod: str, all_rates: List[FreightRate],
                               container_size: str = "40HC", cargo_type: str = "GENERAL",
                               num_containers: int = 1) -> PricingRecommendation:
    """
    Generate intelligent pricing recommendation for a route.
    
    This is where commercial decision-making happens. The function doesn't just
    find the cheapest rate - it evaluates all options and recommends based on:
    
    1. Total landed cost (not just ocean freight)
    2. Transit time (faster is worth paying for)
    3. Service quality (direct vs transhipped)
    4. Validity (rate expiring soon is risky)
    5. Carrier reliability (based on your historical experience)
    
    The output is a PricingRecommendation that explains not just what to quote
    but why this is the best option and what alternatives exist.
    """
    recommendation = PricingRecommendation()
    
    # Find all valid rates for this route
    matching_rates = find_rates_for_route(pol, pod, all_rates, check_validity=True)
    
    if not matching_rates:
        # No valid rates found - check if there are expired rates
        expired_rates = find_rates_for_route(pol, pod, all_rates, check_validity=False)
        if expired_rates:
            recommendation.warnings.append(
                f"⚠️ Found {len(expired_rates)} rate(s) for this route but all have expired. "
                "Upload fresh rate sheets to quote this lane."
            )
        else:
            recommendation.warnings.append(
                f"❌ No rates found for {pol} → {pod} in the system. "
                "Check if this is a served trade lane or upload rate sheets."
            )
        return recommendation
    
    # Calculate total costs for all options
    options_with_costs = []
    for rate in matching_rates:
        cost_breakdown = calculate_total_landed_cost(
            rate, container_size, cargo_type, "NSICT", num_containers
        )
        options_with_costs.append({
            "rate": rate,
            "cost_breakdown": cost_breakdown
        })
    
    # Sort by total cost (lowest first)
    options_with_costs.sort(key=lambda x: x["cost_breakdown"]["total_cost"])
    
    # Primary recommendation is the lowest total cost
    best_option = options_with_costs[0]
    recommendation.primary_rate = best_option["rate"]
    recommendation.total_cost_breakdown = best_option["cost_breakdown"]
    
    # Alternative options
    recommendation.alternatives = [opt["rate"] for opt in options_with_costs[1:]]
    
    # Generate recommendation reason
    if len(options_with_costs) == 1:
        recommendation.recommendation_reason = f"Only option available: {best_option['rate'].carrier}"
    else:
        second_best = options_with_costs[1]
        cost_diff = second_best["cost_breakdown"]["total_cost"] - best_option["cost_breakdown"]["total_cost"]
        recommendation.recommendation_reason = (
            f"Lowest total cost: USD {best_option['cost_breakdown']['total_cost']:.2f} "
            f"(saves USD {cost_diff:.2f} vs next best option)"
        )
    
    # Add warnings for expiring rates
    days_left = recommendation.primary_rate.days_until_expiry()
    if days_left <= 3:
        recommendation.warnings.append(
            f"🚨 URGENT: Rate expires in {days_left} day(s). Confirm with carrier before quoting."
        )
    elif days_left <= 7:
        recommendation.warnings.append(
            f"⚠️ Rate expires in {days_left} days. Consider requesting fresh rates."
        )
    
    # Highlight competitive advantages
    if "direct" in recommendation.primary_rate.routing.lower():
        recommendation.competitive_advantages.append("✅ Direct service (no transhipment)")
    
    transit_days = extract_transit_days(recommendation.primary_rate.transit_time)
    if transit_days and transit_days <= 15:
        recommendation.competitive_advantages.append(f"✅ Fast transit: {transit_days} days")
    
    if recommendation.primary_rate.free_time_days >= 10:
        recommendation.competitive_advantages.append(
            f"✅ Generous free time: {recommendation.primary_rate.free_time_days} days"
        )
    
    return recommendation


def extract_transit_days(transit_time_str: str) -> Optional[int]:
    """
    Extract numeric days from transit time strings.
    
    Handles formats like:
    - "12 Days"
    - "20-22 Days"
    - "35 days"
    
    Returns the lower bound if a range is given.
    """
    if not transit_time_str:
        return None
    
    # Pattern: "12 Days" or "20-22 Days"
    match = re.search(r'(\d+)(?:-\d+)?\s*days?', transit_time_str, re.IGNORECASE)
    if match:
        return int(match.group(1))
    return None


def format_pricing_breakdown_for_whatsapp(recommendation: PricingRecommendation, 
                                          pol: str, pod: str) -> str:
    """
    Format the pricing recommendation for WhatsApp display.
    
    This creates the rich, detailed breakdown that makes you look like a
    professional Pricing Manager who knows every component of the cost structure.
    """
    if not recommendation.primary_rate:
        # No rates available
        msg = f"❌ *No Valid Rates Found*\n\n"
        msg += f"Route: {pol} → {pod}\n\n"
        for warning in recommendation.warnings:
            msg += f"{warning}\n"
        return msg
    
    rate = recommendation.primary_rate
    breakdown = recommendation.total_cost_breakdown
    
    msg = f"📊 *Complete Pricing Breakdown*\n"
    msg += f"Route: {pol} → {pod}\n"
    msg += f"Carrier: {rate.carrier}\n"
    msg += f"━━━━━━━━━━━━━━━━━━━━\n\n"
    
    # Ocean freight
    msg += f"🌊 *OCEAN FREIGHT:*\n"
    msg += f"• Container: {breakdown['container_size']}\n"
    msg += f"• Rate: USD {breakdown['ocean_freight']:.2f}\n"
    msg += f"• Route: {breakdown['routing']}\n"
    msg += f"• Transit: {breakdown['transit_time']}\n"
    msg += f"• Valid: {breakdown['validity_end']}\n\n"
    
    # Surcharges
    if breakdown['surcharges']:
        msg += f"💵 *SURCHARGES:*\n"
        for sc in breakdown['surcharges']:
            msg += f"• {sc['name']}: USD {sc['amount']:.2f}\n"
        msg += f"Subtotal: USD {breakdown['surcharge_total']:.2f}\n\n"
    
    # Local charges
    if breakdown['local_charges']:
        msg += f"🏗️ *ORIGIN LOCAL CHARGES ({breakdown['terminal']}):*\n"
        
        # Group by currency
        usd_charges = [lc for lc in breakdown['local_charges'] if lc['currency'] == 'USD']
        inr_charges = [lc for lc in breakdown['local_charges'] if lc['currency'] == 'INR']
        
        if usd_charges:
            for lc in usd_charges:
                msg += f"• {lc['name']}: USD {lc['amount']:.2f}\n"
        
        if inr_charges:
            msg += f"\nINR Charges:\n"
            for lc in inr_charges:
                msg += f"• {lc['name']}: INR {lc['amount']:.2f}\n"
            msg += f"INR Total: INR {breakdown['local_inr']:.2f} (~USD {breakdown['local_inr'] / USD_TO_INR_RATE:.2f})\n"
        
        msg += f"\nLocal Charges Total: USD {breakdown['local_usd_equivalent']:.2f}\n\n"
    
    # Free time and detention
    if breakdown['free_time_days'] > 0:
        msg += f"📅 *FREE TIME & DETENTION:*\n"
        msg += f"• Free Days: {breakdown['free_time_days']}\n"
        msg += f"• Detention: USD {breakdown['detention_rate']:.2f}/day after free time\n\n"
    
    # Total costs
    msg += f"💰 *COST SUMMARY (per container):*\n"
    msg += f"• Cost: USD {breakdown['cost_per_container']:.2f}\n"
    msg += f"• Margin ({breakdown['margin_percentage']:.0f}%): USD {breakdown['margin_per_container']:.2f}\n"
    msg += f"• Sell Price: USD {breakdown['sell_price_per_container']:.2f}\n\n"
    
    if breakdown['num_containers'] > 1:
        msg += f"*Total for {breakdown['num_containers']} containers:*\n"
        msg += f"• Total Sell: USD {breakdown['total_sell_price']:.2f}\n"
        msg += f"• Total Margin: USD {breakdown['total_margin']:.2f}\n\n"
    
    # Warnings
    if recommendation.warnings:
        msg += f"⚠️ *IMPORTANT:*\n"
        for warning in recommendation.warnings:
            msg += f"{warning}\n"
        msg += "\n"
    
    # Competitive advantages
    if recommendation.competitive_advantages:
        msg += f"✨ *ADVANTAGES:*\n"
        for adv in recommendation.competitive_advantages:
            msg += f"{adv}\n"
        msg += "\n"
    
    # Alternative options
    if recommendation.alternatives:
        msg += f"📋 *{len(recommendation.alternatives)} alternative carrier(s) available.*\n"
        msg += f"Reply *COMPARE* to see all options.\n"
    
    return msg


import re
