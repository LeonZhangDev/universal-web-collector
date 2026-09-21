"""Disposable headed-browser Native Messaging probe used by the PowerShell harness."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import tempfile
import time

from playwright.sync_api import sync_playwright


EXTENSION_ID = "jaihdgjnnpmiabeoefmihmjhoodcjlhf"
HOST_NAME = "dev.zackzhang.sitefilter_collector"


def validate_ping_response(result: object) -> None:
    if not isinstance(result, dict):
        raise RuntimeError(f"native response is not an object: {result!r}")
    if set(result) != {"v", "id", "ok", "result"}:
        raise RuntimeError(f"native response has unexpected top-level keys: {result}")
    ping = result.get("result")
    collector = ping.get("collector") if isinstance(ping, dict) else None
    if (
        type(result.get("v")) is not int
        or result.get("v") != 1
        or result.get("id") != "browser-probe"
        or result.get("ok") is not True
        or not isinstance(ping, dict)
        or set(ping) != {"protocol_version", "port", "collector"}
        or type(ping.get("protocol_version")) is not int
        or ping.get("protocol_version") != 1
        or type(ping.get("port")) is not int
        or not 1 <= ping["port"] <= 65535
        or not isinstance(collector, dict)
        or set(collector) != {"status"}
        or collector.get("status") != "ok"
    ):
        raise RuntimeError(f"native response was not successful: {result}")


def self_test_validation() -> None:
    valid = {
        "v": 1,
        "id": "browser-probe",
        "ok": True,
        "result": {"protocol_version": 1, "port": 8000, "collector": {"status": "ok"}},
    }
    validate_ping_response(valid)
    cases: list[tuple[str, dict[str, object]]] = []
    for field, bad_value in (("v", 2), ("v", True), ("id", "wrong"), ("ok", False)):
        invalid = json.loads(json.dumps(valid))
        invalid[field] = bad_value
        cases.append((f"bad {field}={bad_value!r}", invalid))
    for value in (True, 2):
        invalid = json.loads(json.dumps(valid)); invalid["result"]["protocol_version"] = value
        cases.append((f"bad nested protocol={value!r}", invalid))
    for value in (True, 0, 65536):
        invalid = json.loads(json.dumps(valid)); invalid["result"]["port"] = value
        cases.append((f"bad port={value!r}", invalid))
    for location, key in (("top", "id"), ("result", "collector")):
        invalid = json.loads(json.dumps(valid))
        if location == "top":
            del invalid[key]
        else:
            del invalid["result"][key]
        cases.append((f"missing {location} key {key}", invalid))
    invalid = json.loads(json.dumps(valid)); invalid["extra"] = 1; cases.append(("extra top key", invalid))
    invalid = json.loads(json.dumps(valid)); invalid["result"]["extra"] = 1; cases.append(("extra result key", invalid))
    for label, invalid in cases:
        try:
            validate_ping_response(invalid)
        except RuntimeError:
            continue
        raise AssertionError(f"validator accepted {label}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--browser", choices=("ChromeForTesting", "Chromium", "Edge"))
    parser.add_argument("--browser-path", type=Path)
    parser.add_argument("--integration-root", type=Path)
    parser.add_argument("--self-test-validation", action="store_true")
    args = parser.parse_args()
    if args.self_test_validation:
        self_test_validation()
        print("PASS: native probe rejects protocol, correlation-id, and success mismatches")
        return 0
    if not args.browser or not args.browser_path or not args.integration_root:
        parser.error("--browser, --browser-path, and --integration-root are required")

    with tempfile.TemporaryDirectory(prefix="sitefilter-browser-probe-") as temp:
        root = Path(temp)
        extension = root / "extension"
        extension.mkdir()
        key = (args.integration_root / "manifest.key").read_text(encoding="utf-8").strip()
        (extension / "manifest.json").write_text(
            json.dumps(
                {
                    "manifest_version": 3,
                    "name": "SiteFilter Native Host Probe",
                    "version": "1.0.0",
                    "key": key,
                    "permissions": ["nativeMessaging"],
                }
            ),
            encoding="utf-8",
        )
        (extension / "probe.html").write_text(
            '<!doctype html><meta charset="utf-8"><pre id="result">waiting</pre>'
            '<script src="probe.js"></script>',
            encoding="utf-8",
        )
        (extension / "probe.js").write_text(
            "const r=document.getElementById('result');"
            f"chrome.runtime.sendNativeMessage('{HOST_NAME}',"
            "{v:1,id:'browser-probe',action:'ping',payload:{}},x=>{"
            "r.textContent=JSON.stringify(chrome.runtime.lastError?"
            "{browser_error:chrome.runtime.lastError.message}:x);});",
            encoding="utf-8",
        )

        with sync_playwright() as playwright:
            context = playwright.chromium.launch_persistent_context(
                str(root / "profile"),
                executable_path=str(args.browser_path),
                headless=False,
                args=[
                    f"--disable-extensions-except={extension}",
                    f"--load-extension={extension}",
                    "--no-first-run",
                    "--disable-default-apps",
                    "--disable-background-networking",
                    "--window-position=-32000,-32000",
                    "--window-size=800,600",
                ],
            )
            try:
                page = context.pages[0] if context.pages else context.new_page()
                page.goto(f"chrome-extension://{EXTENSION_ID}/probe.html")
                page.locator("#result").wait_for(state="visible", timeout=10_000)
                deadline = time.monotonic() + 70
                result_text = "waiting"
                while result_text == "waiting" and time.monotonic() < deadline:
                    result_text = page.locator("#result").text_content()
                    if result_text == "waiting":
                        time.sleep(0.2)
                if result_text == "waiting":
                    raise TimeoutError("native response did not arrive within 70 seconds")
                result = json.loads(result_text)
                validate_ping_response(result)
                print(f"PASS: {args.browser} temporary-profile native messaging ping: {json.dumps(result)}")
            finally:
                context.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
