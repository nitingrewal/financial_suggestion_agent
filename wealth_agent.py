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
