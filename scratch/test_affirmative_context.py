import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from server import call_gemini_json

asst_msg = "Would you like me to move this PDF (the-48-laws-of-power-robert-greene-2000--annas-archive--libgenrs-nf-1160432.pdf) to a dedicated folder like Documents/Books/ to keep your storage better organized?"
user_req = f"The user confirmed with 'Yes' to proceed with the recommendation previously proposed by the assistant: '{asst_msg}'"

system_prompt = """You are Storage Copilot. Define JSON:
{
  "type": "plan",
  "message": "...",
  "actions": [...]
}
The user confirmed 'Yes' to proceed with the assistant recommendation.
Actions format:
- {"action_id": "step-1", "type": "make_dir", "path": "Documents/..."}
- {"action_id": "step-2", "type": "move", "source": "<exact filename>", "destination": "Documents/...", "rationale": "..."}
"""

manifest_sample = [{
  "filename": "the-48-laws-of-power-robert-greene-2000--annas-archive--libgenrs-nf-1160432.pdf",
  "size_kb": 0.1,
  "modified_date": "2026-10-12",
  "text_snippet": "The 48 Laws of Power by Robert Greene. Book on strategy and power dynamics.",
  "has_content": True
}]

user_prompt = f"Operational Request: \"{user_req}\"\nAvailable Real Files:\n{manifest_sample}"
resp = call_gemini_json(system_prompt, user_prompt)
print("Gemini response:", resp)
