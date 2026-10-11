import unittest

from applyflow.text import contains_phrase, extract_emails, html_to_text, matching_phrases


class HtmlToTextTests(unittest.TestCase):
    def test_unescapes_entity_encoded_html(self):
        # Greenhouse returns HTML that is itself entity-escaped.
        raw = "&lt;p&gt;Build APIs&lt;/p&gt;&lt;ul&gt;&lt;li&gt;Python &amp;amp; AWS&lt;/li&gt;&lt;/ul&gt;"
        self.assertEqual(html_to_text(raw), "Build APIs\n\nPython & AWS")

    def test_plain_html(self):
        self.assertEqual(html_to_text("<div>Hello<br>world</div>"), "Hello\nworld")

    def test_empty(self):
        self.assertEqual(html_to_text(None), "")


class PhraseTests(unittest.TestCase):
    def test_whole_word(self):
        self.assertTrue(contains_phrase("Senior Engineer", "senior"))
        self.assertFalse(contains_phrase("Seniority matters", "senior"))
        self.assertFalse(contains_phrase("JavaScript", "java"))
        self.assertFalse(contains_phrase("Leading teams", "lead"))

    def test_symbols_and_spacing(self):
        self.assertTrue(contains_phrase("We use C# and .NET", "c#"))
        self.assertTrue(contains_phrase("Node.js services", "node.js"))
        self.assertTrue(contains_phrase("Full-stack role", "full stack"))
        self.assertTrue(contains_phrase("SDE-2 opening", "sde 2"))

    def test_matching_phrases(self):
        self.assertEqual(matching_phrases("Python and AWS Lambda", ["python", "lambda", "go"]), ["python", "lambda"])


class EmailTests(unittest.TestCase):
    def test_plain_and_obfuscated(self):
        text = "Write to jobs@acme.io. Or hiring [at] acme [dot] com, or careers at foo dot dev."
        self.assertEqual(extract_emails(text), ["jobs@acme.io", "hiring@acme.com", "careers@foo.dev"])

    def test_prose_is_not_an_email(self):
        self.assertEqual(extract_emails("Come work with us at Acme. We ship fast."), [])

    def test_dedupes(self):
        self.assertEqual(extract_emails("a@b.co and A@B.co"), ["a@b.co"])


if __name__ == "__main__":
    unittest.main()
