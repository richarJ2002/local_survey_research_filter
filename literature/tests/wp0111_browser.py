"""Opt-in real Firefox qualification, isolated source/XDG/profile and offline.

@File: wp0111_browser.py
@Brief: Run `.venv/bin/python -m literature.tests.wp0111_browser`; no installs.
@Date: 03/10/2026
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import shutil
import signal
import socket
import sqlite3
import subprocess
import sys
import sysconfig
import tempfile
import time
import urllib.error
import urllib.request
import venv
from pathlib import Path

from literature.tests.test_wp0110_launcher import OFFLINE, wait_for


# --- W3C WebDriver over stdlib HTTP; only our newly launched Firefox session ---
class Browser:
    def __init__(self, base):
        self.base = base
        self.prefix = ""

    def command(self, method, path, body=None):
        data = None if body is None else json.dumps(body).encode()
        request = urllib.request.Request(
            self.base + self.prefix + path,
            data=data,
            method=method,
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=45) as response:
                return json.load(response)["value"]
        except urllib.error.HTTPError as exc:
            raise RuntimeError(exc.read().decode()) from exc

    def js(self, script, *args):
        return self.command("POST", "/execute/sync", {"script": script, "args": list(args)})

    def go(self, url, handle=True):
        self.command("POST", "/url", {"url": url})
        if handle:
            self.until("return !!window.__wpHandle?.()")

    def until(self, script):
        assert wait_for(lambda: self.js(script)), script

    def submit(self, selector, values, document="document"):
        if self.js("return location.pathname.startsWith('/p/')"):
            self.until("return !!" + document + "?.defaultView.__wpHandle?.()")
        marker = str(time.monotonic_ns())
        self.js(
            """
            const doc = """
            + document
            + """;
            const form = doc.querySelector(arguments[0]);
            if (!form) throw new Error('Missing form: ' + arguments[0]);
            for (const [name, value] of Object.entries(arguments[1])) {
              const field = form.elements.namedItem(name);
              if (!field) throw new Error('Missing field: ' + name);
              if (field.type === 'checkbox') field.checked = !!value;
              else field.value = value;
            }
            if (!form.checkValidity()) throw new Error('Invalid native form');
            doc.documentElement.dataset.qualificationSubmit = arguments[2];
            form.requestSubmit(); return true;
        """,
            selector,
            values,
            marker,
        )
        self.until(
            "const doc="
            + document
            + "; return !!doc?.body && doc.documentElement.dataset.qualificationSubmit !== "
            + json.dumps(marker)
        )

    def switch(self, handle):
        self.command("POST", "/window", {"handle": handle})

    def alert(self):
        try:
            return self.command("GET", "/alert/text")
        except RuntimeError as exc:
            if '"no such alert"' in str(exc):
                return False
            raise

    def fetch_text(self, url):
        return self.command(
            "POST",
            "/execute/async",
            {
                "script": "const done=arguments[arguments.length-1]; fetch(arguments[0]).then(async r=>done({status:r.status,text:await r.text()})).catch(e=>done({error:String(e)}));",
                "args": [url],
            },
        )

    def native(self, selector, value=None):
        element = self.command("POST", "/element", {"using": "css selector", "value": selector})
        element_id = element["element-6066-11e4-a52e-4f735466cecf"]
        if value is None:
            return self.command("POST", f"/element/{element_id}/click", {})
        self.command("POST", f"/element/{element_id}/clear", {})
        return self.command("POST", f"/element/{element_id}/value", {"text": value})


def rows(db, sql, args=()):
    with sqlite3.connect(db.resolve().as_uri() + "?mode=ro", uri=True) as connection:
        return connection.execute(sql, args).fetchall()


def qualify(browser, root, manager, restart):
    """All writes use rendered forms in our synthetic two-project launcher."""
    b = browser
    registry = root / "state" / "local-survey-filter" / "registry.sqlite3"
    a, other = root / "Project A", root / "Project B"
    main = b.command("GET", "/window")

    def leases(location):
        return rows(
            registry, "SELECT handle_id FROM leases WHERE store_canonical=?", (str(location),)
        )

    def project_new(location, name):
        b.go(manager + "/", False)
        b.submit("#workspace-new-pane form", {"directory": str(location), "name": name})
        b.until("return location.pathname.startsWith('/p/') && !!window.__wpHandle?.()")
        return b.js("return location.origin + location.pathname.split('?')[0]")

    def load(location):
        b.go(manager + "/", False)
        b.js("document.querySelector('input[value=load]').click()")
        b.submit("#workspace-load-pane form", {"directory": str(location)})
        b.until("return location.pathname.startsWith('/p/') && !!window.__wpHandle?.()")
        return b.js("return location.origin + location.pathname")

    def exit_with(selector="[data-project-exit]"):
        b.js("document.querySelector(arguments[0]).click()", selector)
        b.until(
            "return location.origin === arguments[0]".replace("arguments[0]", json.dumps(manager))
        )

    def operation(action, source, destination=""):
        b.submit(
            'form[action$="/workspace/operation/"]',
            {
                "action": action,
                "source": str(source),
                "destination": str(destination),
                "confirm": True,
            },
        )
        b.until("return !!document.querySelector('.messages .success')")
        assert (
            "complete" in b.js("return document.querySelector('.messages').textContent").lower()
            or action == "close"
        )

    def search_and_paper(base, name):
        b.go(base + "search/new/")
        b.js("document.querySelector('#keywords-table input').value='synthetic'; return true")
        b.submit("#search-form", {"name": name})
        b.until("return !location.pathname.endsWith('/search/new/')")
        b.go(base + "papers/add/")
        b.submit(
            "#manual-entry form",
            {
                "search_keyword": "1|synthetic",
                "origin_source": "crossref",
                "doi": "10.1000/shared",
                "title": name + " paper",
                "authors": "Synthetic Author",
                "publication_year": "2025",
                "venue": "Synthetic Journal",
                "abstract": "",
                "references": "",
            },
        )
        b.until("return !location.pathname.endsWith('/papers/add/')")
        b.go(base + "papers/")
        assert name + " paper" in b.js("return document.body.textContent")

    base_a = project_new(a, "Synthetic A")
    search_and_paper(base_a, "A")
    first_handle = b.js("return window.__wpHandle()")
    assert len(leases(a)) == 1

    # A/A genuine duplicate copies sessionStorage, but its live Web Lock mints a lease.
    b.js("window.open(location.href, '_blank'); return true")
    assert wait_for(lambda: len(b.command("GET", "/window/handles")) == 2)
    duplicate = next(h for h in b.command("GET", "/window/handles") if h != main)
    b.switch(duplicate)
    b.until("return !!window.__wpHandle?.()")
    duplicate_handle = b.js("return window.__wpHandle()")
    assert duplicate_handle != first_handle
    assert len(leases(a)) == 2
    b.js(
        "const field=document.querySelector('form input:not([type=hidden])'); field.value='Draft stays'; field.dispatchEvent(new Event('input',{bubbles:true}));"
    )
    b.switch(main)
    b.go(base_a + "papers/1/")
    b.submit('form:has(input[value="details"])', {"title": "A edited paper"})
    b.until("return document.querySelector('h1').textContent.includes('A edited paper')")
    b.switch(duplicate)
    b.js("window.dispatchEvent(new Event('focus')); return true")
    b.until("return document.body.textContent.includes('Your drafts are kept')")
    assert (
        b.js("return document.querySelector('form input:not([type=hidden])').value")
        == "Draft stays"
    )

    # Cancel actual Exit prompt: draft+lease stay; confirmed discard has no second prompt.
    b.js("setTimeout(()=>document.querySelector('#wp-exit').click(),0); return true")
    assert wait_for(b.alert)
    b.command("POST", "/alert/dismiss", {})
    assert b.js("return window.__wpHandle()") == duplicate_handle
    assert len(leases(a)) == 2
    # A failed release is not an Exit: retain the draft and this exact lease.
    b.js(
        "window.__qualificationConfirm=window.confirm; window.confirm=()=>true; const original=window.fetch; window.fetch=(url,options)=>String(url).includes('/lifecycle/release/') ? Promise.resolve({ok:false,status:503}) : original(url,options); document.querySelector('#wp-exit').click(); window.__qualificationFetch=original; return true"
    )
    b.until("return document.body.textContent.includes('Could not release this tab')")
    assert b.js("return window.__wpHandle()") == duplicate_handle
    assert b.js("return window.__wpDirty()")
    assert len(leases(a)) == 2
    # A semantic denial must also fail even if an older endpoint sends HTTP 200.
    b.js(
        "document.querySelector('#wp-handle-note').remove(); const original=window.__qualificationFetch; window.fetch=(url,options)=>String(url).includes('/lifecycle/release/') ? Promise.resolve(new Response(JSON.stringify({released:false}),{status:200})) : original(url,options); document.querySelector('#wp-exit').click(); return true"
    )
    b.until("return document.body.textContent.includes('Could not release this tab')")
    assert b.js("return window.__wpHandle()") == duplicate_handle
    assert b.js("return window.__wpDirty()")
    assert len(leases(a)) == 2

    # Ordinary backend write-failure injection in ONLY this synthetic registry.
    # The real worker DELETE raises; no fetch response is manufactured here.
    with sqlite3.connect(registry) as connection:
        connection.execute(
            "CREATE TRIGGER wp11_functional_delete_failure BEFORE DELETE ON leases "
            "BEGIN SELECT RAISE(ABORT, 'synthetic registry DELETE failure'); END"
        )
    try:
        b.js(
            "document.querySelector('#wp-handle-note').remove(); const original=window.__qualificationFetch; window.fetch=async (url,options)=>{const response=await original(url,options); if(String(url).includes('/lifecycle/release/')) window.__qualificationRelease={status:response.status,data:await response.clone().json()}; return response;}; document.querySelector('#wp-exit').click(); return true"
        )
        b.until("return window.__qualificationRelease?.status === 503")
        b.until("return document.body.textContent.includes('Could not release this tab')")
        assert b.js("return window.__qualificationRelease.data.released") is False
        assert b.js("return window.__wpHandle()") == duplicate_handle
        assert b.js("return window.__wpDirty()")
        assert (
            b.js("return document.querySelector('form input:not([type=hidden])').value")
            == "Draft stays"
        )
        assert b.js("return location.origin") != manager
        assert len(leases(a)) == 2
    finally:
        with sqlite3.connect(registry) as connection:
            connection.execute("DROP TRIGGER wp11_functional_delete_failure")
    b.js(
        "window.fetch=window.__qualificationFetch; window.confirm=window.__qualificationConfirm; return true"
    )
    b.js("setTimeout(()=>document.querySelector('#wp-exit').click(),0); return true")
    assert wait_for(b.alert)
    b.command("POST", "/alert/accept", {})
    b.until("return location.origin === " + json.dumps(manager))
    assert len(leases(a)) == 1
    base_b = project_new(other, "Synthetic B")
    search_and_paper(base_b, "B")
    b_handle = b.js("return window.__wpHandle()")
    db_a, db_b = a / "project.sqlite3", other / "project.sqlite3"
    assert rows(db_a, "SELECT id,doi FROM literature_paper") == rows(
        db_b, "SELECT id,doi FROM literature_paper"
    )
    before_b = rows(db_b, "SELECT * FROM literature_paper")
    rev_b = b.js("return document.querySelector('[data-project-revision-url]').dataset.revision")
    b.switch(main)

    # Real screening controls, partial missing abstract then reference completion.
    b.go(base_a + "screen/title/")
    b.js("document.querySelector('button[data-decision=ACCEPT]').click()")
    b.until("return !document.querySelector('#screening-card')")
    b.go(base_a + "screen/abstract/")
    assert b.js("return document.querySelector('button[data-decision=ACCEPT]').disabled")
    b.submit("#missing-metadata-form", {"abstract": "A abstract supplied offline"})
    b.until(
        "return !!document.querySelector('button[data-decision=ACCEPT]') && !document.querySelector('button[data-decision=ACCEPT]').disabled"
    )
    b.until("return !!window.__wpHandle?.()")
    b.js("document.querySelector('button[data-decision=ACCEPT]').click()")
    b.until("return !document.querySelector('#screening-card')")
    b.go(base_a + "references/review/")
    b.submit(
        'form:has(input[value="save"])',
        {"reference_text": "Synthetic bibliography without an identifier"},
    )
    b.until("return document.body.textContent.includes('No papers are waiting')")
    assert rows(db_a, "SELECT decision FROM literature_screeningdecision ORDER BY stage_id") == [
        ("ACCEPT",),
        ("ACCEPT",),
    ]
    assert rows(db_b, "SELECT * FROM literature_paper") == before_b

    # Graph editor iframe belongs to the same tab; labels/cycles survive each form save.
    for number in (7, 12, 17):
        b.go(base_a + "survey/")
        b.js("document.querySelector('a[data-survey-entry]').click()")
        b.until(
            "return !!document.querySelector('iframe').contentDocument?.querySelector('[name=reference_number]') && !!document.querySelector('iframe').contentWindow.__wpHandle?.()"
        )
        assert (
            b.js("return document.querySelector('iframe').contentWindow.__wpHandle()")
            == first_handle
        )
        assert len(leases(a)) == 1
        b.submit(
            'form:has(input[value="save"])',
            {
                "reference_number": str(number),
                "title": f"A curated [{number}]",
                "publication_year": "2025",
                "origin_source": "crossref",
                "inclusion_origin": "reading",
            },
            "document.querySelector('iframe')?.contentDocument",
        )
        b.until(
            "return !!document.querySelector('dialog') && !document.querySelector('dialog').open"
        )
        b.until("return document.body.textContent.includes('A curated [" + str(number) + "]')")
    for citing, cited in ((1, 2), (2, 3), (3, 1)):
        b.submit('form:has(input[value="link"])', {"citing": str(citing), "cited": str(cited)})
        b.until(
            "return !!document.querySelector('form [name=citing]') && document.querySelectorAll('input[value=remove_link]').length >= "
            + str(citing)
        )
    assert rows(
        db_a, "SELECT citing_id,cited_id FROM literature_surveycitation ORDER BY citing_id"
    ) == [(1, 2), (2, 3), (3, 1)]
    b.until(
        "return typeof Plotly === 'object' && document.querySelectorAll('.js-plotly-plot').length > 0"
    )
    traces = b.js(
        "return document.querySelector('.js-plotly-plot').data.map(t=>({name:t.name,mode:t.mode,n:t.x?.length,text:t.text}))"
    )
    assert traces
    assert (
        "[7]" in json.dumps(traces)
        and "[12]" in json.dumps(traces)
        and "[17]" in json.dumps(traces)
    )
    assert next(t["n"] for t in traces if t["name"] == "Citation direction: citing → cited") == 3
    b.js(
        "document.querySelector('.js-plotly-plot').scrollIntoView({behavior:'instant',block:'center'}); return true"
    )
    b.until("return document.querySelector('.js-plotly-plot').getBoundingClientRect().top < 100")
    screenshot = b.command("GET", "/screenshot")
    (Path("/tmp/opencode") / "wp11-curated-firefox.png").write_bytes(base64.b64decode(screenshot))
    b.go(base_a + "plots/")
    b.until("return document.querySelectorAll('.js-plotly-plot').length >= 3")
    assert "Current papers by source" in b.js("return document.body.textContent")
    assert b.js(
        "return [...document.scripts].every(s=>!s.src || new URL(s.src).origin === location.origin)"
    )
    b.switch(duplicate)
    b.js("window.dispatchEvent(new Event('focus')); return true")
    assert b.js("return window.__wpHandle()") == b_handle
    assert (
        b.js("return document.querySelector('[data-project-revision-url]').dataset.revision")
        == rev_b
    )
    assert rows(db_b, "SELECT * FROM literature_paper") == before_b

    # Busy A admits a submitted search edit while B remains browsable; release commits A.
    b.switch(main)
    b.go(base_a + "search/1/edit/")
    with sqlite3.connect(db_a, isolation_level=None) as lock:
        lock.execute("BEGIN IMMEDIATE")
        try:
            b.js(
                "const form=document.querySelector('#search-form'); form.elements.name.value='A search edited under lock'; form.requestSubmit(); return true"
            )

            def admitted():
                with urllib.request.urlopen(base_a + "lifecycle/drain/") as response:
                    return json.load(response)["in_flight"] == 1

            assert wait_for(admitted, 4)
            b.switch(duplicate)
            b.go(base_b + "papers/")
            assert "B paper" in b.js("return document.body.textContent")
            assert rows(db_b, "SELECT * FROM literature_paper") == before_b
        finally:
            lock.rollback()
    b.switch(main)
    b.until("return !location.pathname.endsWith('/edit/') && !!window.__wpHandle?.()")
    assert rows(db_a, "SELECT name FROM literature_search") == [("A search edited under lock",)]
    for path in ("export/markdown/", "plots/download/"):
        exported = b.fetch_text(base_a + path)
        assert exported["status"] == 200
        assert "Synthetic A" in exported["text"]
        for private in (
            "Synthetic B",
            "B paper",
            first_handle,
            duplicate_handle,
            b_handle,
            str(registry),
        ):
            assert private not in exported["text"]
        assert "csrfmiddlewaretoken" not in exported["text"]
        assert "credentials.env" not in exported["text"]

    # Copy/share while A is open; package whitelist + exact histories/links, new identity.
    b.switch(main)
    exit_with()
    assert leases(a) == []
    copy = root / "Shared A"
    operation("copy", a, copy)
    assert {p.name for p in copy.iterdir()} == {"project.json", "project.sqlite3"}
    assert (
        json.loads((copy / "project.json").read_text())["project_uuid"]
        != json.loads((a / "project.json").read_text())["project_uuid"]
    )
    for table in (
        "paper",
        "screeningdecision",
        "searchrun",
        "discovery",
        "surveyentry",
        "surveycitation",
    ):
        assert rows(
            copy / "project.sqlite3", f"SELECT * FROM literature_{table} ORDER BY id"
        ) == rows(db_a, f"SELECT * FROM literature_{table} ORDER BY id")
    copy_base = load(copy)
    b.go(copy_base + "survey/")
    b.until("return document.querySelectorAll('.js-plotly-plot').length > 0")
    exit_with(".site-brand")
    operation("close", copy)
    operation("close", a)
    backup = root / "Backup A"
    operation("backup", a, backup)
    assert (
        json.loads((backup / "project.json").read_text())["project_uuid"]
        == json.loads((a / "project.json").read_text())["project_uuid"]
    )
    operation("restore", backup, a)
    assert list(root.glob(".Project A.recovery-*"))

    # Ordinary internal Back retains; boundary Back releases before returning to manager.
    reopened = load(a)
    b.go(reopened + "papers/")
    kept = b.js("return window.__wpHandle()")
    b.command("POST", "/back", {})
    b.until("return !!window.__wpHandle?.()")
    assert b.js("return window.__wpHandle()") == kept
    b.command("POST", "/back", {})
    b.until("return location.origin === " + json.dumps(manager))
    assert leases(a) == []
    b.switch(duplicate)
    exit_with()
    assert leases(other) == []
    operation("close", other)
    b.switch(main)
    operation("close", a)
    assert rows(registry, "SELECT * FROM leases") == []
    assert rows(registry, "SELECT * FROM workers") == []
    restart()
    reopened = load(a)
    b.go(reopened + "papers/")
    assert "A edited paper" in b.js("return document.body.textContent")
    b.go(reopened + "survey/")
    assert rows(
        db_a, "SELECT citing_id,cited_id FROM literature_surveycitation ORDER BY citing_id"
    ) == [(1, 2), (2, 3), (3, 1)]
    exit_with()
    operation("close", a)
    for db in (db_a, db_b, copy / "project.sqlite3", backup / "project.sqlite3"):
        assert rows(db, "PRAGMA integrity_check") == [("ok",)]
        assert rows(db, "PRAGMA foreign_key_check") == []
    assert rows(registry, "SELECT * FROM leases") == []
    print(
        "FIREFOX PASS: offline A/B + live A/A duplicate; edits, screening, references, dirty cancel/discard, HTTP/semantic/backend DELETE release failures, iframe single lease, labels/all cycle edges, graphs, busy A/B, export privacy, copy/share/load/backup/restore, Projects/home, internal/boundary Back, restart; zero self leases.",
        flush=True,
    )


def main():
    source = Path(__file__).resolve().parents[2]
    manager_proc = driver = None
    browser = None
    with tempfile.TemporaryDirectory(prefix="wp11-browser-", dir="/tmp/opencode") as tmp:
        root = Path(tmp)
        clone = root / "source checkout"
        clone.mkdir()
        names = (
            subprocess.check_output(
                ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"], cwd=source
            )
            .decode()
            .split("\0")
        )
        for name in names:
            path = Path(name)
            if (
                not name
                or path.parts[0] in {"projects", ".agents", "graphify-out", ".venv"}
                or path.name.startswith(".env")
                or path.name == "AGENTS.md"
            ):
                continue
            if (
                path.suffix not in {".py", ".html", ".js", ".cjs", ".css", ".toml", ".txt", ".md"}
                and name != "LICENSE"
            ):
                continue
            target = clone / path
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source / path, target)
        cache = root / "cache"
        runtime = (
            cache
            / "local-survey-filter"
            / "envs"
            / hashlib.sha256(str(clone).encode()).hexdigest()[:16]
        )
        # Reuse installed modules through a synthetic venv; no pip/dependency installation.
        venv.EnvBuilder(with_pip=False).create(runtime)
        site = (
            runtime
            / "lib"
            / f"python{sys.version_info.major}.{sys.version_info.minor}"
            / "site-packages"
        )
        (site / "installed-runtime.pth").write_text(sysconfig.get_path("purelib") + "\n")
        (runtime / "requirements.sha256").write_text(
            hashlib.sha256((clone / "requirements.txt").read_bytes()).hexdigest()
        )
        offline = root / "offline"
        offline.mkdir()
        (offline / "sitecustomize.py").write_text(OFFLINE)
        env = dict(os.environ)
        for key in (
            "OPENALEX_EMAIL",
            "SEMANTIC_SCHOLAR_KEY",
            "OPENCITATIONS_TOKEN",
            "DJANGO_SECRET_KEY",
            "WP_ROLE",
            "WP_LAUNCHER",
            "WP_OWNER_FD",
            "LOCAL_SURVEY_CREDENTIALS_FILE",
        ):
            env.pop(key, None)
        env.update(
            XDG_DATA_HOME=str(root / "state"),
            XDG_CACHE_HOME=str(cache),
            PYTHON_DOTENV_DISABLED="1",
            PYTHONPATH=str(offline),
            PIP_NO_INDEX="1",
        )

        def free_port():
            with socket.socket() as sock:
                sock.bind(("127.0.0.1", 0))
                return sock.getsockname()[1]

        manager_port, driver_port = free_port(), free_port()
        manager = f"http://127.0.0.1:{manager_port}"
        log = root / "manager.log"

        def start():
            nonlocal manager_proc
            with log.open("a") as stream:
                manager_proc = subprocess.Popen(
                    [sys.executable, str(clone / "launch.py"), "--port", str(manager_port)],
                    env=env,
                    stdout=stream,
                    stderr=stream,
                )
            assert wait_for(
                lambda: (
                    log.exists()
                    and ("Manager PID" in log.read_text() or manager_proc.poll() is not None)
                )
            ), "launcher readiness"
            assert manager_proc.poll() is None, log.read_text()
            time.sleep(0.4)

        def restart():
            manager_proc.send_signal(signal.SIGINT)
            assert manager_proc.wait(timeout=40) == 0
            start()

        try:
            start()
            browser = Browser(f"http://127.0.0.1:{driver_port}")
            home = root / "browser home"
            home.mkdir()
            with (root / "gecko.log").open("w") as stream:
                driver = subprocess.Popen(
                    [
                        "/snap/bin/geckodriver",
                        "--host",
                        "127.0.0.1",
                        "--port",
                        str(driver_port),
                        "--profile-root",
                        "/tmp",
                    ],
                    env=dict(env, HOME=str(home)),
                    stdout=stream,
                    stderr=stream,
                )

            def ready():
                try:
                    return browser.command("GET", "/status")["ready"]
                except OSError:
                    return False

            assert wait_for(ready), "own WebDriver readiness"
            session = browser.command(
                "POST",
                "/session",
                {
                    "capabilities": {
                        "alwaysMatch": {
                            "browserName": "firefox",
                            "unhandledPromptBehavior": "ignore",
                            "moz:firefoxOptions": {
                                "binary": "/snap/firefox/current/usr/lib/firefox/firefox",
                                "args": ["-headless", "--no-remote"],
                                "prefs": {
                                    "network.proxy.type": 1,
                                    "network.proxy.http": "127.0.0.1",
                                    "network.proxy.http_port": 9,
                                    "network.proxy.ssl": "127.0.0.1",
                                    "network.proxy.ssl_port": 9,
                                    "network.proxy.no_proxies_on": "localhost,127.0.0.1",
                                },
                            },
                        }
                    }
                },
            )
            print(
                "Browser:",
                session["capabilities"]["browserName"],
                session["capabilities"]["browserVersion"],
                flush=True,
            )
            browser.prefix = "/session/" + session["sessionId"]
            try:
                qualify(browser, root, manager, restart)
                browser.command("DELETE", "")
                browser.prefix = ""
                session = browser.command(
                    "POST",
                    "/session",
                    {
                        "capabilities": {
                            "alwaysMatch": {
                                "browserName": "firefox",
                                "moz:firefoxOptions": {
                                    "binary": "/snap/firefox/current/usr/lib/firefox/firefox",
                                    "args": ["-headless", "--no-remote"],
                                    "prefs": {
                                        "javascript.enabled": False,
                                        "network.proxy.type": 1,
                                        "network.proxy.http": "127.0.0.1",
                                        "network.proxy.http_port": 9,
                                        "network.proxy.ssl": "127.0.0.1",
                                        "network.proxy.ssl_port": 9,
                                        "network.proxy.no_proxies_on": "localhost,127.0.0.1",
                                    },
                                },
                            }
                        }
                    },
                )
                browser.prefix = "/session/" + session["sessionId"]
                browser.go(manager + "/", False)
                browser.native(
                    '#workspace-new-pane input[name="directory"]', str(root / "NoJS project")
                )
                browser.native('#workspace-new-pane input[name="name"]', "NoJS synthetic")
                browser.native("#workspace-new-pane button[type=submit]")
                browser.until("return location.pathname.startsWith('/p/')")
                assert browser.js("return typeof window.__wpHandle") == "undefined"
                browser.native(".site-nav [data-project-exit]")
                browser.native('input[name="workspace-mode"][value=load]')
                browser.native(
                    '#workspace-load-pane input[name="directory"]', str(root / "Project A")
                )
                browser.native("#workspace-load-pane button[type=submit]")
                browser.until("return location.pathname.startsWith('/p/')")
                assert browser.js("return typeof window.__wpHandle") == "undefined"
                assert (
                    rows(
                        root / "state" / "local-survey-filter" / "registry.sqlite3",
                        "SELECT * FROM leases",
                    )
                    == []
                )
                print(
                    "NOJS FIREFOX PASS: native typed New and radio-selected Load; worker read-only limitation explicit, zero leases.",
                    flush=True,
                )
            except (AssertionError, RuntimeError):
                try:
                    print(
                        "FAILED PAGE:",
                        browser.js(
                            "return {url:location.href,errors:[...document.querySelectorAll('.errorlist,#wp-handle-note')].map(e=>e.textContent),heading:document.querySelector('h1')?.textContent}"
                        ),
                        flush=True,
                    )
                except RuntimeError:
                    pass
                print("Synthetic launcher log:", log.read_text()[-6000:], flush=True)
                raise
        finally:
            if browser is not None and browser.prefix:
                try:
                    browser.command("DELETE", "")
                except (OSError, RuntimeError):
                    pass
            if driver is not None and driver.poll() is None:
                driver.terminate()
                driver.wait(timeout=15)
            if manager_proc is not None and manager_proc.poll() is None:
                manager_proc.send_signal(signal.SIGINT)
                manager_proc.wait(timeout=40)


if __name__ == "__main__":
    main()
