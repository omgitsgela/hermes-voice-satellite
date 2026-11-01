"""Browser acceptance for the generated preview and the shipped UI with offline API fixtures.

Development-only dependency: pip install playwright && playwright install chromium
python packages/installer/tests/browser_ux.py --preview <preview.html>
No hardware or running Satellite Gateway is contacted. The production UI uses the same offline
fixture transport as the preview; real backend command plans are covered by run_ux_tests.py.
"""
from __future__ import annotations

import argparse
import tempfile
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[3]


def run(preview: Path):
    # Strip only the preview badge to test the actual shipped markup/script with identical fixture
    # transport, without replacing any product flow or event handlers.
    original = (ROOT / "packages/installer/src/hermes_installer/web/index.html").read_text()
    preview_html = preview.read_text()
    adapter = preview_html.split("<script>", 1)[1].split("</script>", 1)[0]
    production = original.replace("<script>", "<script>" + adapter + "</script><script>", 1)
    with tempfile.TemporaryDirectory() as directory, sync_playwright() as p:
        shipped = Path(directory) / "shipped.html"
        shipped.write_text(production)
        browser = p.chromium.launch(headless=True, args=["--no-sandbox"])
        checks = 0
        for source in (preview, shipped):
            for reduced in (False, True):
                context = browser.new_context(viewport={"width": 980, "height": 820},
                                              reduced_motion="reduce" if reduced else "no-preference")
                page = context.new_page()
                errors = []
                network = []
                page.on("pageerror", lambda error: errors.append(str(error)))
                page.on("request", lambda request: network.append(request.url)
                        if request.url.startswith(("http:", "https:")) else None)
                page.goto(source.as_uri())
                page.wait_for_function("state.status !== undefined")

                def at(name):
                    page.wait_for_function("name => state.page === name && !state.busy", arg=name)
                    assert page.locator(".page:not(.hidden)").count() == 1
                    assert page.locator("#progress [aria-current=step]").count() == 1
                    assert page.evaluate("document.activeElement.id") == name + "-title"
                    assert page.evaluate("window.scrollY") == 0
                    assert page.locator("#next").bounding_box()["y"] < 820
                    assert page.locator("#start-over").is_visible()
                    if name in ("start", "gateway", "device", "settings", "wake", "voice"):
                        form = page.locator(f"#{name} .form-panel").bounding_box()
                        help_box = page.locator(f"#{name} .help-panel").bounding_box()
                        assert help_box["x"] >= form["x"] + form["width"], name
                        assert abs(help_box["y"] - form["y"]) < 2, name

                def next_to(name):
                    page.locator("#next").click()
                    at(name)

                def start(role, card=False):
                    page.reload()
                    page.wait_for_function("state.status !== undefined")
                    next_to("start")
                    page.locator(f'input[name=role][value={role}]').check()
                    next_to("device")
                    page.locator('input[name=device]').nth(1 if card else 0).check()
                    next_to("connect")

                # Management: both devices, both setting paths. No pairing, no flashing, ever.
                for card in (False, True):
                    for wake in (False, True):
                        start("manage", card)
                        next_to("settings")
                        page.locator("#next").click()
                        page.wait_for_function("!state.busy")
                        assert "Choose Wi-Fi" in page.locator("#message").inner_text()
                        page.locator("#change-wake" if wake else "#change-network").check()
                        next_to("wake" if wake else "network")
                        if not wake:
                            page.locator("#ssid").fill("Example network")
                            if not card:
                                page.locator("#wifi-password").fill("example-passphrase")
                        next_to("review")
                        # Back and forward preserve the value and regenerate the review.
                        page.locator("#back").click()
                        at("wake" if wake else "network")
                        next_to("review")
                        next_to("done")
                        assert "Settings applied" in page.locator("#done-title").inner_text()
                        calls = page.evaluate("window.PREVIEW_REQUESTS")
                        assert not any(c["path"] in ("/api/pair", "/api/flash", "/api/inspect") for c in calls)
                        manages = [c for c in calls if c["path"] == "/api/manage"]
                        assert manages[-2]["body"]["dry_run"] is True
                        assert manages[-1]["body"]["dry_run"] is False
                        assert manages[-1]["body"]["certs_dir"] is None
                        checks += 1
                print(f"ROUTE {source.name} reduced={reduced}: welcome → start → device → connect → settings → network/wake → review → done; change-existing-device completed for USB + card, Wi-Fi + wake; no pairing or firmware writes.")

                for card in (False, True):
                    start("device", card)
                    next_to("pair-host")
                    page.locator("#gwhost").fill("gateway.example")
                    next_to("identity")
                    page.locator('input[name=match][value=no]').check()
                    page.locator("#next").click()
                    page.wait_for_function("!state.busy")
                    assert not page.evaluate("window.PREVIEW_REQUESTS.some(c=>c.path==='/api/pair')")
                    page.locator('input[name=match][value=yes]').check()
                    next_to("pair")
                    page.locator("#words").fill("amber lantern copper thrush meadow")
                    page.locator("#devname").fill("study")
                    next_to("network")
                    if not card:
                        page.locator("#next").click()
                        page.wait_for_function("!state.busy")
                        assert page.evaluate("state.page") == "network"
                        page.locator("#ssid").fill("Example network")
                        page.locator("#wifi-password").fill("example-passphrase")
                    next_to("wake")
                    next_to("voice")
                    assert page.locator("#voice-list input").count() == 5
                    page.locator("#voice-search").fill("zz-no-match")
                    assert page.locator("#voice-list input").count() == 0
                    assert "No matching" in page.locator("#voice-count").inner_text()
                    page.locator("#voice-search").fill("Amy")
                    assert page.locator("#voice-list input").count() == 1
                    page.locator("#voice-list input").check()
                    assert page.locator("#voice-list audio").get_attribute("src").startswith("data:audio/mpeg;")
                    page.locator("#voice-list audio").evaluate("a=>a.play()")
                    page.wait_for_function("document.querySelector('#voice-list audio').currentTime > 0")
                    page.locator("#voice-download").click()
                    page.wait_for_function("!state.busy")
                    calls = page.evaluate("window.PREVIEW_REQUESTS")
                    voice = [c for c in calls if c["path"] == "/api/voice"][-1]
                    assert voice["body"] == {"voice": "en_US-amy-medium", "apply": False}
                    next_to("review")
                    next_to("done")
                    checks += 1

                # A rejected settings write stays on review and can be retried without pairing.
                start("manage")
                next_to("settings")
                page.locator("#change-network").check()
                next_to("network")
                page.locator("#ssid").fill("Example network")
                page.locator("#wifi-password").fill("example-passphrase")
                next_to("review")
                page.evaluate("""() => {
                    const original = window.fetch;
                    let fail = true;
                    window.fetch = async (path, options) => {
                        if (path === '/api/manage' && !JSON.parse(options.body).dry_run && fail) {
                            fail = false;
                            return {ok:false,json:async()=>({ok:false,error:'Connection lost. Retry.'})};
                        }
                        return original(path, options);
                    };
                }""")
                page.locator("#next").click()
                page.wait_for_function("!state.busy")
                assert page.evaluate("state.page") == "review"
                assert "Connection lost" in page.locator("#message").inner_text()
                next_to("done")
                checks += 1

                page.reload()
                page.wait_for_function("state.status !== undefined")
                next_to("start")
                page.locator("#role-gateway").check()
                next_to("gateway")
                assert page.locator("#dial").input_value() == "192.0.2.24"
                next_to("review")
                next_to("done")
                assert "XK7F-9Q2M" in page.locator("#result-details").inner_text()
                for text in ("own Satellite Gateway", "security boundary", "shown once",
                             "cannot be recovered", "each satellite", "another workbench"):
                    assert text in page.locator("#done").inner_text()
                assert page.locator("#next").inner_text() == "Add a satellite to this Satellite Gateway"
                next_to("device")
                page.locator('input[name=device]').first.check()
                next_to("connect")
                next_to("pair-host")
                assert page.locator("#gwhost").input_value() == "192.0.2.24"
                assert page.locator("#gwport").input_value() == "10704"
                next_to("identity")
                assert "XK7F-9Q2M" in page.locator("#saved-code").inner_text()
                page.locator('input[name=match][value=yes]').check()
                next_to("pair")
                assert page.locator("#words").input_value() == "amber lantern copper thrush meadow"
                page.locator("#devname").fill("study")
                next_to("network")
                calls = page.evaluate("window.PREVIEW_REQUESTS")
                pairing = [c["body"] for c in calls if c["path"] == "/api/pair"][-1]
                assert pairing["host"] == "192.0.2.24"
                assert pairing["port"] == 10704
                assert pairing["expected_code"] == "XK7F-9Q2M"
                assert pairing["words"] == "amber lantern copper thrush meadow"
                page.locator("#ssid").fill("Example network")
                page.locator("#wifi-password").fill("example-passphrase")
                next_to("wake")
                next_to("voice")
                next_to("review")
                next_to("done")
                print(f"ROUTE {source.name} reduced={reduced}: welcome → start → gateway → review → done → device → connect → pair-host → identity → pair → network → wake → voice → review → done; address, pairing port, identity code and words carried.")
                assert not errors, errors
                assert not network, network
                checks += 1
                # A second device keeps the record, but a changed identity never receives words.
                next_to("device")
                page.locator('input[name=device]').first.check()
                next_to("connect")
                next_to("pair-host")
                page.evaluate("""() => {
                    const original = window.fetch;
                    window.fetch = async (path, options) => path === '/api/inspect'
                        ? {ok:true,json:async()=>({ok:true,identity_code:'OTHER-CODE'})}
                        : original(path, options);
                    window.restoreFetch = () => { window.fetch = original; };
                }""")
                before_pairs = page.evaluate("window.PREVIEW_REQUESTS.filter(c=>c.path==='/api/pair').length")
                page.locator("#next").click()
                page.wait_for_function("!state.busy")
                assert page.evaluate("state.page") == "pair-host"
                assert "does not match" in page.locator("#message").inner_text()
                assert page.locator("#words").input_value() == ""
                assert page.evaluate("window.PREVIEW_REQUESTS.filter(c=>c.path==='/api/pair').length") == before_pairs
                page.evaluate("window.restoreFetch()")
                # Choosing a different host requires its own words, even if the fixture code matches.
                page.locator("#gwhost").fill("different.example")
                next_to("identity")
                assert not page.locator("#saved-code").is_visible()
                page.locator('input[name=match][value=yes]').check()
                next_to("pair")
                assert page.locator("#words").input_value() == ""
                checks += 1
                # Start over clears the same-run credentials, DOM secrets and task choices.
                page.locator("#start-over").click()
                page.wait_for_function("state.status !== undefined && state.page === 'welcome'")
                at("welcome")
                assert page.evaluate("!state.gateway && !state.role && state.history.length === 0")
                for field in ("words", "gwhost", "devname", "wifi-password", "ssid"):
                    assert page.locator("#" + field).input_value() == "", field
                checks += 1
                next_to("start")
                page.locator("#role-gateway").check()
                next_to("gateway")
                page.set_viewport_size({"width": 390, "height": 820})
                form = page.locator("#gateway .form-panel").bounding_box()
                help_box = page.locator("#gateway .help-panel").bounding_box()
                assert help_box["y"] >= form["y"] + form["height"]
                assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
                assert page.locator("#next").bounding_box()["y"] < 820
                checks += 1
                assert not errors, errors
                assert not network, network
                context.close()
        browser.close()
        print(f"BROWSER PASSED: {checks} end-to-end routes, 0 failed; shipped UI + standalone file, motion + reduced motion; sample playback, search, downloads, focus, back/forward and offline isolation.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--preview", type=Path, required=True)
    run(parser.parse_args().preview.resolve())
