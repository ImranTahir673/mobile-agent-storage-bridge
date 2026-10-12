import os
import json
import re

# Test how a unified prompt with real files works for the Java query
manifest = [
    {"filename": "1508.06576v2_neural_style.pdf", "path": "Download/1508.06576v2_neural_style.pdf", "size_kb": 124.5, "snippet": "A Neural Algorithm of Artistic Style Leon A. Gatys"},
    {"filename": "JavaTheCompleteReference.pdf", "path": "Download/JavaTheCompleteReference.pdf", "size_kb": 8500.0, "snippet": "Java: The Complete Reference, Eleventh Edition by Herbert Schildt"},
    {"filename": "BSAI-182_fee_voucher_fall.pdf", "path": "Download/BSAI-182_fee_voucher_fall.pdf", "size_kb": 88.0, "snippet": "Student Fee Voucher: Name: Imran Tahir, Roll No: BSAI-182"},
    {"filename": "the-48-laws-of-power-robert-greene-2000--annas-archive--libgenrs-nf-1160432.pdf", "path": "Download/the-48-laws-of-power-robert-greene-2000--annas-archive--libgenrs-nf-1160432.pdf", "size_kb": 920.0, "snippet": "The 48 Laws of Power by Robert Greene"}
]

user_query = "Also can you find me a book for Java Language in the downloads?"

# In the unified architecture, Gemini receives this manifest directly!
print(f"User Query: {user_query}")
print(f"Manifest sent to Gemini: {len(manifest)} files")
for m in manifest:
    if "java" in m["filename"].lower() or "java" in m["snippet"].lower():
        print(f"  Direct match found: {m['filename']} ({m['path']})")
