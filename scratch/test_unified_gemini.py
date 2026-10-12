import os
import json
from server import call_gemini_json, APP_VERSION

manifest = [
    {"filename": "1508.06576v2_neural_style.pdf", "path": "Download/1508.06576v2_neural_style.pdf", "size_kb": 124.5, "snippet": "A Neural Algorithm of Artistic Style Leon A. Gatys"},
    {"filename": "JavaTheCompleteReference.pdf", "path": "Download/JavaTheCompleteReference.pdf", "size_kb": 8500.0, "snippet": "Java: The Complete Reference, Eleventh Edition by Herbert Schildt. Comprehensive guide to the Java language, syntax, libraries, and best practices."},
    {"filename": "BSAI-182_fee_voucher_fall.pdf", "path": "Download/BSAI-182_fee_voucher_fall.pdf", "size_kb": 88.0, "snippet": "Student Fee Voucher: Name: Imran Tahir, Roll No: BSAI-182"},
    {"filename": "fawad fee.pdf", "path": "Download/fawad fee.pdf", "size_kb": 79.0, "snippet": "Student Fee Slip: Name: Fawad, Roll No: BSAI-190"},
    {"filename": "Sumbal pass.pdf", "path": "Download/Sumbal pass.pdf", "size_kb": 69.0, "snippet": "University Entry Pass: Student Name: Sumbal, Department: AI."},
    {"filename": "the-48-laws-of-power-robert-greene-2000--annas-archive--libgenrs-nf-1160432.pdf", "path": "Download/the-48-laws-of-power-robert-greene-2000--annas-archive--libgenrs-nf-1160432.pdf", "size_kb": 920.0, "snippet": "The 48 Laws of Power by Robert Greene"}
]

subfolders = ["com_account_usercenter", "Seal (34 items)", "zapya (15 items)"]

system_prompt = f"""You are Storage Copilot, an articulate on-device AI agent running locally via Termux on Android for Imran Tahir (Roll/ID: ['BSAI-182'], Major: Artificial Intelligence).
Storage Root: /storage/emulated/0 (Downloads, Documents).
Known Peers: Fawad, Sumbal, Ahmed, Yousaf.
Peer Routing:
- Fawad -> Documents/Peers/Fawad/
- Sumbal -> Documents/Peers/Sumbal/
- Ahmed -> Documents/Peers/Ahmed/
- Yousaf -> Documents/Peers/Yousaf/

Directory Status for target folder 'Download/':
- Top-level subfolders: {", ".join(subfolders)}
- Direct files indexed ({len(manifest)} files):
{json.dumps(manifest, indent=2)}

INSTRUCTIONS & REASONING RULES:
1. Directly analyze the user's operational request with full access to the real file manifest above.
2. If the user asks a question, lookup, or search (e.g. "find Java book", "receipts", "what's in downloads"):
   - Set "type": "query_info"
   - Inspect the manifest directly. If matching documents exist (e.g. JavaTheCompleteReference.pdf for Java language), cite them with exact names, paths, sizes, and helpful details.
   - Set "actions": []
3. If the user asks for suggestions, ideas, or casual conversation:
   - Set "type": "conversation"
   - Provide articulate guidance tailored to their actual files.
   - Set "actions": []
4. If the user commands an action or confirms a plan (e.g. "Yes", "Organize peer documents", "Sort vouchers"):
   - Set "type": "plan"
   - Stage exact move/rename/trash actions referencing exact filenames from the manifest.

Respond STRICTLY in valid JSON adhering to:
{{
  "type": "conversation" | "query_info" | "plan",
  "message": "<Articulate, empathetic, direct response>",
  "actions": [ ... optional list of actions if plan ... ],
  "quick_chips": [ ... 3-4 dynamic chips ... ]
}}
"""

user_query = "Also can you find me a book for Java Language in the downloads?"
resp = call_gemini_json(system_prompt, user_query)
print("Response from Gemini:")
print(json.dumps(resp, indent=2))
