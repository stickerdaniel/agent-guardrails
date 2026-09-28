import unittest

from tests import yamlsubset


class NullTests(unittest.TestCase):
    def test_null_spellings_read_like_an_empty_value(self) -> None:
        for text in ("null", "Null", "NULL", "~"):
            with self.subTest(text=text):
                self.assertEqual(yamlsubset.load(f"on:\n  pull_request: {text}\n"), {"on": {"pull_request": None}})
        self.assertEqual(yamlsubset.load("on:\n  pull_request:\n"), {"on": {"pull_request": None}})

    def test_quoted_null_stays_a_string(self) -> None:
        self.assertEqual(yamlsubset.load('x: "null"'), {"x": "null"})


class FlowSequenceTests(unittest.TestCase):
    def test_flow_sequence_of_scalars(self) -> None:
        self.assertEqual(yamlsubset.load("types: [opened, edited]"), {"types": ["opened", "edited"]})
        self.assertEqual(yamlsubset.load("types: []"), {"types": []})

    def test_empty_entry_is_rejected(self) -> None:
        for text in ("[a,,b]", "[a,]", "[,a]"):
            with self.subTest(text=text), self.assertRaises(ValueError):
                yamlsubset.load(f"types: {text}")


if __name__ == "__main__":
    unittest.main()
