import os
import sys
import unittest
import json
import glob

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from database.models import JobRecord, JobStage, SessionItem


def parse_quota_string(quota_str: str) -> dict:
    distribution = {}
    for pair in quota_str.split(","):
        if ":" in pair:
            op, count = pair.split(":")
            distribution[op.strip()] = int(count.strip())
    return distribution


def calculate_even_split(total: int, operators: list) -> dict:
    if not operators:
        return {}
    per_op = max(1, total // len(operators))
    rem = total % len(operators)
    distribution = {}
    for i, op in enumerate(operators):
        distribution[op] = per_op + (1 if i < rem else 0)
    return distribution


class TestDistribution(unittest.TestCase):
    def test_quota_string_parsing(self):
        quota_str = "OP-9879:8, OP-2:14, OP-3:28"
        dist = parse_quota_string(quota_str)
        self.assertEqual(dist["OP-9879"], 8)
        self.assertEqual(dist["OP-2"], 14)
        self.assertEqual(dist["OP-3"], 28)
        self.assertEqual(sum(dist.values()), 50)

    def test_even_split_distribution(self):
        ops = ["OP-1", "OP-2", "OP-3"]
        dist = calculate_even_split(50, ops)
        self.assertEqual(dist["OP-1"], 17)
        self.assertEqual(dist["OP-2"], 17)
        self.assertEqual(dist["OP-3"], 16)
        self.assertEqual(sum(dist.values()), 50)

    def test_receiver_profiles_validity(self):
        receivers_dir = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "trading",
            "receivers"
        )
        json_files = glob.glob(os.path.join(receivers_dir, "*.json"))
        self.assertGreaterEqual(len(json_files), 11, "Must contain all 11 receiver profiles")

        for fpath in json_files:
            with open(fpath, "r", encoding="utf-8") as f:
                data = json.load(f)
                self.assertIn("bot_username", data, f"Missing bot_username in {fpath}")
                self.assertIn("name", data, f"Missing name in {fpath}")
                self.assertTrue(data["bot_username"].startswith("@") or len(data["bot_username"]) > 0)


if __name__ == "__main__":
    unittest.main()
