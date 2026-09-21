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


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--browser", choices=("Chrome", "Edge"), required=True)
    parser.add_argument("--browser-path", type=Path, required=True)
    parser.add_argument("--integration-root", type=Path, required=True)
    args = parser.parse_args()

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
                if result.get("ok") is not True:
                    raise RuntimeError(f"native response was not successful: {result}")
                print(f"PASS: {args.browser} temporary-profile native messaging ping: {json.dumps(result)}")
            finally:
                context.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
