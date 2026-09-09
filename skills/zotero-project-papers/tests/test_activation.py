import json
import re
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent


class ActivationRegressionTests(unittest.TestCase):
    def test_activation_cases_are_privacy_sanitized(self):
        data = json.loads((HERE / "activation_cases.json").read_text(encoding="utf-8"))
        self.assertTrue(data["privacy"]["sanitized"])
        blob = json.dumps(data, ensure_ascii=False)
        # Keep regression fixtures synthetic: no URLs, DOI strings, Windows absolute paths,
        # Unix home paths, or reported metric-style decimals.
        self.assertIsNone(re.search(r"https?://", blob))
        self.assertIsNone(re.search(r"\b10\.\d{4,9}/", blob))
        self.assertIsNone(re.search(r"[A-Za-z]:\\\\", blob))
        self.assertNotIn("/home/", blob)
        self.assertIsNone(re.search(r"\b0\.\d{3,}\b", blob))

    def test_conversation_triggers_only_on_explicit_literature_turn(self):
        data = json.loads((HERE / "activation_cases.json").read_text(encoding="utf-8"))
        reg = data["conversationRegression"]
        self.assertEqual(reg["expectedFirstTriggerTurn"], 4)
        self.assertNotIn("论文", reg["turns"][0])
        self.assertNotIn("论文", reg["turns"][1])
        self.assertNotIn("论文", reg["turns"][2])
        self.assertIn("顶级会议论文", reg["turns"][3])

    def test_skill_description_is_narrow(self):
        skill = (HERE.parent / "SKILL.md").read_text(encoding="utf-8")
        first = skill.split("---", 2)[1]
        self.assertIn("explicitly asks", first)
        self.assertIn("Do not activate merely", first)


if __name__ == "__main__":
    unittest.main()
