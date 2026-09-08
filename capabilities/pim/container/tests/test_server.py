import json
import threading
import unittest
from unittest import mock

import capability_pb2
import server


class FakeSmtp:
    refused = {}
    send_exception = None
    login_exception = None
    quit_exception = None
    last = None

    def __init__(self, host, port, timeout):
        self.host = host
        self.port = port
        self.timeout = timeout
        self.message = None
        self.recipients = None
        self.tls_context = None
        self.ehlo_count = 0
        FakeSmtp.last = self

    def ehlo(self):
        self.ehlo_count += 1

    def starttls(self, context):
        self.tls_context = context

    def login(self, email_address, password):
        if self.login_exception:
            raise self.login_exception

    def send_message(self, message, from_addr, to_addrs):
        self.message = message
        self.recipients = list(to_addrs)
        if self.send_exception:
            raise self.send_exception
        return dict(self.refused)

    def quit(self):
        if self.quit_exception:
            raise self.quit_exception

    def close(self):
        return None


class SendEmailTests(unittest.TestCase):
    def setUp(self):
        FakeSmtp.refused = {}
        FakeSmtp.send_exception = None
        FakeSmtp.login_exception = None
        FakeSmtp.quit_exception = None
        FakeSmtp.last = None
        self.config = {
            "EMAIL_ADDRESS": "sender@icloud.com",
            "APP_PASSWORD": "app-password",
        }
        self.args = {
            "to": '"Doe, Jane" <jane@example.com>, john@example.com',
            "subject": "Status",
            "body": "Hello",
        }
        self.artifacts = {}
        self.lock = threading.Lock()
        self.tls_context = object()
        self.smtp_patch = mock.patch.object(server.smtplib, "SMTP", FakeSmtp)
        self.tls_patch = mock.patch.object(
            server.ssl, "create_default_context", return_value=self.tls_context
        )
        self.smtp_patch.start()
        self.tls_patch.start()

    def tearDown(self):
        self.tls_patch.stop()
        self.smtp_patch.stop()

    def send(self, args=None):
        return json.loads(
            server.handle_send_email(
                args or self.args,
                self.config,
                self.artifacts,
                self.lock,
            )
        )

    def test_full_acceptance_has_stable_headers_and_tls(self):
        result = self.send()

        self.assertTrue(result["ok"])
        self.assertEqual(result["delivery_status"], "accepted")
        self.assertFalse(result["retry_safe"])
        self.assertEqual(
            result["accepted_recipients"],
            ["jane@example.com", "john@example.com"],
        )
        self.assertRegex(result["message_id"], r"^<[^<>]+@icloud\.com>$")
        self.assertEqual(FakeSmtp.last.timeout, server.NETWORK_TIMEOUT_SECONDS)
        self.assertIs(FakeSmtp.last.tls_context, self.tls_context)
        self.assertEqual(FakeSmtp.last.ehlo_count, 2)
        self.assertEqual(FakeSmtp.last.message["Message-ID"], result["message_id"])
        self.assertTrue(FakeSmtp.last.message["Date"])

    def test_partial_refusal_is_not_reported_as_success(self):
        FakeSmtp.refused = {"john@example.com": (550, b"rejected")}

        result = self.send()

        self.assertFalse(result["ok"])
        self.assertEqual(result["delivery_status"], "partial")
        self.assertEqual(result["accepted_recipients"], ["jane@example.com"])
        self.assertEqual(result["refused_recipients"], ["john@example.com"])
        self.assertFalse(result["retry_safe"])

    def test_unknown_delivery_is_not_safe_to_retry_and_consumes_attachment(self):
        self.artifacts["artifact-1"] = {
            "filename": "report.pdf",
            "mime_type": "application/pdf",
            "data": b"pdf",
        }
        args = dict(self.args)
        args["attachments"] = [{"artifact_id": "artifact-1"}]
        FakeSmtp.send_exception = server.smtplib.SMTPServerDisconnected("lost")

        result = self.send(args)

        self.assertEqual(result["delivery_status"], "unknown")
        self.assertFalse(result["retry_safe"])
        self.assertNotIn("artifact-1", self.artifacts)

    def test_definite_auth_failure_preserves_attachment(self):
        self.artifacts["artifact-1"] = {
            "filename": "report.pdf",
            "mime_type": "application/pdf",
            "data": b"pdf",
        }
        args = dict(self.args)
        args["attachments"] = [{"capability_artifact_id": "artifact-1"}]
        FakeSmtp.login_exception = server.smtplib.SMTPAuthenticationError(535, b"bad")

        with self.assertRaises(server.ToolFailure) as raised:
            self.send(args)

        self.assertEqual(raised.exception.code, "authentication_failed")
        self.assertIn("artifact-1", self.artifacts)

    def test_all_recipients_refused_is_a_definite_failure(self):
        FakeSmtp.send_exception = server.smtplib.SMTPRecipientsRefused(
            {"jane@example.com": (550, b"no"), "john@example.com": (550, b"no")}
        )

        with self.assertRaises(server.ToolFailure) as raised:
            self.send()

        self.assertEqual(raised.exception.code, "recipients_refused")

    def test_quit_failure_does_not_overturn_acceptance(self):
        FakeSmtp.quit_exception = server.smtplib.SMTPServerDisconnected("quit failed")

        result = self.send()

        self.assertEqual(result["delivery_status"], "accepted")

    def test_reply_requires_rfc_message_id(self):
        args = dict(self.args)
        args["reply_to_email_id"] = "12345"

        with self.assertRaises(server.ToolFailure) as raised:
            self.send(args)

        self.assertEqual(raised.exception.code, "invalid_reply_message_id")


class InvokeContractTests(unittest.TestCase):
    def setUp(self):
        self.servicer = server.PimCapabilityServicer()
        self.config = json.dumps(
            {"EMAIL_ADDRESS": "sender@icloud.com", "APP_PASSWORD": "secret"}
        ).encode()

    def request(self, tool_name="check_email", args=b"{}"):
        return capability_pb2.InvokeRequest(
            tool_name=tool_name,
            capability_id="pim",
            args_json=args,
            config_json=self.config,
        )

    def test_legacy_handler_error_uses_protobuf_error_without_leaking_details(self):
        with mock.patch.object(
            server,
            "handle_check_email",
            return_value=json.dumps({"error": "backend secret detail"}),
        ):
            response = self.servicer.Invoke(self.request(), None)

        self.assertFalse(response.result_json)
        self.assertIn("check_email_failed", response.error)
        self.assertNotIn("backend secret detail", response.error)
        self.assertNotIn("secret", response.error)

    def test_malformed_or_non_object_arguments_are_rejected(self):
        for args in [b"{", b"[]", b'"query"']:
            with self.subTest(args=args):
                response = self.servicer.Invoke(self.request(args=args), None)
                self.assertFalse(response.result_json)
                self.assertIn("invalid_arguments", response.error)

    def test_streaming_mirrors_invoke_errors(self):
        chunks = list(self.servicer.StreamInvoke(self.request(tool_name="unknown"), None))

        self.assertEqual(len(chunks), 1)
        self.assertTrue(chunks[0].done)
        self.assertIn("unknown_tool", chunks[0].error)
        self.assertFalse(chunks[0].data)


if __name__ == "__main__":
    unittest.main()
