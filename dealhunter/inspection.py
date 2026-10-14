"""Conservative signals, never a claim of functionality."""
import re

CONDITIONS = ["Factory sealed", "New open-box", "Like new", "Used excellent",
              "Used good", "Refurbished", "Display model", "Customer return",
              "Damaged packaging", "For parts / not working"]
CATEGORIES = ["Toys & Games", "Electronics", "Power Tools", "Home Appliances",
              "Furniture & Décor", "Lawn & Garden", "Automotive", "Computers",
              "Sports & Outdoors", "Other"]


def assess(description, conditions, photo_count, observations=None, vision=None):
    text = description.lower()
    observations = observations or {}
    flags = []
    damage = bool(re.search(r"\b(damaged|broken|cracked|not working|parts only|for parts)\b", text))
    missing = bool(re.search(r"\b(missing|without|incomplete)\b", text))
    uncertain = bool(re.search(r"\b(untested|as[- ]is|unknown|stock photo|no returns)\b", text))
    if damage:
        flags.append("Description mentions damage or nonworking parts; verify context manually.")
    if missing:
        flags.append("Description suggests missing accessories or an incomplete item.")
    if uncertain:
        flags.append("Description includes uncertainty or restrictive seller terms.")
    conflict = ("Factory sealed" in conditions and (
        any(c != "Factory sealed" for c in conditions) or
        bool(re.search(r"\b(used|open[- ]box|refurbished|missing|damaged)\b", text))))
    if conflict:
        flags.append("Factory-sealed claim conflicts with other condition information.")
    if not description.strip():
        flags.append("Seller description is missing.")
    if not conditions:
        flags.append("Product condition is unspecified.")
    if not photo_count:
        flags.append("No listing photos supplied.")
    else:
        flags.append("Photos cannot establish functionality or hidden defects.")
    for key, label in (("damage", "Visible damage"), ("missing", "Missing accessories"),
                       ("inconsistent", "Inconsistent condition claims")):
        if observations.get(key):
            flags.append(f"Manual photo review: {label.lower()}.")
    if photo_count and not observations.get("reviewed"):
        flags.append("Manual photo inspection has not been completed.")
    if re.search(r"\b(uncertain|unknown|untested|missing|damaged|broken|cracked|inconsistent)\b", observations.get("notes", "").lower()):
        flags.append("Manual inspection notes document uncertainty or a condition concern.")
    if vision:
        for key in ("damage", "missing_accessories", "inconsistencies", "uncertainty"):
            flags.extend(f"AI observation (unverified): {v}" for v in vision.get(key, []))
    return {"flags": flags, "description_damage": damage, "missing_accessories": missing,
            "condition_conflict": conflict, "observations": observations, "vision": vision,
            "functionality": "Unknown; photos are never proof of functionality"}


def decide(calculation, assessment, source, *, complete, accepted_conditions,
           claimed_conditions, approved=False):
    reasons = []
    if not calculation["affordable"]:
        return {"status": "rejected", "reasons": ["No feasible bid meets the discount target."], "approved": False}
    if accepted_conditions and not set(claimed_conditions).issubset(set(accepted_conditions)):
        return {"status": "rejected", "reasons": ["Claimed condition is outside the watchlist."], "approved": False}
    override_required = calculation["above_ceiling"] or not calculation["meets_discount"]
    if override_required:
        if not approved:
            return {"status": "rejected", "reasons": ["Bid exceeds ceiling; explicit approval is required."], "approved": False}
        reasons.append("User approved this exact over-ceiling bid; check the auction's bid ladder.")
        if not calculation["meets_discount"]:
            reasons.append("This bid does not meet the discount rule.")
    if not complete:
        reasons.append("Listing details or manual verification checklist are incomplete.")
    if not source.get("verified"):
        reasons.append("Retail price is manually entered and unverified.")
    if assessment["description_damage"] or assessment["missing_accessories"] or assessment["condition_conflict"]:
        reasons.append("Description damage, accessories, or condition claims require review.")
    obs = assessment["observations"]
    if any(obs.get(k) for k in ("damage", "missing", "inconsistent")):
        reasons.append("Manual photo review identified concerns.")
    # Even reviewed photos cannot establish functionality; an explicit checklist is required.
    if not obs.get("reviewed"):
        reasons.append("Photo inspection is incomplete.")
    if any("uncertainty" in f or "unverified" in f for f in assessment["flags"]):
        reasons.append("Uncertainty or AI observations require manual review.")
    return {"status": "requiring review" if reasons else "eligible", "reasons": reasons,
            "approved": bool(approved and override_required)}
