from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from unittest.mock import Mock

from scripts.feishu_application_inbox import (
    ApplicationInbox,
    FeishuInboxClient,
    InboxStore,
    InboxStateError,
    extract_urls,
    is_official_job_url,
    parse_message_event,
    sdk_event_payload,
    verify_request,
    DeepSeekIdentityEnricher,
)


def event(message_id: str, content: dict, *, message_type: str = "text", chat_type: str = "p2p") -> dict:
    return {
        "schema": "2.0",
        "header": {"event_id": f"evt-{message_id}"},
        "event": {
            "sender": {"sender_id": {"open_id": "ou-test"}},
            "message": {
                "message_id": message_id,
                "chat_id": "oc-test",
                "chat_type": chat_type,
                "message_type": message_type,
                "create_time": "2026-09-23T09:00:00+08:00",
                "content": json.dumps(content, ensure_ascii=False),
            },
        },
    }


class FakeClient:
    def __init__(self) -> None:
        self.jobs: list[dict] = []
        self.records: list[dict] = []

    def download_image(self, message_id: str, file_key: str, destination: Path) -> bytes:
        data = b"fake-image-" + file_key.encode()
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(data)
        return data

    def upsert_applied(self, job: dict, evidence: dict) -> tuple[str, bool]:
        self.jobs.append(job)
        return "rec-test", True

    def record_link(self, record_id: str) -> str:
        return f"https://feishu.test/record/{record_id}"

    def matching_records(self, _job: dict) -> list[dict]:
        return self.records

    def company_title_records(self, _job: dict) -> list[dict]:
        return self.records


class FeishuApplicationInboxTests(unittest.TestCase):
    def test_deepseek_identity_enricher_normalizes_json_and_redacts_contacts(self) -> None:
        response = Mock()
        response.status_code = 200
        response.json.return_value = {
            "output_text": json.dumps(
                {
                    "company": "华大BGI",
                    "title": "AI技术方向储备生（2027届）（J28522）",
                    "location": "深圳",
                    "confidence": "HIGH",
                    "evidence": "heading",
                },
                ensure_ascii=False,
            )
        }
        with patch.dict("os.environ", {"DEEPSEEK_API_KEY": "sk-test-key"}), patch(
            "scripts.feishu_application_inbox.requests.post", return_value=response
        ) as post:
            result = DeepSeekIdentityEnricher({"enabled": True})(
                "华大BGI\n+86 10000000000\n职位 ARAB (2027%R) (J28522)"
            )
        self.assertEqual(result["company"], "华大BGI")
        request_body = post.call_args.kwargs["json"]
        self.assertEqual(request_body["model"], "deepseek-flash")
        self.assertNotIn("10000000000", request_body["input"])

    def test_message_parser_and_url_cleanup(self) -> None:
        parsed = parse_message_event(event("m1", {"text": "公司：Acme 职位：后端工程师 https://example.test/job/1。"}))
        self.assertEqual(parsed.message_id, "m1")
        self.assertEqual(parsed.chat_type, "p2p")
        self.assertEqual(parsed.urls, ["https://example.test/job/1"])
        self.assertEqual(extract_urls("https://example.test/job/1", "https://example.test/job/1"), ["https://example.test/job/1"])

    def test_public_board_url_is_evidence_not_official_url(self) -> None:
        self.assertFalse(is_official_job_url("https://www.zhipin.com/job_detail/1", {"status_code": 200}, "Acme"))
        self.assertTrue(is_official_job_url("https://acme.myworkdayjobs.com/job/1", {}, "Acme"))

    def test_image_download_uses_message_resource_endpoint(self) -> None:
        response = Mock()
        response.iter_content.return_value = [b"image"]
        response.headers = {"content-type": "image/png"}
        with tempfile.TemporaryDirectory() as directory, patch("scripts.feishu_application_inbox.requests.get", return_value=response) as get:
            client = FeishuInboxClient(object())
            client.token = lambda: "token"  # type: ignore[method-assign]
            client.download_image("om-message", "img-key", Path(directory) / "image.bin")
            get.assert_called_once_with(
                "https://open.feishu.cn/open-apis/im/v1/messages/om-message/resources/img-key",
                headers={"Authorization": "Bearer token"},
                params={"type": "image"},
                timeout=30,
                stream=True,
            )

    def test_http_verification_requires_configured_token(self) -> None:
        self.assertFalse(verify_request({"header": {"token": "x"}}, ""))
        self.assertTrue(verify_request({"header": {"token": "x"}}, "x"))

    def test_empty_live_whitelist_rejects_sender(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            inbox = ApplicationInbox(InboxStore(Path(directory) / "state.json"), allowed_open_ids=set())
            result = inbox.process_event(event("m-deny", {"text": "公司：Acme 职位：测试"}))
            self.assertEqual(result.status, "IGNORED")

    def test_corrupt_state_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            path.write_text("not-json", encoding="utf-8")
            with self.assertRaises(InboxStateError):
                InboxStore(path).read()

    def test_sdk_payload_adapter_accepts_event_model_shape(self) -> None:
        class EventModel:
            def model_dump(self):
                return {"message": {"message_id": "m-sdk", "chat_type": "p2p"}}

        class DataModel:
            event = EventModel()
            header = {"event_id": "evt-sdk"}

        payload = sdk_event_payload(DataModel())
        self.assertEqual(payload["event"]["message"]["message_id"], "m-sdk")

    def test_sdk_payload_adapter_recurses_generated_sdk_objects(self) -> None:
        class Message:
            def __init__(self) -> None:
                self.message_id = "m-sdk-object"
                self.chat_type = "p2p"

        class SenderId:
            def __init__(self) -> None:
                self.open_id = "ou-sdk-object"

        class Sender:
            def __init__(self) -> None:
                self.sender_id = SenderId()

        class Event:
            def __init__(self) -> None:
                self.message = Message()
                self.sender = Sender()

        class DataModel:
            def __init__(self) -> None:
                self.event = Event()
                self.header = {"event_id": "evt-sdk-object"}

        payload = sdk_event_payload(DataModel())
        self.assertEqual(payload["event"]["message"]["message_id"], "m-sdk-object")
        self.assertEqual(payload["event"]["sender"]["sender_id"]["open_id"], "ou-sdk-object")

    def test_url_application_is_idempotent_and_defaults_message_time(self) -> None:
        with tempfile.TemporaryDirectory() as directory, patch(
            "scripts.feishu_application_inbox.parse_public_page", return_value={"text": "", "title": "", "meta": {}}
        ):
            client = FakeClient()
            inbox = ApplicationInbox(InboxStore(Path(directory) / "state.json"), client=client)
            first = inbox.process_event(event("m1", {"text": "公司：Acme 职位：后端工程师 地点：深圳 https://example.test/job/1"}))
            second = inbox.process_event(event("m1", {"text": "公司：Acme 职位：后端工程师 地点：深圳 https://example.test/job/1"}))
            self.assertEqual(first.status, "APPLIED")
            self.assertIn("消息发送时间", first.message)
            self.assertTrue(second.duplicate)
            self.assertEqual(len(client.jobs), 1)
            self.assertEqual(client.jobs[0]["official_url"], "")
            self.assertEqual(client.jobs[0]["source_url"], "https://example.test/job/1")
            jobs = json.loads((Path(directory) / "feishu_application_jobs.json").read_text(encoding="utf-8"))
            self.assertEqual(jobs[0]["stage"], "APPLIED")

    def test_verified_ats_url_is_written_as_official_url(self) -> None:
        with tempfile.TemporaryDirectory() as directory, patch(
            "scripts.feishu_application_inbox.parse_public_page", return_value={"text": "", "title": "", "meta": {}}
        ):
            client = FakeClient()
            inbox = ApplicationInbox(InboxStore(Path(directory) / "state.json"), client=client)
            result = inbox.process_event(
                event("m-ats", {"text": "公司：Acme 职位：后端开发 https://acme.myworkdayjobs.com/job/1"})
            )
            self.assertEqual(result.status, "APPLIED")
            self.assertEqual(client.jobs[0]["official_url"], "https://acme.myworkdayjobs.com/job/1")

    def test_raw_event_is_retained_for_restart_recovery(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = InboxStore(Path(directory) / "state.json")
            payload = event("m-raw", {"text": "公司：Acme 职位：测试开发"})
            envelope, early = ApplicationInbox(store).persist_event(payload)
            self.assertIsNotNone(envelope)
            self.assertIsNone(early)
            state = json.loads((Path(directory) / "state.json").read_text(encoding="utf-8"))
            self.assertEqual(state["events"]["m-raw"]["raw_event"]["event"]["message"]["message_id"], "m-raw")
            result = ApplicationInbox(store, client=FakeClient()).process_event(payload)
            self.assertEqual(result.status, "APPLIED")
            state = json.loads((Path(directory) / "state.json").read_text(encoding="utf-8"))
            self.assertEqual(state["events"]["m-raw"]["raw_event"]["event"]["message"]["message_id"], "m-raw")

    def test_ambiguous_existing_jobs_become_pending(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            client = FakeClient()
            client.records = [{"record_id": "r1"}, {"record_id": "r2"}]
            inbox = ApplicationInbox(InboxStore(Path(directory) / "state.json"), client=client)
            result = inbox.process_event(event("m-ambiguous", {"text": "公司：Acme 职位：测试开发"}))
            self.assertEqual(result.status, "NEEDS_INFO")
            self.assertIn("同名", result.message)

    def test_missing_location_does_not_merge_single_same_title_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            client = FakeClient()
            client.records = [{"record_id": "r1"}]
            inbox = ApplicationInbox(InboxStore(Path(directory) / "state.json"), client=client)
            result = inbox.process_event(event("m-no-location", {"text": "公司：Acme 职位：测试开发"}))
            self.assertEqual(result.status, "NEEDS_INFO")
            self.assertIn("地点", result.message)

    def test_screenshot_application_keeps_original_and_uses_ocr(self) -> None:
        with tempfile.TemporaryDirectory() as directory, patch(
            "scripts.feishu_application_inbox.parse_public_page", return_value={"text": "", "title": "", "meta": {}}
        ):
            client = FakeClient()
            inbox = ApplicationInbox(
                InboxStore(Path(directory) / "state.json"),
                client=client,
                ocr=lambda _path: "公司：Robot Co\n职位：测试开发工程师\n地点：深圳",
            )
            result = inbox.process_event(event("m2", {"image_key": "img-key"}, message_type="image"))
            self.assertEqual(result.status, "APPLIED")
            evidence = list((Path(directory) / "feishu_inbox_evidence").glob("*.bin"))
            self.assertEqual(len(evidence), 1)
            self.assertEqual(client.jobs[0]["official_url"], "")
            self.assertTrue(client.jobs[0]["canonical_key"].startswith("manual:"))

    def test_unlabelled_job_heading_ocr_infers_company_and_title(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            client = FakeClient()
            inbox = ApplicationInbox(
                InboxStore(Path(directory) / "state.json"),
                client=client,
                ocr=lambda _path: "华大BGI\n首页 社会招聘 校园招聘\nAI技术方向储备生（2027届）（J28522）\n工作职责",
            )
            result = inbox.process_event(event("m-heading", {"image_key": "img-heading"}, message_type="image"))
            self.assertEqual(result.status, "APPLIED")
            self.assertEqual(client.jobs[0]["company"], "华大BGI")
            self.assertEqual(client.jobs[0]["title"], "AI技术方向储备生（2027届）（J28522）")

    def test_enabled_identity_cleanup_fails_closed_when_provider_is_unavailable(self) -> None:
        with tempfile.TemporaryDirectory() as directory, patch.dict("os.environ", {}, clear=True):
            client = FakeClient()
            inbox = ApplicationInbox(
                InboxStore(Path(directory) / "state.json"),
                client=client,
                identity_settings={"enabled": True, "provider": "deepseek"},
                ocr=lambda _path: "ARAB (2027%R) (J28522)\nBm tae Ea Bae IG ZUoint Cultivation",
            )
            result = inbox.process_event(event("m-no-model", {"image_key": "img-no-model"}, message_type="image"))
            self.assertEqual(result.status, "NEEDS_INFO")
            self.assertEqual(client.jobs, [])

    def test_resending_same_screenshot_identity_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            client = FakeClient()
            inbox = ApplicationInbox(
                InboxStore(Path(directory) / "state.json"),
                client=client,
                ocr=lambda _path: "公司：Robot Co\n职位：测试开发工程师\n地点：深圳",
            )
            first = inbox.process_event(event("m-image-1", {"image_key": "img-1"}, message_type="image"))
            second = inbox.process_event(event("m-image-2", {"image_key": "img-2"}, message_type="image"))
            self.assertEqual(first.status, "APPLIED")
            self.assertEqual(second.status, "APPLIED")
            self.assertEqual(len(client.jobs), 2)  # upsert is called; local key is the dedup boundary
            self.assertEqual(client.jobs[0]["canonical_key"], client.jobs[1]["canonical_key"])
            state = json.loads((Path(directory) / "state.json").read_text(encoding="utf-8"))
            self.assertEqual(len(state["applications"]), 1)

    def test_missing_identity_is_persisted_as_pending_and_asks_one_question(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            inbox = ApplicationInbox(InboxStore(Path(directory) / "state.json"), ocr=lambda _path: "地点：深圳")
            result = inbox.process_event(event("m3", {"text": "https://example.test/restricted"}))
            self.assertEqual(result.status, "NEEDS_INFO")
            self.assertIn("公司和职位", result.message)
            state = json.loads((Path(directory) / "state.json").read_text(encoding="utf-8"))
            self.assertEqual(len(state["pending"]), 1)

    def test_restricted_page_title_is_not_fabricated_identity(self) -> None:
        with tempfile.TemporaryDirectory() as directory, patch(
            "scripts.feishu_application_inbox.parse_public_page",
            return_value={"text": "请登录", "title": "请登录", "meta": {}, "restricted": True, "status_code": 403},
        ):
            inbox = ApplicationInbox(InboxStore(Path(directory) / "state.json"), client=FakeClient())
            result = inbox.process_event(event("m-restricted", {"text": "https://www.zhipin.com/job/1"}))
            self.assertEqual(result.status, "NEEDS_INFO")

    def test_duplicate_unidentified_screenshot_reuses_pending_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            inbox = ApplicationInbox(
                InboxStore(Path(directory) / "state.json"),
                ocr=lambda _path: "地点：深圳",
                client=FakeClient(),
            )
            first = inbox.process_event(event("m-pending-image-1", {"image_key": "same"}, message_type="image"))
            second = inbox.process_event(event("m-pending-image-2", {"image_key": "same"}, message_type="image"))
            self.assertEqual(first.status, "NEEDS_INFO")
            self.assertEqual(second.status, "NEEDS_INFO")
            state = json.loads((Path(directory) / "state.json").read_text(encoding="utf-8"))
            self.assertEqual(len(state["pending"]), 1)

    def test_follow_up_text_completes_pending_identity(self) -> None:
        with tempfile.TemporaryDirectory() as directory, patch(
            "scripts.feishu_application_inbox.parse_public_page", return_value={"text": "", "title": "", "meta": {}}
        ):
            client = FakeClient()
            inbox = ApplicationInbox(InboxStore(Path(directory) / "state.json"), client=client)
            pending = inbox.process_event(event("m-pending", {"text": "职位：测试开发 地点：深圳"}))
            self.assertEqual(pending.status, "NEEDS_INFO")
            completed = inbox.process_event(event("m-follow-up", {"text": "公司：Acme"}))
            self.assertEqual(completed.status, "APPLIED")
            self.assertEqual(client.jobs[0]["company"], "Acme")

    def test_do_not_apply_is_not_employer_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory, patch(
            "scripts.feishu_application_inbox.parse_public_page", return_value={"text": "", "title": "", "meta": {}}
        ):
            inbox = ApplicationInbox(InboxStore(Path(directory) / "state.json"))
            result = inbox.process_event(event("m4", {"text": "我不投 公司：Acme 职位：测试开发 地点：深圳 https://example.test/job/2"}))
            self.assertEqual(result.status, "WITHDRAWN")
            self.assertNotIn("REJECTED", result.message)

    def test_group_message_is_ignored(self) -> None:
        inbox = ApplicationInbox(InboxStore(Path(tempfile.mkdtemp()) / "state.json"))
        result = inbox.process_event(event("m5", {"text": "公司：Acme 职位：测试"}, chat_type="group"))
        self.assertEqual(result.status, "IGNORED")

    def test_processing_marker_is_replayable_after_restart(self) -> None:
        with tempfile.TemporaryDirectory() as directory, patch(
            "scripts.feishu_application_inbox.parse_public_page", return_value={"text": "", "title": "", "meta": {}}
        ):
            path = Path(directory) / "state.json"
            store = InboxStore(path)
            state = store.read()
            state["events"]["m-restart"] = {"event_id": "evt-m-restart", "status": "PROCESSING"}
            store.write(state)
            client = FakeClient()
            result = ApplicationInbox(store, client=client).process_event(
                event("m-restart", {"text": "公司：Acme 职位：测试开发 地点：深圳 https://example.test/job/3"})
            )
            self.assertEqual(result.status, "APPLIED")

    def test_recover_processing_scans_raw_events_on_startup(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            store = InboxStore(path)
            payload = event("m-recover", {"text": "公司：Acme 职位：测试开发"})
            state = store.read()
            state["events"]["m-recover"] = {
                "event_id": "evt-m-recover",
                "message_id": "m-recover",
                "status": "PROCESSING",
                "raw_event": payload,
            }
            store.write(state)
            results = ApplicationInbox(store, client=FakeClient()).recover_processing()
            self.assertEqual([item.status for item in results], ["APPLIED"])


if __name__ == "__main__":
    unittest.main()
