import re

PII_PATTERNS = [
    # Passports (labeled and standalone patterns)
    (re.compile(r"(?i)\b(passport(?:\s*(?:no|number|#))?[:\s]*)[A-Za-z0-9]{6,12}\b"), r"\1[REDACTED_PASSPORT]"),
    (re.compile(r"\b[A-Za-z]{1,2}[0-9]{7,8}\b"), "[REDACTED_PASSPORT]"),

    # National ID / CNIC (both formatted 5-7-1 and unformatted 13 digits)
    (re.compile(r"\b\d{5}-\d{7}-\d\b"), "[REDACTED_CNIC]"),
    (re.compile(r"(?i)\b(?:cnic|national\s+id|nic|b-form)(?:\s*(?:no|number|#))?[:\s]*\b(\d+)\b"), "CNIC: [REDACTED_CNIC]"),
    (re.compile(r"\b\d{13}\b"), "[REDACTED_CNIC]"),

    # Bank Accounts & IBAN
    (re.compile(r"\b[A-Z]{2}\d{2}[A-Z0-9]{12,30}\b"), "[REDACTED_IBAN]"),
    (re.compile(r"(?i)\b(?:account|acc|acct)(?:\s*(?:no|number|#))?[:\s]*(\d{10,24})\b"), "Account: [REDACTED_ACCOUNT]"),

    # Phone Numbers
    (re.compile(r"(?:\+92[- ]?|0)?3\d{2}[- ]?\d{7}\b"), "[REDACTED_PHONE]"),
    (re.compile(r"\b(?:\+?\d{1,3}[- ]?)?\(?\d{3}\)?[- ]?\d{3}[- ]?\d{4}\b"), "[REDACTED_PHONE]"),

    # Payment Cards
    (re.compile(r"\b(?:\d{4}[- ]?){3}\d{4}\b"), "[REDACTED_CARD]"),

    # Email Addresses
    (re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"), "[REDACTED_EMAIL]"),

    # Personal Names in formal administrative labels (Student Name, Applicant, Cardholder, etc.)
    (re.compile(r"(?i)\b((?:student\s+name|applicant\s+name|customer\s+name|cardholder\s+name|father(?:'s)?\s+name|full\s+name|name)\s*[:\-])\s*([A-Za-z][A-Za-z0-9\s.]{1,35})(?=[,\n\r;]|\s{2,}|$)", re.IGNORECASE), r"\1 [REDACTED_NAME]"),
    (re.compile(r"(?i)\b((?:receipt\s+for|voucher\s+for|slip\s+for)\s+)([A-Za-z][A-Za-z\s]{1,25})(?=[,\n\r;]|\s{2,}|$)", re.IGNORECASE), r"\1[REDACTED_NAME]"),
]

def sanitize_pii(text: str) -> str:
    """Pre-flight local scrubbing of sensitive personally identifiable information."""
    if not text:
        return ""
    clean = text
    for pattern, replacement in PII_PATTERNS:
        clean = pattern.sub(replacement, clean)
    return clean
