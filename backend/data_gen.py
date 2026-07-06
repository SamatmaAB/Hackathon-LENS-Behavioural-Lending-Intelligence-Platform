"""
LENS — Synthetic Data Generator
================================
Generates realistic IDBI-style customers + 90 days of transaction history.
Each customer is assigned a hidden "persona" (e.g. gig worker eyeing a home
loan, salaried employee about to buy a car) and their transaction stream is
built so that the PULSE/CLARITY/MOMENT/MATCH engine can genuinely detect it
— nothing here is faked at the scoring layer, only the raw transactions are
synthetic (standing in for the bank's real transaction warehouse).
"""

import random
import sqlite3
from datetime import datetime, timedelta

try:
    from backend import db
except ImportError:
    import db  # type: ignore[no-redef]

FIRST_NAMES = [
    "Aarav", "Vivaan", "Aditya", "Vihaan", "Arjun", "Sai", "Reyansh", "Ayaan",
    "Krishna", "Ishaan", "Ananya", "Diya", "Saanvi", "Aadhya", "Kavya", "Myra",
    "Anika", "Riya", "Pooja", "Neha", "Rohan", "Karthik", "Suresh", "Manoj",
    "Lakshmi", "Priya", "Sneha", "Divya", "Rahul", "Vikram", "Amit", "Sunita",
]
LAST_NAMES = [
    "Sharma", "Verma", "Iyer", "Nair", "Reddy", "Gupta", "Singh", "Rao",
    "Patil", "Joshi", "Mehta", "Kulkarni", "Pillai", "Bhat", "Chowdhury",
    "Desai", "Menon", "Agarwal", "Kumar", "Pandey",
]
CITIES = [
    ("Mumbai", "MH"), ("Bengaluru", "KA"), ("Pune", "MH"), ("Chennai", "TN"),
    ("Hyderabad", "TG"), ("Delhi", "DL"), ("Ahmedabad", "GJ"), ("Jaipur", "RJ"),
    ("Kochi", "KL"), ("Lucknow", "UP"), ("Nagpur", "MH"), ("Indore", "MP"),
]

EMPLOYMENT_TYPES = ["Salaried", "Self-Employed", "Gig Worker", "Freelancer"]

# Persona definitions: each persona biases which of the 14 PULSE triggers
# actually fire in the generated transaction stream, and what the "ground
# truth" loan intent/type is — used later to score MATCH accuracy.
PERSONAS = [
    {
        "name": "home_loan_intent",
        "employment_bias": ["Salaried", "Self-Employed"],
        "true_loan_type": "Home Loan",
        "triggers": ["property_related_payment", "recurring_self_transfer",
                     "bill_payment_consistency", "salary_inflow_clustering"],
    },
    {
        "name": "auto_loan_intent",
        "employment_bias": ["Salaried", "Gig Worker"],
        "true_loan_type": "Auto Loan",
        "triggers": ["auto_dealer_payment", "large_outward_transfer",
                     "recurring_self_transfer"],
    },
    {
        "name": "personal_loan_medical",
        "employment_bias": ["Self-Employed", "Freelancer", "Gig Worker"],
        "true_loan_type": "Personal Loan",
        "triggers": ["medical_large_expense", "overdraft_near_miss",
                     "multiple_income_sources"],
    },
    {
        "name": "personal_loan_wedding",
        "employment_bias": ["Salaried", "Self-Employed"],
        "true_loan_type": "Personal Loan",
        "triggers": ["wedding_season_spike", "large_outward_transfer",
                     "credit_card_full_payment"],
    },
    {
        "name": "personal_loan_education",
        "employment_bias": ["Salaried", "Freelancer"],
        "true_loan_type": "Personal Loan",
        "triggers": ["education_fee_payment", "emi_burden_increase",
                     "bill_payment_consistency"],
    },
    {
        "name": "mortgage_topup_intent",
        "employment_bias": ["Self-Employed", "Salaried"],
        "true_loan_type": "Mortgage",
        "triggers": ["property_related_payment", "emi_burden_increase",
                     "multiple_income_sources", "credit_card_full_payment"],
    },
    {
        "name": "dormant_low_intent",
        "employment_bias": ["Salaried", "Gig Worker", "Freelancer", "Self-Employed"],
        "true_loan_type": "None",
        "triggers": ["wallet_topup_frequency"],
    },
]

REAL_ESTATE_PAYEES = ["Lodha Developers", "Godrej Properties", "DLF Homes",
                       "Sub-Registrar Office", "HDFC Property Escrow", "Brigade Group"]
AUTO_PAYEES = ["Maruti Suzuki Arena", "Tata Motors Showroom", "Hyundai Dealership",
               "Mahindra Auto World", "TVS Motor Showroom"]
EDU_PAYEES = ["DPS School Fees", "VIT Vellore Fees", "Manipal University", "Byju's Tuition"]
MEDICAL_PAYEES = ["Apollo Hospitals", "Fortis Healthcare", "Manipal Hospital", "Star Health Insurance"]
WEDDING_PAYEES = ["Banquet Hall Booking", "Wedding Decor Co", "Jewellery Mart", "Catering Services"]
UTILITY_PAYEES = ["MSEB Electricity", "Bharti Airtel Postpaid", "BSES Delhi", "BWSSB Water"]
GIG_PLATFORMS = ["Swiggy Payout", "Uber Driver Payout", "Zomato Payout", "Urban Company Payout", "Upwork Payment"]


def _rand_date_within(days_back):
    return datetime.now() - timedelta(
        days=random.uniform(0, days_back),
        hours=random.uniform(0, 23),
        minutes=random.uniform(0, 59),
    )


def _gen_transactions(employment_type, persona, base_income):
    """Builds ~90 days of transactions for one customer based on persona."""
    txns = []
    triggers = set(persona["triggers"])

    # --- Income inflows ---
    if employment_type == "Salaried":
        # Clean monthly salary credit, same day, low variance
        for m in range(3):
            amt = base_income * random.uniform(0.98, 1.02)
            txns.append({
                "type": "SALARY_CREDIT", "amount": round(amt, 2),
                "counterparty": "Employer Payroll",
                "days_back": 90 - (m * 30) - random.randint(0, 1),
            })
    else:
        # Non-salaried: irregular multi-source inflows (what CLARITY must reconstruct)
        n_sources = 2 if "multiple_income_sources" in triggers else 1
        sources = random.sample(GIG_PLATFORMS, k=min(n_sources, len(GIG_PLATFORMS)))
        for week in range(13):  # ~13 weeks in 90 days
            for src in sources:
                if random.random() < 0.8:
                    amt = (base_income / 4.3 / len(sources)) * random.uniform(0.6, 1.5)
                    txns.append({
                        "type": "UPI_CREDIT", "amount": round(max(amt, 200), 2),
                        "counterparty": src,
                        "days_back": 90 - (week * 7) - random.randint(0, 6),
                    })

    # --- Recurring self-transfer (financial discipline signal) ---
    if "recurring_self_transfer" in triggers:
        for m in range(3):
            txns.append({
                "type": "IMPS", "amount": round(base_income * random.uniform(0.08, 0.18), 2),
                "counterparty": "Self - RD Account",
                "days_back": 90 - (m * 30) - random.randint(0, 3),
            })

    # --- Property related payment (home loan / mortgage intent) ---
    if "property_related_payment" in triggers:
        txns.append({
            "type": "NEFT", "amount": round(base_income * random.uniform(4, 9), 2),
            "counterparty": random.choice(REAL_ESTATE_PAYEES),
            "days_back": random.randint(1, 30),
        })

    # --- Auto dealer payment ---
    if "auto_dealer_payment" in triggers:
        txns.append({
            "type": "NEFT", "amount": round(random.uniform(50000, 250000), 2),
            "counterparty": random.choice(AUTO_PAYEES),
            "days_back": random.randint(1, 20),
        })

    # --- Large outward transfer (general purchase intent) ---
    if "large_outward_transfer" in triggers:
        txns.append({
            "type": "IMPS", "amount": round(base_income * random.uniform(1.5, 3.5), 2),
            "counterparty": "External Beneficiary",
            "days_back": random.randint(1, 15),
        })

    # --- EMI burden increase ---
    if "emi_burden_increase" in triggers:
        for m in range(2):
            txns.append({
                "type": "EMI_DEBIT", "amount": round(base_income * random.uniform(0.15, 0.25), 2),
                "counterparty": "NBFC EMI Collection",
                "days_back": 60 - (m * 30) - random.randint(0, 3),
            })

    # --- Education fee ---
    if "education_fee_payment" in triggers:
        txns.append({
            "type": "BILL_PAY", "amount": round(random.uniform(40000, 180000), 2),
            "counterparty": random.choice(EDU_PAYEES),
            "days_back": random.randint(1, 25),
        })

    # --- Medical large expense ---
    if "medical_large_expense" in triggers:
        txns.append({
            "type": "IMPS", "amount": round(random.uniform(60000, 220000), 2),
            "counterparty": random.choice(MEDICAL_PAYEES),
            "days_back": random.randint(1, 10),
        })

    # --- Wedding season spike ---
    if "wedding_season_spike" in triggers:
        spike_window = random.randint(1, 20)
        for _ in range(random.randint(4, 7)):
            txns.append({
                "type": "UPI_DEBIT", "amount": round(random.uniform(15000, 90000), 2),
                "counterparty": random.choice(WEDDING_PAYEES),
                "days_back": max(0, spike_window - random.randint(0, 5)),
            })

    # --- Bill payment consistency ---
    if "bill_payment_consistency" in triggers:
        for m in range(3):
            txns.append({
                "type": "BILL_PAY", "amount": round(random.uniform(1500, 6000), 2),
                "counterparty": random.choice(UTILITY_PAYEES),
                "days_back": 90 - (m * 30) - random.randint(0, 2),
            })

    # --- Wallet topup frequency ---
    if "wallet_topup_frequency" in triggers or employment_type in ("Gig Worker", "Freelancer"):
        for _ in range(random.randint(6, 14)):
            txns.append({
                "type": "WALLET_TOPUP", "amount": round(random.uniform(200, 2000), 2),
                "counterparty": "Paytm/PhonePe Wallet",
                "days_back": random.randint(0, 90),
            })

    # --- Overdraft near miss ---
    if "overdraft_near_miss" in triggers:
        for _ in range(random.randint(1, 3)):
            txns.append({
                "type": "UPI_DEBIT", "amount": round(random.uniform(50, 500), 2),
                "counterparty": "ATM/POS — Low Balance Flag",
                "days_back": random.randint(0, 60),
            })

    # --- Credit card full payment ---
    if "credit_card_full_payment" in triggers:
        for m in range(3):
            txns.append({
                "type": "BILL_PAY", "amount": round(random.uniform(8000, 40000), 2),
                "counterparty": "Credit Card Bill — Full Payment",
                "days_back": 90 - (m * 30) - random.randint(0, 2),
            })

    # --- Background noise: everyday spending so the dataset feels real ---
    for _ in range(random.randint(15, 35)):
        txns.append({
            "type": random.choice(["UPI_DEBIT", "UPI_CREDIT", "BILL_PAY"]),
            "amount": round(random.uniform(100, 4000), 2),
            "counterparty": random.choice(
                ["Zomato", "Swiggy", "Amazon Pay", "BigBasket", "Local Kirana",
                 "Friend Settlement", "Netflix", "Electricity Board", "Petrol Pump"]
            ),
            "days_back": random.uniform(0, 90),
        })

    for t in txns:
        t["timestamp"] = (datetime.now() - timedelta(days=t.pop("days_back"))).isoformat()

    txns.sort(key=lambda x: x["timestamp"])
    return txns


def generate_dataset(n_customers=150, seed=None):
    if seed is not None:
        random.seed(seed)

    customers = []
    transactions = []

    for i in range(n_customers):
        cust_id = f"CUST{10000 + i}"
        first, last = random.choice(FIRST_NAMES), random.choice(LAST_NAMES)
        city, state = random.choice(CITIES)
        persona = random.choice(PERSONAS)
        employment_type = random.choice(persona["employment_bias"])
        age = random.randint(23, 55)

        if employment_type == "Salaried":
            base_income = random.uniform(35000, 180000)
        else:
            base_income = random.uniform(25000, 220000)

        declared_income = round(base_income, 2) if employment_type == "Salaried" else None

        customers.append({
            "customer_id": cust_id,
            "name": f"{first} {last}",
            "age": age,
            "city": city,
            "state": state,
            "employment_type": employment_type,
            "declared_income": declared_income,
            "true_monthly_income": round(base_income, 2),
            "true_loan_type": persona["true_loan_type"],
            "persona": persona["name"],
        })

        for t in _gen_transactions(employment_type, persona, base_income):
            t["customer_id"] = cust_id
            transactions.append(t)

    return customers, transactions


SCHEMA = """
CREATE TABLE IF NOT EXISTS customers (
    customer_id TEXT PRIMARY KEY,
    name TEXT, age INTEGER, city TEXT, state TEXT,
    employment_type TEXT, declared_income REAL,
    true_monthly_income REAL, true_loan_type TEXT, persona TEXT
);
CREATE TABLE IF NOT EXISTS transactions (
    txn_id INTEGER PRIMARY KEY AUTOINCREMENT,
    customer_id TEXT, timestamp TEXT, type TEXT,
    amount REAL, counterparty TEXT,
    FOREIGN KEY(customer_id) REFERENCES customers(customer_id)
);
CREATE TABLE IF NOT EXISTS leads (
    customer_id TEXT PRIMARY KEY,
    intent_score REAL, triggers_fired TEXT,
    synthetic_income REAL, income_accuracy_pct REAL,
    predicted_loan_type TEXT, match_correct INTEGER,
    trust_score REAL, tier TEXT,
    outreach_channel TEXT, outreach_window_start TEXT, outreach_window_end TEXT,
    signal_detected_at TEXT, lead_card_generated_at TEXT, hours_to_lead REAL,
    FOREIGN KEY(customer_id) REFERENCES customers(customer_id)
);
"""

POSTGRES_SCHEMA = """
CREATE TABLE IF NOT EXISTS customers (
    customer_id TEXT PRIMARY KEY,
    name TEXT, age INTEGER, city TEXT, state TEXT,
    employment_type TEXT, declared_income DOUBLE PRECISION,
    true_monthly_income DOUBLE PRECISION, true_loan_type TEXT, persona TEXT
);
CREATE TABLE IF NOT EXISTS transactions (
    txn_id SERIAL PRIMARY KEY,
    customer_id TEXT, timestamp TEXT, type TEXT,
    amount DOUBLE PRECISION, counterparty TEXT,
    FOREIGN KEY(customer_id) REFERENCES customers(customer_id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS leads (
    customer_id TEXT PRIMARY KEY,
    intent_score DOUBLE PRECISION, triggers_fired TEXT,
    synthetic_income DOUBLE PRECISION, income_accuracy_pct DOUBLE PRECISION,
    predicted_loan_type TEXT, match_correct INTEGER,
    trust_score DOUBLE PRECISION, tier TEXT,
    outreach_channel TEXT, outreach_window_start TEXT, outreach_window_end TEXT,
    signal_detected_at TEXT, lead_card_generated_at TEXT, hours_to_lead DOUBLE PRECISION,
    FOREIGN KEY(customer_id) REFERENCES customers(customer_id) ON DELETE CASCADE
);
"""


def create_schema(conn):
    db.executescript(conn, POSTGRES_SCHEMA if db.IS_POSTGRES else SCHEMA)


def clear_customer_data(conn):
    if db.IS_POSTGRES:
        db.executescript(conn, "DELETE FROM leads; DELETE FROM transactions; DELETE FROM customers;")
    else:
        conn.executescript("DELETE FROM leads; DELETE FROM transactions; DELETE FROM customers;")


def insert_dataset(conn, customers, transactions):
    db.executemany(
        conn,
        """INSERT INTO customers (customer_id, name, age, city, state, employment_type,
           declared_income, true_monthly_income, true_loan_type, persona)
           VALUES (?,?,?,?,?,?,?,?,?,?)""",
        [
            (
                c["customer_id"], c["name"], c["age"], c["city"], c["state"], c["employment_type"],
                c["declared_income"], c["true_monthly_income"], c["true_loan_type"], c["persona"],
            )
            for c in customers
        ],
    )
    db.executemany(
        conn,
        """INSERT INTO transactions (customer_id, timestamp, type, amount, counterparty)
           VALUES (?,?,?,?,?)""",
        [
            (t["customer_id"], t["timestamp"], t["type"], t["amount"], t["counterparty"])
            for t in transactions
        ],
    )


def build_current_database(n_customers=150, seed=42, db_path=None):
    conn = db.connect(db_path)
    create_schema(conn)
    clear_customer_data(conn)
    customers, transactions = generate_dataset(n_customers, seed=seed)
    insert_dataset(conn, customers, transactions)
    conn.commit()
    conn.close()
    return len(customers), len(transactions)


def build_database(db_path, n_customers=150, seed=42):
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    cur.executescript("DROP TABLE IF EXISTS leads; DROP TABLE IF EXISTS transactions; DROP TABLE IF EXISTS customers;")
    cur.executescript(SCHEMA)

    customers, transactions = generate_dataset(n_customers, seed=seed)

    cur.executemany(
        """INSERT INTO customers (customer_id, name, age, city, state, employment_type,
           declared_income, true_monthly_income, true_loan_type, persona)
           VALUES (:customer_id, :name, :age, :city, :state, :employment_type,
           :declared_income, :true_monthly_income, :true_loan_type, :persona)""",
        customers,
    )
    cur.executemany(
        """INSERT INTO transactions (customer_id, timestamp, type, amount, counterparty)
           VALUES (:customer_id, :timestamp, :type, :amount, :counterparty)""",
        transactions,
    )
    conn.commit()
    conn.close()
    return len(customers), len(transactions)


if __name__ == "__main__":
    n_cust, n_txn = build_database("lens.db", n_customers=150, seed=42)
    print(f"Generated {n_cust} customers and {n_txn} transactions -> lens.db")