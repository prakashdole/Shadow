"""Real provider calls with synthetic images. Skipped unless SHADOW_LIVE_PROVIDERS=1."""

from contextlib import redirect_stdout
from io import StringIO
import json
import os
from pathlib import Path
from datetime import datetime, timezone
import subprocess
from tempfile import TemporaryDirectory
import unittest

from PIL import Image, ImageDraw, ImageFont

from dotenv import load_dotenv

from shadow.gemini_client import ProviderFailure, gemini_keys
from shadow.model_openai import openai_api_key
from shadow.screenshot_loop import (
    LiveVision, draft_and_save, gemini_fallback, openai_primary, parse_transcript,
)
from shadow.screen_decision import choose_action
from shadow.storage import open_storage
from shadow.viewport import label, png_bytes

load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))
FONT = next((path for path in (
    "/System/Library/Fonts/Supplemental/Arial.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
) if Path(path).is_file()), "DejaVuSans.ttf")
NOW = datetime(2026, 11, 21, tzinfo=timezone.utc)
LIVE = os.environ.get("SHADOW_LIVE_PROVIDERS") == "1"


def bubble(order, side, text, media=None):
    return {"order": order, "side": side, "text": text, "quote": None, "media": media,
            "unreadable": False, "clipped": False}


def _font(size):
    return ImageFont.truetype(FONT, size)


def chat_image(rows):
    image = Image.new("RGB", (1280, 720), "#efeae2")
    draw = ImageDraw.Draw(image)
    font = _font(32)
    y = 70
    for side, text in rows:
        width = int(font.getlength(text)) + 48
        if side == "left":
            box = (48, y, 48 + width, y + 64)
            fill = "#ffffff"
        else:
            box = (1232 - width, y, 1232, y + 64)
            fill = "#d9fdd3"
        draw.rounded_rectangle(box, 16, fill=fill, outline="#d5d0c8")
        draw.text((box[0] + 20, y + 14), text, font=font, fill="#111111")
        y += 88
    return png_bytes(image)


def inbox_image(names):
    image = Image.new("RGB", (1280, 720), "#f4f6f8")
    draw = ImageDraw.Draw(image)
    font = _font(36)
    draw.rectangle((0, 0, 1280, 64), fill="#23324d")
    boxes = []
    for index, name in enumerate(names):
        top = 100 + index * 120
        box = (40, top, 520, top + 80)
        draw.rounded_rectangle(box, 12, fill="#ffffff", outline="#c5ced8")
        draw.text((70, top + 20), name, font=font, fill="#182134")
        boxes.append((f"row-{index + 1}", box))
    return image, boxes


@unittest.skipUnless(LIVE, "set SHADOW_LIVE_PROVIDERS=1 to call Gemini and OpenAI")
class ProviderTests(unittest.TestCase):
    def test_chromium_to_saved_draft(self):
        from tools.chromium_trial import local_fixture
        from tools.screenshot_turn import main

        self.assertTrue(gemini_keys() and openai_api_key())
        with local_fixture("screenshot_drafts.html") as url:
            for case in ("plain", "reel"):
                with self.subTest(case=case), TemporaryDirectory() as directory:
                    stdout = StringIO()
                    with redirect_stdout(stdout):
                        main(["--data-dir", directory, "--fixture", f"{url}?{case}"])
                    result = json.loads(stdout.getvalue())
                    self.assertEqual(result["outcome"], "draft", result)
                    self.assertEqual(result["opened"], "row-1")
                    self.assertEqual((result["sends"], result["reactions"]), (0, 0))
                    with open_storage(directory) as storage:
                        record = json.loads(storage.decisions_for(result["conversation_id"])[0]["record_json"])
                        self.assertEqual(len(record["reply"]["messages"]), 1)
                        self.assertEqual(storage.outbox_items(), [])
                    if case == "plain":
                        self.assertIsNone(record["media"])
                        self.assertIn("chai", record["bubbles"][-1]["text"].lower())
                    else:
                        self.assertTrue(record["media"]["advanced"])
                        self.assertGreaterEqual(record["media"]["distinct"], 2)
                        self.assertIn("kite", json.dumps(record["media"]).lower())
                        self.assertIsNone(record["media"]["speech"])
                    print(json.dumps({"check": "chromium_saved_draft", "case": case,
                                      "model": result["model"], "sends": 0, "reactions": 0}))

    def test_decision_transcription_reel_draft_and_fallback(self):
        keys = gemini_keys()
        openai = openai_api_key()
        self.assertTrue(keys and openai)
        vision = LiveVision(keys)

        image, boxes = inbox_image(["TOP ROW", "BOTTOM ROW"])
        choice, confidence = choose_action(keys[0], label(image, boxes), "Open the lower inbox row.", {
            "row-1": "The upper row.", "row-2": "The lower row.",
            "hold": "The lower row is not visible."})
        self.assertEqual(choice, "row-2")
        self.assertGreaterEqual(confidence, 0.8)
        print(json.dumps({"check": "decision", "choice": choice, "confidence": round(confidence, 3)}))

        same = inbox_image(["Sam", "Sam"])
        names, name_confidence = choose_action(keys[0], label(*same), "Do the visible inbox rows show unique display names?", {
            "unique": "Every visible inbox row has a distinct display name.",
            "duplicate": "Two visible rows show the same display name.",
            "hold": "The names cannot be read."})
        self.assertNotEqual((names, name_confidence >= 0.8), ("unique", True))
        print(json.dumps({"check": "duplicate", "choice": names, "confidence": round(name_confidence, 3)}))

        transcript = parse_transcript(vision.transcribe(chat_image([
            ("left", "haan bilkul"), ("left", "haan bilkul"), ("right", "ok")])))
        texts = [item["text"].strip().lower() for item in transcript["bubbles"] if item["side"] == "left"]
        self.assertEqual(texts.count("haan bilkul"), 2)
        print(json.dumps({"check": "transcript", "bubbles": len(transcript["bubbles"])}))

        held = parse_transcript(vision.transcribe(chat_image([
            ("left", "kal subah nikalna hai"), ("left", "kal subah nikalna hai"),
            ("right", "theek"), ("left", "ticket is ₹240")])))
        held_left = [item["text"] for item in held["bubbles"] if item["side"] == "left"]
        self.assertEqual(sum(item["text"].strip().lower() == "kal subah nikalna hai" for item in held["bubbles"]), 2)
        self.assertTrue(any("240" in item["text"] and "241" not in item["text"] for item in held["bubbles"]))
        self.assertGreaterEqual(len(held_left), 2)
        print(json.dumps({"check": "heldout_transcript", "bubbles": len(held["bubbles"])}))

        frames = []
        for index, word in enumerate(("red kite", "kite climbs", "kite lands")):
            frame = Image.new("RGB", (480, 270), "#882222")
            ImageDraw.Draw(frame).text((40, 110), word, font=_font(36), fill="white")
            frames.append((float(index), png_bytes(frame)))
        clip = _clip()
        described = vision.describe(frames, "which kite?", audio=False)
        self.assertIsNotNone(clip)
        import base64
        from shadow.gemini_client import generate
        from shadow.screenshot_loop import _one_reel
        clipped = generate(model="gemini-3.1-flash-lite", keys=keys, parts=[
            {"text": "Silent clip plus the frames above. Audio has no speech. "
                     "Return one JSON object with subject, action, on_screen_text, speech, missing. speech must be null."},
            {"inline_data": {"mime_type": "video/mp4", "data": base64.b64encode(clip).decode("ascii")}},
            {"inline_data": {"mime_type": "image/png", "data": base64.b64encode(frames[-1][1]).decode("ascii")}}])
        folded = _one_reel(json.loads(clipped["text"]), audio=False)
        self.assertIsNone(folded["speech"])
        self.assertIn("kite", json.dumps(folded).lower())
        print(json.dumps({"check": "clip", "model": clipped["model"], "usage": clipped["usage"]}))
        blob = json.dumps(described).lower()
        self.assertIn("kite", blob)
        self.assertIsNone(described.get("speech"))
        print(json.dumps({"check": "reel", "model": described.get("model"), "usage": described.get("usage")}))

        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        with open_storage(directory.name) as storage:
            def primary(system, user):
                raise AssertionError("primary should be replaced")

            drafted = draft_and_save(
                storage, account_id="acct", thread_id="101",
                bubbles=[bubble(1, "left", "yaar kal milte hai?")], media=None,
                primary=openai_primary(openai), fallback=lambda *_args: (_ for _ in ()).throw(AssertionError("no fallback")),
                now=NOW)
            self.assertEqual(drafted["outcome"], "draft")
            self.assertTrue(str(drafted["model"]).startswith("gpt-4o"))
            record = json.loads(storage.decisions_for(drafted["conversation_id"])[0]["record_json"])
            self.assertEqual(len(record["reply"]["messages"]), 1)
            self.assertEqual(storage.outbox_items(), [])
            versions = json.loads(storage.decisions_for(drafted["conversation_id"])[0]["versions_json"])
            print(json.dumps({"check": "draft", "model": drafted["model"], "outcome": drafted["outcome"],
                              "latency_ms": versions.get("latency_ms"), "usage": versions.get("usage")}))

            forced = draft_and_save(
                storage, account_id="acct", thread_id="202",
                bubbles=[bubble(1, "left", "kya kar rahe ho?")], media=None,
                primary=lambda system, user: (_ for _ in ()).throw(ProviderFailure("provider")),
                fallback=gemini_fallback(keys), now=NOW)
            self.assertEqual(forced["used"], "fallback")
            self.assertIn(forced["outcome"], ("draft", "hold"))
            fallback_versions = json.loads(storage.decisions_for(forced["conversation_id"])[-1]["versions_json"])
            print(json.dumps({"check": "fallback", "model": forced.get("model"), "outcome": forced["outcome"],
                              "latency_ms": fallback_versions.get("latency_ms"),
                              "usage": fallback_versions.get("usage")}))

        middle, middle_boxes = inbox_image(["North", "Middle", "South"])
        held_choice, held_confidence = choose_action(
            keys[0], label(middle, middle_boxes), "Open the middle inbox row.", {
                "row-1": "The top row.", "row-2": "The middle row.", "row-3": "The bottom row.",
                "hold": "The middle row is not visible."})
        print(json.dumps({"check": "heldout_row", "choice": held_choice,
                          "confidence": round(held_confidence, 3)}))
        self.assertEqual(held_choice, "row-2")
        self.assertGreaterEqual(held_confidence, 0.8)


def _clip():
    directory = TemporaryDirectory()
    path = directory.name + "/clip.mp4"
    try:
        subprocess.run([
            "ffmpeg", "-y", "-f", "lavfi", "-i", "color=c=0x882222:s=320x240:d=2",
            "-f", "lavfi", "-i", "anullsrc=r=44100:cl=mono", "-shortest",
            "-pix_fmt", "yuv420p", path,
        ], check=True, capture_output=True)
        with open(path, "rb") as handle:
            return handle.read()
    finally:
        directory.cleanup()


class KeyTests(unittest.TestCase):
    def test_owner_openai_key_does_not_replace_the_existing_name(self):
        self.assertEqual(openai_api_key({"OPENAI_API_KEY": "sk-owner"}), "sk-owner")
        self.assertEqual(openai_api_key({"SHADOW_OPENAI_API_KEY": "sk-old", "OPENAI_API_KEY": "sk-owner"}), "sk-old")


if __name__ == "__main__":
    unittest.main()
