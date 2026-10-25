# storage_copilot/security/pii_sanitizer.py
import re

# Tier 1 Zero-Tolerance Redaction Patterns with strict word-boundary & context guards
PII_PATTERNS = [
    # 1. Labeled Passports (e.g., "Passport No: PK9823412", "Passport: A1234567")
    (
        re.compile(r"(?i)\b(passport(?:\s*(?:no|number|#))?[:\s]+)[A-Za-z0-9-]{6,12}\b"),
        r"\1[REDACTED_PASSPORT]",
    ),
    # Standalone Passport format: 2 uppercase letters (excluding academic prefixes BS, CS, AI, SE, EE, MS, PH, FA, SP) + 7 digits,
    # or standard ICAO 1-letter + 7-8 digits when not preceded by hyphen/dot/academic markers
    (
        re.compile(
            r"(?<![A-Za-z0-9_.-])(?!(?:BS|CS|AI|SE|EE|MS|FA|SP)\d)[A-Z]{2}\d{7}(?![A-Za-z0-9_.-])"
        ),
        "[REDACTED_PASSPORT]",
    ),
    (
        re.compile(
            r"(?<![A-Za-z0-9_.-])[A-PR-WY][1-9]\d\s?\d{4}[1-9](?![A-Za-z0-9_.-])"
        ),
        "[REDACTED_PASSPORT]",
    ),

    # 2. National ID / CNIC (formatted 5-7-1, labeled CNIC, and valid 13-digit Pakistani CNICs)
    (
        re.compile(r"(?<!\d)\d{5}-\d{7}-\d(?!\d)"),
        "[REDACTED_CNIC]",
    ),
    (
        re.compile(r"(?i)\b(?:cnic|national\s+id|nic|b-form)(?:\s*(?:no|number|#))?[:\s]*\b(\d{5,15})\b"),
        "CNIC: [REDACTED_CNIC]",
    ),
    # Unhyphenated 13-digit CNIC: Pakistani CNICs start with region code 1-8 (primarily 1-7)
    # Exclude 13-digit Unix millisecond timestamps (e.g., 16xxxxxxxxxxx or 17xxxxxxxxxxx preceded by _ or timestamp=)
    (
        re.compile(r"(?<![A-Za-z0-9_=-])(?!(?:16|17)\d{8}000\b)[1-8]\d{12}(?![A-Za-z0-9_.-])"),
        "[REDACTED_CNIC]",
    ),

    # 3. Bank Accounts & IBAN
    (
        re.compile(r"(?<![A-Za-z0-9])[A-Z]{2}\d{2}[A-Z0-9]{12,30}(?![A-Za-z0-9])"),
        "[REDACTED_IBAN]",
    ),
    (
        re.compile(r"(?i)\b(?:account|acc|acct)(?:\s*(?:no|number|#))?[:\s]*(\d{10,24})\b"),
        "Account: [REDACTED_ACCOUNT]",
    ),

    # 4. Phone Numbers (Pakistani mobile +923xx / 03xx and international formats)
    # Negative lookbehind prevents matching inside ArXiv IDs or floating point numbers
    (
        re.compile(r"(?<![\d.])(?:\+92[- ]?|0)?3\d{2}[- ]?\d{7}(?!\d)"),
        "[REDACTED_PHONE]",
    ),
    (
        re.compile(r"(?<![\d.])(?:\+\d{1,3}[- ])?\(\d{3}\)[- ]?\d{3}[- ]\d{4}(?!\d)"),
        "[REDACTED_PHONE]",
    ),

    # 5. Payment Cards (16-digit Visa/Mastercard/etc.)
    (
        re.compile(r"(?<!\d)(?:\d{4}[- ]?){3}\d{4}(?!\d)"),
        "[REDACTED_CARD]",
    ),

    # 6. Email Addresses
    (
        re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"),
        "[REDACTED_EMAIL]",
    ),

    # 7. Credentials, Passwords, PINs, Secrets & API Keys
    (
        re.compile(r"(?i)\b(password|passwd|pin|secret|token|api_key)\s*([:=])\s*(\S+)"),
        r"\1\2 [REDACTED_SECRET]",
    ),

    # 8. Personal Names in formal administrative labels (Student Name, Applicant, Cardholder, etc.)
    (
        re.compile(
            r"(?i)\b((?:student\s+name|applicant\s+name|customer\s+name|cardholder\s+name|father(?:'s)?\s+name|full\s+name|name)\s*[:\-])\s*([A-Za-z][A-Za-z0-9\s.]{1,35})(?=[,\n\r;]|\s{2,}|$)"
        ),
        r"\1 [REDACTED_NAME]",
    ),
    (
        re.compile(
            r"(?i)\b((?:receipt\s+for|voucher\s+for|slip\s+for)\s+)([A-Za-z][A-Za-z\s]{1,25})(?=[,\n\r;]|\s{2,}|$)"
        ),
        r"\1[REDACTED_NAME]",
    ),
]


def sanitize_pii(text: str) -> str:
    """Pre-flight local scrubbing of sensitive personally identifiable information."""
    if not text:
        return ""
    clean = text
    for pattern, replacement in PII_PATTERNS:
        clean = pattern.sub(replacement, clean)
    return clean


def contains_tier1_pii(text: str) -> bool:
    """
    Returns True if the given filename, path, or snippet contains any unredacted Tier 1 PII
    (CNIC, Phone, Payment Card, IBAN, Email, Labeled Passport, or Secret).
    """
    if not text:
        return False
    # Check patterns 1 through 7 (strictly sensitive identifiers)
    for pattern, _ in PII_PATTERNS[:-2]:
        if pattern.search(text):
            return True
    return False