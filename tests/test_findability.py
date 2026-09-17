"""The checks that decide whether machines can read the site."""

import unittest

from website_audit.findability import (
    has_identity_schema,
    js_dependency,
    structured_data_types,
    visible_text,
)


class StripTest(unittest.TestCase):
    def test_scripts_and_styles_are_not_content(self):
        html = """
        <html><head><style>p{color:red}</style>
        <script>var lots = "of javascript text that is not visible copy";</script></head>
        <body><h1>Real heading</h1><p>Real body copy.</p>
        <noscript>Enable JS</noscript></body></html>
        """
        text = visible_text(html)
        self.assertIn("Real heading", text)
        self.assertIn("Real body copy", text)
        self.assertNotIn("javascript text", text)
        self.assertNotIn("color:red", text)


class JSDependencyTest(unittest.TestCase):
    def test_a_server_rendered_page_scores_high(self):
        body = "The quick brown fox jumps over the lazy dog. " * 40
        result = js_dependency(f"<html><body><p>{body}</p></body></html>", body)
        self.assertTrue(result.measurable)
        self.assertGreater(result.ratio, 0.9)

    def test_a_client_rendered_page_scores_low(self):
        """An empty shell plus a bundle: the crawler sees nothing."""
        shell = '<html><body><div id="root"></div><script src="/app.js"></script></body></html>'
        rendered = "Words rendered entirely by the framework after load. " * 50
        result = js_dependency(shell, rendered)
        self.assertTrue(result.measurable)
        self.assertLess(result.ratio, 0.1)

    def test_a_short_page_is_not_judged(self):
        result = js_dependency("<html><body>Hi</body></html>", "Hi there")
        self.assertFalse(result.measurable)

    def test_ratio_never_exceeds_one(self):
        """Hidden markup can leave more text in the source than on screen."""
        result = js_dependency("<html><body>" + "word " * 500 + "</body></html>", "word word")
        self.assertLessEqual(result.ratio, 1.0)


class StructuredDataTest(unittest.TestCase):
    def test_types_are_pulled_from_nested_blocks(self):
        blocks = ['{"@context":"https://schema.org","@type":"LocalBusiness",'
                  '"address":{"@type":"PostalAddress","addressLocality":"Denison"}}']
        types = structured_data_types(blocks)
        self.assertEqual(types, ["LocalBusiness", "PostalAddress"])
        self.assertTrue(has_identity_schema(types))

    def test_a_graph_array_is_handled(self):
        blocks = ['{"@graph":[{"@type":"WebPage"},{"@type":["Organization","Thing"]}]}']
        self.assertIn("Organization", structured_data_types(blocks))

    def test_malformed_json_is_ignored_not_fatal(self):
        self.assertEqual(structured_data_types(["{not json", ""]), [])

    def test_schema_without_identity_is_recognised_as_such(self):
        types = structured_data_types(['{"@type":"BreadcrumbList"}'])
        self.assertEqual(types, ["BreadcrumbList"])
        self.assertFalse(has_identity_schema(types))


if __name__ == "__main__":
    unittest.main()


class ClientRenderedPageTest(unittest.TestCase):
    """Prove the whole path fires: raw HTML captured, ratio computed, finding raised."""

    @classmethod
    def setUpClass(cls):
        import http.server
        import socketserver
        import tempfile
        import threading
        from pathlib import Path

        from website_audit.analysis import analyse
        from website_audit.collector import collect

        directory = str(Path(__file__).parent)

        class Handler(http.server.SimpleHTTPRequestHandler):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, directory=directory, **kwargs)

            def log_message(self, *args):
                pass

        cls.server = socketserver.TCPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        port = cls.server.server_address[1]
        cls.data = collect(
            f"http://127.0.0.1:{port}/spa_fixture.html", Path(tempfile.mkdtemp())
        )
        cls.results = analyse(cls.data)

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def test_raw_html_is_captured_as_served(self):
        self.assertIn('<div id="root">', self.data.raw_html)

    def test_copy_inside_a_script_is_not_counted_as_readable(self):
        """The literal sits in the served HTML as JS source; a crawler cannot read it."""
        self.assertIn("Homes built properly", self.data.raw_html)
        self.assertNotIn("Homes built properly", visible_text(self.data.raw_html))

    def test_the_rendered_page_has_the_copy(self):
        self.assertIn("Homes built properly", self.data.probe["body_text"])

    def test_the_finding_fires(self):
        finding = next(
            (f for f in self.results["findings"] if f.id == "find-js-dependency"), None
        )
        self.assertIsNotNone(finding, "client-rendered page should be flagged")
        self.assertEqual(finding.severity, "high")
        self.assertIn("%", finding.title)
