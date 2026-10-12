import os

fixtures_dir = os.path.join(os.path.dirname(__file__), "..", "fixtures", "Download")
os.makedirs(fixtures_dir, exist_ok=True)

mobile_files = {
    "the-48-laws-of-power-robert-greene-2000--annas-archive--libgenrs-nf-1160432.pdf": "%PDF-1.4 The 48 Laws of Power by Robert Greene. Book on strategy and power dynamics.",
    "DOC-20250413-WA0017..pdf": "%PDF-1.4 Artificial Intelligence BSAI Semester 6 Knowledge Representation Lecture 04.",
    "DOC-20250413-WA0018..pdf": "%PDF-1.4 Department of AI Course Assignment 02 Neural Networks and Deep Learning.",
    "DOC-20250413-WA0019..pdf": "%PDF-1.4 Islamabad Electric Supply Company IESCO Electricity Bill Consumer Invoice March 2025.",
    "JavaTheCompleteReference.pdf": "%PDF-1.4 Java: The Complete Reference Eleventh Edition by Herbert Schildt. Programming textbook.",
    "Best Resume_CV Templete For CS _ AI_DS.pdf": "%PDF-1.4 Professional Curriculum Vitae and Resume template for Computer Science and AI.",
    "Imran_Tahir_certificate.pdf": "%PDF-1.4 Certificate of Completion awarded to Imran Tahir for AI & Data Science.",
    "Yousaf Pass.pdf": "%PDF-1.4 Student Identity Card and Entry Pass for Yousaf. Roll: BSAI-195.",
    "Sumbal pass.pdf": "%PDF-1.4 University Entry Pass: Student Name: Sumbal, Department: AI.",
    "fawad fee.pdf": "%PDF-1.4 Student Fee Slip: Name: Fawad, Roll No: BSAI-190, Tuition Fee Challan.",
    "BSAI-182_fee_voucher_fall.pdf": "%PDF-1.4 National University Student Tuition Fee Voucher: Imran Tahir Roll: BSAI-182 Fall Semester.",
    "1508.06576v2_neural_style.pdf": "%PDF-1.4 A Neural Algorithm of Artistic Style by Leon A. Gatys ArXiv research paper.",
    "Screenshot_20241001-142210.png": "PNG_IMAGE_DATA_SCREENSHOT",
    "temp_cache_sync.tmp": "TEMP_CACHE_DATA_EXPIRED",
    "Project_Progress_Report.docx": "Project Progress Report: IR Solutions Mobile Agent Development status and roadmap.",
    "SRS IR SOLUTION  edit.docx": "Software Requirements Specification (SRS) for IR Solutions intelligent mobile storage agent.",
    "VBN FYP Idea Presentation.pptx": "Final Year Project Idea Presentation: Vision Based Navigation and AI Drone System."
}

for filename, content in mobile_files.items():
    p = os.path.join(fixtures_dir, filename)
    with open(p, "w", encoding="utf-8") as f:
        f.write(content)

print(f"Successfully populated {len(mobile_files)} mobile files in fixtures/Download")
