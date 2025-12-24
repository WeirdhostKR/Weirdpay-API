from __future__ import annotations
from fastapi import FastAPI, Request, Depends, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware
import uvicorn
from datetime import datetime
import json
import re
import sqlite3
import requests
import time
from typing import Optional, Dict, Any
import os
from pathlib import Path
from dotenv import load_dotenv
import secrets
load_dotenv()


app = FastAPI(title="WHMCS Invoice Payment Matcher")

# Session configuration
SECRET_KEY = os.getenv("SECRET_KEY", secrets.token_urlsafe(32))
app.add_middleware(SessionMiddleware, secret_key=SECRET_KEY)

# Mount static files
app.mount("/static", StaticFiles(directory="static"), name="static")

# Configuration
WHMCS_URL = "https://weirdhost.xyz/whmcs/includes/api.php"
WHMCS_USERNAME = os.getenv("WHMCS_USERNAME", "YOUR_USERNAME")
WHMCS_PASSWORD = os.getenv("WHMCS_PASSWORD", "YOUR_PASSWORD")
DB_PATH = "invoice_payments.db"

# Admin credentials for dashboard login
ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "admin123")

# ============================================================================
# DATABASE SETUP
# ============================================================================

def init_database():
    """Initialize SQLite database with required tables"""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    # Table for all incoming SMS transactions
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS transactions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            received_at TEXT NOT NULL,
            raw_sms_text TEXT,
            amount INTEGER,
            depositor_raw TEXT,
            depositor_name TEXT,
            invoice_id INTEGER,
            status TEXT NOT NULL,
            match_result TEXT,
            whmcs_response TEXT,
            whmcs_userid INTEGER,
            client_name TEXT,
            client_email TEXT,
            order_id INTEGER,
            order_accepted BOOLEAN DEFAULT 0,
            order_error TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    
    # Table for successfully matched and paid invoices
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS matched_payments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            transaction_id INTEGER,
            invoice_id INTEGER NOT NULL,
            amount INTEGER NOT NULL,
            depositor_name TEXT,
            whmcs_invoice_total TEXT,
            whmcs_userid INTEGER,
            client_name TEXT,
            client_email TEXT,
            order_id INTEGER,
            order_accepted BOOLEAN DEFAULT 0,
            order_error TEXT,
            matched_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (transaction_id) REFERENCES transactions(id)
        )
    """)
    
    # Table for unmatched deposits requiring manual review
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS unmatched_deposits (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            transaction_id INTEGER,
            reason TEXT NOT NULL,
            amount INTEGER,
            invoice_id INTEGER,
            depositor_raw TEXT,
            needs_review BOOLEAN DEFAULT 1,
            reviewed_at TIMESTAMP,
            notes TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (transaction_id) REFERENCES transactions(id)
        )
    """)
    
    conn.commit()
    conn.close()
    print("✓ Database initialized")


# ============================================================================
# WHMCS API CLIENT
# ============================================================================

class WHMCSClient:
    def __init__(self, url: str, username: str, password: str):
        self.url = url
        self.username = username
        self.password = password
    
    def _make_request(self, action: str, params: Dict[str, Any]) -> Dict[str, Any]:
        """Make API request to WHMCS"""
        data = {
            'action': action,
            'username': self.username,
            'password': self.password,
            'responsetype': 'json',
            **params
        }
        
        try:
            response = requests.post(self.url, data=data, timeout=10)
            response.raise_for_status()
            return response.json()
        except requests.RequestException as e:
            print(f"❌ WHMCS API error: {e}")
            return {"result": "error", "message": str(e)}
    
    def get_invoice(self, invoice_id: int) -> Optional[Dict[str, Any]]:
        """Get invoice details from WHMCS"""
        result = self._make_request('GetInvoice', {'invoiceid': invoice_id})
        
        if result.get('result') == 'success':
            return result
        else:
            print(f"❌ Failed to get invoice {invoice_id}: {result.get('message', 'Unknown error')}")
            return None
    
    def update_invoice_status(self, invoice_id: int, status: str = 'Paid') -> bool:
        """Update invoice status in WHMCS"""
        result = self._make_request('UpdateInvoice', {
            'invoiceid': invoice_id,
            'status': status
        })
        
        if result.get('result') == 'success':
            print(f"✓ Invoice {invoice_id} marked as {status}")
            return True
        else:
            print(f"❌ Failed to update invoice {invoice_id}: {result.get('message', 'Unknown error')}")
            return False
    
    def add_invoice_payment(self, invoice_id: int, amount: float, transid: str, 
                           gateway: str, date: Optional[str] = None) -> tuple[bool, Optional[str]]:
        """
        Add payment to a given invoice - this triggers WHMCS automation
        Returns (success, error_message)
        """
        params = {
            "invoiceid": invoice_id,
            "amount": amount,
            "transid": transid,
            "gateway": gateway,
        }
        if date:
            params["date"] = date  # Y-m-d format
        
        result = self._make_request("AddInvoicePayment", params)
        
        if result.get("result") == "success":
            print(f"✓ Payment of {amount}원 added to invoice {invoice_id} (Transaction ID: {transid})")
            return True, None
        else:
            error_msg = result.get("message", "Unknown error")
            print(f"❌ Failed to add payment to invoice {invoice_id}: {error_msg}")
            return False, error_msg
    
    def get_pending_orders(self, userid: int) -> Optional[list]:
        """Get pending orders for a user"""
        result = self._make_request('GetOrders', {
            'userid': userid,
            'status': 'Pending'
        })
        
        if result.get('result') == 'success':
            orders = result.get('orders', {}).get('order', [])
            # Ensure it's always a list (API returns dict for single item)
            if isinstance(orders, dict):
                orders = [orders]
            print(f"✓ Found {len(orders)} pending order(s) for user {userid}")
            return orders
        else:
            print(f"❌ Failed to get orders for user {userid}: {result.get('message', 'Unknown error')}")
            return None
    
    def accept_order(self, order_id: int) -> tuple[bool, Optional[str]]:
        """Accept an order in WHMCS. Returns (success, error_message)"""
        result = self._make_request('AcceptOrder', {
            'orderid': order_id,
            'autosetup': True,
            'sendemail': True
        })
        
        if result.get('result') == 'success':
            print(f"✓ Order {order_id} accepted successfully")
            return True, None
        else:
            error_msg = result.get('message', 'Unknown error')
            print(f"❌ Failed to accept order {order_id}: {error_msg}")
            return False, error_msg
    
    def get_client_details(self, clientid: int) -> Optional[Dict[str, Any]]:
        """Get client details from WHMCS"""
        result = self._make_request('GetClientsDetails', {
            'clientid': clientid,
            'stats': False
        })
        
        if result.get('result') == 'success':
            client = result.get('client', {})
            print(f"✓ Retrieved client details for {client.get('fullname', 'Unknown')}")
            return client
        else:
            print(f"❌ Failed to get client {clientid}: {result.get('message', 'Unknown error')}")
            return None


# ============================================================================
# SMS PARSING
# ============================================================================

def parse_bank_sms(raw_text: str) -> Dict[str, Any]:
    """
    Parse Hana Bank SMS format
    Returns: amount, depositor_raw, name, invoice_id
    """
    amount = None
    depositor_raw = None
    name = None
    invoice_id = None
    
    lines = raw_text.splitlines()
    
    for i, line in enumerate(lines):
        # Look for 입금 line
        if "입금" in line:
            # Parse amount (e.g., "입금10원" or "입금 10,000원")
            amt_match = re.search(r"입금\s*([\d,]+)원", line)
            if amt_match:
                try:
                    amount = int(amt_match.group(1).replace(",", ""))
                except ValueError:
                    amount = None
            
            # Next line should be depositor info
            if i + 1 < len(lines):
                depositor_raw = lines[i + 1].strip()
            break
    
    # Flexible depositor line parsing
    if depositor_raw:
        d = depositor_raw.strip()
        
        # CASE 1: Name + invoice (e.g., 홍길동1234)
        mix_match = re.match(r"^([가-힣A-Za-z]+)(\d{1,6})$", d)
        if mix_match:
            name = mix_match.group(1)
            try:
                invoice_id = int(mix_match.group(2))
            except:
                invoice_id = None
        
        # CASE 2: Only Korean/English name (e.g., 홍길동)
        elif re.match(r"^[가-힣A-Za-z]+$", d):
            name = d
            invoice_id = None
        
        # CASE 3: Only numbers (e.g., 1234)
        elif re.match(r"^\d{1,6}$", d):
            name = None
            invoice_id = int(d)
        
        # CASE 4: Unknown format
        else:
            name = None
            invoice_id = None
    
    return {
        "amount": amount,
        "depositor_raw": depositor_raw,
        "name": name,
        "invoice_id": invoice_id
    }


# ============================================================================
# INVOICE MATCHING LOGIC
# ============================================================================

def process_payment(parsed_data: Dict[str, Any], raw_text: str, whmcs_client: WHMCSClient) -> Dict[str, Any]:
    """
    Main payment processing logic:
    1. Check if invoice ID exists
    2. Verify invoice is unpaid
    3. Match amount
    4. Mark as paid if everything matches
    5. Check for pending orders and accept them
    6. Get client details for logging
    """
    amount = parsed_data.get("amount")
    invoice_id = parsed_data.get("invoice_id")
    name = parsed_data.get("name")
    
    result = {
        "status": "unmatched",
        "reason": None,
        "whmcs_data": None,
        "action_taken": None,
        "client_data": None,
        "order_data": None
    }
    
    # Validation checks
    if not invoice_id:
        result["reason"] = "No invoice ID found in SMS"
        return result
    
    if not amount:
        result["reason"] = "No amount found in SMS"
        return result
    
    # Get invoice from WHMCS
    print(f"🔍 Checking invoice #{invoice_id}...")
    invoice_data = whmcs_client.get_invoice(invoice_id)
    
    if not invoice_data:
        result["reason"] = f"Invoice #{invoice_id} not found in WHMCS"
        return result
    
    result["whmcs_data"] = invoice_data
    userid = invoice_data.get("userid")
    
    # Check if invoice is unpaid
    invoice_status = invoice_data.get("status", "").lower()
    if invoice_status != "unpaid":
        result["reason"] = f"Invoice #{invoice_id} status is '{invoice_status}' (not unpaid)"
        return result
    
    # Match amount
    invoice_total = float(invoice_data.get("total", 0))
    invoice_total_krw = int(invoice_total)  # Assuming KRW, convert to integer
    
    if amount != invoice_total_krw:
        result["reason"] = f"Amount mismatch: Received {amount}원 but invoice total is {invoice_total_krw}원"
        return result
    
    # Everything matches! Mark invoice as paid
    print(f"✅ Perfect match! Invoice #{invoice_id}, Amount: {amount}원")
    
    # Create unique transaction ID for this payment
    transid = f"HANA-{invoice_id}-{amount}-{int(time.time())}"
    
    # Add payment to WHMCS - this triggers automation (service date updates, etc.)
    success, error_msg = whmcs_client.add_invoice_payment(
        invoice_id=invoice_id,
        amount=float(invoice_total),  # Use exact invoice total from WHMCS
        transid=transid,
        gateway="banktransfer",  # Must match WHMCS system name for your payment gateway
        date=datetime.now().strftime("%Y-%m-%d")
    )
    
    if not success:
        result["status"] = "match_failed_to_update"
        result["reason"] = f"Failed to add invoice payment in WHMCS: {error_msg}"
        return result
    
    actions_taken = [f"Payment recorded for invoice #{invoice_id} (Transaction ID: {transid})"]
    
    # Get client details
    if userid:
        print(f"👤 Fetching client details for user {userid}...")
        client_data = whmcs_client.get_client_details(userid)
        if client_data:
            result["client_data"] = client_data
            actions_taken.append(f"Client: {client_data.get('fullname', 'Unknown')} ({client_data.get('email', 'N/A')})")
    
    # Check for pending orders
    if userid:
        print(f"📦 Checking for pending orders for user {userid}...")
        pending_orders = whmcs_client.get_pending_orders(userid)
        
        if pending_orders:
            # Find order that matches this invoice
            matched_order = None
            for order in pending_orders:
                if int(order.get('invoiceid', 0)) == invoice_id:
                    matched_order = order
                    break
            
            if matched_order:
                order_id = int(matched_order.get('id'))
                print(f"🎯 Found matching pending order #{order_id}!")
                
                # Accept the order
                order_accepted, order_error = whmcs_client.accept_order(order_id)
                
                if order_accepted:
                    result["order_data"] = {
                        "order_id": order_id,
                        "accepted": True,
                        "error": None,
                        "order_details": matched_order
                    }
                    actions_taken.append(f"Order #{order_id} accepted and provisioned")
                    print(f"✅ Order #{order_id} accepted successfully!")
                else:
                    result["order_data"] = {
                        "order_id": order_id,
                        "accepted": False,
                        "error": order_error,
                        "order_details": matched_order
                    }
                    actions_taken.append(f"Order #{order_id} found but failed to accept: {order_error}")
                    print(f"⚠️  Order #{order_id} failed: {order_error}")
            else:
                print(f"ℹ️  No pending order found matching invoice #{invoice_id}")
        else:
            print(f"ℹ️  No pending orders for user {userid}")
    
    result["status"] = "matched_and_paid"
    result["action_taken"] = " | ".join(actions_taken)
    
    return result


# ============================================================================
# DATABASE OPERATIONS
# ============================================================================

def log_transaction(raw_text: str, parsed_data: Dict[str, Any], 
                   match_result: Dict[str, Any]) -> int:
    """Log transaction to database and return transaction ID"""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    # Extract client and order data
    client_data = match_result.get("client_data", {})
    order_data = match_result.get("order_data", {})
    whmcs_data = match_result.get("whmcs_data", {})
    
    cursor.execute("""
        INSERT INTO transactions 
        (received_at, raw_sms_text, amount, depositor_raw, depositor_name, 
         invoice_id, status, match_result, whmcs_response, whmcs_userid,
         client_name, client_email, order_id, order_accepted, order_error)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        datetime.now().isoformat(),
        raw_text,
        parsed_data.get("amount"),
        parsed_data.get("depositor_raw"),
        parsed_data.get("name"),
        parsed_data.get("invoice_id"),
        match_result.get("status"),
        match_result.get("reason") or match_result.get("action_taken"),
        json.dumps(whmcs_data) if whmcs_data else None,
        whmcs_data.get("userid") if whmcs_data else None,
        client_data.get("fullname") if client_data else None,
        client_data.get("email") if client_data else None,
        order_data.get("order_id") if order_data else None,
        order_data.get("accepted", False) if order_data else False,
        order_data.get("error") if order_data else None
    ))
    
    transaction_id = cursor.lastrowid
    conn.commit()
    conn.close()
    
    return transaction_id


def log_matched_payment(transaction_id: int, invoice_id: int, amount: int, 
                       name: Optional[str], match_result: Dict[str, Any]):
    """Log successfully matched payment"""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    whmcs_data = match_result.get("whmcs_data", {})
    client_data = match_result.get("client_data", {})
    order_data = match_result.get("order_data", {})
    
    cursor.execute("""
        INSERT INTO matched_payments 
        (transaction_id, invoice_id, amount, depositor_name, 
         whmcs_invoice_total, whmcs_userid, client_name, client_email,
         order_id, order_accepted, order_error)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        transaction_id,
        invoice_id,
        amount,
        name,
        whmcs_data.get("total"),
        whmcs_data.get("userid"),
        client_data.get("fullname") if client_data else None,
        client_data.get("email") if client_data else None,
        order_data.get("order_id") if order_data else None,
        order_data.get("accepted", False) if order_data else False,
        order_data.get("error") if order_data else None
    ))
    
    conn.commit()
    conn.close()


def log_unmatched_deposit(transaction_id: int, reason: str, 
                         parsed_data: Dict[str, Any]):
    """Log unmatched deposit for manual review"""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    cursor.execute("""
        INSERT INTO unmatched_deposits 
        (transaction_id, reason, amount, invoice_id, depositor_raw)
        VALUES (?, ?, ?, ?, ?)
    """, (
        transaction_id,
        reason,
        parsed_data.get("amount"),
        parsed_data.get("invoice_id"),
        parsed_data.get("depositor_raw")
    ))
    
    conn.commit()
    conn.close()


# ============================================================================
# AUTHENTICATION
# ============================================================================

def get_current_user(request: Request) -> Optional[str]:
    """Get current logged-in user from session"""
    return request.session.get("user")


def require_auth(request: Request) -> str:
    """Dependency that requires authentication"""
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return user


# ============================================================================
# API ENDPOINTS
# ============================================================================

@app.on_event("startup")
async def startup_event():
    """Initialize database on startup"""
    init_database()


@app.post("/webhook/bank-sms")
async def receive_bank_sms(request: Request):
    """
    Main webhook endpoint for receiving bank SMS from Tasker
    Handles the entire payment matching workflow
    """
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    
    # Read raw SMS text
    raw_body = await request.body()
    raw_text = raw_body.decode("utf-8", errors="replace") if raw_body else ""
    
    print("\n" + "="*60)
    print(f"📱 NEW SMS RECEIVED at {now}")
    print("="*60)
    print(raw_text)
    print("-"*60)
    
    # Parse SMS
    parsed_data = parse_bank_sms(raw_text)
    print(f"💰 Amount: {parsed_data['amount']}원")
    print(f"👤 Depositor: {parsed_data['depositor_raw']}")
    print(f"📄 Invoice ID: {parsed_data['invoice_id']}")
    print(f"✏️  Name: {parsed_data['name']}")
    print("-"*60)
    
    # Initialize WHMCS client
    whmcs_client = WHMCSClient(WHMCS_URL, WHMCS_USERNAME, WHMCS_PASSWORD)
    
    # Process payment and match with invoice
    match_result = process_payment(parsed_data, raw_text, whmcs_client)
    
    print(f"📊 Status: {match_result['status']}")
    if match_result.get('reason'):
        print(f"ℹ️  Reason: {match_result['reason']}")
    if match_result.get('action_taken'):
        print(f"✅ Action: {match_result['action_taken']}")
    
    # Display client info
    if match_result.get('client_data'):
        client = match_result['client_data']
        print(f"👤 Client: {client.get('fullname', 'Unknown')} ({client.get('email', 'N/A')}) [ID: {client.get('userid', 'N/A')}]")
    
    # Display order info
    if match_result.get('order_data'):
        order = match_result['order_data']
        if order.get('accepted'):
            print(f"📦 Order: #{order.get('order_id')} ✅ ACCEPTED & PROVISIONED")
        else:
            error_msg = order.get('error', 'Unknown error')
            print(f"📦 Order: #{order.get('order_id')} ❌ FAILED: {error_msg}")
    
    print("="*60 + "\n")
    
    # Log to database
    transaction_id = log_transaction(raw_text, parsed_data, match_result)
    
    # Additional logging based on result
    if match_result["status"] == "matched_and_paid":
        log_matched_payment(
            transaction_id,
            parsed_data["invoice_id"],
            parsed_data["amount"],
            parsed_data["name"],
            match_result
        )
    else:
        log_unmatched_deposit(
            transaction_id,
            match_result.get("reason", "Unknown error"),
            parsed_data
        )
    
    # Return response
    return {
        "status": "received",
        "transaction_id": transaction_id,
        "received_at": now,
        "parsed": parsed_data,
        "match_result": {
            "status": match_result["status"],
            "reason": match_result.get("reason"),
            "action_taken": match_result.get("action_taken")
        }
    }


@app.post("/api/login")
async def login(request: Request):
    """Handle login requests"""
    try:
        data = await request.json()
        username = data.get("username", "").strip()
        password = data.get("password", "")

        # Validate credentials
        if username == ADMIN_USERNAME and password == ADMIN_PASSWORD:
            # Set session
            request.session["user"] = username
            return {"success": True, "message": "Login successful"}
        else:
            return JSONResponse(
                status_code=401,
                content={"success": False, "message": "Invalid username or password"}
            )
    except Exception as e:
        print(f"Login error: {e}")
        return JSONResponse(
            status_code=500,
            content={"success": False, "message": "An error occurred"}
        )


@app.post("/api/logout")
async def logout(request: Request):
    """Handle logout requests"""
    request.session.clear()
    return {"success": True, "message": "Logged out successfully"}


@app.get("/api/dashboard/stats")
async def get_dashboard_stats(user: str = Depends(require_auth)):
    """Get dashboard statistics"""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    # Total transactions
    cursor.execute("SELECT COUNT(*) FROM transactions")
    total_transactions = cursor.fetchone()[0]
    
    # Matched payments
    cursor.execute("SELECT COUNT(*) FROM matched_payments")
    matched_count = cursor.fetchone()[0]
    
    # Accepted orders
    cursor.execute("SELECT COUNT(*) FROM matched_payments WHERE order_accepted = 1")
    accepted_orders = cursor.fetchone()[0]
    
    # Unmatched deposits (needing review)
    cursor.execute("SELECT COUNT(*) FROM unmatched_deposits WHERE needs_review = 1")
    unmatched_count = cursor.fetchone()[0]
    
    # Total amount matched today
    cursor.execute("""
        SELECT COALESCE(SUM(amount), 0) 
        FROM matched_payments 
        WHERE DATE(matched_at) = DATE('now')
    """)
    today_amount = cursor.fetchone()[0]
    
    conn.close()
    
    return {
        "total_transactions": total_transactions,
        "matched_payments": matched_count,
        "accepted_orders": accepted_orders,
        "unmatched_deposits": unmatched_count,
        "today_matched_amount": today_amount
    }


@app.get("/api/transactions/recent")
async def get_recent_transactions(limit: int = 20, user: str = Depends(require_auth)):
    """Get recent transactions"""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    
    cursor.execute("""
        SELECT * FROM transactions 
        ORDER BY created_at DESC 
        LIMIT ?
    """, (limit,))
    
    rows = cursor.fetchall()
    conn.close()
    
    return [dict(row) for row in rows]


@app.get("/api/matched-payments")
async def get_matched_payments(limit: int = 50, user: str = Depends(require_auth)):
    """Get matched and paid invoices"""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    
    cursor.execute("""
        SELECT * FROM matched_payments 
        ORDER BY matched_at DESC 
        LIMIT ?
    """, (limit,))
    
    rows = cursor.fetchall()
    conn.close()
    
    return [dict(row) for row in rows]


@app.get("/api/unmatched-deposits")
async def get_unmatched_deposits(user: str = Depends(require_auth)):
    """Get unmatched deposits needing review"""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    
    cursor.execute("""
        SELECT u.*, t.raw_sms_text, t.received_at
        FROM unmatched_deposits u
        JOIN transactions t ON u.transaction_id = t.id
        WHERE u.needs_review = 1
        ORDER BY u.created_at DESC
    """)
    
    rows = cursor.fetchall()
    conn.close()
    
    return [dict(row) for row in rows]


@app.post("/api/unmatched-deposits/{deposit_id}/resolve")
async def resolve_unmatched_deposit(deposit_id: int, notes: str = "", user: str = Depends(require_auth)):
    """Mark an unmatched deposit as reviewed"""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    cursor.execute("""
        UPDATE unmatched_deposits 
        SET needs_review = 0, reviewed_at = ?, notes = ?
        WHERE id = ?
    """, (datetime.now().isoformat(), notes, deposit_id))
    
    conn.commit()
    conn.close()
    
    return {"status": "resolved", "deposit_id": deposit_id}


# ============================================================================
# WEB DASHBOARD
# ============================================================================

@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    """Login page"""
    # If already logged in, redirect to dashboard
    if get_current_user(request):
        return RedirectResponse(url="/", status_code=302)

    html_file = Path("static/login.html")
    if html_file.exists():
        return html_file.read_text()
    else:
        return HTMLResponse(
            content="<h1>Login page not found</h1>",
            status_code=404
        )


@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    """Main dashboard page"""
    # Check if user is authenticated
    if not get_current_user(request):
        return RedirectResponse(url="/login", status_code=302)

    html_file = Path("static/index.html")
    if html_file.exists():
        return html_file.read_text()
    else:
        return HTMLResponse(
            content="<h1>Dashboard not found</h1><p>Please ensure static/index.html exists.</p>",
            status_code=404
        )


# ============================================================================
# MAIN
# ============================================================================

if __name__ == "__main__":
    print("\n" + "="*60)
    print("🚀 Invoice Payment Matching System")
    print("="*60)
    print(f"📍 WHMCS URL: {WHMCS_URL}")
    print(f"🔑 Username: {WHMCS_USERNAME}")
    print(f"💾 Database: {DB_PATH}")
    print("="*60 + "\n")
    
    uvicorn.run(
        "clerk:app",
        host="0.0.0.0",
        port=8000,
        reload=False
    )
