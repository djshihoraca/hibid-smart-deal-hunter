"""Search saved evidence without pretending it is a live auction feed."""


def catalogue_entries(history, query="", category="All categories", status="All decisions"):
    # History is newest first. Show the latest inspection for each known listing URL.
    seen, latest = set(), []
    for entry in history:
        identity = entry.get("listing_url", "").strip() or f"record:{entry['id']}"
        if identity in seen:
            continue
        seen.add(identity)
        latest.append(entry)
    words = query.casefold().split()
    results = []
    for entry in latest:
        searchable = " ".join(str(entry.get(field, "")) for field in ("name", "brand", "category", "description"))
        if words and not all(word in searchable.casefold() for word in words):
            continue
        if category != "All categories" and entry.get("category") != category:
            continue
        if status != "All decisions" and entry.get("decision", {}).get("status") != status:
            continue
        results.append(entry)
    return results


def amount(value):
    """Display unavailable values honestly, including older partial records."""
    from decimal import Decimal, InvalidOperation
    try:
        number = Decimal(str(value))
        return f"CAD ${number:.2f}" if number.is_finite() else "Not recorded"
    except (ValueError, TypeError, InvalidOperation):
        return "Not recorded"


def percentage(value):
    from decimal import Decimal, InvalidOperation
    try:
        number = Decimal(str(value))
        return f"{number:.1f}%" if number.is_finite() else "Not recorded"
    except (ValueError, TypeError, InvalidOperation):
        return "Not recorded"
