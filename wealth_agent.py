import sys
import os
import glob
import json
import re
import hashlib
import csv
import subprocess
import webbrowser
from datetime import datetime
from typing import List, Dict, Any, Optional

import pandas as pd
import markdown

try:
    import yfinance as yf
except ImportError:
    yf = None

from pypdf import PdfReader

try:
    from PIL import Image, ImageEnhance
except ImportError:
    Image, ImageEnhance = None, None

from langchain_openai import ChatOpenAI
from langchain_core.messages import SystemMessage, HumanMessage
from langchain_core.tools import tool
from rich.console import Console
from rich.markdown import Markdown as RichMarkdown

try:
    import pytesseract
    default_win_tesseract = r"C:\Program Files\Tesseract-OCR\tesseract.exe"
    if os.path.exists(default_win_tesseract):
        pytesseract.pytesseract.tesseract_cmd = default_win_tesseract
except ImportError:
    pytesseract = None

console = Console()

# --- Initialize Local LLM via LM Studio ---
llm = ChatOpenAI(
    base_url=os.getenv("LLM_BASE_URL", "http://localhost:1234/v1"),
    api_key=os.getenv("LLM_API_KEY", "lm-studio"),
    model=os.getenv("LLM_MODEL", "local-model"),
    temperature=0.1,
    max_tokens=5500,
    frequency_penalty=0.3,
    presence_penalty=0.2
)

FILE_CACHE_PATH = ".agent_file_cache.json"
SCALABLE_ACCOUNTS_FILE = ".scalable_accounts.json"
COMPLETED_ACTIONS_FILE = ".completed_actions.json"
PENDING_ACTIONS_FILE = ".pending_actions.json"
SCALABLE_LOGIN_URL = os.getenv("SCALABLE_LOGIN_URL", "https://de.scalable.capital/en/login")

# ==========================================
# 1. LOCAL PERSISTENT CACHE & ACTION MEMORY
# ==========================================

def calculate_file_md5(file_path: str, chunk_size: int = 65536) -> str:
    hasher = hashlib.md5()
    try:
        with open(file_path, "rb") as f:
            while chunk := f.read(chunk_size):
                hasher.update(chunk)
        return hasher.hexdigest()
    except Exception:
        return ""

def load_file_cache() -> Dict[str, Any]:
    if os.path.exists(FILE_CACHE_PATH):
        try:
            with open(FILE_CACHE_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}

def save_file_cache(cache: Dict[str, Any]) -> None:
    try:
        with open(FILE_CACHE_PATH, "w", encoding="utf-8") as f:
            json.dump(cache, f, indent=2)
    except Exception as e:
        console.print(f"[dim red]Warning: Could not write cache file: {e}[/dim red]")

def load_completed_actions() -> List[str]:
    if os.path.exists(COMPLETED_ACTIONS_FILE):
        try:
            with open(COMPLETED_ACTIONS_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return []
    return []

def save_completed_actions(actions: List[str]) -> None:
    try:
        with open(COMPLETED_ACTIONS_FILE, "w", encoding="utf-8") as f:
            json.dump(actions, f, indent=2)
    except Exception as e:
        console.print(f"[dim red]Warning: Could not save completed actions: {e}[/dim red]")

def save_pending_actions(action_texts: List[str]) -> None:
    try:
        with open(PENDING_ACTIONS_FILE, "w", encoding="utf-8") as f:
            json.dump(action_texts, f, indent=2)
    except Exception as e:
        console.print(f"[dim red]Warning: Could not save pending actions: {e}[/dim red]")

def load_pending_actions() -> List[str]:
    if os.path.exists(PENDING_ACTIONS_FILE):
        try:
            with open(PENDING_ACTIONS_FILE, "r", encoding="utf-8") as f:
                acts = json.load(f)
                if acts:
                    return acts
        except Exception:
            pass

    report_path = "agent_portfolio_report.html"
    if os.path.exists(report_path):
        try:
            with open(report_path, "r", encoding="utf-8") as f:
                content = f.read()
            matches = re.findall(r"(?:<h[34]>Action\s*\d+:?\s*|####?\s*Action\s*\d+:?\s*)([^<>\n]+)", content, re.IGNORECASE)
            if matches:
                return [m.strip() for m in matches if len(m.strip()) > 3]
        except Exception:
            pass
    return []

def extract_action_titles_from_strategy(strategy_text: str) -> List[str]:
    matches = re.findall(r"(?:####?\s*Action\s*\d+:?\s*)([^\n\r]+)", strategy_text, re.IGNORECASE)
    if not matches:
        matches = re.findall(r"(?:\d+\.\s*\*\*([^*]+)\*\*)", strategy_text)
    return [m.strip() for m in matches if len(m.strip()) > 3]

def clean_german_amount(raw_val: Any) -> Optional[float]:
    if raw_val is None:
        return None
    if isinstance(raw_val, (int, float)):
        if pd.isna(raw_val):
            return None
        return float(raw_val)
    s = str(raw_val).replace("€", "").replace("EUR", "").replace("$", "").replace("USD", "").strip()
    if s.lower() in ["nan", "none", "", "null", "nat", "--"]:
        return None
    if "," in s and ("." in s and s.rfind(",") > s.rfind(".")):
        s = s.replace(".", "").replace(",", ".")
    elif "," in s and "." not in s:
        s = s.replace(",", ".")
    try:
        f = float(re.sub(r"[^\d.-]", "", s))
        return None if pd.isna(f) else f
    except ValueError:
        return None

def safe_json_loads(raw_str: str) -> Optional[Dict[str, Any]]:
    if not raw_str:
        return None
    cleaned = raw_str.strip()
    if "<think>" in cleaned and "</think>" in cleaned:
        cleaned = cleaned.split("</think>")[1].strip()
    elif "<think>" in cleaned:
        cleaned = re.sub(r"<think>.*?</think>", "", cleaned, flags=re.DOTALL).strip()

    if "```json" in cleaned:
        cleaned = cleaned.split("```json")[1].split("```")[0].strip()
    elif "```" in cleaned:
        cleaned = cleaned.split("```")[1].split("```")[0].strip()

    cleaned = re.sub(r'//.*', '', cleaned)
    cleaned = re.sub(r'/\*.*?\*/', '', cleaned, flags=re.DOTALL)
    cleaned = re.sub(r',\s*([\]}])', r'\1', cleaned)

    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        return None

# ==========================================
# 2. SCALABLE MULTI-ACCOUNT & PROFILE MANAGER
# ==========================================

def get_profile_env_cmd(profile_name: str, base_cmd: List[str]) -> List[str]:
    safe_profile = re.sub(r"[^a-zA-Z0-9_]", "_", profile_name.lower())
    profile_home = f"$HOME/.scalable_profiles/{safe_profile}"
    config_dir_cli = f"{profile_home}/.config/scalable-cli"
    config_file_cli = f"{config_dir_cli}/config.toml"
    
    setup_config_script = (
        f"mkdir -p {config_dir_cli} && "
        f"if [ ! -f {config_file_cli} ] || ! grep -q 'session_backend' {config_file_cli} 2>/dev/null; then "
        f"  printf '[auth]\\nsession_backend = \"file\"\\n' > {config_file_cli}; "
        f"fi && "
        f"HOME={profile_home} {' '.join(base_cmd)}"
    )
    return ["wsl", "-d", "Ubuntu", "bash", "-c", setup_config_script]

def load_scalable_account_registry() -> List[Dict[str, str]]:
    if os.path.exists(SCALABLE_ACCOUNTS_FILE):
        try:
            with open(SCALABLE_ACCOUNTS_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return []
    return []

def save_scalable_account_registry(accounts: List[Dict[str, str]]) -> None:
    try:
        with open(SCALABLE_ACCOUNTS_FILE, "w", encoding="utf-8") as f:
            json.dump(accounts, f, indent=2)
    except Exception as e:
        console.print(f"[dim red]Warning: Could not save Scalable accounts: {e}[/dim red]")

def check_account_session(profile_name: str) -> Optional[Dict[str, str]]:
    cmd = get_profile_env_cmd(profile_name, ["sc", "broker", "holdings", "--json"])
    profile_cmd = get_profile_env_cmd(profile_name, ["sc", "broker", "profile", "--json"])
    
    account_id = None
    try:
        res = subprocess.run(cmd, capture_output=True, text=True)
        raw = json.loads(res.stdout) if res.stdout else {}
        if raw.get("ok"):
            account_id = raw.get("data", {}).get("account_id")
    except Exception:
        return None

    if not account_id:
        return None

    official_name = profile_name
    try:
        p_res = subprocess.run(profile_cmd, capture_output=True, text=True)
        p_raw = json.loads(p_res.stdout) if p_res.stdout else {}
        if p_raw.get("ok"):
            data = p_raw.get("data", {})
            first = data.get("first_name", "")
            last = data.get("last_name", "")
            holder = f"{first} {last}".strip()
            if holder:
                official_name = holder
    except Exception:
        pass

    return {"account_id": account_id, "official_name": official_name}

def trigger_profile_login(profile_name: str) -> Optional[Dict[str, str]]:
    console.print(f"\n[bold yellow]➜ Authentication required for profile: '{profile_name}'[/bold yellow]")
    console.print(f"[cyan]Scalable Web Login URL:[/cyan] {SCALABLE_LOGIN_URL}")
    console.print(f"[dim]Enter {profile_name}'s credentials and confirm phone 2FA prompt below:[/dim]\n")
    cmd = get_profile_env_cmd(profile_name, ["sc", "login"])
    subprocess.run(cmd)
    return check_account_session(profile_name)

def configure_and_select_scalable_accounts() -> List[Dict[str, str]]:
    console.print("\n[bold cyan]=== Scalable Capital Multi-Account Manager ===[/bold cyan]")
    registered = load_scalable_account_registry()

    if not registered:
        registered = [
            {"nickname": "Primary", "official_name": "Primary Account", "account_id": ""},
            {"nickname": "Secondary", "official_name": "Secondary Account", "account_id": ""}
        ]
        save_scalable_account_registry(registered)

    selected_accounts = []
    console.print("\n[bold green]Select Accounts for this Wealth Advisory Run:[/bold green]")
    for acc in registered:
        alias = acc.get("nickname") or acc.get("name") or "Depot"
        official = acc.get("official_name") or alias
        include = input(f"Include '{alias}' (Official: {official})? (Y/n): ").strip().lower()
        if include in ["", "y", "yes"]:
            selected_accounts.append(acc)

    verified_accounts = []
    for acc in selected_accounts:
        alias = acc.get("nickname")
        console.print(f"\n[dim]Verifying authentication session for '{alias}'...[/dim]")
        session_info = check_account_session(alias)

        if not session_info:
            console.print(f"[bold red]✗ No active authentication session found for '{alias}'.[/bold red]")
            session_info = trigger_profile_login(alias)

        if session_info:
            acc["account_id"] = session_info["account_id"]
            acc["official_name"] = session_info["official_name"]
            console.print(f"[bold green]✓ '{alias}' session active (ID: {session_info['account_id']} | Legal: {session_info['official_name']})[/bold green]")
            verified_accounts.append(acc)
        else:
            console.print(f"[bold red]Skipping '{alias}' due to unauthenticated session.[/bold red]")

    save_scalable_account_registry(registered)
    return verified_accounts

def fetch_scalable_holdings_for_account(acc: Dict[str, str]) -> List[Dict[str, Any]]:
    alias = acc.get("nickname", "Depot")
    cmd = get_profile_env_cmd(alias, ["sc", "broker", "holdings", "--json"])
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, check=True)
        raw = json.loads(res.stdout)
        if not raw.get("ok"):
            return []
        items = raw.get("data", {}).get("result", {}).get("items", [])
        records = []
        for itm in items:
            qty = float(itm.get("quantity") or 0.0)
            quote = itm.get("quote_mid_price")
            quote_price = float(quote) if quote is not None else float(itm.get("fifo_price") or 0.0)
            val = round(qty * quote_price, 2)
            if val <= 0:
                continue
            records.append({
                "account_nickname": alias,
                "official_name": acc.get("official_name", alias),
                "account_id": acc.get("account_id", ""),
                "isin": itm.get("isin", "UNKNOWN"),
                "name": itm.get("name", "Asset"),
                "quantity": qty,
                "current_price_eur": quote_price,
                "valuation_eur": val,
                "silo": f"Scalable ({alias})"
            })
        return records
    except Exception as e:
        console.print(f"[red]Error fetching holdings for {alias}: {e}[/red]")
        return []

def fetch_scalable_cash_for_account(acc: Dict[str, str]) -> Optional[Dict[str, Any]]:
    alias = acc.get("nickname", "Depot")
    for subcmd in [["sc", "broker", "account", "--json"], ["sc", "broker", "cash", "--json"]]:
        cmd = get_profile_env_cmd(alias, subcmd)
        try:
            res = subprocess.run(cmd, capture_output=True, text=True)
            if not res.stdout:
                continue
            raw = json.loads(res.stdout)
            if not raw.get("ok"):
                continue
            data = raw.get("data", {})
            cash_val = None
            for key in ["cash", "settled_cash", "buying_power", "available_cash", "balance"]:
                if key in data and data[key] is not None:
                    cash_val = clean_german_amount(data[key])
                    if cash_val is not None:
                        break
            if cash_val is not None and cash_val > 0.01:
                return {
                    "institution": f"Scalable Capital / Baader ({alias})",
                    "deposit_type": "Clearing Cash (Verrechnungskonto)",
                    "principal_eur": float(cash_val),
                    "interest_rate_pct": 2.6,
                    "maturity_date": "Liquid / Instant",
                    "file": f"sc_cli://{alias}/cash"
                }
        except Exception:
            continue
    return None

# ==========================================
# 3. PANDAS + LLM DYNAMIC SCHEMA MAPPING
# ==========================================

@tool
def list_workspace_files() -> str:
    """Scans the workspace directory and lists all discovered files (PDF, CSV, JSON, images)."""
    ignore_dirs = {".git", ".idea", ".vscode", "__pycache__", "venv", "env", ".pytest_cache", ".langgraph"}
    ignore_files = {FILE_CACHE_PATH, SCALABLE_ACCOUNTS_FILE, COMPLETED_ACTIONS_FILE, PENDING_ACTIONS_FILE, "agent_portfolio_report.html"}
    discovered = []
    supported_exts = {".pdf", ".csv", ".json", ".xlsx", ".xls", ".png", ".jpg", ".jpeg", ".webp"}
    for root, dirs, files in os.walk("."):
        dirs[:] = [d for d in dirs if d not in ignore_dirs]
        for f in files:
            if f.startswith(".") or f in ignore_files:
                continue
            ext = os.path.splitext(f)[1].lower()
            if ext in supported_exts:
                discovered.append(os.path.relpath(os.path.join(root, f), "."))
    return json.dumps({"files": discovered}, indent=2)

def extract_file_with_pandas_and_llm(fpath: str) -> Optional[Dict[str, Any]]:
    try:
        ext = os.path.splitext(fpath)[1].lower()
        if ext == ".csv":
            df = pd.read_csv(fpath)
        elif ext in [".xlsx", ".xls"]:
            df = pd.read_excel(fpath)
        else:
            return None

        if df.empty:
            return None

        cleaned_columns = []
        for idx, c in enumerate(df.columns):
            c_str = str(c).strip()
            if "unnamed" in c_str.lower() or not c_str:
                cleaned_columns.append(f"column_{idx}")
            else:
                cleaned_columns.append(c_str)
        df.columns = cleaned_columns

        sample_json = df.head(10).to_json(orient="records")

        mapping_prompt = HumanMessage(content=f"""
You are an autonomous financial data schema mapper.
Examine the following table headers and sample rows from file "{os.path.basename(fpath)}":
{sample_json}

Determine:
1. "asset_group": Exactly one of ["Liquid Cash & Deposits", "Brokerage & Equity Holdings", "Private Pension & Annuities"]
2. "provider": Best guess of institution/bank/broker name.
3. Column mapping (exact column name strings from the table headers):
   - "identifier_col": Column representing ISIN, ticker, symbol, or policy ID (or null)
   - "name_col": Column representing asset name, description, or title
   - "shares_col": Column representing quantity or shares (or null if cash/pension)
   - "value_col": Column representing current value, valuation, amount, or principal in EUR/USD
   - "rate_col": Column representing interest rate % (or null)

Return valid JSON ONLY (no markdown wrappers):
{{
  "asset_group": "Liquid Cash & Deposits" | "Brokerage & Equity Holdings" | "Private Pension & Annuities",
  "provider": "str",
  "identifier_col": "str or null",
  "name_col": "str or null",
  "shares_col": "str or null",
  "value_col": "str",
  "rate_col": "str or null"
}}
""")
        res = llm.invoke([
            SystemMessage(content="You are a financial schema mapping assistant. Return strict JSON only."),
            mapping_prompt
        ])
        mapping = safe_json_loads(res.content)
        if not mapping or not mapping.get("value_col"):
            return None

        val_col = mapping.get("value_col")
        name_col = mapping.get("name_col")
        id_col = mapping.get("identifier_col")
        shares_col = mapping.get("shares_col")
        rate_col = mapping.get("rate_col")
        asset_group = mapping.get("asset_group", "Brokerage & Equity Holdings")
        provider = mapping.get("provider") or os.path.splitext(os.path.basename(fpath))[0]

        items = []
        for _, row in df.iterrows():
            raw_val = row.get(val_col) if val_col in df.columns else None
            val = clean_german_amount(raw_val)
            if val is None or val <= 0:
                continue

            name = str(row.get(name_col) if name_col and name_col in df.columns else "Asset").strip()
            identifier = str(row.get(id_col) if id_col and id_col in df.columns else name).strip()
            shares = clean_german_amount(row.get(shares_col)) if shares_col and shares_col in df.columns else 0.0
            rate = clean_german_amount(row.get(rate_col)) if rate_col and rate_col in df.columns else 0.0

            if name.lower() in ["nan", "none", "total", "gesamt", "summe"] or identifier.lower() in ["nan", "none"]:
                continue

            price = round(val / shares, 2) if shares and shares > 0 else 0.0

            items.append({
                "identifier": identifier,
                "name": name,
                "provider": provider,
                "valuation_eur": val,
                "quantity": shares,
                "price_eur": price,
                "interest_rate_pct": rate
            })

        return {
            "asset_group": asset_group,
            "specific_category_name": provider,
            "institution_or_provider": provider,
            "items": items
        }
    except Exception as e:
        console.print(f"[dim red]Pandas+LLM extraction error for {fpath}: {e}[/dim red]")
        return None

def execute_autonomous_data_discovery(active_scalable_accounts: List[Dict[str, str]]) -> Dict[str, Any]:
    console.print("\n[bold cyan]🤖 Agent Autonomously Discovering Workspace Assets via Pandas + LLM Schema Mapping...[/bold cyan]")
    files_json = list_workspace_files.invoke({})
    all_files = json.loads(files_json).get("files", [])

    structured_data = {
        "cash_and_fixed_deposits": [],
        "private_pension_policies": [],
        "liquid_brokerage": {
            "scalable_depots": [],
            "external_depots": []
        }
    }

    for fpath in all_files:
        if not os.path.exists(fpath):
            continue

        ext = os.path.splitext(fpath)[1].lower()
        if ext not in [".csv", ".xlsx", ".xls"]:
            continue

        extraction = extract_file_with_pandas_and_llm(fpath)
        if not extraction or not extraction.get("items"):
            continue

        group = str(extraction.get("asset_group") or "").lower()
        default_provider = extraction.get("institution_or_provider") or os.path.splitext(os.path.basename(fpath))[0]

        for item in extraction.get("items", []):
            val = clean_german_amount(item.get("valuation_eur") or item.get("principal_or_capital_eur"))
            if val is None or val <= 0:
                continue

            provider = item.get("provider") or default_provider
            asset_name = item.get("name") or item.get("identifier") or "Asset"

            if "brokerage" in group or "equity" in group:
                structured_data["liquid_brokerage"]["external_depots"].append({
                    "institution": provider,
                    "identifier": item.get("identifier") or "UNKNOWN",
                    "isin": item.get("identifier") or "UNKNOWN",
                    "name": asset_name,
                    "quantity": float(item.get("quantity") or 0.0),
                    "current_price_eur": float(item.get("price_eur") or 0.0),
                    "valuation_eur": float(val),
                    "file": fpath
                })
            elif "pension" in group or "annuity" in group:
                structured_data["private_pension_policies"].append({
                    "policy_id": item.get("identifier") or "Policy",
                    "provider": provider,
                    "files": [fpath],
                    "accumulated_capital_eur": float(val),
                    "guaranteed_monthly_pension_eur": clean_german_amount(item.get("monthly_annuity_eur"))
                })
            else:
                structured_data["cash_and_fixed_deposits"].append({
                    "institution": provider,
                    "deposit_type": item.get("identifier") or asset_name,
                    "principal_eur": float(val),
                    "interest_rate_pct": float(item.get("interest_rate_pct") or 0.0),
                    "maturity_date": "Liquid / On Demand",
                    "file": fpath
                })

    for acc in active_scalable_accounts:
        items = fetch_scalable_holdings_for_account(acc)
        structured_data["liquid_brokerage"]["scalable_depots"].extend(items)
        
        cash_item = fetch_scalable_cash_for_account(acc)
        if cash_item:
            structured_data["cash_and_fixed_deposits"].append(cash_item)

    return structured_data

# ==========================================
# 4. STRATEGY SYNTHESIS & REVIEWER AUDIT
# ==========================================

def generate_rich_portfolio_audit_text() -> str:
    """Dynamically scans all workspace CSV files and extracts their contents for audit synthesis."""
    text_breakdown = []
    csv_files = glob.glob("*.csv")
    for fpath in csv_files:
        if fpath in [FILE_CACHE_PATH, SCALABLE_ACCOUNTS_FILE, COMPLETED_ACTIONS_FILE, PENDING_ACTIONS_FILE]:
            continue
        try:
            text_breakdown.append(f"--- DATA FILE: {fpath} ---")
            df = pd.read_csv(fpath)
            for _, row in df.head(50).iterrows():
                text_breakdown.append(f"- {dict(row)}")
        except Exception:
            pass
    return "\n".join(text_breakdown) if text_breakdown else "No local portfolio CSV files discovered."

def calculate_compounding_horizon(start_val: float, monthly_inflow: float, target_val: float, annual_return: float) -> float:
    if start_val >= target_val:
        return 0.0
    r = annual_return / 12.0
    val = start_val
    months = 0
    while val < target_val and months < 1200:
        val = val * (1 + r) + monthly_inflow
        months += 1
    return round(months / 12.0, 1)

def run_strategic_wealth_synthesis(structured_data: Dict[str, Any], household_profile: Dict[str, Any], completed_actions: List[str]) -> str:
    console.print("[yellow]➜ Node 1: CIO Formulating Cross-Border Strategy...[/yellow]")
    
    total_scalable_stocks = sum(float(p.get('valuation_eur', 0) or 0) for p in structured_data['liquid_brokerage']['scalable_depots'])
    total_external_stocks = sum(float(p.get('valuation_eur', 0) or 0) for p in structured_data['liquid_brokerage']['external_depots'])
    total_liquid_stocks = total_scalable_stocks + total_external_stocks
    total_cash = sum(float(c.get('principal_eur', 0) or 0) for c in structured_data['cash_and_fixed_deposits'])
    total_pension = sum(float(p.get('accumulated_capital_eur', 0) or 0) for p in structured_data['private_pension_policies'])
    total_liquid_net_worth = total_liquid_stocks + total_cash

    goal_target = household_profile['liquid_wealth_goal']
    inflow = household_profile['inflow']
    remaining_gap = max(0.0, goal_target - total_liquid_net_worth)

    years_conservative = calculate_compounding_horizon(total_liquid_net_worth, inflow, goal_target, 0.05)
    years_moderate = calculate_compounding_horizon(total_liquid_net_worth, inflow, goal_target, 0.07)
    years_growth = calculate_compounding_horizon(total_liquid_net_worth, inflow, goal_target, 0.09)

    emergency_months = total_cash / household_profile['monthly_expenses'] if household_profile['monthly_expenses'] > 0 else 0
    cash_ratio_pct = (total_cash / total_liquid_net_worth * 100) if total_liquid_net_worth > 0 else 0

    spouse_summary = (
        f"Spouse Age: {household_profile['spouse_age']} | Spouse Employed: {household_profile['spouse_employed']}"
        if household_profile.get("spouse_age")
        else "Single income household"
    )

    completed_str = "\n".join([f"- {act}" for act in completed_actions]) if completed_actions else "None logged yet."
    granular_audit_text = generate_rich_portfolio_audit_text()

    cio_prompt = f"""
You are an institutional Chief Investment Officer (CIO) and Wealth Strategist.
The client lives in {household_profile['living_city']} with monthly living expenses of €{household_profile['monthly_expenses']:,.2f}.

HOUSEHOLD & TAX RULES:
- Tax Resident jurisdiction: {household_profile['living_city']}.
- Marital Status: {household_profile['marital_status']}.
- Client Age: {household_profile['user_age']} | {spouse_summary}.

ALREADY COMPLETED ACTIONS (DO NOT RE-RECOMMEND THESE):
{completed_str}

GRANULAR PORTFOLIO ASSET INVENTORY:
{granular_audit_text}

PORTFOLIO AGGREGATE AUDIT:
- Total Liquid Stocks & ETFs: €{total_liquid_stocks:,.2f}
- Emergency Cash & Fixed Deposits: €{total_cash:,.2f} ({emergency_months:.1f} months runway, {cash_ratio_pct:.1f}% of net worth)
- Verified Pension Capital: €{total_pension:,.2f}
- Total Liquid Net Worth: €{total_liquid_net_worth:,.2f}
- Progress to Target Goal: {(total_liquid_net_worth / goal_target * 100):.2f}% (Gap: €{remaining_gap:,.2f})
- Monthly Fresh Inflow: €{inflow:,.2f}/month

ADVISORY REQUIREMENTS:
1. CAPITAL DEPLOYMENT TABLE:
   - Provide a clean markdown table allocating 100% of the €{inflow:,.2f}/month fresh inflow into specific broad ETFs.
   - Euro amounts MUST sum up to exactly €{inflow:,.2f}.
2. NEXT 30 DAYS TACTICAL EXECUTION PLAN:
   - Header MUST be: "### NEXT 30 DAYS TACTICAL EXECUTION PLAN"
   - Deliver 3 comprehensive, granular tactical actions referencing specific holdings and cash buffer thresholds based on the inventory above.
"""
    cio_draft = llm.invoke(cio_prompt).content
    cio_draft_clean = cio_draft.split("</think>")[1].strip() if "</think>" in cio_draft else cio_draft.strip()

    console.print("[yellow]➜ Node 2: Tax Compliance & Consistency Auditor Reviewing Draft...[/yellow]")
    critic_prompt = f"""
You are the Chief Tax Compliance Officer and Lead Auditor.
Audit the following CIO wealth memo:

--- DRAFT TEXT ---
{cio_draft_clean}
--- END DRAFT ---

USER PROFILE:
- Monthly Inflow: €{household_profile['inflow']:,.2f}
- Suppressed Actions: {completed_str}

AUDIT CHECKLIST:
1. Verify the Capital Deployment Table sums up to exactly €{household_profile['inflow']:,.2f}.
2. Ensure the exact header "### NEXT 30 DAYS TACTICAL EXECUTION PLAN" is kept intact with exactly 3 comprehensive steps referencing the client's actual portfolio holdings.

Output the finalized report directly.
"""
    final_reviewed = llm.invoke(critic_prompt).content
    return final_reviewed.split("</think>")[1].strip() if "</think>" in final_reviewed else final_reviewed.strip()

# ==========================================
# 5. HTML EXPORTER & VISUALIZATION
# ==========================================

HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>Autonomous Wealth & Multi-Vault Intelligence Report — <!--DATE--></title>
<style>
  :root {
    --bg: #0f172a; --card: #1e293b; --border: #334155; --text: #f8fafc;
    --muted: #94a3b8; --accent: #38bdf8; --green: #4ade80; --red: #f87171;
    --yellow: #facc15; --purple: #c084fc;
  }
  @media print {
    body { background: #fff !important; color: #000 !important; font-size: 10pt; }
    .card { border: 1px solid #ddd !important; background: transparent !important; box-shadow: none !important; }
    .no-print { display: none; }
  }
  body {
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
    background-color: var(--bg); color: var(--text); margin: 0; padding: 32px 24px; line-height: 1.6;
  }
  .container { max-width: 1100px; margin: 0 auto; }
  header { display: flex; justify-content: space-between; align-items: flex-end; border-bottom: 1px solid var(--border); padding-bottom: 16px; margin-bottom: 24px; }
  h1 { margin: 0; font-size: 24px; font-weight: 700; }
  .meta { color: var(--muted); font-size: 13px; }
  .kpi-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 16px; margin-bottom: 24px; }
  .kpi-card { background: var(--card); border: 1px solid var(--border); border-radius: 8px; padding: 16px; }
  .kpi-label { font-size: 12px; text-transform: uppercase; color: var(--muted); font-weight: 600; }
  .kpi-value { font-size: 22px; font-weight: 700; margin-top: 4px; color: var(--accent); }
  .card { background: var(--card); border: 1px solid var(--border); border-radius: 8px; padding: 20px; margin-bottom: 24px; }
  h2 { font-size: 18px; margin-top: 0; margin-bottom: 14px; border-bottom: 1px solid var(--border); padding-bottom: 8px; color: var(--accent); }
  table { width: 100%; border-collapse: collapse; font-size: 13px; text-align: left; }
  th { color: var(--muted); font-weight: 600; border-bottom: 1px solid var(--border); padding: 8px; text-transform: uppercase; font-size: 11px; }
  td { padding: 8px; border-bottom: 1px solid var(--border); }
  .text-right { text-align: right; }
  .action-box { background: rgba(56, 189, 248, 0.08); border-left: 4px solid var(--accent); padding: 16px 20px; margin-bottom: 24px; border-radius: 0 8px 8px 0; }
  .markdown-body table { width: 100%; border-collapse: collapse; margin: 16px 0; font-size: 13px; }
  .markdown-body th { background: rgba(255,255,255,0.05); color: var(--accent); padding: 8px; border: 1px solid var(--border); }
  .markdown-body td { padding: 8px; border: 1px solid var(--border); }
  .btn-print { background: var(--accent); color: #000; border: none; padding: 8px 16px; border-radius: 6px; font-weight: 600; cursor: pointer; }
</style>
</head>
<body>
<div class="container">
  <header>
    <div>
      <h1>Autonomous Multi-Vault Wealth & Pension Intelligence Memo</h1>
      <div class="meta">Generated: <!--DATE--></div>
    </div>
    <button class="btn-print no-print" onclick="window.print()">Save to PDF</button>
  </header>

  <div class="kpi-grid">
    <div class="kpi-card">
      <div class="kpi-label">Total Liquid Net Worth</div>
      <div class="kpi-value">€<!--TOTAL_LIQUID--></div>
      <div class="meta"><!--PCT_OF_GOAL-->% of €<!--GOAL_VAL--> Target (Stocks + Cash)</div>
    </div>
    <div class="kpi-card">
      <div class="kpi-label">Cash & Fixed Deposits</div>
      <div class="kpi-value" style="color:var(--green);">€<!--TOTAL_CASH--></div>
      <div class="meta">Emergency Buffer (<!--RUNWAY_MONTHS--> Mo. Runway)</div>
    </div>
    <div class="kpi-card">
      <div class="kpi-label">Verified Pension Capital</div>
      <div class="kpi-value" style="color:var(--purple);">€<!--PENSION_CAP--></div>
      <div class="meta">Locked Annuities</div>
    </div>
    <div class="kpi-card">
      <div class="kpi-label">Monthly Rebalancing Inflow</div>
      <div class="kpi-value">€<!--INFLOW--></div>
      <div class="meta">Fresh Capital</div>
    </div>
  </div>

  <div class="card" style="border-left: 4px solid var(--purple);">
    <h2>Compounding Horizon & Milestone Projections</h2>
    <p style="color:var(--muted); font-size:13px; margin-top:-6px;">
      Trajectory modeling based on current liquid net worth of <strong>€<!--TOTAL_LIQUID--></strong>, 
      investing <strong>€<!--INFLOW-->/month</strong> toward the <strong>€<!--GOAL_VAL--></strong> milestone:
    </p>
    <table>
      <thead>
        <tr>
          <th>Scenario</th>
          <th>Annualized Return (p.a.)</th>
          <th>Projected Time to Goal</th>
          <th>Estimated Milestone Year</th>
          <th>Scenario Trajectory Profile</th>
        </tr>
      </thead>
      <tbody>
        <tr>
          <td><strong>Conservative</strong></td>
          <td>5.0%</td>
          <td><strong><!--YEARS_CONSERVATIVE--> years</strong></td>
          <td><!--YEAR_CONSERVATIVE--></td>
          <td>Preservation bias (high fixed income / conservative allocation)</td>
        </tr>
        <tr style="background: rgba(56, 189, 248, 0.05);">
          <td><strong style="color:var(--accent);">Base / Moderate</strong></td>
          <td style="color:var(--accent);"><strong>7.0%</strong></td>
          <td><strong style="color:var(--accent);"><!--YEARS_MODERATE--> years</strong></td>
          <td><strong style="color:var(--accent);"><!--YEAR_MODERATE--></strong></td>
          <td>Global broad equity index standard</td>
        </tr>
        <tr>
          <td><strong>Accelerated Growth</strong></td>
          <td>9.0%</td>
          <td><strong><!--YEARS_GROWTH--> years</strong></td>
          <td><!--YEAR_GROWTH--></td>
          <td>Growth equity tilt with fully reinvested dividend yields</td>
        </tr>
      </tbody>
    </table>
  </div>

  <div class="action-box">
    <h3>Top Priorities: Next 30 Days Tactical Plan</h3>
    <div class="markdown-body">
      <!--ACTION_ITEMS_HTML-->
    </div>
  </div>

  <div class="card">
    <h2>Autonomous Asset Inventory (Extracted by AI Agent)</h2>
    <!--ASSET_TABLES_HTML-->
  </div>

  <div class="card">
    <h2>Strategic Wealth & Tax Deployment Memo</h2>
    <div class="markdown-body">
      <!--STRATEGY_HTML-->
    </div>
  </div>
</div>
</body>
</html>
"""

def generate_agentic_html_report(structured_data: Dict[str, Any], household_profile: Dict[str, Any], final_strategy: str) -> str:
    scalable_val = sum(float(p.get("valuation_eur", 0.0) or 0.0) for p in structured_data.get("liquid_brokerage", {}).get("scalable_depots", []) if p.get("valuation_eur"))
    external_val = sum(float(p.get("valuation_eur", 0.0) or 0.0) for p in structured_data.get("liquid_brokerage", {}).get("external_depots", []) if p.get("valuation_eur"))
    total_cash = sum(float(c.get("principal_eur", 0.0) or 0.0) for c in structured_data.get("cash_and_fixed_deposits", []) if c.get("principal_eur"))
    
    total_liquid = scalable_val + external_val + total_cash
    total_pension = sum(float(p.get("accumulated_capital_eur", 0.0) or 0.0) for p in structured_data.get("private_pension_policies", []) if p.get("accumulated_capital_eur"))

    goal_val = household_profile.get("liquid_wealth_goal", 1000000.0)
    inflow = household_profile.get("inflow", 2000.0)
    pct_goal = round((total_liquid / goal_val * 100), 2) if goal_val > 0 else 0.0
    runway_months = round(total_cash / household_profile["monthly_expenses"], 1) if household_profile.get("monthly_expenses", 0) > 0 else 0.0

    years_cons = calculate_compounding_horizon(total_liquid, inflow, goal_val, 0.05)
    years_mod = calculate_compounding_horizon(total_liquid, inflow, goal_val, 0.07)
    years_grow = calculate_compounding_horizon(total_liquid, inflow, goal_val, 0.09)
    current_yr = datetime.now().year

    action_items = ""
    strategy_body = final_strategy
    match = re.search(r"### NEXT 30 DAYS.*", final_strategy, re.DOTALL | re.IGNORECASE)
    if match:
        action_items = match.group(0).strip()
        strategy_body = final_strategy[:match.start()].strip()

    actions_html = markdown.markdown(action_items, extensions=["tables"])
    strategy_html = markdown.markdown(strategy_body, extensions=["tables"])

    asset_parts = []
    
    scalable_items = structured_data.get("liquid_brokerage", {}).get("scalable_depots", [])
    if scalable_items:
        asset_parts.append("<h3>Brokerage Depots</h3><table><thead><tr><th>Account Alias</th><th>Official Legal Title</th><th>Ticker / ISIN</th><th>Name</th><th class=\"text-right\">Shares</th><th class=\"text-right\">Price (€)</th><th class=\"text-right\">Valuation (€)</th></tr></thead><tbody>")
        for itm in scalable_items:
            val = itm.get('valuation_eur', 0) or 0
            asset_parts.append(f"<tr><td><strong>{itm.get('account_nickname')}</strong></td><td><small style=\"color:var(--muted)\">{itm.get('official_name')}</small></td><td>{itm.get('isin')}</td><td>{itm.get('name')}</td><td class=\"text-right\">{itm.get('quantity', 0):,.3f}</td><td class=\"text-right\">€{itm.get('current_price_eur', 0):,.2f}</td><td class=\"text-right\">€{val:,.2f}</td></tr>")
        asset_parts.append("</tbody></table>")

    external_items = [i for i in structured_data.get("liquid_brokerage", {}).get("external_depots", []) if (i.get('valuation_eur') or 0) > 0]
    if external_items:
        asset_parts.append("<h3 style=\"margin-top:20px;\">Other Discovered Brokerage Depots & Portfolios</h3><table><thead><tr><th>Custodian / Provider</th><th>Identifier / Symbol</th><th>Asset Name</th><th class=\"text-right\">Shares</th><th class=\"text-right\">Price (€)</th><th class=\"text-right\">Valuation (€)</th></tr></thead><tbody>")
        for itm in external_items:
            val = itm.get('valuation_eur', 0) or 0
            asset_parts.append(f"<tr><td><strong>{itm.get('institution')}</strong></td><td>{itm.get('identifier')}</td><td>{itm.get('name')}</td><td class=\"text-right\">{itm.get('quantity', 0):,.3f}</td><td class=\"text-right\">€{itm.get('current_price_eur', 0):,.2f}</td><td class=\"text-right\">€{val:,.2f}</td></tr>")
        asset_parts.append("</tbody></table>")

    fds = structured_data.get("cash_and_fixed_deposits", [])
    if fds:
        asset_parts.append("<h3 style=\"margin-top:20px;\">Fixed Deposits & Cash Reserves</h3><table><thead><tr><th>Institution</th><th>Type</th><th class=\"text-right\">Principal (€)</th><th class=\"text-right\">Interest Rate</th><th>Maturity</th></tr></thead><tbody>")
        for fd in fds:
            val = fd.get('principal_eur', 0) or 0
            asset_parts.append(f"<tr><td><strong>{fd.get('institution')}</strong></td><td>{fd.get('deposit_type')}</td><td class=\"text-right\">€{val:,.2f}</td><td class=\"text-right\">{fd.get('interest_rate_pct', 0)}%</td><td>{fd.get('maturity_date')}</td></tr>")
        asset_parts.append("</tbody></table>")

    pensions = structured_data.get("private_pension_policies", [])
    if pensions:
        asset_parts.append("<h3 style=\"margin-top:20px;\">Private Pension Policies</h3><table><thead><tr><th>Policy / Provider</th><th>Source Files / Screenshots</th><th class=\"text-right\">Verified Capital</th><th class=\"text-right\">Guaranteed/mo</th></tr></thead><tbody>")
        for pen in pensions:
            cap = pen.get('accumulated_capital_eur')
            cap_str = f"€{cap:,.2f}" if cap is not None else '<span style="color:var(--yellow)">Unspecified</span>'
            gar = pen.get('guaranteed_monthly_pension_eur')
            gar_str = f"€{gar:,.2f}" if gar is not None else '<span style="color:var(--yellow)">Unspecified</span>'
            asset_parts.append(f"<tr><td><strong>{pen.get('provider')}</strong> ({pen.get('policy_id')})</td><td><small style=\"color:var(--muted)\">{', '.join(pen.get('files', []))}</small></td><td class=\"text-right\">{cap_str}</td><td class=\"text-right\">{gar_str}</td></tr>")
        asset_parts.append("</tbody></table>")

    rendered = HTML_TEMPLATE
    rendered = rendered.replace("<!--DATE-->", datetime.now().strftime("%Y-%m-%d %H:%M"))
    rendered = rendered.replace("<!--TOTAL_LIQUID-->", f"{total_liquid:,.2f}")
    rendered = rendered.replace("<!--TOTAL_CASH-->", f"{total_cash:,.2f}")
    rendered = rendered.replace("<!--RUNWAY_MONTHS-->", str(runway_months))
    rendered = rendered.replace("<!--PENSION_CAP-->", f"{total_pension:,.2f}")
    rendered = rendered.replace("<!--INFLOW-->", f"{inflow:,.2f}")
    rendered = rendered.replace("<!--GOAL_VAL-->", f"{goal_val:,.2f}")
    rendered = rendered.replace("<!--PCT_OF_GOAL-->", str(pct_goal))

    rendered = rendered.replace("<!--YEARS_CONSERVATIVE-->", f"{years_cons:.1f}")
    rendered = rendered.replace("<!--YEARS_MODERATE-->", f"{years_mod:.1f}")
    rendered = rendered.replace("<!--YEARS_GROWTH-->", f"{years_grow:.1f}")
    rendered = rendered.replace("<!--YEAR_CONSERVATIVE-->", str(int(current_yr + years_cons)))
    rendered = rendered.replace("<!--YEAR_MODERATE-->", str(int(current_yr + years_mod)))
    rendered = rendered.replace("<!--YEAR_GROWTH-->", str(int(current_yr + years_grow)))

    rendered = rendered.replace("<!--ACTION_ITEMS_HTML-->", actions_html)
    rendered = rendered.replace("<!--ASSET_TABLES_HTML-->", "".join(asset_parts))
    rendered = rendered.replace("<!--STRATEGY_HTML-->", strategy_html)

    out_file = os.path.abspath("agent_portfolio_report.html")
    with open(out_file, "w", encoding="utf-8") as f:
        f.write(rendered)
        f.flush()
        os.fsync(f.fileno())

    return out_file

# ==========================================
# 6. MAIN AGENT EXECUTION
# ==========================================

def main():
    console.print("[bold blue]=== Autonomous Multi-Vault Wealth & Pension Agent ===[/bold blue]\n")

    active_scalable_accounts = configure_and_select_scalable_accounts()

    completed_actions = load_completed_actions()
    pending_actions = load_pending_actions()

    if completed_actions:
        console.print("\n[bold cyan]Already Completed Actions (Suppressed from Suggestions):[/bold cyan]")
        for act in completed_actions:
            console.print(f"  [dim green]✓ {act}[/dim green]")

    active_pending = [a for a in pending_actions if a not in completed_actions]

    if active_pending:
        console.print("\n[bold yellow]Pending Action Items from Previous Report:[/bold yellow]")
        for idx, act in enumerate(active_pending, 1):
            console.print(f"  [bold cyan][{idx}][/bold cyan] {act}")

        selection = input("\nEnter numbers of completed actions to mark done (e.g. 1, 3 or Enter to skip): ").strip()
        if selection:
            indexes = [s.strip() for s in selection.replace(" ", "").split(",") if s.strip().isdigit()]
            for s_idx in indexes:
                pos = int(s_idx) - 1
                if 0 <= pos < len(active_pending):
                    chosen_action = active_pending[pos]
                    if chosen_action not in completed_actions:
                        completed_actions.append(chosen_action)
                        console.print(f"  [green]Marked as done: {chosen_action}[/green]")
            save_completed_actions(completed_actions)
    else:
        new_done = input("\nEnter any completed actions to suppress (comma-separated, or Enter to skip): ").strip()
        if new_done:
            for item in new_done.split(","):
                clean_item = item.strip()
                if clean_item and clean_item not in completed_actions:
                    completed_actions.append(clean_item)
            save_completed_actions(completed_actions)

    console.print("\n[bold green]Household & Strategic Planning Profile:[/bold green]")
    goal = input("1. Primary Goal [default: Growth]: ").strip() or "Growth"
    horizon = input("2. Time Horizon in years [default: 10]: ").strip() or "10"
    inflow = input("3. Monthly Inflow in € [default: 2000]: ").strip() or "2000"
    liquid_wealth_goal = input("4. Target Liquid Wealth Goal in € [default: 1000000]: ").strip() or "1000000"
    marital_status = input("5. Marital Status (Single / Married) [default: Married]: ").strip() or "Married"
    user_age = input("6. Your Age [default: 40]: ").strip() or "40"
    
    spouse_age = None
    spouse_employed = "Yes"
    if marital_status.lower().startswith("m"):
        spouse_age = input("7. Spouse Age [default: 40]: ").strip() or "40"
        spouse_employed = input("8. Is spouse earning/employed? (Yes / No) [default: Yes]: ").strip() or "Yes"

    living_city = input("9. City & Country for Living Expenses Audit [default: Frankfurt, Germany]: ").strip() or "Frankfurt, Germany"
    monthly_expenses = input("10. Estimated Monthly Household Living Expenses in € [default: 3000]: ").strip() or "3000"

    household_profile = {
        "goal": goal,
        "horizon": int(horizon),
        "inflow": float(inflow),
        "liquid_wealth_goal": float(liquid_wealth_goal),
        "marital_status": marital_status,
        "user_age": int(user_age),
        "spouse_age": int(spouse_age) if spouse_age else None,
        "spouse_employed": spouse_employed,
        "living_city": living_city,
        "monthly_expenses": float(monthly_expenses)
    }

    structured_assets = execute_autonomous_data_discovery(active_scalable_accounts)
    final_strategy = run_strategic_wealth_synthesis(structured_assets, household_profile, completed_actions)

    newly_recommended = extract_action_titles_from_strategy(final_strategy)
    if newly_recommended:
        save_pending_actions(newly_recommended)

    console.print("\n[bold green]=== Audited Strategic Advisory Plan ===[/bold green]\n")
    console.print(RichMarkdown(final_strategy))

    report_file = generate_agentic_html_report(structured_assets, household_profile, final_strategy)
    console.print(f"\n[bold green]✓ Verified Report compiled:[/bold green] {report_file}")
    webbrowser.open(f"file://{report_file}")

if __name__ == "__main__":
    main()
