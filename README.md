# financial_suggestion_agent
# Autonomous Multi-Vault Wealth & Pension Intelligence Agent

An autonomous, multi-agent financial advisor and portfolio auditing tool built with Python, LangChain, and Pandas. It connects to local LLMs (via LM Studio) and brokerage CLI tools to discover assets, map schemas dynamically, perform tax-aware portfolio synthesis, and generate comprehensive HTML/PDF executive wealth reports.

## Features
- **Dynamic Schema Mapping:** Uses Pandas and LLMs to automatically ingest and categorize CSV/Excel holdings or pension data without rigid column structures.
- **Multi-Account Brokerage Integration:** Supports multi-profile session management and automated asset retrieval.
- **Two-Stage CIO & Compliance Review:** Combines an institutional Chief Investment Officer strategy node with a Lead Auditor tax compliance check.
- **Interactive Reporting:** Compiles a clean, print-ready HTML wealth dashboard complete with compounding horizon projections and tactical 30-day execution steps.

## Prerequisites
- Python 3.10+
- [LM Studio](https://lmstudio.ai/) running locally with an OpenAI-compatible server endpoint (`http://localhost:1234/v1`).
- Optional: WSL (Windows Subsystem for Linux) if interacting with Linux-based brokerage CLI tools.

## Installation

1. Clone the repository:
   ```bash
   git clone [https://github.com/your-username/your-repo-name.git](https://github.com/your-username/your-repo-name.git)
   cd your-repo-name
2. Install dependencies:   
```bash
   pip install pandas pypdf pillow langchain-openai langchain-core rich markdown yfinance pytesseract
```
4. Run the agent:
```bash
python main.py
```
