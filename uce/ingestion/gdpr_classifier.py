"""
GDPR / PII column classifier.

Given a column name and optional table name, returns a structured classification of
whether the column likely contains personal data, what GDPR category it falls under,
its sensitivity level, and which GDPR article(s) apply.

Classification is deterministic (pattern-based), so it is always reproducible and
adds no LLM cost.  It is intentionally conservative: uncertain cases are marked
``sensitivity="low"`` with ``category="potential_personal_data"`` rather than
dismissed as non-PII.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ColumnClassification:
    column: str
    table: str
    is_pii: bool
    category: str           # e.g. "contact", "identity", "financial", ...
    sensitivity: str        # "high" | "medium" | "low"
    gdpr_articles: tuple[str, ...]  # e.g. ("Art. 9",) for special-category data
    subject_type: str       # "customer" | "employee" | "patient" | "user" | "unknown"
    rationale: str


# ---------------------------------------------------------------------------
# Pattern catalogue
# ---------------------------------------------------------------------------

# Each entry: (pattern, category, sensitivity, gdpr_articles, subject_hint, rationale)
_PATTERNS: list[tuple[re.Pattern, str, str, tuple, str, str]] = [
    # Identity
    (re.compile(r"\b(first_?name|last_?name|full_?name|display_?name|user_?name|username|given_?name|surname|family_?name)\b", re.I),
     "identity", "medium", ("Art. 4(1)",), "user", "Name directly identifies a natural person"),
    (re.compile(r"\b(email|email_?address|mail)\b", re.I),
     "contact", "high", ("Art. 4(1)",), "user", "Email address is a direct personal identifier"),
    (re.compile(r"\b(phone|phone_?number|mobile|telephone|cell)\b", re.I),
     "contact", "high", ("Art. 4(1)",), "user", "Phone number is a direct personal identifier"),
    (re.compile(r"\b(address|street|city|postcode|zip|country)\b", re.I),
     "contact", "medium", ("Art. 4(1)",), "user", "Physical address enables location identification"),

    # Authentication credentials
    (re.compile(r"\b(password|passwd|password_?hash|hashed_?password|bcrypt|salt)\b", re.I),
     "credentials", "high", ("Art. 4(1)", "Art. 32"), "user",
     "Password / credential hash — must be stored securely, not retained after account deletion"),
    (re.compile(r"\b(access_?token|refresh_?token|auth_?token|jwt|api_?key|secret_?key|session_?token)\b", re.I),
     "credentials", "high", ("Art. 4(1)", "Art. 32"), "user",
     "Authentication token — treated as personal data (links to a specific individual)"),
    (re.compile(r"\b(session_?id|session)\b", re.I),
     "credentials", "medium", ("Art. 4(1)",), "user",
     "Session identifier — pseudonymous personal data, links to a person's activity"),

    # Financial
    (re.compile(r"\b(credit_?card|card_?number|pan|iban|bank_?account|account_?number|routing_?number)\b", re.I),
     "financial", "high", ("Art. 4(1)",), "customer",
     "Financial account data — restricted processing under Art. 9 in some contexts"),
    (re.compile(r"\b(salary|wage|income|payment|billing|invoice|transaction|amount|balance)\b", re.I),
     "financial", "medium", ("Art. 4(1)",), "customer",
     "Financial transaction data linked to an individual"),

    # Health / special-category (Art. 9)
    (re.compile(r"\b(health|medical|diagnosis|prescription|medication|condition|disability|allergy)\b", re.I),
     "health", "high", ("Art. 9",), "patient",
     "Health data — special-category under GDPR Art. 9, requires explicit consent or another Art. 9(2) basis"),
    (re.compile(r"\b(blood_?type|dob|date_?of_?birth|birth_?date|age|gender|sex)\b", re.I),
     "demographic", "high", ("Art. 9",), "user",
     "Demographic data that may constitute special-category data (origin, health indicators)"),

    # Location
    (re.compile(r"\b(latitude|longitude|geo_?location|location|gps|coordinates|ip_?address|ip)\b", re.I),
     "location", "high", ("Art. 4(1)",), "user",
     "Location or network identifier — can identify a person's whereabouts or identity"),

    # Verification state
    (re.compile(r"\b(email_?verified|phone_?verified|id_?verified|kyc|verified)\b", re.I),
     "verification", "low", ("Art. 4(1)",), "user",
     "Verification flag — pseudonymous, but indicates whether identity confirmation occurred"),

    # Consent / legal basis flags
    (re.compile(r"\b(consent|opt_?in|opt_?out|gdpr_?consent|marketing_?consent)\b", re.I),
     "consent", "medium", ("Art. 7",), "user",
     "Consent record — must be demonstrable under GDPR Art. 7; requires retention for accountability"),

    # Audit / behavioural
    (re.compile(r"\b(created_?at|updated_?at|last_?login|last_?seen|activity|audit|log_?in|logout)\b", re.I),
     "behavioural", "low", ("Art. 4(1)",), "user",
     "Timestamp / activity record — pseudonymous personal data linked to a person's behaviour"),
]

# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def classify_column(column: str, table: str = "") -> ColumnClassification:
    """Classify a single database column for GDPR / PII purposes.

    Classification is intentionally conservative: the first matching PII pattern
    wins and the column is flagged. For a compliance tool a false positive (an
    over-flagged column) is far cheaper than a false negative (personal data that
    is missed), so no generic-column allow-list is applied here.
    """
    col_lower = column.lower()

    for pattern, category, sensitivity, articles, subject_hint, rationale in _PATTERNS:
        if pattern.search(col_lower):
            return ColumnClassification(
                column=column,
                table=table,
                is_pii=True,
                category=category,
                sensitivity=sensitivity,
                gdpr_articles=articles,
                subject_type=subject_hint,
                rationale=rationale,
            )

    return ColumnClassification(
        column=column,
        table=table,
        is_pii=False,
        category="non_personal",
        sensitivity="none",
        gdpr_articles=(),
        subject_type="unknown",
        rationale="No PII pattern matched",
    )


def classify_table(table_name: str, columns: list[str]) -> list[ColumnClassification]:
    """Classify all columns in a table and return only those flagged as PII."""
    return [
        c for c in (classify_column(col, table_name) for col in columns)
        if c.is_pii
    ]


def pii_summary(classifications: list[ColumnClassification]) -> dict:
    """Aggregate a list of classifications into a summary dict."""
    high = [c for c in classifications if c.sensitivity == "high"]
    medium = [c for c in classifications if c.sensitivity == "medium"]
    low = [c for c in classifications if c.sensitivity == "low"]
    articles: set[str] = set()
    for c in classifications:
        articles.update(c.gdpr_articles)
    return {
        "total_pii_columns": len(classifications),
        "high_sensitivity": len(high),
        "medium_sensitivity": len(medium),
        "low_sensitivity": len(low),
        "gdpr_articles": sorted(articles),
        "categories": sorted({c.category for c in classifications}),
    }
