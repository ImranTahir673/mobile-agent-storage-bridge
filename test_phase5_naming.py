"""
Storage Copilot: Phase 5 Content-Derived Collision-Free Naming Engine Test Suite
Verifies:
1. Dynamic Sequence Extraction (`extract_sequence_info`):
   - Extracts Lecture 4 -> 04, Lecture 7 -> 07, Assignment 2 -> 02, Assignment 5 -> 05,
     Lab 3 -> 03, Chapter 1 -> 01, Week 5 -> 05, Tutorial 4 -> 04.
2. Academic Topic & Research Paper Title Extraction (`ContentDerivedNamer`):
   - Preserves canonical names (`Knowledge_Representation_Lecture_04.pdf`,
     `AI_Course_Assignment_02.pdf`, `Neural_Algorithm_Artistic_Style_Paper.pdf`).
   - Dynamically adapts to new sequence numbers (e.g. Lecture 9 -> `_09`, Assignment 5 -> `_05`, Lab 3 -> `AI_Lab_03.pdf`).
3. Financial Bill Entity & Period Extraction:
   - Extracts utility + period (`Electricity Utility Bill Invoice for March 2025` -> `Electricity_Utility_Bill_March2025.pdf`
     or `Electricity_Bill_Invoice.pdf` in concise mode; `Gas Bill for October 2026` -> `Gas_Utility_Bill_October2026.pdf`).
4. User-Configurable Naming Templates (`user_profile.json` `naming_templates`):
   - Honors custom templates like `{course}_{topic}_Lec{sequence:02d}` and `{utility}_Invoice_{period}`.
5. Batch Collision-Free Guarantee:
   - Multiple files with identical content topics in the same batch automatically disambiguate to unique destination paths.
"""
import os
import unittest
import tempfile

test_storage_dir = tempfile.mkdtemp(prefix="storage_phase5_naming_")
os.environ["STORAGE_BASE_DIR"] = test_storage_dir
os.environ["LEDGER_DB_PATH"] = os.path.join(test_storage_dir, ".ledger.db")
os.environ["GEMINI_API_KEY"] = ""

import server
from storage_copilot.storage.namer import (
    ContentDerivedNamer,
    extract_sequence_info,
    extract_academic_topic,
    extract_financial_bill_info,
)
from storage_copilot.intelligence.document_classifier import classify_document
from storage_copilot.storage.collision_resolver import ensure_unique_plan_destinations


class TestPhase5ContentDerivedNaming(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        server.BASE_STORAGE_DIR = os.path.realpath(test_storage_dir)
        server.BASE_DIR = server.BASE_STORAGE_DIR
        server.TRASH_DIR = os.path.join(server.BASE_DIR, ".agent_trash")
        server.LEDGER_DB_PATH = os.path.join(server.BASE_DIR, ".ledger.db")
        server.ENFORCE_LOCAL_AUTH = False
        os.makedirs(os.path.join(server.BASE_DIR, "Download"), exist_ok=True)
        os.makedirs(os.path.join(server.BASE_DIR, "Documents"), exist_ok=True)
        server.init_db()
        cls.client = server.app.test_client()
        cls.client.post("/seed_fixtures")

    def test_01_dynamic_sequence_extraction(self):
        """Verify dynamic regex extraction of Lecture, Assignment, Lab, Chapter, Week, and Tutorial numbers."""
        self.assertEqual(
            extract_sequence_info("Knowledge Representation and Reasoning Lecture 4: First-Order Logic"),
            ("Lecture", 4)
        )
        self.assertEqual(
            extract_sequence_info("Deep Learning Lecture 11: Transformers and Attention"),
            ("Lecture", 11)
        )
        self.assertEqual(
            extract_sequence_info("Artificial Intelligence Course Assignment 2: A* Search"),
            ("Assignment", 2)
        )
        self.assertEqual(
            extract_sequence_info("Machine Learning Homework 5: Support Vector Machines"),
            ("Assignment", 5)
        )
        self.assertEqual(
            extract_sequence_info("Artificial Intelligence Lab Report 3: Minimax Game Trees"),
            ("Lab", 3)
        )
        self.assertEqual(
            extract_sequence_info("Chapter 1: Introduction to Intelligent Agents"),
            ("Chapter", 1)
        )
        self.assertEqual(
            extract_sequence_info("Week 6: Probabilistic Reasoning and Bayesian Networks"),
            ("Week", 6)
        )
        self.assertEqual(
            extract_sequence_info("Tutorial #4: Backpropagation Derivation"),
            ("Tutorial", 4)
        )

    def test_02_canonical_backward_compatibility_and_dynamic_sequences(self):
        """Verify exact backward-compatible titles for existing fixtures AND dynamic numbers for new files."""
        namer = ContentDerivedNamer()

        # Existing fixtures must match 100% verbatim
        t_kr4 = namer.derive_academic_title(
            "DOC-20250413-WA0017..pdf",
            "Knowledge Representation and Reasoning Lecture 4: First-Order Predicate Logic, Ontologies. BSAI Semester 6."
        )
        self.assertEqual(t_kr4, "Knowledge_Representation_Lecture_04.pdf")

        t_assign2 = namer.derive_academic_title(
            "DOC-20250413-WA0018..pdf",
            "Artificial Intelligence Course Assignment 2: Heuristic Search and A* Graph Exploration by Imran Tahir."
        )
        self.assertEqual(t_assign2, "AI_Course_Assignment_02.pdf")

        t_paper = namer.derive_academic_title(
            "1508.06576v2_neural_style.pdf",
            "A Neural Algorithm of Artistic Style by Leon A. Gatys, Alexander S. Ecker, Matthias Bethge. ArXiv research paper."
        )
        self.assertEqual(t_paper, "Neural_Algorithm_Artistic_Style_Paper.pdf")

        # Dynamic new sequence numbers (proving no hardcoded _04 or _02)
        t_kr9 = namer.derive_academic_title(
            "DOC-20250501-WA0099.pdf",
            "Knowledge Representation and Reasoning Lecture 9: Markov Decision Processes. BSAI Semester 6."
        )
        self.assertEqual(t_kr9, "Knowledge_Representation_Lecture_09.pdf")

        t_assign7 = namer.derive_academic_title(
            "DOC-20250502-WA0100.pdf",
            "Artificial Intelligence Course Assignment 7: Reinforcement Learning Q-Tables."
        )
        self.assertEqual(t_assign7, "AI_Course_Assignment_07.pdf")

        t_lab3 = namer.derive_academic_title(
            "DOC-20250503-WA0101.pdf",
            "Artificial Intelligence Lab 3: Breadth-First and Depth-First Search in Python."
        )
        self.assertEqual(t_lab3, "AI_Lab_03.pdf")

    def test_03_financial_bill_entity_and_period_extraction(self):
        """Verify utility type and billing month/year extraction."""
        namer = ContentDerivedNamer()

        # Detailed mode (used by classify_document)
        b_mar = namer.derive_financial_bill_title(
            "DOC-20250413-WA0019..pdf",
            "Electricity Utility Bill Invoice for March 2025. Consumer ID: 948201. Amount Due: PKR 8,500.",
            concise_invoice=False
        )
        self.assertEqual(b_mar, "Electricity_Utility_Bill_March2025.pdf")

        # Concise mode (used by Clean Download Names)
        b_concise = namer.derive_financial_bill_title(
            "DOC-20250413-WA0019..pdf",
            "Electricity Utility Bill Invoice for March 2025. Consumer ID: 948201.",
            concise_invoice=True
        )
        self.assertEqual(b_concise, "Electricity_Bill_Invoice.pdf")

        # Dynamic Gas bill for October 2026
        b_gas = namer.derive_financial_bill_title(
            "DOC-20261015-WA0042.pdf",
            "Gas Bill Invoice for October 2026. Amount Due: PKR 3,200.",
            concise_invoice=False
        )
        self.assertEqual(b_gas, "Gas_Utility_Bill_October2026.pdf")

    def test_04_user_configurable_naming_templates(self):
        """Verify custom naming_templates in user_profile override default formats cleanly."""
        custom_profile = {
            "naming_templates": {
                "lecture": "{course}_{topic}_Lec{sequence:02d}",
                "assignment": "{course}_HW_{sequence:02d}",
                "financial_bill": "{utility}_Bill_{period}"
            }
        }
        c_lec = classify_document(
            "DOC-20250413-WA0017..pdf",
            "Knowledge Representation and Reasoning Lecture 4: First-Order Predicate Logic.",
            user_profile=custom_profile
        )
        self.assertEqual(c_lec["suggested_title"], "AI_Knowledge_Representation_Lec04.pdf")

        c_hw = classify_document(
            "DOC-20250413-WA0018..pdf",
            "Artificial Intelligence Course Assignment 2: Heuristic Search.",
            user_profile=custom_profile
        )
        self.assertEqual(c_hw["suggested_title"], "AI_HW_02.pdf")

        c_bill = classify_document(
            "DOC-20250413-WA0019..pdf",
            "Electricity Utility Bill Invoice for March 2025.",
            user_profile=custom_profile
        )
        self.assertEqual(c_bill["suggested_title"], "Electricity_Bill_March2025.pdf")

    def test_05_collision_free_batch_disambiguation(self):
        """Verify two files generating the same content-derived title in a batch never collide."""
        namer = ContentDerivedNamer()
        snippet = "Knowledge Representation and Reasoning Lecture 4: First-Order Predicate Logic."
        t1 = namer.derive_academic_title("DOC-20250413-WA0017.pdf", snippet)
        t2 = namer.derive_academic_title("DOC-20250413-WA0025.pdf", snippet)

        raw_actions = [
            {
                "action_id": "step-1",
                "type": "move",
                "source": "Download/DOC-20250413-WA0017.pdf",
                "destination": f"Documents/University/AI/{t1}"
            },
            {
                "action_id": "step-2",
                "type": "move",
                "source": "Download/DOC-20250413-WA0025.pdf",
                "destination": f"Documents/University/AI/{t2}"
            }
        ]
        deduped = ensure_unique_plan_destinations(raw_actions)
        dests = [a["destination"] for a in deduped]
        self.assertEqual(len(dests), len(set(d.lower() for d in dests)))
        self.assertEqual(dests[0], "Documents/University/AI/Knowledge_Representation_Lecture_04.pdf")
        self.assertIn("WA0025", dests[1])


if __name__ == "__main__":
    unittest.main()
