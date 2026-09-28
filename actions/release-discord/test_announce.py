import io
import json
import os
import tempfile
import unittest

import announce

REPO = "DigitalGuards/example"
WEBHOOK = "https://discord.com/api/webhooks/123456/abc-DEF_789"


def release(body, **extra):
    return {
        "tag_name": "v1.2.3",
        "html_url": f"https://github.com/{REPO}/releases/tag/v1.2.3",
        "body": body,
        "prerelease": False,
        "draft": False,
        **extra,
    }


class WebhookTests(unittest.TestCase):
    def test_github_suffix_is_removed(self):
        self.assertEqual(announce.normalize_webhook(WEBHOOK + "/github\n"), WEBHOOK)

    def test_other_hosts_are_refused_without_echoing_the_url(self):
        with self.assertRaises(announce.AnnounceError) as caught:
            announce.normalize_webhook("https://example.com/api/webhooks/1/secret-token")
        self.assertNotIn("secret-token", str(caught.exception))


class ConversionTests(unittest.TestCase):
    def convert(self, text):
        return announce.to_discord(text, REPO)

    def test_headings_move_below_the_title(self):
        self.assertEqual(self.convert("# One\n## Two\n### Three\n#### Four ##"), "### One\n### Two\n**Three**\n**Four**")

    def test_comments_images_and_tags_are_removed(self):
        text = "<!-- hidden -->Hi ![logo](https://x/y.png)<details><summary>More</summary>text</details><br/>"
        self.assertEqual(self.convert(text), "Hi Moretext")

    def test_autolinks_survive_tag_stripping(self):
        self.assertEqual(self.convert("See <https://example.org/a>."), "See <https://example.org/a>.")

    def test_references_become_links(self):
        text = f"Fixes #65 and https://github.com/{REPO}/pull/66 and https://github.com/theQRL/qrysm/pull/7"
        self.assertEqual(
            self.convert(text),
            f"Fixes [#65](https://github.com/{REPO}/issues/65) and "
            f"[#66](https://github.com/{REPO}/pull/66) and "
            "[theQRL/qrysm#7](https://github.com/theQRL/qrysm/pull/7)",
        )

    def test_existing_links_and_anchors_are_left_alone(self):
        text = f"[#65](https://github.com/{REPO}/pull/65) and page#12 and &#39;"
        self.assertEqual(self.convert(text), text)

    def test_code_is_kept_verbatim(self):
        text = "Use `#65` and `<Card>`\n\n```ts\n# not a heading\n<div>#9</div>\n```\n\n# After"
        self.assertEqual(self.convert(text), "Use `#65` and `<Card>`\n\n```ts\n# not a heading\n<div>#9</div>\n```\n\n### After")

    def test_blank_runs_collapse_and_crlf_is_normalized(self):
        self.assertEqual(self.convert(announce.select_notes("a\r\n\r\n\r\n\r\nb  \r\nc")), "a\n\nb\nc")


class SelectionTests(unittest.TestCase):
    def test_marked_block_replaces_the_notes(self):
        body = "Technical notes\n<!-- discord -->\nCommunity text\n<!-- /discord -->\nChecksums"
        self.assertEqual(announce.select_notes(body).strip(), "Community text")

    def test_whole_body_without_markers(self):
        self.assertEqual(announce.select_notes("Only notes"), "Only notes")

    def test_missing_body(self):
        self.assertEqual(announce.select_notes(None), "")


class MessageTests(unittest.TestCase):
    def test_layout(self):
        message = announce.build_message("Example", release("Short notes."), REPO)
        self.assertEqual(
            message,
            "## Example v1.2.3\n\nShort notes.\n\n"
            f"Release notes and downloads: https://github.com/{REPO}/releases/tag/v1.2.3",
        )

    def test_empty_notes_keep_title_and_link(self):
        message = announce.build_message("Example", release(""), REPO)
        self.assertEqual(message.count("\n\n"), 1)

    def test_long_notes_are_cut_at_a_paragraph(self):
        paragraphs = [f"Paragraph {n} " + "x" * 300 for n in range(10)]
        message = announce.build_message("Example", release("\n\n".join(paragraphs)), REPO)
        self.assertLessEqual(len(message), announce.LIMIT)
        self.assertIn("Paragraph 4", message)
        self.assertNotIn("Paragraph 9", message)
        self.assertIn("*Continued in the release notes.*", message)
        self.assertTrue(message.endswith("/releases/tag/v1.2.3"))

    def test_one_huge_paragraph_is_cut_at_a_word(self):
        message = announce.build_message("Example", release("word " * 1000), REPO)
        self.assertLessEqual(len(message), announce.LIMIT)
        self.assertIn("word…", message)

    def test_cut_never_leaves_an_open_code_fence(self):
        body = "Intro\n\n```\n" + "\n\n".join(["line " * 40] * 20) + "\n```\n\nEnd"
        message = announce.build_message("Example", release(body), REPO)
        self.assertLessEqual(len(message), announce.LIMIT)
        self.assertEqual(message.count("```") % 2, 0)


class MainTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.sent = []

    def event(self, payload_release):
        path = os.path.join(self.temp.name, "event.json")
        with open(path, "w", encoding="utf-8") as handle:
            json.dump({"release": payload_release}, handle)
        return {
            "GITHUB_REPOSITORY": REPO,
            "GITHUB_EVENT_NAME": "release",
            "GITHUB_EVENT_PATH": path,
            "PROJECT": "Example",
            "WEBHOOK_URL": WEBHOOK + "/github",
        }

    def run_main(self, env, fetch=None):
        out = io.StringIO()

        def send(url, payload):
            self.sent.append((url, payload))
            return {"id": "42"}

        code = announce.main(env=env, fetch=fetch or self.fail_fetch, send=send, out=out)
        return code, out.getvalue()

    def fail_fetch(self, *args):
        raise AssertionError("fetch was not expected")

    def test_release_event_posts_one_message(self):
        code, output = self.run_main(self.event(release("Notes @everyone")))
        self.assertEqual(code, 0)
        self.assertIn("Discord message 42", output)
        url, payload = self.sent[0]
        self.assertEqual(url, WEBHOOK)
        self.assertEqual(payload["allowed_mentions"], {"parse": []})
        self.assertEqual(payload["flags"], announce.SUPPRESS_EMBEDS)
        self.assertEqual(payload["username"], "Example")
        self.assertIn("Notes @everyone", payload["content"])
        self.assertNotIn("::add-mask::", output)

    def test_normalized_webhook_is_masked_on_actions(self):
        env = {**self.event(release("x")), "GITHUB_ACTIONS": "true"}
        output = self.run_main(env)[1]
        self.assertTrue(output.startswith(f"::add-mask::{WEBHOOK}\n"))

    def test_prerelease_is_skipped_unless_enabled(self):
        env = self.event(release("x", prerelease=True))
        self.assertIn("not announced", self.run_main(env)[1])
        self.assertEqual(self.sent, [])
        self.run_main({**env, "PRERELEASES": "true"})
        self.assertEqual(len(self.sent), 1)

    def test_manual_tag_fetches_the_release(self):
        calls = []

        def fetch(repo, tag, token):
            calls.append((repo, tag, token))
            return release("Fetched", prerelease=True)

        env = {"GITHUB_REPOSITORY": REPO, "GITHUB_EVENT_NAME": "workflow_dispatch", "TAG": "v1.2.3", "GH_TOKEN": "t", "WEBHOOK_URL": WEBHOOK}
        self.run_main(env, fetch)
        self.assertEqual(calls, [(REPO, "v1.2.3", "t")])
        self.assertIn("Fetched", self.sent[0][1]["content"])

    def test_dry_run_needs_no_webhook(self):
        env = {**self.event(release("Preview")), "DRY_RUN": "true", "WEBHOOK_URL": ""}
        code, output = self.run_main(env)
        self.assertEqual(code, 0)
        self.assertIn("## Example v1.2.3", output)
        self.assertEqual(self.sent, [])

    def test_missing_webhook_fails_clearly(self):
        env = {**self.event(release("x")), "WEBHOOK_URL": " "}
        with self.assertRaisesRegex(announce.AnnounceError, "DISCORD_RELEASE_WEBHOOK"):
            self.run_main(env)

    def test_manual_run_without_tag_fails(self):
        with self.assertRaisesRegex(announce.AnnounceError, "set a tag"):
            self.run_main({"GITHUB_REPOSITORY": REPO, "GITHUB_EVENT_NAME": "workflow_dispatch"})

    def test_project_defaults_to_repository_name(self):
        env = self.event(release("x"))
        del env["PROJECT"]
        self.run_main(env)
        self.assertTrue(self.sent[0][1]["content"].startswith("## example v1.2.3"))



class ReviewRegressionTests(unittest.TestCase):
    def convert(self, text):
        return announce.to_discord(text, REPO)

    def test_half_marker_pair_fails_closed(self):
        for body in (
            "<!-- discord -->\nCommunity\n<!-- end discord -->\nSHA256: dead",
            "Technical\n<!-- /discord -->",
            "<!-- discord -->A<!-- /discord -->\n<!-- discord -->\nSHA256: dead",
            "<!-- /discord -->A<!-- discord -->",
        ):
            with self.assertRaisesRegex(announce.AnnounceError, "exactly one"):
                announce.select_notes(body)

    def test_numbers_inside_link_labels_are_left_alone(self):
        text = "See [issue #65](https://github.com/o/r/issues/65) and #66"
        self.assertEqual(
            self.convert(text),
            f"See [issue #65](https://github.com/o/r/issues/65) and [#66](https://github.com/{REPO}/issues/66)",
        )

    def test_urls_with_a_tail_stay_bare(self):
        for url in (
            f"https://github.com/{REPO}/pull/66/files",
            f"https://github.com/{REPO}/pull/66#issuecomment-1",
            f"https://github.com/{REPO}/issues/66?x=1",
        ):
            self.assertEqual(self.convert(url), url)

    def test_tilde_and_unclosed_fences_are_code(self):
        self.assertEqual(self.convert("~~~\n# keep\n~~~\n# After"), "~~~\n# keep\n~~~\n### After")
        self.assertEqual(self.convert("Intro\n```sh\n# echo hi\n#65"), "Intro\n```sh\n# echo hi\n#65")

    def test_heading_ending_in_hash_keeps_it(self):
        self.assertEqual(self.convert("## C#\n### F# ##"), "### C#\n**F#**")

    def test_cut_never_leaves_an_open_tilde_fence(self):
        body = "Intro\n\n~~~\n" + "\n\n".join(["line " * 40] * 20) + "\n~~~\n\nEnd"
        message = announce.build_message("Example", release(body), REPO)
        self.assertLessEqual(len(message), announce.LIMIT)
        self.assertEqual(message.count("~~~") % 2, 0)

    def test_huge_project_name_stays_under_the_limit(self):
        message = announce.build_message("P" * 1900, release("notes " * 500), REPO)
        self.assertLessEqual(len(message), announce.LIMIT)
        self.assertTrue(message.endswith("/releases/tag/v1.2.3"))

    def test_username_avoids_reserved_words(self):
        self.assertEqual(announce.webhook_username("Discord Bot"), announce.FALLBACK_USERNAME)
        self.assertEqual(announce.webhook_username("  "), announce.FALLBACK_USERNAME)
        self.assertEqual(announce.webhook_username("MyQRLWallet"), "MyQRLWallet")

    def test_retry_after_tolerates_odd_bodies(self):
        for detail in ("[]", '{"retry_after": null}', "not json", '{"retry_after": 99}'):
            self.assertLessEqual(announce._retry_after(detail), 30)

    def test_gateway_errors_and_rate_limits_are_retried(self):
        import urllib.error

        answers = [429, 503, None]
        waits = []

        class Response(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

        def opener(request, timeout):
            code = answers.pop(0)
            if code:
                raise urllib.error.HTTPError(request.full_url, code, "x", {}, io.BytesIO(b'{"retry_after": 1.5}'))
            return Response(b'{"id": "7"}')

        self.assertEqual(announce.post(WEBHOOK, {}, sleep=waits.append, opener=opener), {"id": "7"})
        self.assertEqual(waits, [1.5, 2])

    def test_client_errors_are_not_retried_and_hide_the_url(self):
        import urllib.error

        def opener(request, timeout):
            raise urllib.error.HTTPError(request.full_url, 404, "x", {}, io.BytesIO(b'{"message": "Unknown Webhook"}'))

        with self.assertRaises(announce.AnnounceError) as caught:
            announce.post(WEBHOOK, {}, sleep=self.fail, opener=opener)
        self.assertIn("HTTP 404", str(caught.exception))
        self.assertNotIn("abc-DEF_789", str(caught.exception))



class SecondRoundTests(unittest.TestCase):
    def convert(self, text):
        return announce.to_discord(text, REPO)

    def test_unclosed_fence_is_closed_before_the_footer(self):
        message = announce.build_message("Example", release("Install with:\n\n```sh\nnpm i -g thing"), REPO)
        self.assertEqual(message.count("```") % 2, 0)
        self.assertTrue(message.endswith("```\n\nRelease notes and downloads: https://github.com/DigitalGuards/example/releases/tag/v1.2.3"))

    def test_slug_ending_in_a_dash_gets_no_nested_link(self):
        url = "https://github.com/theQRL/qrysm-/pull/7"
        self.assertEqual(self.convert(url), f"[theQRL/qrysm-#7]({url})")

    def test_multi_line_link_label_is_left_alone(self):
        text = "[issue\n#65](https://x/y)"
        self.assertEqual(self.convert(text), text)

    def test_deeply_indented_fence_is_code(self):
        text = "- item\n\n    ```\n    #65\n    ```"
        self.assertEqual(self.convert(text), text)

    def test_fence_line_with_info_string_does_not_close(self):
        text = "```\ncode\n```ts\n# inside\n```\n# After"
        self.assertEqual(self.convert(text), "```\ncode\n```ts\n# inside\n```\n### After")

    def test_markers_inside_code_are_examples(self):
        body = "Use it like this:\n```\n<!-- discord -->\n```\nDone"
        self.assertEqual(announce.select_notes(body), body)


if __name__ == "__main__":
    unittest.main()
